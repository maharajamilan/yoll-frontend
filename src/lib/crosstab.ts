import type {
  Bucket,
  Codebook,
  Group,
  Question,
  ResponseCode,
  WaveData,
} from "./types";

export type CrosstabColumn = {
  key: string;
  label: string;
};

export type CrosstabRow = {
  key: string;
  label: string;
  pct: Record<string, number>;
};

export type CrosstabResult = {
  question: string;
  questionText: string;
  columns: CrosstabColumn[];
  rows: CrosstabRow[];
  weightedN: Record<string, number>;
  /** Kish's effective sample size: (Σw)² / Σ(w²). Lower than weightedN when weights vary. */
  effectiveN: Record<string, number>;
  /**
   * Margin of error in percentage points at 95% confidence, computed as
   * 1.96·√(0.25/n_eff) · 100. Uses p = 0.5 (the conservative max MOE), per
   * standard pollster convention. NaN when the column has no respondents.
   */
  moe: Record<string, number>;
  /** Error message if this question couldn't be computed (e.g. column missing). */
  error?: string;
};

type InternalCol = CrosstabColumn & {
  isTotal: boolean;
  predicates: { colIdx: number; codes: Set<string> }[];
};

type InternalRow = {
  key: string;
  label: string;
  codes: Set<string>; // codes included in this row bucket
};

/**
 * Build the internal column list for a list of groups.
 * Each Group produces the Cartesian product of its dimensions' buckets.
 */
function buildColumns(
  codebook: Codebook,
  data: WaveData,
  includeTotal: boolean,
  groups: Group[],
): InternalCol[] {
  const colIdx: Record<string, number> = {};
  data.columns.forEach((c, i) => {
    colIdx[c] = i;
  });

  const result: InternalCol[] = [];
  if (includeTotal) {
    result.push({ key: "__total", label: "Total", isTotal: true, predicates: [] });
  }

  for (const g of groups) {
    const usableDims = g.dimensions.filter(
      (d) => d.column && d.buckets.some((b) => b.codes.length > 0),
    );
    if (!usableDims.length) continue;

    // Precompute per-dimension bucket lists with their column index & code set.
    type DimEntry = {
      bucket: Bucket;
      colIdx: number;
      codeSet: Set<string>;
    };
    const dimChoices: DimEntry[][] = usableDims.map((dim) => {
      const ci = colIdx[dim.column!];
      if (ci === undefined) return [];
      return dim.buckets
        .filter((b) => b.codes.length > 0)
        .map((b) => ({
          bucket: b,
          colIdx: ci,
          codeSet: new Set(b.codes.map(String)),
        }));
    });
    if (dimChoices.some((dc) => dc.length === 0)) continue;

    // Cartesian product.
    const combos: DimEntry[][] = cartesian(dimChoices);
    for (const combo of combos) {
      const label = combo.map((e) => e.bucket.name).join(" / ");
      const key = `${g.id}:` + combo.map((e) => e.bucket.id).join("/");
      result.push({
        key,
        label,
        isTotal: false,
        predicates: combo.map((e) => ({ colIdx: e.colIdx, codes: e.codeSet })),
      });
    }
  }
  return result;
}

function cartesian<T>(arrs: T[][]): T[][] {
  let acc: T[][] = [[]];
  for (const arr of arrs) {
    const next: T[][] = [];
    for (const prefix of acc) {
      for (const item of arr) {
        next.push([...prefix, item]);
      }
    }
    acc = next;
  }
  return acc;
}

/**
 * Build the row definitions for a question: either custom response-buckets,
 * or the codebook's native options one-to-one.
 */
function buildRows(codebook: Codebook, question: Question): InternalRow[] {
  const cbCol = codebook.columns[question.column];
  if (!cbCol) return [];
  if (question.responseBuckets && question.responseBuckets.length > 0) {
    return question.responseBuckets
      .filter((b) => b.codes.length > 0)
      .map((b) => ({
        key: b.id,
        label: b.name,
        codes: new Set(b.codes.map(String)),
      }));
  }
  return (cbCol.options ?? []).map((opt) => ({
    key: String(opt.code),
    label: opt.label,
    codes: new Set([String(opt.code)]),
  }));
}

