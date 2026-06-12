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

# Raw replication files live in the repo (data-raw/source/<wave>/), pulled from
# Yale Dataverse by scripts/fetch_data.py — no dependency on local ~/Downloads.
SOURCE_DIR = REPO_ROOT / "data-raw" / "source"
S25_DIR = SOURCE_DIR / "S25"
F25_DIR = SOURCE_DIR / "F25"
S26_DIR = SOURCE_DIR / "S26"
F24_DIR = SOURCE_DIR / "F24"

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
DROP_SUFFIXES = ("_TEXT", "_text",
                 "_ado_1", "_ado_2", "_ado_3", "_ado_4", "_ado_5",
                 "_labels", "_actualnumber", "_count")
# `_do_N` (display order) columns are kept conditionally: they're MaxDiff
# offer-tracking siblings for some bases (issue_rank, electable, ...) and noise
# for others (gender, education, ...). Detection handled inside preprocess_wave.
_DO_RE = re.compile(r"_do_\d+$")
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


# Minimum number of _do_N siblings to qualify a base as MaxDiff. Three filters
# out the binary forced-choice tasks (e.g. obbba_maxdiff_1..4 with only 2
# items) — they're equivalent to ordinary categoricals so we leave them alone.
_MAXDIFF_MIN_ITEMS = 3


def detect_maxdiff_bases(
    values: pd.DataFrame,
) -> tuple[dict[str, list[int]], set[str]]:
    """Find base columns that look like a MaxDiff:
      - the base column itself exists with numeric codes
      - at least _MAXDIFF_MIN_ITEMS sibling columns named `<base>_do_<N>`
      - each `_do_N` is sparse-binary (values ⊂ {1, 2, NaN})

    Returns (bases, do_cols) where:
      bases   = {base_col: sorted list of item N's that have _do_N siblings}
      do_cols = set of every consumed `_do_N` column name
    """
    cols = list(values.columns)
    base_to_items: dict[str, list[int]] = {}
    consumed: set[str] = set()
    by_base: dict[str, list[tuple[int, str]]] = {}
    for c in cols:
        m = _DO_RE.search(c)
        if not m:
            continue
        n = int(m.group(0).split("_")[-1])
        base = c[: m.start()]
        if base not in cols:
            continue
        by_base.setdefault(base, []).append((n, c))
    for base, sibs in by_base.items():
        if len(sibs) < _MAXDIFF_MIN_ITEMS:
            continue
        # Verify each sibling is binary {1, 2, NaN}
        ok_items: list[int] = []
        ok_do_cols: list[str] = []
        for n, do_col in sorted(sibs):
            uniq = set(values[do_col].dropna().unique().tolist())
            if not uniq:
                continue
            # Allow 1.0/2.0 floats too
            uniq_int = {int(v) for v in uniq if pd.notna(v) and float(v).is_integer()}
            if uniq <= {1, 2, 1.0, 2.0} or (uniq_int <= {1, 2} and len(uniq_int) >= 1):
                ok_items.append(n)
                ok_do_cols.append(do_col)
        if len(ok_items) < _MAXDIFF_MIN_ITEMS:
            continue
        base_to_items[base] = ok_items
        consumed.update(ok_do_cols)
    return base_to_items, consumed


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


# Verasight ships F25 `age` as bracketed codes 1-6 with labels that just echo
# the codes ("1.0", "2.0", ...). The codebook XLSX flags it as `[Numeric]` and
# offers no human labels, so we override with the actual brackets (verified
# against `age_actualnumber` in the raw CSV: each code's min/max age).
F25_DEMO_OPTION_OVERRIDES: dict[str, list[tuple[int, str]]] = {
    "age": [
        (1, "18-22"),
        (2, "23-29"),
        (3, "30-34"),
        (4, "35-44"),
        (5, "45-64"),
        (6, "65+"),
    ],
}

# Populated in load_f25() by build_f25_obbba_messages and read by preprocess_wave
# to wire the synthetic `obbba_messages` MaxDiff codebook entry.
F25_OBBBA_ITEMS: list[dict] = []


# Items that drive the synthetic `obbba_messages` MaxDiff. The codebook XLSX
# labels for `obbba_maxdiff_<round>` are unfilled Qualtrics piped placeholders
# ("Democrats say: ${e://Field/d_msg_<round>}"); the actual message text lives
# in per-respondent `d_msg_<round>` / `r_msg_<round>` columns. Across all
# respondents these resolve to 24 unique D messages × 24 unique R messages.
# Each respondent saw 4 rounds, each round = a randomly drawn (D, R) pair, and
# picked one of the two. We collate the 4 rounds into a single MaxDiff with 48
# items so per-message win rates (picks ÷ times shown) are directly readable.
_OBBBA_ROUNDS = (1, 2, 3, 4)


