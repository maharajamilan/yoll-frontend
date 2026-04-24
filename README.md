# Yale Youth Poll — Crosstab Explorer

Frontend for exploring weighted crosstabs of the Yale Youth Poll (YYP) survey
data. Pick a wave, define demographic groups (with optional subgroup
dimensions), select questions, and export results to CSV.

Deployed at: <https://yoll-crosstabs.vercel.app> (member-only — password gate
configured via the `SITE_PASSWORD` env var on Vercel).

## Stack

* Next.js 16 (App Router, Turbopack), React 19, TypeScript
* Tailwind CSS v4
* @dnd-kit for drag-and-drop reordering
* All crosstab math runs client-side on JSON data files in `public/data/`
* Edge `proxy.ts` enforces a single-password gate on every route (Next.js 16
  renamed the `middleware` convention to `proxy`)

## Local development

```bash
npm install
npm run dev
```

The app serves at `http://localhost:3000`. Without `SITE_PASSWORD` set, the
auth gate is disabled.

## Data pipeline

The `scripts/` directory contains the Python pipeline that turns the raw YYP
replication packages into the JSON files the frontend consumes. To re-run end
to end, drop the F24 / S25 / F25 replication packages in your `~/Downloads`
under their canonical names and run:

```bash
python scripts/crosswalk.py        # raw -> harmonized S25-coded demographics
python scripts/rake_weights.py     # apply S25 weighting pipeline to all waves
python scripts/preprocess.py       # produce public/data/{codebook,data}_*.json
```

Outputs in `public/data/`:

```
codebook_<wave>.json    column metadata (question text, options, etc.)
data_<wave>.json        compact { columns, rows, weights }
```

Stacked datasets (`stacked_2026`, `stacked_all`) only expose the demographic
columns that share canonical S25 coding across all pooled waves.

## Weighting

Every wave is reweighted using the **Spring 2025 YYP raking procedure**, applied
to harmonized demographics (Age × Race × Education × Gender × 5-cat Party ID,
raked to national registered-voter targets, post-trim rescale to N). This
keeps results comparable across waves but means originally-published topline
numbers may differ slightly from what this tool produces.

## Deployment

Push to GitHub, then on Vercel:

1. Import the repo
2. Set `SITE_PASSWORD` env var in Project Settings → Environment Variables
3. Deploy — proxy.ts will enforce HTTP Basic Auth on every request

## Source data

* Yale Dataverse: <https://dataverse.yale.edu/dataverse/YYP>
* Yale Youth Poll: <https://youthpoll.yale.edu>
