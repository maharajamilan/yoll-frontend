"use client";

import { useEffect, useState } from "react";
import type { Codebook, Config, WaveData } from "@/lib/types";
import { loadCodebook, loadWaveData } from "@/lib/dataLoader";
import { StepCard, Button } from "./components/ui";
import { GroupsStep } from "./components/GroupsStep";
import { QuestionsStep } from "./components/QuestionsStep";
import { ResultsStep } from "./components/ResultsStep";

const AVAILABLE_WAVES = [
  { id: "F24", label: "Fall 2024", group: "wave" as const },
  { id: "S25", label: "Spring 2025", group: "wave" as const },
  { id: "F25", label: "Fall 2025", group: "wave" as const },
  { id: "stacked_2026", label: "2026 cycle (S25 + F25)", group: "stacked" as const },
  { id: "stacked_all", label: "All waves (F24 + S25 + F25)", group: "stacked" as const },
];

export default function Home() {
  const [codebook, setCodebook] = useState<Codebook | null>(null);
  const [data, setData] = useState<WaveData | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [config, setConfig] = useState<Config>({
    wave: null,
    includeTotal: true,
    groups: [],
    questions: [],
  });

  useEffect(() => {
    if (!config.wave) return;
    setLoading(true);
    setLoadError(null);
    Promise.all([loadCodebook(config.wave), loadWaveData(config.wave)])
      .then(([cb, d]) => {
        setCodebook(cb);
        setData(d);
      })
      .catch((e) =>
        setLoadError(e instanceof Error ? e.message : String(e)),
      )
      .finally(() => setLoading(false));
  }, [config.wave]);

  function resetDownstream() {
    setConfig((c) => ({ ...c, groups: [], questions: [] }));
  }

  const waveReady = !!codebook && !!data && !loading;

  return (
    <div className="flex-1">
      <header className="bg-[color:var(--accent)] text-white">
        <div className="max-w-5xl mx-auto px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="text-lg font-semibold tracking-tight">
              Yale Youth Poll — Crosstab Explorer
            </div>
          </div>
          <div className="flex gap-2">
            <Button
              variant="ghost"
              className="!text-white hover:!bg-white/10"
            >
              Import Config
            </Button>
            <Button
              variant="ghost"
              className="!text-white hover:!bg-white/10"
            >
              Export Config
            </Button>
          </div>
        </div>
      </header>

      <main className="max-w-3xl mx-auto px-4 py-8 space-y-5">
        <section className="rounded-xl bg-card border border-[color:var(--border)] p-6 text-sm leading-relaxed text-[color:var(--ink)]">
          <p className="mb-2">
            This is a <strong>preliminary website</strong> for exploring Yale
            Youth Poll (YYP) survey results. Select a wave, define demographic
            groups with optional subgroup dimensions, pick one or more
            questions, and the tool will produce weighted crosstabs you can
            export as CSV.
          </p>
          <p className="text-[color:var(--muted)]">
            <strong>A note on weighting:</strong> to keep results comparable
            across waves, every wave is reweighted using the Spring 2025 YYP
            weighting procedure (age × gender × race × education × party ID
            × 2024 vote, raked to national registered-voter targets).
            Originally-published topline numbers may differ slightly.{" "}
            <strong>Stacking</strong> pools weighted respondents across the
            selected waves; a given question only appears when it is present
            in every pooled wave.
          </p>
        </section>

        <StepCard number={1} title="Select Data Source">
          <div className="flex items-center gap-3 flex-wrap">
            <select
              value={config.wave ?? ""}
              onChange={(e) => {
                const wave = e.target.value || null;
                setConfig((c) => ({ ...c, wave }));
                resetDownstream();
              }}
              className="border border-[color:var(--border)] rounded-md px-3 py-2 text-sm bg-white min-w-64"
            >
              <option value="">— Choose a dataset —</option>
              <optgroup label="Single waves">
                {AVAILABLE_WAVES.filter((w) => w.group === "wave").map((w) => (
                  <option key={w.id} value={w.id}>
                    {w.label}
                  </option>
                ))}
              </optgroup>
              <optgroup label="Stacked (demographic crosstabs only)">
                {AVAILABLE_WAVES.filter((w) => w.group === "stacked").map((w) => (
                  <option key={w.id} value={w.id}>
                    {w.label}
                  </option>
                ))}
              </optgroup>
            </select>
            {loading && <span className="text-sm text-[color:var(--muted)]">Loading…</span>}
            {loadError && (
              <span className="text-sm text-[color:var(--danger)]">{loadError}</span>
            )}
            {waveReady && codebook && config.wave && (
              <span className="text-xs text-[color:var(--muted)]">
                N = {codebook.waves[config.wave].n.toLocaleString()}
              </span>
            )}
          </div>
          {waveReady && config.wave && codebook?.waves[config.wave]?.note && (
            <p className="mt-3 text-xs text-[color:var(--muted)] italic">
              Note: {codebook.waves[config.wave].note}
            </p>
          )}
        </StepCard>

        <StepCard number={2} title="Configure Groups" disabled={!waveReady}>
          {codebook && (
            <GroupsStep
              codebook={codebook}
              groups={config.groups}
              includeTotal={config.includeTotal}
              onToggleTotal={(v) =>
                setConfig((c) => ({ ...c, includeTotal: v }))
              }
              onUpdate={(groups) => setConfig((c) => ({ ...c, groups }))}
            />
          )}
        </StepCard>

        <StepCard
          number={3}
          title="Select Questions & Response Groupings"
          disabled={!waveReady}
        >
          {codebook && (
            <QuestionsStep
              codebook={codebook}
              questions={config.questions}
              onUpdate={(qs) => setConfig((c) => ({ ...c, questions: qs }))}
            />
          )}
        </StepCard>

        <StepCard number={4} title="Results" disabled={!waveReady}>
          {codebook && (
            <ResultsStep codebook={codebook} data={data} config={config} />
          )}
        </StepCard>

        <footer className="text-xs text-[color:var(--muted)] text-center py-6">
          Yale Youth Poll · replication data on{" "}
          <a
            href="https://dataverse.yale.edu/dataverse/YYP"
            className="underline hover:text-[color:var(--accent)]"
          >
            Yale Dataverse
          </a>
          {" · "}
          <a
            href="https://youthpoll.yale.edu"
            className="underline hover:text-[color:var(--accent)]"
          >
            youthpoll.yale.edu
          </a>
        </footer>
      </main>
    </div>
  );
}
