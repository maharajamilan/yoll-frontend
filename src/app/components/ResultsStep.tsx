"use client";

import { useMemo, useState } from "react";
import type { Codebook, Config, WaveData } from "@/lib/types";
import { Button } from "./ui";
import { runCrosstab, crosstabToCsv, type CrosstabResult } from "@/lib/crosstab";

export function ResultsStep({
  codebook,
  data,
  config,
}: {
  codebook: Codebook;
  data: WaveData | null;
  config: Config;
}) {
  const [results, setResults] = useState<CrosstabResult[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const canRun = useMemo(() => {
    if (!data) return false;
    if (!config.questions.length) return false;
    if (
      !config.includeTotal &&
      !config.groups.some((g) =>
        g.dimensions.some(
          (d) => d.column && d.buckets.some((b) => b.codes.length > 0),
        ),
      )
    ) {
      return false;
    }
    return true;
  }, [data, config]);

  function run() {
    if (!data) return;
    setError(null);
    try {
      const out = config.questions.map((q) =>
        runCrosstab(data, codebook, q, {
          includeTotal: config.includeTotal,
          groups: config.groups,
        }),
      );
      setResults(out);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  function exportCsv() {
    if (!results) return;
    const csv = crosstabToCsv(results);
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `yyp_crosstabs_${config.wave}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="space-y-5">
      <div className="flex gap-2">
        <Button onClick={run} disabled={!canRun}>
          Run Analysis
        </Button>
        {results && (
          <Button variant="secondary" onClick={exportCsv}>
            Export CSV
          </Button>
        )}
      </div>
      {error && (
        <div className="text-sm text-[color:var(--danger)]">{error}</div>
      )}
      {results?.map((r) => (
        <CrosstabTable key={r.question} result={r} />
      ))}
    </div>
  );
}

function CrosstabTable({ result }: { result: CrosstabResult }) {
  if (result.error) {
    return (
      <div className="space-y-1.5">
        <div className="font-semibold text-[15px]">{result.question}</div>
        <div className="text-sm text-[color:var(--danger)]">{result.error}</div>
      </div>
    );
  }
  return (
    <div className="space-y-1.5">
      <div className="font-semibold text-[15px]">{result.question}</div>
      {result.questionText && result.questionText !== result.question && (
        <div className="text-sm text-[color:var(--muted)] italic">
          {result.questionText}
        </div>
      )}
      <div className="overflow-x-auto rounded-md border border-[color:var(--border)]">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-[color:var(--accent)] text-white">
              <th className="text-left px-3 py-2 font-semibold whitespace-nowrap">
                Response
              </th>
              {result.columns.map((c) => (
                <th
                  key={c.key}
                  className="text-right px-3 py-2 font-semibold whitespace-nowrap"
                >
                  {c.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {result.rows.map((row, i) => (
              <tr
                key={row.key}
                className={i % 2 === 0 ? "bg-white" : "bg-[color:var(--stripe)]"}
              >
                <td className="px-3 py-2">{row.label}</td>
                {result.columns.map((c) => (
                  <td
                    key={c.key}
                    className="text-right px-3 py-2 tabular-nums"
                  >
                    {row.pct[c.key].toFixed(1)}%
                  </td>
                ))}
              </tr>
            ))}
            <tr className="border-t border-[color:var(--border)] bg-[color:var(--stripe)]">
              <td className="px-3 py-2 font-medium">Weighted N</td>
              {result.columns.map((c) => (
                <td
                  key={c.key}
                  className="text-right px-3 py-2 font-medium tabular-nums"
                >
                  {Math.round(result.weightedN[c.key]).toLocaleString()}
                </td>
              ))}
            </tr>
            <tr className="bg-[color:var(--stripe)]">
              <td
                className="px-3 py-2 font-medium"
                title="95% margin of error in percentage points, computed as 1.96·√(0.25/n_eff) using Kish's effective sample size n_eff = (Σw)²/Σ(w²) and assuming p = 0.5 (the conservative max MOE)."
              >
                MOE (95%)
              </td>
              {result.columns.map((c) => {
                const m = result.moe[c.key];
                return (
                  <td
                    key={c.key}
                    className="text-right px-3 py-2 font-medium tabular-nums"
                  >
                    {Number.isFinite(m) ? `\u00B1${m.toFixed(1)}` : "—"}
                  </td>
                );
              })}
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}
