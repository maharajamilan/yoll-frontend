# Yale Youth Poll — Crosstab Explorer

A web tool for exploring Yale Youth Poll (YYP) survey data. You pick a wave
(or a stacked combination of waves), define some demographic crosstab
columns, choose one or more questions, and the tool produces weighted
crosstabs you can export as CSV.

**Live (password-protected):** <https://yoll-crosstabs.vercel.app> — ask
Milan for the team password.

If you just want a quick read on what the tool does, scroll down to
[**What the app does**](#what-the-app-does). The rest of this page is mostly
about getting it running on your computer (which you only need to do if you
want to play with the code, not just use the tool).

> **Heads up — this is preliminary.**
> A couple of caveats are surfaced inside the app itself, but worth flagging
> here too:
> - **MaxDiff questions don't render correctly yet** (issue rank, electability,
>   etc. — the codes show up as raw numbers instead of meaningful counts).
> - **Some questions repeated across waves don't get pooled in stacked
>   datasets** — when a question's variable name changed between waves it's
>   currently treated as two single-wave columns rather than one combined one.

---

## Run the tool on your computer

The tool runs locally — you download this repo, install the dependencies once,
and start a local server that opens in your browser. **No coding required**;
you just paste a few commands into your terminal.

If you don't already have Node.js installed, install it first:

* **Mac:** download the **LTS** installer from <https://nodejs.org> and run it.
  Or, if you use Homebrew: `brew install node`.
* **Windows:** download the **LTS** installer from <https://nodejs.org> and
  run it. Click "Next" through the prompts.
* **Linux:** install via your package manager, or download from
  <https://nodejs.org>. The version should be 20 or newer.

To check it worked, open a terminal (Mac: **Terminal** app from
Applications/Utilities; Windows: **PowerShell** from the Start menu) and run:

```
node --version
npm --version
```

You should see version numbers like `v20.x.x` and `10.x.x`. If you don't, the
install didn't take — try restarting the terminal first.

### Get the code

Two ways:

**Option A — download a ZIP (easiest, no Git required):**
On the [GitHub page](https://github.com/maharajamilan/yoll-frontend), click
the green **Code** button → **Download ZIP**. Unzip it somewhere you'll
remember (e.g. your Desktop). The folder will be called something like
`yoll-frontend-main`.

**Option B — clone with Git (if you have it):**
```
git clone https://github.com/maharajamilan/yoll-frontend.git
```

### Install and run

In your terminal, navigate to the folder you just downloaded. On Mac, the
easiest way is to type `cd ` (with a trailing space), then drag the folder
from Finder into the terminal window — it'll auto-fill the path. On Windows,
right-click the folder and pick **Open in Terminal**.

Then run:

```
npm install
```

This downloads all the libraries the tool needs. It takes a minute or two
the first time and prints a lot of text — that's normal. You're done when
you get your prompt back.

Then start the tool:

```
npm run dev
```

Wait until you see something like `✓ Ready in 1.5s` and then open
<http://localhost:3000> in your browser. That's it.

When you're done, go back to the terminal and press **Ctrl+C** to stop the
server. Next time you want to use the tool, just run `npm run dev` again
from the same folder — you don't need to re-run `npm install` unless you've
re-downloaded the repo.

### Common hiccups

* **"command not found: npm"** — Node.js isn't installed (or your terminal
  hasn't picked it up). Close and reopen the terminal, or revisit the install
  step above.
* **"Port 3000 is already in use"** — something else is using that port.
  Either close that other thing, or run `npm run dev -- -p 3001` and use
  `http://localhost:3001` instead.
* **The page loads but shows a password prompt** — that only happens in
  the deployed version. Locally there's no password gate.

---

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

All weighting and crosstab math runs in your browser — there's no server
beyond the static Next.js bundle. Datasets live in `public/data/` as
preprocessed JSON.

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

## Weighting

Every wave is reweighted from scratch using the **Spring 2025 YYP raking
procedure**, applied to harmonized demographics (Age × Race × Education ×
Gender × 5-cat Party ID, raked to national registered-voter targets,
post-trim rescale to N). This keeps results comparable across waves but
means originally-published topline numbers may differ slightly from what
this tool produces.

---

## For developers

### Stack

* **Next.js 16** (App Router, Turbopack), **React 19**, **TypeScript**
* **Tailwind CSS v4**
* **@dnd-kit** for drag-and-drop reordering
* All crosstab math is in `src/lib/crosstab.ts`, runs client-side on the JSON
  data files in `public/data/`
* Edge `src/proxy.ts` enforces a single-password gate on every route (Next.js
  16 renamed the `middleware` convention to `proxy`)

### Data pipeline (optional — only needed to refresh data)

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

### Deployment

Push to GitHub, then on Vercel:

1. Import the repo
2. Set `SITE_PASSWORD` env var in Project Settings → Environment Variables
   (the `proxy.ts` gate is auto-disabled if this is unset)
3. Deploy — `proxy.ts` will enforce HTTP Basic Auth on every request

## Source data

* Yale Dataverse: <https://dataverse.yale.edu/dataverse/YYP>
* Yale Youth Poll: <https://youthpoll.yale.edu>