def build_f25_obbba_messages(raw: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """Returns (extra_columns_df, items_list).

    extra_columns_df: per-respondent integer counts. For each item id i:
      - `obbba_messages_do_<i>` = # rounds (1..4) this respondent was shown msg i
      - `obbba_messages_pick_<i>` = # rounds (1..4) this respondent picked msg i
      Null when count is 0.
    items_list: codebook items list, each with code/label/do_col/pick_col.
    """
    # Collect unique D + R messages with deterministic codes (D first, then R,
    # each side sorted alphabetically so codes stay stable across pipeline runs).
    d_msgs: set[str] = set()
    r_msgs: set[str] = set()
    for n in _OBBBA_ROUNDS:
        if f"d_msg_{n}" in raw.columns:
            d_msgs.update(raw[f"d_msg_{n}"].dropna().astype(str).tolist())
        if f"r_msg_{n}" in raw.columns:
            r_msgs.update(raw[f"r_msg_{n}"].dropna().astype(str).tolist())
    d_sorted = sorted(d_msgs)
    r_sorted = sorted(r_msgs)
    items: list[dict] = []
    msg_to_code: dict[str, int] = {}
    code = 1
    for txt in d_sorted:
        msg_to_code[txt] = code
        items.append({
            "code": code,
            "label": f"Democrats say: {txt}",
            "do_col": f"obbba_messages_do_{code}",
            "pick_col": f"obbba_messages_pick_{code}",
        })
        code += 1
    for txt in r_sorted:
        msg_to_code[txt] = code
        items.append({
            "code": code,
            "label": f"Republicans say: {txt}",
            "do_col": f"obbba_messages_do_{code}",
            "pick_col": f"obbba_messages_pick_{code}",
        })
        code += 1

    n = len(raw)
    n_items = len(items)
    # Pre-allocate Python int matrices (NaN-friendly via 0 sentinel; we fill
    # columns with NaN when count is 0 so the data file stays sparse).
    do_counts = [[0] * n for _ in range(n_items)]
    pick_counts = [[0] * n for _ in range(n_items)]

    # Fast access to per-round arrays
    arrays = {}
    for n_round in _OBBBA_ROUNDS:
        arrays[f"d_{n_round}"] = raw[f"d_msg_{n_round}"].astype(object).where(raw[f"d_msg_{n_round}"].notna(), None).tolist() if f"d_msg_{n_round}" in raw.columns else [None]*n
        arrays[f"r_{n_round}"] = raw[f"r_msg_{n_round}"].astype(object).where(raw[f"r_msg_{n_round}"].notna(), None).tolist() if f"r_msg_{n_round}" in raw.columns else [None]*n
        arrays[f"pick_{n_round}"] = raw[f"obbba_maxdiff_{n_round}"].tolist() if f"obbba_maxdiff_{n_round}" in raw.columns else [None]*n
        arrays[f"do1_{n_round}"] = raw[f"obbba_maxdiff_{n_round}_do_1"].tolist() if f"obbba_maxdiff_{n_round}_do_1" in raw.columns else [None]*n

    for ri in range(n):
        for n_round in _OBBBA_ROUNDS:
            d_msg = arrays[f"d_{n_round}"][ri]
            r_msg = arrays[f"r_{n_round}"][ri]
            if d_msg is None or r_msg is None:
                continue
            d_id = msg_to_code.get(str(d_msg))
            r_id = msg_to_code.get(str(r_msg))
            if d_id is None or r_id is None:
                continue
            # Both items were shown this round
            do_counts[d_id - 1][ri] += 1
            do_counts[r_id - 1][ri] += 1
            pick = arrays[f"pick_{n_round}"][ri]
            do1 = arrays[f"do1_{n_round}"][ri]
            if pick is None or do1 is None or pd.isna(pick) or pd.isna(do1):
                continue
            pick_int = int(pick) if not pd.isna(pick) else None
            do1_int = int(do1) if not pd.isna(do1) else None
            if pick_int not in (1, 2) or do1_int not in (1, 2):
                continue
            # do1_int says which side is in slot 1: 1 = D in slot 1, 2 = R in slot 1
            d_in_slot_1 = (do1_int == 1)
            picked_d = (pick_int == 1 and d_in_slot_1) or (pick_int == 2 and not d_in_slot_1)
            if picked_d:
                pick_counts[d_id - 1][ri] += 1
            else:
                pick_counts[r_id - 1][ri] += 1

    # Build the extras DataFrame, using NaN where count == 0 so the data file stays sparse.
    cols = {}
    for it in items:
        code_i = it["code"]
        do_arr = do_counts[code_i - 1]
        pk_arr = pick_counts[code_i - 1]
        cols[it["do_col"]] = [v if v > 0 else np.nan for v in do_arr]
        cols[it["pick_col"]] = [v if v > 0 else np.nan for v in pk_arr]
    extras = pd.DataFrame(cols, index=raw.index)
    return extras, items


def load_f25() -> tuple[pd.DataFrame, dict[str, str], pd.DataFrame, str]:
    data_path = F25_DIR / "yypfall25dat_withweights.csv"
    codebook_path = F25_DIR / "2025-138a_codebook.xlsx"

    raw = pd.read_csv(data_path, low_memory=False)
    # Strip _labels columns (kept separately as labels_df for codebook building)
    numeric_cols = [c for c in raw.columns if not c.endswith("_labels")]
    values = raw[numeric_cols].copy()

    # Build the synthetic obbba_messages MaxDiff by collating obbba_maxdiff_1..4
    # rounds with the actual message text per respondent. Append the new
    # `obbba_messages_do_<id>` / `obbba_messages_pick_<id>` columns plus a
    # main token column. Stash items list in module-level holder so the
    # codebook builder can pick it up.
    obbba_extras, obbba_items = build_f25_obbba_messages(raw)
    # Main token column: 1 if respondent answered any obbba round, else NaN.
    any_pick = pd.Series(np.nan, index=raw.index)
    for n in _OBBBA_ROUNDS:
        col = f"obbba_maxdiff_{n}"
        if col in raw.columns:
            mask = raw[col].notna()
            any_pick.loc[mask] = 1
    values = pd.concat([values, obbba_extras], axis=1).copy()
    values["obbba_messages"] = any_pick
    F25_OBBBA_ITEMS.clear()
    F25_OBBBA_ITEMS.extend(obbba_items)

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
# S26 loader (Verasight CSV; no _labels columns, so labels come from the
# codebook XLSX. Race is an 8-way multi-select we collapse to a single column.)
# ------------------------------------------------------------------

# Multi-round MaxDiff families: the same battery shown across several rounds
# (e.g. issue_maxdiff1/2/3). We collate the rounds into ONE MaxDiff, summing
# offers (times shown) and picks across rounds per item — rather than exposing
# each round as its own column. (stem, [round numbers])
S26_MAXDIFF_FAMILIES = [
    ("issue_maxdiff", [1, 2, 3]),
    ("ai_risks_md", [1, 2]),
    ("ai_benefits_md", [1, 2]),
]
S26_MAXDIFF_QTEXT = {
    "issue_maxdiff": "When it comes to deciding your vote, which issue is more important to you? — collated across 3 MaxDiff rounds",
    "ai_risks_md": "Which is the bigger risk of AI? — collated across 2 MaxDiff rounds",
    "ai_benefits_md": "Which is the bigger benefit of AI? — collated across 2 MaxDiff rounds",
}
S26_MAXDIFF_LABEL = {
    "issue_maxdiff": "Issue importance MaxDiff (collated 3 rounds)",
    "ai_risks_md": "AI risks MaxDiff (collated 2 rounds)",
    "ai_benefits_md": "AI benefits MaxDiff (collated 2 rounds)",
}
# Populated by build_s26_multiround_maxdiffs (in load_s26), read by preprocess_wave.
S26_MAXDIFF_ITEMS: dict[str, list[dict]] = {}
S26_MAXDIFF_HIDE: set[str] = set()
S26_MAXDIFF_AUX: set[str] = set()


def build_s26_multiround_maxdiffs(
    raw: pd.DataFrame, code_label: dict[str, dict[float, str]]
) -> pd.DataFrame:
    """Collate each S26 multi-round MaxDiff family into one. Returns a DataFrame
    of new columns: per item code c, `<stem>_do_<c>` (# rounds shown) and
    `<stem>_pick_<c>` (# rounds picked), plus a main token column `<stem>`.
    Populates S26_MAXDIFF_ITEMS / _HIDE / _AUX."""
    S26_MAXDIFF_ITEMS.clear()
    S26_MAXDIFF_HIDE.clear()
    S26_MAXDIFF_AUX.clear()
    n = len(raw)
    out_cols: dict[str, list] = {}
    for stem, rounds in S26_MAXDIFF_FAMILIES:
        round_cols = [f"{stem}{r}" for r in rounds]
        if not all(c in raw.columns for c in round_cols):
            continue
        cl = code_label.get(round_cols[0], {})
        if not cl:
            continue
        item_codes = sorted(int(c) for c in cl.keys())
        do_counts = {c: [0] * n for c in item_codes}
        pick_counts = {c: [0] * n for c in item_codes}
        answered = [False] * n
        for r in rounds:
            rc = f"{stem}{r}"
            S26_MAXDIFF_HIDE.add(rc)
            picks = pd.to_numeric(raw[rc], errors="coerce").tolist()
            shown_by_item = {}
            for c in item_codes:
                sib = f"{rc}_do_{c}"
                if sib in raw.columns:
                    S26_MAXDIFF_HIDE.add(sib)
                    shown_by_item[c] = raw[sib].notna().tolist()
            for i in range(n):
                pv = picks[i]
                if pv is not None and not pd.isna(pv):
                    answered[i] = True
                    pvi = int(pv)
                    if pvi in pick_counts:
                        pick_counts[pvi][i] += 1
                for c in item_codes:
                    sb = shown_by_item.get(c)
                    if sb is not None and sb[i]:
                        do_counts[c][i] += 1
        items = []
        for c in item_codes:
            do_col, pick_col = f"{stem}_do_{c}", f"{stem}_pick_{c}"
            out_cols[do_col] = [v if v > 0 else np.nan for v in do_counts[c]]
            out_cols[pick_col] = [v if v > 0 else np.nan for v in pick_counts[c]]
            S26_MAXDIFF_AUX.add(do_col)
            S26_MAXDIFF_AUX.add(pick_col)
            items.append({"code": c, "label": cl.get(float(c), str(c)),
                          "do_col": do_col, "pick_col": pick_col})
        S26_MAXDIFF_ITEMS[stem] = items
        out_cols[stem] = [1 if a else np.nan for a in answered]
    return pd.DataFrame(out_cols, index=raw.index)


# Codebook ships these with a leading 'a' (an export quirk); the official
# cleaning notebook strips it. We do the same so names read cleanly and match
# cross-wave (e.g. 2024_recalled_vote pools with F25).
S26_STRIP_A_PREFIX = (
    "a2024_recalled_vote", "a2028_dem_primary", "a2028_gop_primary",
    "a2028_gop_primary_djt",
)
S26_CES_RACE_LABELS = {
    1: "White", 2: "Black or African-American", 3: "Hispanic or Latino",
    4: "Asian or Asian-American", 5: "Native American",
    7: "Two or more races", 8: "Other",
}
# S26 ships `age` as a raw integer; bucket to Yale's official 138b 6-cat scheme
# for display so the age demographic reads cleanly (and matches YYP toplines).
S26_AGE_BINS = [18, 22, 29, 34, 44, 64, 200]
S26_AGE_CODES = [1, 2, 3, 4, 5, 6]
S26_DEMO_OPTION_OVERRIDES: dict[str, list[tuple[int, str]]] = {
    "age": [(1, "18-22"), (2, "23-29"), (3, "30-34"),
            (4, "35-44"), (5, "45-64"), (6, "65+")],
}


def _s26_codebook_maps(codebook_path: Path) -> tuple[dict[str, str], dict[str, dict[float, str]]]:
    """Return (qtexts, code_label) from the S26 codebook XLSX.
    code_label[var] = {numeric_code: label}."""
    cb = pd.read_excel(codebook_path)
    cb["Variable"] = cb["Variable"].ffill()
    qtexts: dict[str, str] = {}
    code_label: dict[str, dict[float, str]] = {}
    for var in cb["Variable"].dropna().unique():
        rows = cb[cb["Variable"] == var]
        desc = rows["Description"].dropna()
        qtexts[var] = str(desc.iloc[0]) if len(desc) else str(var)
        cl: dict[float, str] = {}
        for _, r in rows.iterrows():
            resp, lab = r["Response"], r["Label"]
            if pd.notna(resp) and pd.notna(lab):
                try:
                    cl[float(resp)] = str(lab)
                except (ValueError, TypeError):
                    continue
        if cl:
            code_label[var] = cl
    return qtexts, code_label


def load_s26() -> tuple[pd.DataFrame, dict[str, str], pd.DataFrame, str]:
    data_path = S26_DIR / "2025-138b_client.csv"
    codebook_path = S26_DIR / "2025-138b_codebook.xlsx"

    raw = pd.read_csv(data_path, low_memory=False)
    qtexts, code_label = _s26_codebook_maps(codebook_path)

    # Bucket raw integer age into the 6-cat 138b scheme (override supplies labels).
    if "age" in raw.columns:
        raw["age"] = pd.cut(
            pd.to_numeric(raw["age"], errors="coerce"),
            bins=S26_AGE_BINS, labels=S26_AGE_CODES, right=True, include_lowest=True,
        ).astype("Int64")

    # Collapse the 8-way ces_race multi-select into a single ces_race column
    # (Yale's priority pick; Middle Eastern 6 -> Asian 4). Then drop the binaries.
    race_priority = [f"ces_race_{i}" for i in (2, 4, 5, 3, 1, 6, 7, 8)]

    def pick_race(row):
        for col in race_priority:
            if row.get(col) == 1:
                return int(col.rsplit("_", 1)[-1])
        return np.nan
    if all(c in raw.columns for c in race_priority):
        ces = raw.apply(pick_race, axis=1).replace(6, 4)
        raw = raw.drop(columns=[c for c in raw.columns if c.startswith("ces_race_")])
        raw["ces_race"] = ces
        code_label["ces_race"] = {float(k): v for k, v in S26_CES_RACE_LABELS.items()}
        qtexts["ces_race"] = "What racial or ethnic group best describes you?"

    # Collate multi-round MaxDiff families (issue_maxdiff1/2/3, ai_*_md1/2)
    # into single MaxDiff columns with offer/pick counts per item.
    md_extras = build_s26_multiround_maxdiffs(raw, code_label)
    if not md_extras.empty:
        raw = pd.concat([raw, md_extras], axis=1).copy()

    values = raw.copy()

    # Reconstruct a labels_df from the codebook (no _labels columns in S26).
    labels = pd.DataFrame(index=values.index)
    for c in values.columns:
        cl = code_label.get(c)
        if not cl:
            continue
        num = pd.to_numeric(values[c], errors="coerce")
        labels[c] = num.map(cl)

    # Strip the leading 'a' on the recalled-vote / primary columns (and their
    # _do_/_text siblings) across values, labels, and qtexts so names read
    # cleanly and pool cross-wave.
    rename: dict[str, str] = {}
    for c in values.columns:
        for pref in S26_STRIP_A_PREFIX:
            if c == pref or c.startswith(pref + "_"):
                rename[c] = c[1:]
                break
    if rename:
        values = values.rename(columns=rename)
        labels = labels.rename(columns={k: v for k, v in rename.items() if k in labels.columns})
        for k, v in rename.items():
            if k in qtexts:
                qtexts[v] = qtexts.pop(k)

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
    loader = {"S25": load_s25, "F25": load_f25, "F24": load_f24, "S26": load_s26}[wave]
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

    # Per-wave hand-coded option label overrides for cases where the upstream
    # codebook ships only numeric codes.
    option_overrides = {
        "F24": F24_DEMO_OPTION_OVERRIDES,
        "F25": F25_DEMO_OPTION_OVERRIDES,
        "S26": S26_DEMO_OPTION_OVERRIDES,
    }.get(wave)

    # First pass: detect MaxDiff bases (and their _do_N siblings). The siblings
    # don't get user-facing codebook entries — they're consumed as offer
    # tracking metadata into the parent's codebook entry — but we keep them in
    # the data file so the frontend can compute wins/offers per item.
    maxdiff_bases, maxdiff_do_cols = detect_maxdiff_bases(values)

    # F25 obbba: the per-round binary tasks (`obbba_maxdiff_1..4`) and their
    # message-text companions (`d_msg_1..4`, `r_msg_1..4`) are superseded by
    # the synthetic `obbba_messages` MaxDiff (built in load_f25). Hide the
    # raw inputs from the user-facing codebook; keep their pre-aggregated
    # offer/pick siblings in the data file.
    obbba_hide_cols: set[str] = set()
    obbba_aux_cols: set[str] = set()
    if wave == "F25" and F25_OBBBA_ITEMS:
        for n in _OBBBA_ROUNDS:
            obbba_hide_cols.add(f"obbba_maxdiff_{n}")
            obbba_hide_cols.add(f"obbba_maxdiff_{n}_do_1")
            obbba_hide_cols.add(f"obbba_maxdiff_{n}_do_2")
            obbba_hide_cols.add(f"d_msg_{n}")
            obbba_hide_cols.add(f"r_msg_{n}")
        for it in F25_OBBBA_ITEMS:
            obbba_aux_cols.add(it["do_col"])
            obbba_aux_cols.add(it["pick_col"])
        # Auto-detected MaxDiff might have caught obbba_maxdiff_1..4 (each is
        # a 2-item _do_ pair, threshold is 3+ siblings, but be defensive).
        maxdiff_bases = {b: items for b, items in maxdiff_bases.items()
                         if not b.startswith("obbba_maxdiff_")}
        maxdiff_do_cols = {c for c in maxdiff_do_cols
                           if not c.startswith("obbba_maxdiff_")}

    # S26 multi-round MaxDiff families: hide the per-round bases + their _do_
    # siblings (superseded by the collated column built in load_s26); keep the
    # collated offer/pick count columns in the data file.
    if wave == "S26" and S26_MAXDIFF_ITEMS:
        obbba_hide_cols |= set(S26_MAXDIFF_HIDE)
        obbba_aux_cols |= set(S26_MAXDIFF_AUX)
        # Drop per-round bases from auto-detected MaxDiff (they're collated now).
        hide_stems = tuple(S26_MAXDIFF_ITEMS.keys())
        maxdiff_bases = {b: items for b, items in maxdiff_bases.items()
                         if b not in S26_MAXDIFF_HIDE}
        maxdiff_do_cols = {c for c in maxdiff_do_cols if c not in S26_MAXDIFF_HIDE}

    # Build codebook columns
    columns_out: dict[str, dict] = {}
    for col in values.columns:
        if is_dropped(col):
            continue
        if _DO_RE.search(col) and col not in maxdiff_do_cols:
            continue  # display-order column for a non-MaxDiff base — drop
        if col in maxdiff_do_cols:
            continue  # consumed by parent MaxDiff entry below
        if col in obbba_hide_cols or col in obbba_aux_cols:
            continue  # hidden from user; aux cols still flow into data file below
        labels_col = labels[col] if col in labels.columns else None
        question_text = qtexts.get(col, col)
        readable_label = F24_READABLE_NAMES.get(col) if wave == "F24" else col
        entry = build_column_entry(
            col, values[col], labels_col, question_text,
            readable_label, wave, option_overrides,
        )
        if entry is None:
            continue
        if col in maxdiff_bases:
            # Promote to MaxDiff: rebuild as { type: "maxdiff", items: [...] }
            items = []
            opt_by_code = {
                int(o["code"]) if isinstance(o["code"], (int, float)) and float(o["code"]).is_integer() else o["code"]: o["label"]
                for o in (entry.get("options") or [])
            }
            for n in maxdiff_bases[col]:
                lbl = opt_by_code.get(n, str(n))
                items.append({
                    "code": n,
                    "label": lbl,
                    "do_col": f"{col}_do_{n}",
                })
            columns_out[col] = {
                "label": entry["label"],
                "question": entry["question"],
                "type": "maxdiff",
                "items": items,
                "waves": entry.get("waves", [wave]),
            }
        else:
            columns_out[col] = entry

    # Inject the synthetic obbba_messages MaxDiff entry (F25 only).
    if wave == "F25" and F25_OBBBA_ITEMS:
        columns_out["obbba_messages"] = {
            "label": "OBBBA D vs R messaging (collated 4 rounds)",
            "question": (
                "There is a lot of talk these days about the recently passed "
                "One Big Beautiful Bill Act. Which position do you agree with "
                "more? — collated across 4 rounds of randomly drawn (Democrat, "
                "Republican) message pairs"
            ),
            "type": "maxdiff",
            "items": list(F25_OBBBA_ITEMS),
            "waves": [wave],
        }

    # Inject collated S26 multi-round MaxDiff entries.
    if wave == "S26" and S26_MAXDIFF_ITEMS:
        for stem, items in S26_MAXDIFF_ITEMS.items():
            columns_out[stem] = {
                "label": S26_MAXDIFF_LABEL.get(stem, stem),
                "question": S26_MAXDIFF_QTEXT.get(stem, stem),
                "type": "maxdiff",
                "items": list(items),
                "waves": [wave],
            }

    # Put demographic columns first for UX
    demo_priority = {
        "S25": ["Age", "Gender", "Race", "Education", "Income", "Party ID", "PID Lean", "2024 vote"],
        "F25": ["age", "gender", "ces_race", "education", "anes_party_id", "pid_leaners", "2024_recalled_vote"],
        "F24": ["age", "gender", "race", "education", "income", "party_id", "pid_lean", "x2024_horserace", "x2020_vote"],
        "S26": ["age", "gender", "ces_race", "education", "income", "anes_party_id", "pid_leaners", "2024_recalled_vote", "anes_ideology"],
    }
    priority_cols = [c for c in demo_priority.get(wave, []) if c in columns_out]
    other_cols = [c for c in columns_out if c not in priority_cols]
    ordered_columns = {c: columns_out[c] for c in (priority_cols + other_cols)}

    wave_meta = {
        "S25": {"label": "Spring 2025", "n": int(len(values)), "note": "Weighted via S25 pipeline."},
        "F25": {"label": "Fall 2025", "n": int(len(values)), "note": "Reweighted using S25 pipeline as standard."},
        "F24": {"label": "Fall 2024", "n": int(len(values)), "note": "Registered voters only; reweighted using S25 pipeline."},
        "S26": {"label": "Spring 2026", "n": int(len(values)), "note": "Weighted with Yale's official Spring-2026 procedure (CPS-2024 age×gender + race/education/party targets), not the S25 pipeline."},
    }[wave]

    codebook = {
        "waves": {wave: wave_meta},
        "columns": ordered_columns,
    }

    # Build data payload. Codebook columns + MaxDiff `_do_N` siblings + obbba
    # aux columns (offer/pick counts per message). All non-codebook columns
    # are data-only metadata — the frontend reads them to compute wins/offers
    # but they don't appear in the question picker.
    user_cols = list(ordered_columns.keys())
    do_cols = sorted(c for c in maxdiff_do_cols if c in values.columns)
    aux_cols = sorted(c for c in obbba_aux_cols if c in values.columns)
    keep_cols = user_cols + do_cols + aux_cols
    n_rows_v = len(values)
    col_arrays: list[list] = []
    for c in keep_cols:
        col_arrays.append([to_compact_value(v) for v in values[c].tolist()])
    rows: list[list] = [
        [col_arrays[ci][ri] for ci in range(len(keep_cols))]
        for ri in range(n_rows_v)
    ]

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


def _norm_label(s: str) -> str:
    """Normalize an option/item label for cross-wave matching."""
    return re.sub(r"\s+", " ", str(s).strip().lower()).rstrip(".")


def union_by_label(entries: list[tuple[str, dict]], key: str):
    """Union the options/items of the same question across waves when the
    response set changed (options added/removed) and/or codes were renumbered.

    Matches by normalized LABEL, not code, because YYP renumbers codes across
    waves (e.g. a 2028 candidate is code 2 one wave, code 21 the next) — the
    label is the stable identity. Returns:
        (union_list, value_remap, coverage)
      union_list : list of {code, label, [do_col/pick_col for maxdiff]} with
                   fresh canonical codes (most-recent wave's items first).
      value_remap: {wave: {orig_code: canonical_code}}   (categorical only;
                   maxdiff remap is carried per-item via wave_to_orig fields)
      coverage   : {canonical_code: [waves that offered it]}
    `key` is "options" (categorical) or "items" (maxdiff).
    Returns None if any single wave maps two different labels to one code
    ambiguously (shouldn't happen) — caller then skips.
    """
    # Wave priority: most-recent first so current items get the low codes.
    order = ["S26", "F25", "S25", "F24"]
    ordered = sorted(entries, key=lambda e: order.index(e[0]) if e[0] in order else 99)

    canon_by_norm: dict[str, dict] = {}
    coverage: dict[int, list[str]] = {}
    value_remap: dict[str, dict] = {}
    next_code = 1
    for wave, e in ordered:
        opts = e.get(key) or []
        remap: dict = {}
        for o in opts:
            norm = _norm_label(o["label"])
            if not norm:
                continue
            canon = canon_by_norm.get(norm)
            if canon is None:
                canon = {"code": next_code, "label": o["label"]}
                if key == "items":
                    # carry the per-wave source columns for maxdiff aggregation
                    canon["_src"] = {}
                canon_by_norm[norm] = canon
                coverage[next_code] = []
                next_code += 1
            if wave not in coverage[canon["code"]]:
                coverage[canon["code"]].append(wave)
            remap[o["code"]] = canon["code"]
            if key == "items":
                canon["_src"][wave] = {
                    "do_col": o.get("do_col"),
                    "pick_col": o.get("pick_col"),
                    "orig_code": o["code"],
                }
        value_remap[wave] = remap

    union_list = sorted(canon_by_norm.values(), key=lambda c: c["code"])
    # Re-sort coverage waves into canonical wave order for stable tags.
    for code in coverage:
        coverage[code] = [w for w in order if w in coverage[code]]
    return union_list, value_remap, coverage


def _normalize_question(q: str | None) -> str | None:
    """Aggressive-but-conservative wording normalization for cross-wave matches.

    Lowercases, strips whitespace, collapses internal whitespace, drops trailing
    punctuation, and strips a few non-substantive Qualtrics suffixes ("(check
    all that apply)", " - selected choice", etc.).
    """
    if not q:
        return None
    s = str(q).lower().strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s)  # drop trailing "(check all...)"
    s = re.sub(r"\s*-\s*selected choice\s*$", "", s)
    s = s.rstrip("?.! ")
    return s or None


