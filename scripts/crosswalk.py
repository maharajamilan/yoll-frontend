"""
Demographic crosswalk: harmonize F24, S25, and F25 raw demographics into the
canonical S25 coding scheme so the same weighting pipeline can be applied to
every wave.

Canonical S25 schema (all integer codes unless noted):

    Age         1=18-21   2=22-29   3=30-44   4=45-64   5=65+
    Race        1=White   2=Black   3=Hispanic  4=Asian   5=Other
    Education   1=<HS  2=HS  3=Some college  4=AA  5=BA  6=Grad  7=PNTS
    Gender      1=Man   2=Woman   3=Other
    Party ID    1=Democrat   2=Republican   3=Independent
    PID Lean    1=Dem   2=Rep   3=Neither              (only for Independents)
    2024 Vote   1=Harris  2=Trump  3=Other  4=Did not vote  5=Not old enough

In addition we emit a derived 5-category Party ID used for raking (matches the
S25 notebook's `PID Lean` recode):

    pid5  1=Strong Dem  2=Lean Dem  3=Pure Ind  4=Lean Rep  5=Strong Rep

Output: writes `data/harmonized_<wave>.csv` with columns
    case_id, wave, Age, Race, Education, Gender, Party ID, PID Lean,
    2024 Vote, pid5

F24 note: F24 does not ship a weight column; we also filter to rv_screen==1
(registered voters) to match how the S25 pool is constructed.
F25 note: F25's `anes_party_id` uses SWAPPED codes (1=Rep, 2=Dem, 3=Ind, 4=Other);
F25 `ces_race` is a single-select (1-8) which we fold down to S25's 5 categories.
F25 raw age is available in `age_actualnumber` so we re-bucket to the S25 scheme.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = REPO_ROOT / "data-raw" / "harmonized"

F24_DIR = Path("/Users/milansingh/Downloads/yyp f24 repo")
S25_DIR = Path("/Users/milansingh/Downloads/yyp s25 repo")
F25_DIR = Path("/Users/milansingh/Downloads/yyp f25 repo")

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def age_to_s25_bucket(years: float) -> int | float:
    if pd.isna(years):
        return np.nan
    y = int(years)
    if y < 18:
        return np.nan
    if y <= 21:
        return 1
    if y <= 29:
        return 2
    if y <= 44:
        return 3
    if y <= 64:
        return 4
    return 5


def derive_pid5(party_id: pd.Series, pid_lean: pd.Series) -> pd.Series:
    """S25 raking uses 5-cat PID: strong Dem/lean Dem/pure Ind/lean Rep/strong Rep.

    Party ID codes: 1=Dem, 2=Rep, 3=Ind.
    PID Lean codes: 1=Dem, 2=Rep, 3=Neither (present only when Party ID == 3).
    """
    out = pd.Series(np.nan, index=party_id.index, dtype="float64")
    out[party_id == 1] = 1  # Strong Dem
    out[party_id == 2] = 5  # Strong Rep
    ind_mask = party_id == 3
    out[ind_mask & (pid_lean == 1)] = 2  # Lean Dem
    out[ind_mask & (pid_lean == 2)] = 4  # Lean Rep
    out[ind_mask & (pid_lean == 3)] = 3  # Pure Ind
    # Independents with no lean response: treat as Pure Ind so they still get
    # a weight bucket. (S25 notebook fills NaN lean with '3'=pure ind, same
    # outcome.)
    out[ind_mask & out.isna()] = 3
    return out


# ------------------------------------------------------------------
# Per-wave harmonizers
# ------------------------------------------------------------------


def harmonize_f24() -> pd.DataFrame:
    """F24 uses the same numeric codes as S25 for every demographic we need.
    The only transform is renaming + filtering to registered voters (rv_screen==1)
    since that's the YYP analysis universe."""
    src = F24_DIR / "data_yyp_F24.csv"
    df = pd.read_csv(src, low_memory=False)
    total = len(df)
    df = df[df["rv_screen"] == 1].copy()
    print(f"F24: {total} -> {len(df)} after rv_screen==1 filter")

    out = pd.DataFrame(
        {
            "case_id": df["response_id"],
            "wave": "F24",
            "Age": df["age"],
            "Race": df["race"],
            "Education": df["education"],
            "Gender": df["gender"],
            "Party ID": df["party_id"],
            "PID Lean": df["pid_lean"],
            "2024 Vote": df["x2024_horserace"],
        }
    )
    # F24 x2024_horserace codes: 1=Harris, 2=Trump, 3=Other Dem, 4=Other Rep,
    # 5=Other, 6=Did not vote / won't vote, 7=not old enough, 8=other
    # We only need S25's 5-cat 2024 Vote (post-election recall): Harris, Trump,
    # Other, Did not vote, Not old enough. Fold F24's pre-election codes:
    vote_map = {1: 1, 2: 2, 3: 3, 4: 3, 5: 3, 6: 4, 7: 5, 8: 3}
    out["2024 Vote"] = out["2024 Vote"].map(vote_map)
    out["pid5"] = derive_pid5(out["Party ID"], out["PID Lean"])
    return out


