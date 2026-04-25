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
import re
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

# Canonical demographic columns get their values from the harmonized crosswalk
# so Race / Party ID / Age coding is identical across waves regardless of how
# each raw wave coded them. Every other column from any pooled wave is also
# exposed in the stacked dataset, but only when its option code-set agrees
# across all waves that asked it. Rows from a wave that didn't ask a given
# question carry null for that column — the crosstab math already skips nulls,
# so weighted N naturally restricts to respondents from waves that asked it.
HARMONIZED_DEMOGRAPHICS: dict[str, str] = {
    # canonical name in stacked dataset -> column name in harmonized_<wave>.csv
    "Age": "Age",
    "Gender": "Gender",
    "Race": "Race",
    "Education": "Education",
    "Party ID": "Party ID",
    "PID Lean": "PID Lean",
    "2024 vote": "2024 Vote",
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


def canonicalize(name: str) -> str:
    """Normalize a column name so 'Need for cognition_1' and
    'need_for_cognition_1' collide — lowercase, alphanumeric-only, single
    underscores, stripped."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


HARMONIZED_CANONS = {canonicalize(k) for k in HARMONIZED_DEMOGRAPHICS} | {
    "income",  # exists in F24 + S25 but not F25; not part of S25 raking targets
}


def load_harmonized(wave: str) -> pd.DataFrame:
    path = REPO_ROOT / "data-raw" / "harmonized" / f"harmonized_{wave.lower()}.csv"
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}; run crosswalk.py first")
    return pd.read_csv(path)


def _options_signature(options: list | None) -> tuple | None:
    """Comparable, order-independent signature of an options list."""
    if not options:
        return None
    return tuple(sorted((o["code"], o["label"]) for o in options))


def _options_codes(options: list | None) -> tuple | None:
    if not options:
        return None
    return tuple(sorted(o["code"] for o in options))


def build_stacked(stack_id: str, label: str, waves: list[str]) -> None:
    print(f"\n===== stack {stack_id} ({'+'.join(waves)}) =====")

    # Per wave, gather: processed codebook (to know surviving column schema),
    # raw values DataFrame (so we can pull non-demographic values keyed by
    # case_id), harmonized demographics, and final weights.
    loaders = {"S25": load_s25, "F25": load_f25, "F24": load_f24}
    per_wave: dict[str, dict] = {}
    for wave in waves:
        cb_path = OUTPUT_DIR / f"codebook_{wave.lower()}.json"
        if not cb_path.exists():
            raise FileNotFoundError(
                f"{cb_path} missing; run preprocess for {wave} first"
            )
        cb = json.loads(cb_path.read_text())
        values, _, _, case_id_col = loaders[wave]()
        if case_id_col != "case_id":
            values = values.rename(columns={case_id_col: "case_id"})
        # Some raw waves ship their own 'weight'/'weights' column; we always
        # want our re-raked weight from data-raw/weights/, so drop them first
        # to keep the merge unambiguous.
        for w_col in ("weight", "weights"):
            if w_col in values.columns:
                values = values.drop(columns=[w_col])
        harm = load_harmonized(wave).rename(
            columns={c: f"__HARM__{c}" for c in HARMONIZED_DEMOGRAPHICS.values()}
        )
        weights = load_weights(wave)
        df = (
            values.merge(harm[["case_id"] + [f"__HARM__{v}" for v in HARMONIZED_DEMOGRAPHICS.values()]],
                          on="case_id", how="left")
                  .merge(weights, on="case_id", how="left")
        )
        df["weight"] = df["weight"].fillna(1.0)
        per_wave[wave] = {"cb": cb, "df": df}
        print(f"  {wave}: {len(df)} rows; {len(cb['columns'])} columns survived per-wave preprocess")

    # Pool non-demographic columns by canonical name across waves.
    # canon -> {wave: orig_col_name}
    canon_map: dict[str, dict[str, str]] = {}
    for wave, info in per_wave.items():
        for orig in info["cb"]["columns"]:
            ck = canonicalize(orig)
            if ck in HARMONIZED_CANONS:
                continue  # demographics handled separately
            canon_map.setdefault(ck, {})[wave] = orig

    # Decide each canonical column's stack-compatibility and merged schema.
    accepted: dict[str, dict] = {}  # canon -> {output_key, label, question, type, options, wave_to_orig, present_waves}
    skipped: list[tuple[str, str]] = []
    for canon, by_wave in canon_map.items():
        present_waves = sorted(by_wave.keys(), key=lambda w: waves.index(w))
        entries = [(w, per_wave[w]["cb"]["columns"][by_wave[w]]) for w in present_waves]

        # Single wave: always include verbatim
        if len(entries) == 1:
            w, e = entries[0]
            accepted[canon] = {
                "label": e["label"],
                "question": e["question"],
                "type": e.get("type", "categorical"),
                "options": e.get("options"),
                "wave_to_orig": dict(by_wave),
                "present_waves": present_waves,
            }
            continue

        # Multiple waves: figure out compatibility
        sigs = [_options_signature(e.get("options")) for _, e in entries]
        codes = [_options_codes(e.get("options")) for _, e in entries]

        if all(s is None for s in sigs):
            # Numeric in every wave that has it
            accepted[canon] = {
                "label": entries[0][1]["label"],
                "question": entries[0][1]["question"],
                "type": "numeric",
                "options": None,
                "wave_to_orig": dict(by_wave),
                "present_waves": present_waves,
            }
            continue
        if any(s is None for s in sigs):
            skipped.append((canon, "mixed numeric/categorical across waves"))
            continue
        if len(set(sigs)) == 1:
            # Codes AND labels match exactly
            accepted[canon] = {
                "label": entries[0][1]["label"],
                "question": entries[0][1]["question"],
                "type": "categorical",
                "options": entries[0][1]["options"],
                "wave_to_orig": dict(by_wave),
                "present_waves": present_waves,
            }
            continue
        if len(set(codes)) == 1:
            # Codes match, labels drift — common when one wave hand-coded options
            # and another auto-generated them. Adopt the first (most-readable)
            # wave's labels but flag for visibility.
            accepted[canon] = {
                "label": entries[0][1]["label"],
                "question": entries[0][1]["question"],
                "type": "categorical",
                "options": entries[0][1]["options"],
                "wave_to_orig": dict(by_wave),
                "present_waves": present_waves,
                "label_drift": True,
            }
            continue
        skipped.append((canon, f"diverging code-sets {dict(zip([w for w,_ in entries], codes))}"))

    print(f"  pooled non-demog canonicals: {len(canon_map)}; "
          f"accepted: {len(accepted)}; skipped: {len(skipped)}")
    if skipped:
        for canon, reason in skipped[:8]:
            print(f"    skip {canon}: {reason}")
        if len(skipped) > 8:
            print(f"    ... and {len(skipped) - 8} more")

    # Pick the prettiest available native name as the output column ID. Prefer
    # S25 (capitalized + spaces, most readable), then F25, then F24.
    name_priority = ("S25", "F25", "F24")

    def output_key_for(by_wave: dict[str, str]) -> str:
        for w in name_priority:
            if w in by_wave:
                return by_wave[w]
        return next(iter(by_wave.values()))

    # For each accepted canonical column, finalize the output key, dedup any
    # collisions (same key across different canonicals — extremely rare).
    used_keys: set[str] = set()
    for canon, info in accepted.items():
        key = output_key_for(info["wave_to_orig"])
        original = key
        n = 2
        while key in used_keys:
            key = f"{original} ({n})"
            n += 1
        info["output_key"] = key
        used_keys.add(key)

    # Compose final column ordering: _wave, then canonical demographics, then
    # accepted non-demographics sorted by output_key.
    output_columns: list[str] = ["_wave"] + list(HARMONIZED_DEMOGRAPHICS.keys())
    nondemog_ordered = sorted(
        accepted.values(),
        key=lambda info: info["output_key"].lower(),
    )
    output_columns += [info["output_key"] for info in nondemog_ordered]

    # Build the codebook entries.
    columns_out: dict[str, dict] = {}
    columns_out["_wave"] = {
        "label": "Wave",
        "question": "Survey wave",
        "type": "categorical",
        "options": [{"code": w, "label": w} for w in waves],
        "waves": waves,
    }
    for canon_name in HARMONIZED_DEMOGRAPHICS:
        columns_out[canon_name] = {
            "label": canon_name,
            "question": CANONICAL_QUESTIONS.get(canon_name, canon_name),
            "type": "categorical",
            "options": [{"code": c, "label": l} for c, l in CANONICAL_OPTIONS[canon_name]],
            "waves": waves,
        }
    for info in nondemog_ordered:
        present = info["present_waves"]
        # Annotate label with wave coverage when not asked in every pooled wave.
        coverage = "" if set(present) == set(waves) else f"  [{'+'.join(present)}]"
        entry: dict = {
            "label": f"{info['label']}{coverage}",
            "question": info["question"],
            "type": info["type"],
            "waves": present,
        }
        if info["options"]:
            entry["options"] = info["options"]
        columns_out[info["output_key"]] = entry

    # Materialize stacked rows. We do this column-by-column per wave because
    # itertuples renames columns with spaces/punct (e.g. "Need for cognition_1"
    # -> "_0"), which would silently null them out.
    rows: list[list] = []
    weights_out: list[float] = []
    for wave in waves:
        wdf = per_wave[wave]["df"]
        n_wave = len(wdf)

        # Build per-wave column arrays aligned to output_columns.
        wave_cols: list[list] = []
        for out_key in output_columns:
            if out_key == "_wave":
                wave_cols.append([wave] * n_wave)
            elif out_key in HARMONIZED_DEMOGRAPHICS:
                src = f"__HARM__{HARMONIZED_DEMOGRAPHICS[out_key]}"
                wave_cols.append(
                    [to_compact_value(v) for v in wdf[src].tolist()]
                )
            else:
                # Find the canonical info for this output key
                src = None
                for info in nondemog_ordered:
                    if info["output_key"] == out_key:
                        src = info["wave_to_orig"].get(wave)
                        break
                if src is None or src not in wdf.columns:
                    wave_cols.append([None] * n_wave)
                else:
                    wave_cols.append(
                        [to_compact_value(v) for v in wdf[src].tolist()]
                    )

        # Transpose column-major to row-major and append.
        for i in range(n_wave):
            rows.append([wave_cols[c][i] for c in range(len(output_columns))])
        weights_out.extend(float(w) for w in wdf["weight"].tolist())

    total_n = len(rows)

    cov_summary = {
        "all_waves": sum(1 for info in accepted.values() if set(info["present_waves"]) == set(waves)),
        "two_waves": sum(1 for info in accepted.values() if 1 < len(info["present_waves"]) < len(waves)),
        "single_wave": sum(1 for info in accepted.values() if len(info["present_waves"]) == 1),
    }
    note = (
        f"Stacked: {'+'.join(waves)}. {len(HARMONIZED_DEMOGRAPHICS)} canonical "
        f"demographics + {len(accepted)} non-demographic columns "
        f"(in all {len(waves)} waves: {cov_summary['all_waves']}; "
        f"partial coverage: {cov_summary['two_waves']}; "
        f"single wave: {cov_summary['single_wave']}). "
        f"Rows from waves that didn't ask a given question carry null."
    )

    codebook = {
        "waves": {stack_id: {"label": label, "n": int(total_n), "note": note}},
        "columns": columns_out,
    }
    data_payload = {
        "wave": stack_id,
        "n": int(total_n),
        "columns": output_columns,
        "rows": rows,
        "weights": weights_out,
    }
    cb_path = OUTPUT_DIR / f"codebook_{stack_id}.json"
    data_path = OUTPUT_DIR / f"data_{stack_id}.json"
    cb_path.write_text(json.dumps(codebook, indent=2))
    data_path.write_text(json.dumps(data_payload, separators=(",", ":")))
    print(f"  Wrote {cb_path} ({cb_path.stat().st_size/1024:.1f} KB)")
    print(f"  Wrote {data_path} ({data_path.stat().st_size/1024:.1f} KB)")
    print(f"  Total N={total_n}; columns={len(output_columns)}; "
          f"all-waves={cov_summary['all_waves']}, "
          f"partial={cov_summary['two_waves']}, "
          f"single={cov_summary['single_wave']}")


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
