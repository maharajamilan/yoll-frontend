# Yale Youth Poll — Crosstab Explorer

Frontend for exploring weighted crosstabs of the Yale Youth Poll (YYP) survey
data. Pick a wave (or a stacked combination), define demographic groups with
optional subgroup dimensions, select questions, and export results to CSV.
All crosstab math runs in the browser on preprocessed JSON in `public/data/`,
so there's no server beyond the static Next.js bundle.

## Run it locally

You'll need [Node.js 20+](https://nodejs.org/) and `npm`. The preprocessed
poll JSON is committed to the repo, so no Python or raw-data download is
required to run the app.

```bash
git clone https://github.com/<your-org>/yoll-frontend.git
cd yoll-frontend
npm install
npm run dev
```

Then open <http://localhost:3000>.

For a production build:

```bash
npm run build
npm start
```

`SITE_PASSWORD` enables an HTTP Basic Auth gate (see [Deployment](#deployment)).
Leave it unset for local dev — the gate is skipped.

## What the app does

Four steps, each builds on the previous:

1. **Select Data Source** — single wave (Fall 2024, Spring 2025, Fall 2025) or
   a stacked combination (`2026 cycle` = S25+F25, `All waves` = F24+S25+F25).
2. **Configure Groups** — define columns of the crosstab. Each group is one
   or more demographic dimensions whose Cartesian product becomes the column
   set (e.g. Party ID × Gender → 6 columns).
3. **Select Questions** — pick one or more questions to put down the rows.
   Optional per-question custom row buckets let you collapse Likert scales
   (e.g. Strongly + Somewhat agree → Agree).
4. **Results** — weighted crosstabs render live with weighted N per column,
   and you can export everything as CSV.

## Stack

* **Next.js 16** (App Router, Turbopack), **React 19**, **TypeScript**
* **Tailwind CSS v4**
* **@dnd-kit** for drag-and-drop reordering
* All crosstab math is in `src/lib/crosstab.ts`, runs client-side on the JSON
  data files in `public/data/`
* Edge `src/proxy.ts` enforces a single-password gate on every route (Next.js
  16 renamed the `middleware` convention to `proxy`)

## Data pipeline (optional — only needed to refresh data)

The committed JSON in `public/data/` is what the app actually loads. You only
need to re-run the pipeline if you're adding a new wave or changing the
weighting scheme.

The `scripts/` directory contains the Python pipeline that turns the raw YYP
replication packages into the frontend-ready JSON. To re-run end to end, drop
the F24 / S25 / F25 replication packages from
[Yale Dataverse](https://dataverse.yale.edu/dataverse/YYP) into your
`~/Downloads` under their canonical names and run:

```bash
python scripts/crosswalk.py        # raw -> harmonized S25-coded demographics
python scripts/rake_weights.py     # apply S25 weighting pipeline to every wave
python scripts/preprocess.py       # produce public/data/{codebook,data}_*.json
```

Outputs in `public/data/`:

```
codebook_<wave>.json    column metadata (question text, options, etc.)
data_<wave>.json        compact { columns, rows, weights }
```

Python deps: `pandas`, `numpy`, `openpyxl` (for the F25 codebook XLSX). The
intermediate harmonized CSVs and per-wave weights live in `data-raw/` (in
`.gitignore`) so the repo only ships the small JSON outputs.

## Weighting

Every wave is reweighted from scratch using the **Spring 2025 YYP raking
procedure**, applied to harmonized demographics (Age × Race × Education ×
Gender × 5-cat Party ID, raked to national registered-voter targets,
post-trim rescale to N). This keeps results comparable across waves but
means originally-published topline numbers may differ slightly from what
this tool produces.

## Stacked datasets

`stacked_2026` (S25+F25) and `stacked_all` (F24+S25+F25) pool weighted
respondents across the selected waves. **Every column from every pooled
wave is exposed** — respondents from waves that didn't ask a given question
carry `null`, so the weighted N naturally restricts to the waves that did.
Column labels carry a coverage tag like `[F25]` or `[F24+S25]` when a
question wasn't asked everywhere; full-coverage columns are unannotated.

Demographics use the canonical S25 coding regardless of how each wave coded
them. Columns whose option codes diverge across waves (e.g. MaxDiff item
batteries whose candidate list grew between waves) are dropped from the
stacked view rather than silently fused.

## Deployment

Push to GitHub, then on Vercel:

1. Import the repo
2. Set `SITE_PASSWORD` env var in Project Settings → Environment Variables
   (the `proxy.ts` gate is auto-disabled if this is unset)
3. Deploy — `proxy.ts` will enforce HTTP Basic Auth on every request

## Source data

* Yale Dataverse: <https://dataverse.yale.edu/dataverse/YYP>
* Yale Youth Poll: <https://youthpoll.yale.edu>