def harmonize_s25() -> pd.DataFrame:
    """S25 is already canonical; this just extracts the columns we need."""
    src = S25_DIR / "yyp2025_official_values.csv"
    df = pd.read_csv(src, skiprows=[1, 2], low_memory=False)
    print(f"S25: {len(df)} rows")
    out = pd.DataFrame(
        {
            "case_id": df["ResponseId"],
            "wave": "S25",
            "Age": df["Age"],
            "Race": df["Race"],
            "Education": df["Education"],
            "Gender": df["Gender"],
            "Party ID": df["Party ID"],
            "PID Lean": df["PID Lean"],
            "2024 Vote": df["2024 vote"],
        }
    )
    out["pid5"] = derive_pid5(out["Party ID"], out["PID Lean"])
    return out


def harmonize_f25() -> pd.DataFrame:
    """F25 is trickier — party codes are swapped, race is single-select over 8
    categories, and age ships as both a 6-bucket scheme and a raw integer. We
    re-bucket the raw age to S25's 5-bucket scheme."""
    src = F25_DIR / "yypfall25dat_withweights.csv"
    df = pd.read_csv(src, low_memory=False)
    print(f"F25: {len(df)} rows")

    # Age: prefer raw numeric, fall back to the 6-bucket column
    age = df["age_actualnumber"].apply(age_to_s25_bucket)

    # Race: F25 ces_race is single-select 1-8
    # 1=White, 2=Black, 3=Hispanic, 4=Asian, 5=Native American,
    # 6=Asian (Pacific Islander collapsed in F25 labels), 7=Two or more, 8=Other
    race_map = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 4, 7: 5, 8: 5}
    race = df["ces_race"].map(race_map)

    # Party ID: F25 anes_party_id codes are SWAPPED vs S25
    # F25: 1=Rep, 2=Dem, 3=Ind, 4=Other -> S25: 1=Dem, 2=Rep, 3=Ind
    # Collapse Other into Independent.
    party_map = {1: 2, 2: 1, 3: 3, 4: 3}
    party_id = df["anes_party_id"].map(party_map)

    # PID lean: F25 pid_leaners codes match S25 exactly
    pid_lean = df["pid_leaners"]

    # Education: F25 has 1-6 (no "Prefer not to say" option); matches S25 codes
    education = df["education"]

    # Gender: matches S25 exactly
    gender = df["gender"]

    # 2024 vote: matches S25 exactly
    vote = df["2024_recalled_vote"]

    out = pd.DataFrame(
        {
            "case_id": df["case_id"],
            "wave": "F25",
            "Age": age,
            "Race": race,
            "Education": education,
            "Gender": gender,
            "Party ID": party_id,
            "PID Lean": pid_lean,
            "2024 Vote": vote,
        }
    )
    out["pid5"] = derive_pid5(out["Party ID"], out["PID Lean"])
    return out


# ------------------------------------------------------------------
# Sanity reporting
# ------------------------------------------------------------------


def report(df: pd.DataFrame, wave: str) -> None:
    print(f"\n===== {wave} harmonized N={len(df)} =====")
    for col in ["Age", "Race", "Education", "Gender", "Party ID", "PID Lean", "2024 Vote", "pid5"]:
        dist = df[col].value_counts(dropna=False).sort_index()
        pct = (dist / len(df) * 100).round(1)
        print(f"\n  {col}:")
        for k, n in dist.items():
            print(f"    {k}: {n}  ({pct[k]}%)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wave", choices=["F24", "S25", "F25", "all"], default="all")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    waves = ["F24", "S25", "F25"] if args.wave == "all" else [args.wave]
    fns = {"F24": harmonize_f24, "S25": harmonize_s25, "F25": harmonize_f25}

    for w in waves:
        df = fns[w]()
        out_path = OUTPUT_DIR / f"harmonized_{w.lower()}.csv"
        df.to_csv(out_path, index=False)
        print(f"Wrote {out_path} ({len(df)} rows)")
        if args.verbose:
            report(df, w)

    return 0


if __name__ == "__main__":
    sys.exit(main())
