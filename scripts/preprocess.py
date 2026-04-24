"""
Unified preprocessor: turn each YYP wave's raw replication files into the
compact JSON format the Next.js frontend consumes directly.

Output per wave (in public/data/):
  - codebook_<wave>.json    { waves: {...}, columns: {colname: {label, question, options, ...}} }
  - data_<wave>.json        { wave, n, columns, rows, weights }

Weights come from data-raw/weights/weights_<wave>.csv (emitted by rake_weights.py).

Wave loaders:
  - s25: Qualtrics-style CSV (values + labels files); ResponseId is case_id.
  - f25: Plain CSV (yypfall25dat_withweights.csv) + XLSX codebook.
  - f24: Plain CSV + qualtrics mappings CSV (PDF codebook not yet parsed;
    categorical option labels are inferred from S25 overlap for shared
    demographic columns, left empty otherwise).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
WEIGHTS_DIR = REPO_ROOT / "data-raw" / "weights"
OUTPUT_DIR = REPO_ROOT / "public" / "data"

S25_DIR = Path("/Users/milansingh/Downloads/yyp s25 repo")
F25_DIR = Path("/Users/milansingh/Downloads/yyp f25 repo")
F24_DIR = Path("/Users/milansingh/Downloads/yyp f24 repo")

# Columns we never expose (PII, admin, free-text, metadata).
DROP_EXACT_ALL = {
    # Qualtrics admin
    "StartDate", "EndDate", "Status", "IPAddress", "Progress",
    "Duration (in seconds)", "Finished", "RecordedDate", "ResponseId",
    "RecipientLastName", "RecipientFirstName", "RecipientEmail",
    "ExternalReference", "LocationLatitude", "LocationLongitude",
    "DistributionChannel", "UserLanguage",
    # Consent / screens
    "Consent_Age", "Consent", "RV_Screen",
    # Prolific
    "PROLIFIC_PID", "STUDY_ID", "SESSION_ID", "comments",
    # F24 snake_case variants
    "response_id", "duration_in_seconds", "finished", "recorded_date",
    "location_latitude", "location_longitude", "distribution_channel",
    "user_language", "consent_age", "consent", "rv_screen",
    "prolific_pid", "study_id", "session_id",
    "q_straightlining_count", "q_straightlining_percentage",
    "q_straightlining_questions",
    # F25 admin
    "case_id", "start_date", "end_date", "sample_type", "over_18", "consent_q",
    "us_voter",
}
DROP_SUFFIXES = ("_TEXT", "_text", "_do_1", "_do_2", "_do_3", "_do_4", "_do_5",
                 "_ado_1", "_ado_2", "_ado_3", "_ado_4", "_ado_5",
                 "_labels", "_actualnumber", "_count")
DROP_PREFIXES = ("Unnamed:",)

CATEGORICAL_MAX_OPTIONS = 40


# ------------------------------------------------------------------
# Common helpers
# ------------------------------------------------------------------


def is_dropped(col: str) -> bool:
    if col in DROP_EXACT_ALL:
        return True
    if any(col.endswith(s) for s in DROP_SUFFIXES):
        return True
    if any(col.startswith(p) for p in DROP_PREFIXES):
        return True
    return False


def to_compact_value(v):
    if pd.isna(v):
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    return v


def load_weights(wave: str) -> pd.DataFrame:
    path = WEIGHTS_DIR / f"weights_{wave.lower()}.csv"
    if not path.exists():
        raise FileNotFoundError(
            f"Missing weights file {path}; run scripts/rake_weights.py --wave {wave} first"
        )
    w = pd.read_csv(path)
    if "case_id" not in w.columns or "weight" not in w.columns:
        raise ValueError(f"{path} must have case_id,weight columns")
    return w[["case_id", "weight"]]


# ------------------------------------------------------------------
# S25 loader (Qualtrics 3-row header)
# ------------------------------------------------------------------


def load_s25() -> tuple[pd.DataFrame, dict[str, str], pd.DataFrame, str]:
    """Returns (values_df, question_text_map, labels_df, case_id_col)."""
    values_path = S25_DIR / "yyp2025_official_values.csv"
    labels_path = S25_DIR / "yyp2025_official_labels.csv"
    header = pd.read_csv(values_path, nrows=2)
    qtexts = dict(zip(header.columns, header.iloc[0].astype(str).tolist()))
    values = pd.read_csv(values_path, skiprows=[1, 2], low_memory=False)
    labels = pd.read_csv(labels_path, skiprows=[1, 2], low_memory=False)
    return values, qtexts, labels, "ResponseId"


# ------------------------------------------------------------------
# F25 loader (plain CSV + XLSX codebook)
# ------------------------------------------------------------------


def load_f25() -> tuple[pd.DataFrame, dict[str, str], pd.DataFrame, str]:
    data_path = F25_DIR / "yypfall25dat_withweights.csv"
    codebook_path = F25_DIR / "2025-138a_codebook.xlsx"

    raw = pd.read_csv(data_path, low_memory=False)
    # Strip _labels columns (kept separately as labels_df for codebook building)
    numeric_cols = [c for c in raw.columns if not c.endswith("_labels")]
    values = raw[numeric_cols].copy()

    # Build labels_df by pulling the _labels sibling for each column (where present)
    labels = pd.DataFrame(index=raw.index)
    for c in numeric_cols:
        lbl_col = f"{c}_labels"
        if lbl_col in raw.columns:
            labels[c] = raw[lbl_col]

    # Question text comes from the XLSX codebook's Description column
    cb = pd.read_excel(codebook_path)
    cb["Variable"] = cb["Variable"].ffill()
    qtexts: dict[str, str] = {}
    for var in cb["Variable"].dropna().unique():
        rows = cb[cb["Variable"] == var]
        desc = rows["Description"].dropna()
        if len(desc):
            qtexts[var] = str(desc.iloc[0])
        else:
            qtexts[var] = var
    # Derived columns that don't exist in the XLSX codebook
    qtexts.setdefault("ces_race", "What racial or ethnic group best describes you? (combined)")
    qtexts.setdefault(
        "2024_recalled_vote",
        "Who did you vote for for president in 2024?",
    )
    return values, qtexts, labels, "case_id"


# ------------------------------------------------------------------
# F24 loader (plain CSV + qualtrics mappings for readable names)
# ------------------------------------------------------------------


F24_DEMO_OPTION_OVERRIDES: dict[str, list[tuple[int, str]]] = {
    # Hand-encoded from the F24 PDF codebook so the frontend can render labels
    # for the demographic columns. Other F24 columns expose numeric codes until
    # we parse the full PDF codebook.
    "age": [(1, "18-21"), (2, "22-29"), (3, "30-44"), (4, "45-64"), (5, "65+")],
    "gender": [(1, "Man"), (2, "Woman"), (3, "Other")],
    "race": [(1, "White"), (2, "Black"), (3, "Hispanic"), (4, "Asian"), (5, "Other")],
    "education": [
        (1, "Some high school or less"),
        (2, "High school diploma or GED"),
        (3, "Some college, but no degree"),
        (4, "Associates or technical degree"),
        (5, "Bachelor\u2019s degree"),
        (6, "Graduate or professional degree (MA, MS, MBA, PhD, JD, MD, DDS etc.)"),
        (7, "Prefer not to say"),
    ],
    "income": [
        (1, "Less than $25,000"),
        (2, "$25,000-$49,999"),
        (3, "$50,000-$74,999"),
        (4, "$75,000-$99,999"),
        (5, "$100,000-$149,999"),
        (6, "$150,000 or more"),
        (7, "Prefer not to say"),
    ],
    "party_id": [
        (1, "The Democratic Party"),
        (2, "The Republican Party"),
        (3, "Independent (also known as no party affiliation in some states)"),
    ],
    "pid_lean": [
        (1, "The Democratic Party"),
        (2, "The Republican Party"),
        (3, "Neither"),
    ],
    "x2020_vote": [
        (1, "Joe Biden"),
        (2, "Donald Trump"),
        (3, "Other"),
        (4, "Did not vote"),
        (5, "Was not old enough to vote"),
    ],
    "x2024_horserace": [
        (1, "Democrat Kamala Harris"),
        (2, "Republican Donald Trump"),
        (3, "Green Party candidate Jill Stein"),
        (4, "Libertarian candidate Chase Oliver"),
        (5, "Independent candidate Cornel West"),
        (6, "Not sure"),
        (7, "Someone else"),
        (8, "Would not vote"),
    ],
}

F24_READABLE_NAMES: dict[str, str] = {}


def load_f24() -> tuple[pd.DataFrame, dict[str, str], pd.DataFrame, str]:
    data_path = F24_DIR / "data_yyp_F24.csv"
    mappings_path = F24_DIR / "qualtrics_id_mappings_to_columns_F24.csv"

    values = pd.read_csv(data_path, low_memory=False)
    # Filter to registered voters (the YYP analysis universe)
    if "rv_screen" in values.columns:
        values = values[values["rv_screen"] == 1].copy()

    # Readable names from the mappings file: header row is the "nice" Qualtrics
    # name, data file uses snake_case. Build snake_case -> readable map.
    mappings = pd.read_csv(mappings_path)
    readable = {}
    for col in mappings.columns:
        # Qualtrics sends "Some Column" -> snake_case becomes "some_column"
        sc = col.lower().replace(" ", "_").replace(".", "")
        sc = sc.replace("'", "").replace(",", "").replace("-", "_")
        readable[sc] = col
    F24_READABLE_NAMES.update(readable)

    # Question text: we use the readable name as the question text for now
    # (PDF codebook not machine-parsed). Hand-wired for key demographic cols.
    qtexts: dict[str, str] = {c: readable.get(c, c) for c in values.columns}
    hand = {
        "age": "What is your age?",
        "gender": "What is your gender?",
        "race": "What is your race?",
        "education": "What is the highest level of education you have completed?",
        "income": "What was your annual income last year?",
        "party_id": "Which political party do you most closely identify with?",
        "pid_lean": "If you had to choose, would you say you are closer to the Democratic Party or the Republican Party?",
        "x2020_vote": "Who did you vote for for president in 2020?",
        "x2024_horserace": "If the November 2024 election for U.S. president was held today, and these were the candidates, who would you vote for?",
    }
    qtexts.update(hand)

    # Build a pseudo labels_df from hand-encoded options (used by build_codebook)
    labels = pd.DataFrame(index=values.index)
    for col, opts in F24_DEMO_OPTION_OVERRIDES.items():
        if col in values.columns:
            code_to_label = dict(opts)
            labels[col] = values[col].map(code_to_label)

    return values, qtexts, labels, "response_id"


# ------------------------------------------------------------------
# Codebook builder (shared)
# ------------------------------------------------------------------


def build_column_entry(
    col: str,
    values_col: pd.Series,
    labels_col: pd.Series | None,
    question_text: str,
    readable_label: str | None,
    wave: str,
    option_overrides: dict[str, list[tuple[int, str]]] | None = None,
) -> dict | None:
    non_null = values_col.dropna()
    if len(non_null) == 0:
        return None

    # Drop columns with many unique values unless we've hard-coded options
    unique_vals = non_null.unique()

    # Skip columns whose values aren't numeric codes (free-text responses like
    # maxdiff items). Unless we have hand-encoded options, we can only crosstab
    # numeric-coded categoricals.
    if not (option_overrides and col in option_overrides):
        try:
            _ = [float(v) for v in unique_vals]
        except (ValueError, TypeError):
            return None

    # Option overrides take priority (hand-encoded labels)
    if option_overrides and col in option_overrides:
        options = [{"code": c, "label": l} for c, l in option_overrides[col]]
        return {
            "label": readable_label or col,
            "question": question_text,
            "type": "categorical",
            "options": options,
            "waves": [wave],
        }

    if labels_col is not None and len(unique_vals) <= CATEGORICAL_MAX_OPTIONS:
        paired = pd.DataFrame({"v": values_col, "l": labels_col}).dropna()
        if paired.empty:
            # No labels available — fall through to numeric
            pass
        else:
            code_label: dict[float, str] = {}
            for code, group in paired.groupby("v"):
                lab_mode = group["l"].astype(str).mode()
                code_label[float(code)] = lab_mode.iloc[0] if len(lab_mode) else str(code)
            options = []
            for c in sorted(code_label.keys()):
                code_val = int(c) if float(c).is_integer() else c
                options.append({"code": code_val, "label": code_label[c]})
            return {
                "label": readable_label or col,
                "question": question_text,
                "type": "categorical",
                "options": options,
                "waves": [wave],
            }

    # Numeric or no labels: expose as numeric if small range of values
    if len(unique_vals) <= CATEGORICAL_MAX_OPTIONS:
        # Treat as categorical with code==label (so frontend still shows each code)
        options = []
        for v in sorted(unique_vals, key=lambda x: float(x) if pd.notna(x) else 0):
            v = to_compact_value(v)
            options.append({"code": v, "label": str(v)})
        return {
            "label": readable_label or col,
            "question": question_text,
            "type": "categorical",
            "options": options,
            "waves": [wave],
        }

    return {
        "label": readable_label or col,
        "question": question_text,
        "type": "numeric",
        "waves": [wave],
    }


def preprocess_wave(wave: str) -> None:
    print(f"\n===== preprocess {wave} =====")
    loader = {"S25": load_s25, "F25": load_f25, "F24": load_f24}[wave]
    values, qtexts, labels, case_id_col = loader()

    print(f"  {len(values)} rows; {len(values.columns)} cols")

    # Load weights
    weights_df = load_weights(wave)
    merged = values[[case_id_col]].copy()
    merged.columns = ["case_id"]
    merged = merged.merge(weights_df, on="case_id", how="left")
    missing = merged["weight"].isna().sum()
    if missing:
        print(f"  WARNING: {missing} rows had no weight match; filling with 1.0")
        merged["weight"] = merged["weight"].fillna(1.0)

    # Hand overrides only apply to F24
    option_overrides = F24_DEMO_OPTION_OVERRIDES if wave == "F24" else None

    # Build codebook columns
    columns_out: dict[str, dict] = {}
    for col in values.columns:
        if is_dropped(col):
            continue
        labels_col = labels[col] if col in labels.columns else None
        question_text = qtexts.get(col, col)
        readable_label = F24_READABLE_NAMES.get(col) if wave == "F24" else col
        entry = build_column_entry(
            col, values[col], labels_col, question_text,
            readable_label, wave, option_overrides,
        )
        if entry:
            columns_out[col] = entry

    # Put demographic columns first for UX
    demo_priority = {
        "S25": ["Age", "Gender", "Race", "Education", "Income", "Party ID", "PID Lean", "2024 vote"],
        "F25": ["age", "gender", "ces_race", "education", "anes_party_id", "pid_leaners", "2024_recalled_vote"],
        "F24": ["age", "gender", "race", "education", "income", "party_id", "pid_lean", "x2024_horserace", "x2020_vote"],
    }
    priority_cols = [c for c in demo_priority.get(wave, []) if c in columns_out]
    other_cols = [c for c in columns_out if c not in priority_cols]
    ordered_columns = {c: columns_out[c] for c in (priority_cols + other_cols)}

    wave_meta = {
        "S25": {"label": "Spring 2025", "n": int(len(values)), "note": "Weighted via S25 pipeline."},
        "F25": {"label": "Fall 2025", "n": int(len(values)), "note": "Reweighted using S25 pipeline as standard."},
        "F24": {"label": "Fall 2024", "n": int(len(values)), "note": "Registered voters only; reweighted using S25 pipeline."},
    }[wave]

    codebook = {
        "waves": {wave: wave_meta},
        "columns": ordered_columns,
    }

    # Build data payload
    keep_cols = list(ordered_columns.keys())
    kept = values[keep_cols]
    rows: list[list] = []
    for _, row in kept.iterrows():
        rows.append([to_compact_value(v) for v in row])

    data_payload = {
        "wave": wave,
        "n": int(len(values)),
        "columns": keep_cols,
        "rows": rows,
        "weights": [float(w) for w in merged["weight"].tolist()],
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cb_path = OUTPUT_DIR / f"codebook_{wave.lower()}.json"
    data_path = OUTPUT_DIR / f"data_{wave.lower()}.json"
    cb_path.write_text(json.dumps(codebook, indent=2))
    data_path.write_text(json.dumps(data_payload, separators=(",", ":")))
    print(f"  Wrote {cb_path} ({cb_path.stat().st_size/1024:.1f} KB)")
    print(f"  Wrote {data_path} ({data_path.stat().st_size/1024:.1f} KB)")
    print(f"  Columns exposed: {len(keep_cols)}")


# ------------------------------------------------------------------
# Stacked datasets
# ------------------------------------------------------------------

# Canonical column name -> per-wave source column name. A column appears in a
# stacked dataset only if it exists in every wave being stacked. Values are
# canonicalized to the S25 coding scheme — in particular Party ID / Race are
# mapped via the harmonized_<wave>.csv file, so the F25 swaps and F25 race
# fold-down are respected.
CANONICAL_ALIASES: dict[str, dict[str, str]] = {
    # key: canonical name; value: {wave: source column in that wave's data}.
    # For demographic columns we substitute the harmonized value (from
    # data-raw/harmonized/harmonized_<wave>.csv) so Race/Party ID/Age are
    # already in S25 coding.
    "Age":        {"F24": "_HARM_Age", "S25": "_HARM_Age", "F25": "_HARM_Age"},
    "Gender":     {"F24": "_HARM_Gender", "S25": "_HARM_Gender", "F25": "_HARM_Gender"},
    "Race":       {"F24": "_HARM_Race", "S25": "_HARM_Race", "F25": "_HARM_Race"},
    "Education":  {"F24": "_HARM_Education", "S25": "_HARM_Education", "F25": "_HARM_Education"},
    "Party ID":   {"F24": "_HARM_Party ID", "S25": "_HARM_Party ID", "F25": "_HARM_Party ID"},
    "PID Lean":   {"F24": "_HARM_PID Lean", "S25": "_HARM_PID Lean", "F25": "_HARM_PID Lean"},
    "2024 vote":  {"F24": "_HARM_2024 Vote", "S25": "_HARM_2024 Vote", "F25": "_HARM_2024 Vote"},
}

CANONICAL_OPTIONS: dict[str, list[tuple[int, str]]] = {
    "Age": [(1, "18-21"), (2, "22-29"), (3, "30-44"), (4, "45-64"), (5, "65+")],
    "Gender": [(1, "Man"), (2, "Woman"), (3, "Other")],
    "Race": [(1, "White"), (2, "Black"), (3, "Hispanic"), (4, "Asian"), (5, "Other")],
    "Education": [
        (1, "Some high school or less"),
        (2, "High school diploma or GED"),
        (3, "Some college, but no degree"),
        (4, "Associates or technical degree"),
        (5, "Bachelor\u2019s degree"),
        (6, "Graduate or professional degree"),
        (7, "Prefer not to say"),
    ],
    "Party ID": [
        (1, "The Democratic Party"),
        (2, "The Republican Party"),
        (3, "Independent"),
    ],
    "PID Lean": [(1, "The Democratic Party"), (2, "The Republican Party"), (3, "Neither")],
    "2024 vote": [
        (1, "Kamala Harris"),
        (2, "Donald Trump"),
        (3, "Other"),
        (4, "Did not vote"),
        (5, "Was not old enough to vote"),
    ],
}

CANONICAL_QUESTIONS: dict[str, str] = {
    "Age": "What is your age?",
    "Gender": "What is your gender?",
    "Race": "What is your race?",
    "Education": "What is the highest level of education you have completed?",
    "Party ID": "Which political party do you most closely identify with?",
    "PID Lean": "Do you lean closer to the Democratic Party or the Republican Party?",
    "2024 vote": "Who did you vote for for president in 2024?",
}


def load_harmonized(wave: str) -> pd.DataFrame:
    path = REPO_ROOT / "data-raw" / "harmonized" / f"harmonized_{wave.lower()}.csv"
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}; run crosswalk.py first")
    return pd.read_csv(path)


def build_stacked(stack_id: str, label: str, waves: list[str]) -> None:
    print(f"\n===== stack {stack_id} ({'+'.join(waves)}) =====")
    all_rows = []
    total_n = 0
    for wave in waves:
        harm = load_harmonized(wave)
        weights = load_weights(wave)
        merged = harm.merge(weights, on="case_id", how="left")
        merged["weight"] = merged["weight"].fillna(1.0)
        merged["_wave"] = wave
        all_rows.append(merged)
        total_n += len(merged)
        print(f"  {wave}: {len(merged)} rows")

    stacked = pd.concat(all_rows, ignore_index=True)

    # Build columns list in stacked data: canonical names + wave id
    canonical_cols = list(CANONICAL_ALIASES.keys())
    data_cols = ["_wave"] + canonical_cols

    # For the compact data, materialize the harmonized columns under their
    # canonical names
    harm_col_map = {
        "Age": "Age",
        "Gender": "Gender",
        "Race": "Race",
        "Education": "Education",
        "Party ID": "Party ID",
        "PID Lean": "PID Lean",
        "2024 vote": "2024 Vote",
    }
    export = pd.DataFrame({"_wave": stacked["_wave"]})
    for canon in canonical_cols:
        export[canon] = stacked[harm_col_map[canon]]

    rows = []
    for _, row in export.iterrows():
        rows.append([to_compact_value(v) for v in row])

    columns_out: dict[str, dict] = {}
    # Meta column _wave: categorical with wave options
    columns_out["_wave"] = {
        "label": "Wave",
        "question": "Survey wave",
        "type": "categorical",
        "options": [{"code": w, "label": w} for w in waves],
        "waves": waves,
    }
    for canon in canonical_cols:
        columns_out[canon] = {
            "label": canon,
            "question": CANONICAL_QUESTIONS.get(canon, canon),
            "type": "categorical",
            "options": [{"code": c, "label": l} for c, l in CANONICAL_OPTIONS[canon]],
            "waves": waves,
        }

    codebook = {
        "waves": {stack_id: {"label": label, "n": int(total_n),
                             "note": f"Stacked: {'+'.join(waves)}. Only demographic columns available across all waves."}},
        "columns": columns_out,
    }
    data_payload = {
        "wave": stack_id,
        "n": int(total_n),
        "columns": data_cols,
        "rows": rows,
        "weights": [float(w) for w in stacked["weight"].tolist()],
    }
    cb_path = OUTPUT_DIR / f"codebook_{stack_id}.json"
    data_path = OUTPUT_DIR / f"data_{stack_id}.json"
    cb_path.write_text(json.dumps(codebook, indent=2))
    data_path.write_text(json.dumps(data_payload, separators=(",", ":")))
    print(f"  Wrote {cb_path} ({cb_path.stat().st_size/1024:.1f} KB)")
    print(f"  Wrote {data_path} ({data_path.stat().st_size/1024:.1f} KB)")
    print(f"  Total N={total_n}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--wave",
        choices=["F24", "S25", "F25", "stacked_all", "stacked_2026", "all"],
        default="all",
    )
    args = parser.parse_args()

    if args.wave == "all":
        targets = ["F24", "S25", "F25", "stacked_all", "stacked_2026"]
    else:
        targets = [args.wave]

    for t in targets:
        if t in {"F24", "S25", "F25"}:
            preprocess_wave(t)
        elif t == "stacked_all":
            build_stacked("stacked_all", "All waves (stacked)", ["F24", "S25", "F25"])
        elif t == "stacked_2026":
            build_stacked("stacked_2026", "2026 cycle (S25 + F25)", ["S25", "F25"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