def build_stacked(stack_id: str, label: str, waves: list[str]) -> None:
    print(f"\n===== stack {stack_id} ({'+'.join(waves)}) =====")

    # Per wave, gather: processed codebook (to know surviving column schema),
    # raw values DataFrame (so we can pull non-demographic values keyed by
    # case_id), harmonized demographics, and final weights.
    loaders = {"S25": load_s25, "F25": load_f25, "F24": load_f24, "S26": load_s26}
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

    # Second-pass: merge single-wave entries from different waves that share
    # the same (normalized question wording) AND (option-codes signature).
    # Catches cases where Yale renamed a variable across waves but kept the
    # text identical — e.g. `Need_for_cognition_1` -> `cognition_need_1`.
    # Strict-after-normalization, not fuzzy: pooling responses to two slightly
    # different questions silently is a worse failure than missing a pool.
    wording_buckets: dict[tuple, list[tuple[str, str, str]]] = {}
    for canon, by_wave in list(canon_map.items()):
        if len(by_wave) != 1:
            continue
        wave, orig = next(iter(by_wave.items()))
        e = per_wave[wave]["cb"]["columns"][orig]
        # Skip MaxDiff and other non-pool-able types.
        if e.get("type") not in (None, "categorical", "numeric"):
            continue
        nq = _normalize_question(e.get("question"))
        if not nq:
            continue
        codes = _options_codes(e.get("options"))
        key = (nq, codes, e.get("type", "categorical"))
        wording_buckets.setdefault(key, []).append((canon, wave, orig))

    merged_count = 0
    for key, entries in wording_buckets.items():
        if len(entries) < 2:
            continue
        waves_seen = {w for _, w, _ in entries}
        if len(waves_seen) < 2:
            continue  # all from the same wave (unlikely but safe)
        # Promote into a single canon entry. Pick the F25 > S25 > F24 priority
        # canon as the merged key (or first encountered if none of those waves
        # qualifies); pop the others.
        priority = ("F25", "S25", "F24")
        entries_sorted = sorted(
            entries,
            key=lambda t: priority.index(t[1]) if t[1] in priority else 99,
        )
        keep_canon, _, _ = entries_sorted[0]
        merged_by_wave: dict[str, str] = {}
        for canon, wave, orig in entries:
            merged_by_wave[wave] = orig
            if canon != keep_canon:
                canon_map.pop(canon, None)
        canon_map[keep_canon] = merged_by_wave
        merged_count += 1
    if merged_count:
        print(f"  wording-merged {merged_count} cross-wave column groups "
              f"(different variable names, identical question text + codes)")

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
                "items": e.get("items"),  # MaxDiff items list (None for non-MaxDiff)
                "wave_to_orig": dict(by_wave),
                "present_waves": present_waves,
            }
            continue

        # MaxDiff present in any wave: union the item sets across the MaxDiff
        # waves by LABEL (codes get renumbered across waves — e.g. an electable
        # candidate is code 5 one wave, code 23 the next — so the candidate name
        # is the stable identity). Categorical-only waves of the same question
        # can't contribute item-level offers/picks, so they're left out of the
        # pooled MaxDiff (their respondents simply show null for it).
        types_seen = {e.get("type", "categorical") for _, e in entries}
        if "maxdiff" in types_seen:
            md_entries = [(w, e) for w, e in entries if e.get("type") == "maxdiff"]
            md_present = [w for w in waves if w in {w for w, _ in md_entries}]
            union, _, coverage = union_by_label(md_entries, "items")
            if union and len(md_present) >= 1:
                items = []
                item_src: dict[int, dict] = {}
                for u in union:
                    c = u["code"]
                    do_col = f"__md_{canon}__do_{c}"
                    pick_col = f"__md_{canon}__pick_{c}"
                    items.append({"code": c, "label": u["label"],
                                  "do_col": do_col, "pick_col": pick_col})
                    item_src[c] = u.get("_src", {})
                accepted[canon] = {
                    "label": md_entries[0][1]["label"],
                    "question": md_entries[0][1]["question"],
                    "type": "maxdiff",
                    "options": None,
                    "items": items,
                    "item_src": item_src,
                    "wave_to_orig": {w: by_wave[w] for w, _ in md_entries},
                    "present_waves": md_present,
                    "unioned": len({tuple(sorted(it["code"] for it in e.get("items", []))) for _, e in md_entries}) > 1,
                }
                continue
            skipped.append((canon, f"maxdiff union failed: {types_seen}"))
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
        # Diverging code-sets: same question, but options were added/removed
        # and/or codes were renumbered across waves. Union by LABEL so the
        # question still stacks (the YYP/Argument approach). Codes are remapped
        # per wave to fresh canonical codes during row materialization.
        union, value_remap, coverage = union_by_label(entries, "options")
        if union:
            accepted[canon] = {
                "label": entries[0][1]["label"],
                "question": entries[0][1]["question"],
                "type": "categorical",
                "options": [{"code": o["code"], "label": o["label"]} for o in union],
                "wave_to_orig": dict(by_wave),
                "present_waves": present_waves,
                "value_remap": value_remap,
                "option_coverage": coverage,
                "unioned": True,
            }
            continue
        skipped.append((canon, f"diverging code-sets {dict(zip([w for w,_ in entries], codes))}"))

    # Drop SINGLE-WAVE MaxDiff columns from stacked datasets: in a stack they'd
    # just be a [tag] column for one wave, and their per-item offer/pick aux
    # columns bloat the file (carried mostly-null across every wave's rows — all
    # MaxDiffs together hit 52 MB and crashed the browser). They remain fully
    # available in the single-wave views. MaxDiffs that pool across ≥2 waves
    # (e.g. electability, issue) are KEPT — that's the cross-wave value.
    drop = [c for c, i in accepted.items()
            if i.get("type") == "maxdiff" and len(i.get("present_waves", [])) < 2]
    for c in drop:
        accepted.pop(c)
    if drop:
        print(f"  dropped {len(drop)} single-wave MaxDiff columns from stack "
              f"(kept cross-wave ones)")

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

    # MaxDiff `_do_N` (and optional `_pick_N`) siblings: not user-facing (no
    # codebook entry) but carried in the data file so the frontend can compute
    # offers and (multi-task) wins per item. For cross-wave unioned MaxDiffs the
    # aux columns are canonical (`__md_<canon>__do_<c>`) and sourced per wave
    # from that wave's original item columns via `aux_source`.
    maxdiff_do_extras: list[str] = []
    # aux_plan[aux_col][wave] = how to fill that wave's value. Handles two source
    # styles uniformly: multi-round sources already store offer/pick *counts*;
    # single-round sources store a shown-slot indicator + the pick in the main
    # column, which we synthesize into counts (1 shown / 1 picked) here.
    aux_plan: dict[str, dict[str, tuple]] = {}
    for info in nondemog_ordered:
        if info["type"] != "maxdiff" or not info.get("items"):
            continue
        item_src = info.get("item_src")
        for it in info["items"]:
            for k in ("do_col", "pick_col"):
                col = it.get(k)
                if col and col not in output_columns and col not in maxdiff_do_extras:
                    maxdiff_do_extras.append(col)
            if item_src is None:
                continue
            for wv, srcs in item_src.get(it["code"], {}).items():
                multiround = srcs.get("pick_col") is not None
                main_col = info["wave_to_orig"].get(wv)
                aux_plan.setdefault(it["do_col"], {})[wv] = (
                    "do", srcs.get("do_col"), multiround)
                aux_plan.setdefault(it["pick_col"], {})[wv] = (
                    "pick", srcs.get("pick_col"), main_col, srcs.get("orig_code"), multiround)
    output_columns += maxdiff_do_extras

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
        label = f"{info['label']}{coverage}"
        if info.get("unioned"):
            label += "  ⊕"  # marks a cross-wave union (options/items pooled by label)
        entry: dict = {
            "label": label,
            "question": info["question"],
            "type": info["type"],
            "waves": present,
        }
        if info.get("options"):
            # For union columns, tag options that weren't offered in every
            # wave the question appeared in, so partial coverage is visible.
            opt_cov = info.get("option_coverage")
            if opt_cov and len(present) > 1:
                opts = []
                for o in info["options"]:
                    waves_for = opt_cov.get(o["code"], [])
                    tag = "" if set(waves_for) >= set(present) else f"  [{'+'.join(waves_for)}]"
                    opts.append({"code": o["code"], "label": f"{o['label']}{tag}"})
                entry["options"] = opts
            else:
                entry["options"] = info["options"]
        if info.get("items"):
            entry["items"] = info["items"]
        columns_out[info["output_key"]] = entry

    # Materialize stacked rows. We do this column-by-column per wave because
    # itertuples renames columns with spaces/punct (e.g. "Need for cognition_1"
    # -> "_0"), which would silently null them out.
    info_by_key = {info["output_key"]: info for info in nondemog_ordered}
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
            elif out_key in maxdiff_do_extras:
                # MaxDiff offer/pick aux column, sourced per wave via aux_plan
                # (synthesizing counts for single-round source MaxDiffs).
                spec = (aux_plan.get(out_key) or {}).get(wave)
                if spec is None:
                    wave_cols.append([None] * n_wave)
                elif spec[0] == "do":
                    _, src_do, multi = spec
                    if not src_do or src_do not in wdf.columns:
                        wave_cols.append([None] * n_wave)
                    elif multi:
                        wave_cols.append([to_compact_value(v) for v in wdf[src_do].tolist()])
                    else:  # single-round: non-null slot indicator -> shown once
                        wave_cols.append([1 if pd.notna(v) else None for v in wdf[src_do].tolist()])
                else:  # pick
                    _, src_pick, main_col, orig_code, multi = spec
                    if multi and src_pick and src_pick in wdf.columns:
                        wave_cols.append([to_compact_value(v) for v in wdf[src_pick].tolist()])
                    elif main_col and main_col in wdf.columns and orig_code is not None:
                        oc = float(orig_code)
                        wave_cols.append([
                            1 if (pd.notna(v) and float(v) == oc) else None
                            for v in wdf[main_col].tolist()
                        ])
                    else:
                        wave_cols.append([None] * n_wave)
            else:
                # Regular (categorical / numeric / maxdiff-main) column.
                info = info_by_key.get(out_key)
                src = info["wave_to_orig"].get(wave) if info else None
                if src is None or src not in wdf.columns:
                    wave_cols.append([None] * n_wave)
                else:
                    raw_vals = wdf[src].tolist()
                    remap = (info.get("value_remap") or {}).get(wave) if info else None
                    if remap:
                        # Union categorical: remap this wave's codes -> canonical.
                        col = []
                        for v in raw_vals:
                            cv = to_compact_value(v)
                            col.append(remap.get(cv, remap.get(v, None) if cv is not None else None))
                        wave_cols.append(col)
                    else:
                        wave_cols.append([to_compact_value(v) for v in raw_vals])

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
        choices=["F24", "S25", "F25", "S26", "stacked_all", "stacked_2026", "all"],
        default="all",
    )
    args = parser.parse_args()

    if args.wave == "all":
        targets = ["F24", "S25", "F25", "S26", "stacked_all", "stacked_2026"]
    else:
        targets = [args.wave]

    for t in targets:
        if t in {"F24", "S25", "F25", "S26"}:
            preprocess_wave(t)
        elif t == "stacked_all":
            build_stacked("stacked_all", "All waves (stacked)", ["F24", "S25", "F25", "S26"])
        elif t == "stacked_2026":
            build_stacked("stacked_2026", "2026 cycle (S25 + F25 + S26)", ["S25", "F25", "S26"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
