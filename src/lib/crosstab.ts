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
  /** MaxDiff only: per-cell weighted "wins" (numerator) per column. */
  cellWins?: Record<string, number>;
  /** MaxDiff only: per-cell weighted "offers" (denominator) per column. */
  cellOffers?: Record<string, number>;
  /** MaxDiff only: per-cell MOE in percentage points (95% CI). */
  cellMoe?: Record<string, number>;
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
  /** True for MaxDiff questions; the renderer uses per-cell wins/offers/MOE. */
  isMaxDiff?: boolean;
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

  // MaxDiff path: rows are items, cell value = wins/offers (per item win rate).
  if (cbCol.type === "maxdiff") {
    return runMaxDiffCrosstab(data, codebook, question, cbCol, opts);
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

/**
 * MaxDiff aggregation. Each respondent saw a randomly assigned pair of items
 * (recorded in the `_do_N` siblings; `_do_N` is non-null iff item N was shown
 * to that respondent). The respondent picked one (recorded in the main
 * column). For each (column-predicate-set, item):
 *   wins   = Σw of respondents matching predicates whose pick == item.code
 *   offers = Σw of respondents matching predicates with item.do_col non-null
 *   pct    = wins / offers · 100  (per-item win rate)
 *   MOE    = 1.96·√(0.25/n_eff) · 100  with n_eff = (Σw_offered)² / Σ(w²_offered)
 */
function runMaxDiffCrosstab(
  data: WaveData,
  codebook: Codebook,
  question: Question,
  cbCol: { question: string; items?: { code: string | number; label: string; do_col: string }[] },
  opts: { includeTotal: boolean; groups: Group[] },
): CrosstabResult {
  const colIdx: Record<string, number> = {};
  data.columns.forEach((c, i) => {
    colIdx[c] = i;
  });

  const items = cbCol.items ?? [];
  if (items.length === 0) {
    return {
      question: question.column,
      questionText: cbCol.question,
      columns: [],
      rows: [],
      weightedN: {},
      effectiveN: {},
      moe: {},
      isMaxDiff: true,
      error: `MaxDiff column ${question.column} has no items defined.`,
    };
  }
  const mainIdx = colIdx[question.column];
  if (mainIdx === undefined) {
    return {
      question: question.column,
      questionText: cbCol.question,
      columns: [],
      rows: [],
      weightedN: {},
      effectiveN: {},
      moe: {},
      isMaxDiff: true,
      error: `Column ${question.column} is not in this wave's data file.`,
    };
  }

  // Resolve do_col indices once. Skip items whose do_col is missing in this
  // wave's data (defensive — shouldn't happen with the F25-only MaxDiff path
  // but matters for stacked datasets where the do_col is present only for F25 rows).
  const itemPlan: { code: string; label: string; doIdx: number }[] = [];
  for (const it of items) {
    const doIdx = colIdx[it.do_col];
    if (doIdx === undefined) continue;
    itemPlan.push({ code: String(it.code), label: it.label, doIdx });
  }

  const columns = buildColumns(codebook, data, opts.includeTotal, opts.groups);

  // Per-cell accumulators: wins[item][col], offers[item][col], offersSq[item][col].
  const wins: Record<string, Record<string, number>> = {};
  const offers: Record<string, Record<string, number>> = {};
  const offersSq: Record<string, Record<string, number>> = {};
  for (const it of itemPlan) {
    wins[it.code] = {};
    offers[it.code] = {};
    offersSq[it.code] = {};
    for (const c of columns) {
      wins[it.code][c.key] = 0;
      offers[it.code][c.key] = 0;
      offersSq[it.code][c.key] = 0;
    }
  }
  // Column-level: respondents in column with non-null pick (used for the
  // "Weighted N" footer row, mirroring the regular crosstab semantics).
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
    const pick = row[mainIdx];
    const pickStr = pick === null || pick === undefined ? null : String(pick);

    for (const c of columns) {
      // Column predicate check.
      if (!c.isTotal) {
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
      }

      // Per-item: was this item shown? (offers) and was it picked? (wins)
      let anyShown = false;
      for (const it of itemPlan) {
        const shown = row[it.doIdx];
        if (shown === null || shown === undefined) continue;
        anyShown = true;
        offers[it.code][c.key] += w;
        offersSq[it.code][c.key] += w2;
        if (pickStr !== null && pickStr === it.code) {
          wins[it.code][c.key] += w;
        }
      }
      // Column-level N: count any respondent who saw at least one item AND made a pick.
      if (anyShown && pickStr !== null) {
        weightedTotal[c.key] += w;
        weightedSqTotal[c.key] += w2;
      }
    }
  }

  // Build rows = items, sorted by overall (Total or first column) win rate descending.
  const sortKey = columns[0]?.key ?? null;
  const rowDefs = [...itemPlan].sort((a, b) => {
    if (!sortKey) return 0;
    const ra = offers[a.code][sortKey] > 0 ? wins[a.code][sortKey] / offers[a.code][sortKey] : 0;
    const rb = offers[b.code][sortKey] > 0 ? wins[b.code][sortKey] / offers[b.code][sortKey] : 0;
    return rb - ra;
  });

  const rows: CrosstabRow[] = rowDefs.map((it) => {
    const pct: Record<string, number> = {};
    const cellWins: Record<string, number> = {};
    const cellOffers: Record<string, number> = {};
    const cellMoe: Record<string, number> = {};
    for (const c of columns) {
      const sumW = offers[it.code][c.key];
      const sumW2 = offersSq[it.code][c.key];
      const winsW = wins[it.code][c.key];
      pct[c.key] = sumW > 0 ? (winsW / sumW) * 100 : 0;
      cellWins[c.key] = winsW;
      cellOffers[c.key] = sumW;
      if (sumW <= 0 || sumW2 <= 0) {
        cellMoe[c.key] = NaN;
      } else {
        const nEff = (sumW * sumW) / sumW2;
        cellMoe[c.key] = 1.96 * Math.sqrt(0.25 / nEff) * 100;
      }
    }
    return { key: it.code, label: it.label, pct, cellWins, cellOffers, cellMoe };
  });

  // Column-level effectiveN + MOE (used as a fallback in the footer; per-cell
  // MOE in `cellMoe` is the relevant precision number for MaxDiff).
  const effectiveN: Record<string, number> = {};
  const moe: Record<string, number> = {};
  for (const c of columns) {
    const sumW = weightedTotal[c.key];
    const sumW2 = weightedSqTotal[c.key];
    if (sumW <= 0 || sumW2 <= 0) {
      effectiveN[c.key] = 0;
      moe[c.key] = NaN;
    } else {
      effectiveN[c.key] = (sumW * sumW) / sumW2;
      moe[c.key] = 1.96 * Math.sqrt(0.25 / effectiveN[c.key]) * 100;
    }
  }

  return {
    question: question.column,
    questionText: cbCol.question,
    columns: columns.map(({ key, label }) => ({ key, label })),
    rows,
    weightedN: weightedTotal,
    effectiveN,
    moe,
    isMaxDiff: true,
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
    if (r.isMaxDiff) {
      // MaxDiff CSV: 4 sub-rows per item \u2014 Win rate, MOE, Wins, Offers \u2014 so
      // analysts can re-derive everything downstream.
      lines.push(
        csvRow([
          "Item",
          ...r.columns.flatMap((c) => [
            `${c.label} \u2014 win %`,
            `${c.label} \u2014 MOE (\u00B1pp, 95%)`,
            `${c.label} \u2014 wins`,
            `${c.label} \u2014 offers`,
          ]),
        ]),
      );
      for (const row of r.rows) {
        lines.push(
          csvRow([
            row.label,
            ...r.columns.flatMap((c) => {
              const m = row.cellMoe?.[c.key];
              const w = row.cellWins?.[c.key];
              const o = row.cellOffers?.[c.key];
              return [
                row.pct[c.key].toFixed(1) + "%",
                Number.isFinite(m) ? (m as number).toFixed(1) : "",
                w !== undefined ? Math.round(w).toString() : "",
                o !== undefined ? Math.round(o).toString() : "",
              ];
            }),
          ]),
        );
      }
      lines.push(
        csvRow([
          "Column N (any item shown + pick)",
          ...r.columns.flatMap((c) => [
            Math.round(r.weightedN[c.key]).toString(),
            "",
            "",
            "",
          ]),
        ]),
      );
    } else {
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
    }
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