export function runCrosstab(
  data: WaveData,
  codebook: Codebook,
  question: Question,
  opts: { includeTotal: boolean; groups: Group[] },
): CrosstabResult {
  const colIdx: Record<string, number> = {};
  data.columns.forEach((c, i) => {
    colIdx[c] = i;
  });

  const cbCol = codebook.columns[question.column];
  if (!cbCol) {
    return {
      question: question.column,
      questionText: question.column,
      columns: [],
      rows: [],
      weightedN: {},
      effectiveN: {},
      moe: {},
      error: `Column ${question.column} is not in this wave's codebook.`,
    };
  }
  const qIdx = colIdx[question.column];
  if (qIdx === undefined) {
    return {
      question: question.column,
      questionText: cbCol.question,
      columns: [],
      rows: [],
      weightedN: {},
      effectiveN: {},
      moe: {},
      error: `Column ${question.column} is not in this wave's data file.`,
    };
  }

  const columns = buildColumns(codebook, data, opts.includeTotal, opts.groups);
  const rowDefs = buildRows(codebook, question);

  // Initialize accumulators.
  const weightedCount: Record<string, Record<string, number>> = {};
  for (const r of rowDefs) {
    weightedCount[r.key] = {};
    for (const c of columns) weightedCount[r.key][c.key] = 0;
  }
  const weightedTotal: Record<string, number> = {};
  const weightedSqTotal: Record<string, number> = {};
  for (const c of columns) {
    weightedTotal[c.key] = 0;
    weightedSqTotal[c.key] = 0;
  }

  const nRows = data.rows.length;
  for (let i = 0; i < nRows; i++) {
    const row = data.rows[i];
    const w = data.weights[i];
    const w2 = w * w;
    const resp = row[qIdx];
    if (resp === null || resp === undefined) continue;
    const respStr = String(resp);

    // Which row bucket does this response fall into? (At most one.)
    let matchedRow: InternalRow | null = null;
    for (const rd of rowDefs) {
      if (rd.codes.has(respStr)) {
        matchedRow = rd;
        break;
      }
    }

    for (const c of columns) {
      if (c.isTotal) {
        if (matchedRow) weightedCount[matchedRow.key][c.key] += w;
        weightedTotal[c.key] += w;
        weightedSqTotal[c.key] += w2;
      } else {
        // All predicates must match.
        let ok = true;
        for (const p of c.predicates) {
          const v = row[p.colIdx];
          if (v === null || v === undefined) {
            ok = false;
            break;
          }
          if (!p.codes.has(String(v))) {
            ok = false;
            break;
          }
        }
        if (!ok) continue;
        if (matchedRow) weightedCount[matchedRow.key][c.key] += w;
        weightedTotal[c.key] += w;
        weightedSqTotal[c.key] += w2;
      }
    }
  }

  // Kish effective N + 95% MOE (max, at p=0.5) per column.
  const effectiveN: Record<string, number> = {};
  const moe: Record<string, number> = {};
  for (const c of columns) {
    const sumW = weightedTotal[c.key];
    const sumW2 = weightedSqTotal[c.key];
    if (sumW <= 0 || sumW2 <= 0) {
      effectiveN[c.key] = 0;
      moe[c.key] = NaN;
    } else {
      const nEff = (sumW * sumW) / sumW2;
      effectiveN[c.key] = nEff;
      // 1.96 · √(0.25 / n_eff) · 100  →  percentage points
      moe[c.key] = (1.96 * Math.sqrt(0.25 / nEff)) * 100;
    }
  }

  const rows: CrosstabRow[] = rowDefs.map((rd) => {
    const pct: Record<string, number> = {};
    for (const c of columns) {
      const tot = weightedTotal[c.key];
      pct[c.key] = tot > 0 ? (weightedCount[rd.key][c.key] / tot) * 100 : 0;
    }
    return { key: rd.key, label: rd.label, pct };
  });

  return {
    question: question.column,
    questionText: cbCol.question,
    columns: columns.map(({ key, label }) => ({ key, label })),
    rows,
    weightedN: weightedTotal,
    effectiveN,
    moe,
  };
}

export function crosstabToCsv(results: CrosstabResult[]): string {
  const lines: string[] = [];
  for (const r of results) {
    lines.push(csvRow([r.question]));
    if (r.questionText && r.questionText !== r.question) {
      lines.push(csvRow([r.questionText]));
    }
    if (r.error) {
      lines.push(csvRow([`ERROR: ${r.error}`]));
      lines.push("");
      continue;
    }
    lines.push(csvRow(["Response", ...r.columns.map((c) => c.label)]));
    for (const row of r.rows) {
      lines.push(
        csvRow([
          row.label,
          ...r.columns.map((c) => row.pct[c.key].toFixed(1) + "%"),
        ]),
      );
    }
    lines.push(
      csvRow([
        "Weighted N",
        ...r.columns.map((c) => Math.round(r.weightedN[c.key]).toString()),
      ]),
    );
    lines.push(
      csvRow([
        "MOE (\u00B1pp, 95%)",
        ...r.columns.map((c) => {
          const m = r.moe[c.key];
          return Number.isFinite(m) ? m.toFixed(1) : "";
        }),
      ]),
    );
    lines.push("");
  }
  return lines.join("\n");
}

function csvRow(fields: string[]): string {
  return fields
    .map((f) => {
      if (f.includes(",") || f.includes('"') || f.includes("\n")) {
        return `"${f.replace(/"/g, '""')}"`;
      }
      return f;
    })
    .join(",");
}

/** Helper: generate default buckets for a column from the codebook. */
export function defaultBucketsForColumn(
  codebook: Codebook,
  col: string,
  idPrefix = "b",
): Bucket[] {
  const def = codebook.columns[col];
  if (!def?.options) return [];
  return def.options.map((opt, i) => ({
    id: `${idPrefix}_${i}_${String(opt.code)}`,
    name: opt.label,
    codes: [opt.code],
  }));
}
