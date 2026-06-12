"""
Apply the Yale Youth Poll Spring 2025 (S25) weighting pipeline to any wave's
harmonized demographic data. The pipeline is deliberately transcribed from the
official S25 notebook — same raking variables, same targets, same seeds, same
IPF settings, same weight trimming. Per the user's decision, all waves are
weighted to identical S25 targets so results are directly comparable.

Usage:
    python scripts/rake_weights.py --wave F24
    python scripts/rake_weights.py --wave all

Input:  data-raw/harmonized/harmonized_<wave>.csv  (from crosswalk.py)
Output: data-raw/weights/weights_<wave>.csv        (case_id, weight)

The harmonized file must have columns:
    case_id, Age, Race, Education, Gender, Party ID, PID Lean

Notes:
  * Party ID is collapsed to 5 categories in the same way S25 does it:
      1=Strong Dem, 2=Strong Rep, 3=Lean Dem, 4=Lean Rep, 5=Pure Ind
    (this overwrites the 3-cat Party ID column before raking).
  * Seeds are applied to Black-Dem, Black-Rep, Hispanic-Dem, Hispanic-Rep
    cells as in the official pipeline.
  * Targets for Education sum to 1.10 and Party ID to 0.90 — these are
    verbatim S25 values, not a bug we fixed, because the user explicitly
    chose Approach A (apply S25 targets unchanged to every wave).
  * Rows with any NaN in the raking variables are dropped BEFORE raking
    and get weight 1.0 written back. This matches how S25 handled them
    (they never entered the rake; fallback to unit weight).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
HARMONIZED_DIR = REPO_ROOT / "data-raw" / "harmonized"
WEIGHTS_DIR = REPO_ROOT / "data-raw" / "weights"

# S26 is weighted with Yale's OWN official Spring-2026 (138b) procedure rather
# than the S25-replication rake used for F24/S25/F25. Per the project decision
# to "refresh targets for S26", we reproduce the official pipeline faithfully so
# our S26 matches the published YYP S26 toplines. It reads the RAW S26 data
# (its own demographic coding), not the canonical harmonized CSV.
S26_DIR = REPO_ROOT / "data-raw" / "source" / "S26"
S26_RAW = S26_DIR / "2025-138b_client.csv"

# S25 pipeline constants, copied verbatim from yyp2025_weighting_pipeline_official.ipynb
RAKE_VARS = ["Age", "Race", "Education", "Gender", "Party ID"]
TARGETS = {
    "Age":       {"1": 0.07, "2": 0.05, "3": 0.23, "4": 0.39, "5": 0.26},
    "Race":      {"1": 0.50, "2": 0.18, "3": 0.24, "4": 0.04, "5": 0.04},
    "Education": {"1": 0.35, "2": 0.105, "3": 0.185, "4": 0.11, "5": 0.175, "6": 0.175, "7": 0.0},
    "Gender":    {"1": 0.50, "2": 0.49, "3": 0.01},
    # 5-category Party ID: 1=Strong Dem, 2=Strong Rep, 3=Lean Dem, 4=Lean Rep, 5=Pure Ind
    "Party ID":  {"1": 0.18, "2": 0.27, "3": 0.15, "4": 0.15, "5": 0.15},
}
RAKE_MAX_ITER = 50
RAKE_TOLERANCE = 1e-2
TRIM_RATIO = 0.3  # Weights clipped to [TRIM_RATIO * mean, mean / TRIM_RATIO]


def collapse_party_id_to_5cat(party_id: pd.Series, pid_lean: pd.Series) -> pd.Series:
    """Replicate the S25 notebook's PID Lean recode and Party ID overwrite.

    Input codes (canonical S25 schema):
        Party ID: 1=Dem, 2=Rep, 3=Ind
        PID Lean: 1=Dem, 2=Rep, 3=Neither, NaN=(skipped, treat as pure ind)

    Output 5-category Party ID:
        1=Strong Dem (was Party ID==1)
        2=Strong Rep (was Party ID==2)
        3=Lean Dem   (Party ID==3 & PID Lean==1)
        4=Lean Rep   (Party ID==3 & PID Lean==2)
        5=Pure Ind   (Party ID==3 & PID Lean in {3, NaN})
    """
    out = pd.Series(pd.NA, index=party_id.index, dtype="object")
    out[party_id == 1] = "1"
    out[party_id == 2] = "2"
    ind_mask = party_id == 3
    out[ind_mask & (pid_lean == 1)] = "3"
    out[ind_mask & (pid_lean == 2)] = "4"
    out[ind_mask & (pid_lean == 3)] = "5"
    # Independents with NaN lean: S25 fills lean with '3' (Neither) -> Pure Ind
    out[ind_mask & pid_lean.isna()] = "5"
    return out


def adjust_weights(
    df: pd.DataFrame,
    variable: str,
    target_distribution: dict,
    weight_col: str = "weight",
) -> pd.DataFrame:
    """One-shot rim adjustment for a single variable (from S25 notebook)."""
    weighted_counts = df.groupby(variable)[weight_col].sum()
    total_weighted = weighted_counts.sum()
    sample_props = weighted_counts / total_weighted

    adjustment_factors = {}
    for category, target_prop in target_distribution.items():
        if category in sample_props.index:
            sp = sample_props.loc[category]
            adjustment_factors[category] = (target_prop / sp) if sp != 0 else 1.0
        else:
            adjustment_factors[category] = 1.0

    df = df.copy()
    df[weight_col] *= df[variable].map(adjustment_factors).fillna(1)
    return df


def rake_weights(
    df: pd.DataFrame,
    variables: list[str],
    targets: dict,
    weight_col: str = "weight",
    max_iterations: int = RAKE_MAX_ITER,
    tolerance: float = RAKE_TOLERANCE,
) -> pd.DataFrame:
    for iteration in range(max_iterations):
        max_change = 0.0
        for var in variables:
            target = targets[var]
            before = df[weight_col].copy()
            df = adjust_weights(df, var, target, weight_col)
            change = (df[weight_col] - before).abs().max()
            max_change = max(max_change, change)
        if max_change < tolerance:
            print(f"  Converged after {iteration + 1} iterations (max change={max_change:.4f}).")
            return df
    print(f"  Reached max iterations ({max_iterations}) without full convergence.")
    return df


def apply_seed_weights(df: pd.DataFrame) -> pd.DataFrame:
    """S25 seeds (pre-rake) for Black/Hispanic × Dem/Rep cells."""
    df = df.copy()
    df["weight"] = 1.0
    # Race==2 Black, Race==3 Hispanic, Party ID 5-cat: 1=Strong Dem, 2=Strong Rep
    df.loc[(df["Race"] == "2") & (df["Party ID"] == "1"), "weight"] = 1.8
    df.loc[(df["Race"] == "2") & (df["Party ID"] == "2"), "weight"] = 0.2
    df.loc[(df["Race"] == "3") & (df["Party ID"] == "1"), "weight"] = 0.5
    df.loc[(df["Race"] == "3") & (df["Party ID"] == "2"), "weight"] = 1.5
    return df


def weight_one_wave(harmonized_csv: Path, wave: str) -> pd.DataFrame:
    print(f"\n===== {wave} =====")
    h = pd.read_csv(harmonized_csv)
    print(f"  Loaded {len(h)} rows from {harmonized_csv.name}")

    # Collapse Party ID to 5-cat
    h["Party ID 5cat"] = collapse_party_id_to_5cat(h["Party ID"], h["PID Lean"])

    # Working copy with variables as strings (match S25 typing)
    work = h.copy()
    for var in ["Age", "Race", "Education", "Gender"]:
        work[var] = work[var].apply(
            lambda v: str(int(v)) if pd.notna(v) else np.nan
        )
    work["Party ID"] = work["Party ID 5cat"]

    # Drop anyone missing a raking variable
    before = len(work)
    complete = work.dropna(subset=RAKE_VARS).copy()
    dropped = before - len(complete)
    if dropped:
        print(f"  Dropped {dropped} rows with missing rake vars (assigned weight 1.0)")

    # Seed + rake + trim
    complete = apply_seed_weights(complete)
    complete = rake_weights(complete, RAKE_VARS, TARGETS)

    mean_w = complete["weight"].mean()
    lower = max(TRIM_RATIO * mean_w, 0.0)
    upper = mean_w / TRIM_RATIO
    complete["weight"] = complete["weight"].clip(lower=lower, upper=upper)
    complete["weight"] *= len(complete) / complete["weight"].sum()

    # Merge weights back to the full harmonized sample; rows that were dropped
    # get weight 1.0 so the data file doesn't have holes.
    out = h[["case_id"]].copy()
    out = out.merge(
        complete[["case_id", "weight"]], on="case_id", how="left"
    )
    out["weight"] = out["weight"].fillna(1.0)
    out["wave"] = wave

    print(f"  Mean={out['weight'].mean():.3f}  Std={out['weight'].std():.3f}")
    print(f"  Min={out['weight'].min():.3f}  Max={out['weight'].max():.3f}")
    print(f"  N={len(out)}  sum(weights)={out['weight'].sum():.1f}")

    # Post-raking distribution check
    if len(complete):
        print("  Post-rake weighted proportions:")
        for var in RAKE_VARS:
            wp = complete.groupby(var)["weight"].sum()
            wp = (wp / wp.sum()).round(3).to_dict()
            tgt = TARGETS[var]
            tgt_norm = {k: round(v / sum(tgt.values()), 3) for k, v in tgt.items()}
            print(f"    {var}: got={wp}")
            print(f"             target(norm)={tgt_norm}")
    return out


# ------------------------------------------------------------------
# S26 official weighting (Yale Spring-2026 / 138b pipeline, reproduced)
# ------------------------------------------------------------------

# CPS Nov 2024 Voting Supplement age×gender targets (registered voters,
# PWSSWGT-weighted), hardcoded from the official 138b pipeline as its offline
# fallback. Age codes use the 6-bucket 138b scheme (NOT the 5-bucket S25 one):
#   1=18-22  2=23-29  3=30-34  4=35-44  5=45-64  6=65+
S26_AGE_GENDER_TARGETS = {
    "1_Male": 0.02234, "1_Female": 0.017012,
    "2_Male": 0.037895, "2_Female": 0.038445,
    "3_Male": 0.020044, "3_Female": 0.020184,
    "4_Male": 0.096043, "4_Female": 0.101207,
    "5_Male": 0.164943, "5_Female": 0.168198,
    "6_Male": 0.143542, "6_Female": 0.159548,
}
S26_TARGETS = {
    # Race (CPS): codes 1=White 2=Black 3=Hispanic 4=Asian 5=Native 7=Two+
    "ces_race": {"1": 0.703, "2": 0.121, "3": 0.093, "4": 0.036, "5": 0.007, "7": 0.030},
    # Education (CPS 2021 adults 25+): 1=<HS 2=HS 3=SomeColl 4=AA 5=BA 6=Grad
    "education": {"1": 0.089, "2": 0.279, "3": 0.149, "4": 0.105, "5": 0.235, "6": 0.143},
    # Party post-leaner-fold (Pew/Gallup 2024): 1=Rep 2=Dem 3=Ind 4=Other
    "anes_party_id": {"1": 0.480, "2": 0.450, "3": 0.050, "4": 0.020},
    "age_gender": S26_AGE_GENDER_TARGETS,
}
S26_SEEDS = {
    ("ces_race", 2, "anes_party_id", 1): 0.2,   # Black Republicans
    ("ces_race", 2, "anes_party_id", 2): 1.8,   # Black Democrats
    ("ces_race", 3, "anes_party_id", 1): 1.5,   # Hispanic Republicans
    ("ces_race", 3, "anes_party_id", 2): 0.5,   # Hispanic Democrats
}
S26_AGE_BINS = [18, 22, 29, 34, 44, 64, np.inf]
S26_AGE_CODES = [1, 2, 3, 4, 5, 6]
# Race priority pick over the 8 ces_race binaries (first match wins), then 6->4.
S26_RACE_PRIORITY = ["ces_race_2", "ces_race_4", "ces_race_5", "ces_race_3",
                     "ces_race_1", "ces_race_6", "ces_race_7", "ces_race_8"]


def weight_s26_official(raw_csv: Path) -> pd.DataFrame:
    """Reproduce Yale's official Spring-2026 (138b) weighting on the raw S26
    data. Returns a (case_id, weight, wave) DataFrame for all eligible rows."""
    print("\n===== S26 (official 138b pipeline) =====")
    df = pd.read_csv(raw_csv, low_memory=False)
    if "a2024_recalled_vote" in df.columns:
        df = df.rename(columns={"a2024_recalled_vote": "2024_recalled_vote"})
    # Eligibility filters (138b coding: consent_q==2 means Yes)
    df = df[(df["over_18"] == 1) & (df["consent_q"] == 2) & (df["us_voter"] == 1)].copy()
    print(f"  Eligible N: {len(df)}")

    # ces_race: priority pick over the 8 binaries, then Middle Eastern (6) -> Asian (4)
    def pick_race(row):
        for col in S26_RACE_PRIORITY:
            if row.get(col) == 1:
                return int(col.rsplit("_", 1)[-1])
        return pd.NA
    df["ces_race"] = df.apply(pick_race, axis=1).replace(6, 4)

    # Age -> 6-bucket codes
    df["age"] = pd.cut(
        pd.to_numeric(df["age"], errors="coerce"),
        bins=S26_AGE_BINS, labels=S26_AGE_CODES, right=True, include_lowest=True,
    ).astype("Int64")

    # Fold party-ID leaners (anes_party_id 1=Rep 2=Dem 3=Ind 4=Other;
    # pid_leaners 1=Rep 2=Dem). Non-partisans (3/4) who lean recode to 1/2.
    leaner_to_pid = {1: 1, 2: 2}
    nonpart = df["anes_party_id"].isin([3, 4])
    mapped = df.loc[nonpart, "pid_leaners"].map(leaner_to_pid)
    df.loc[nonpart & mapped.notna(), "anes_party_id"] = mapped[mapped.notna()]
    pid_label = {1: "Republican", 2: "Democrat", 3: "Independent", 4: "Other"}
    df["anes_party_id_labels"] = df["anes_party_id"].map(pid_label)
    vote_label = {1: "Kamala Harris", 2: "Donald Trump", 3: "Other",
                  4: "Did not vote", 5: "Was not old enough to vote"}
    df["2024_recalled_vote_labels"] = df["2024_recalled_vote"].map(vote_label)

    # Seed weights (Black/Hispanic × R/D), keyed on post-fold party
    df["weight"] = 1.0
    for (rc, rv, pc, pv), mult in S26_SEEDS.items():
        mask = (pd.to_numeric(df[rc], errors="coerce") == float(rv)) & (
            pd.to_numeric(df[pc], errors="coerce") == float(pv))
        df.loc[mask, "weight"] *= mult

    # Age×gender interaction column
    gname = {1: "Male", 2: "Female", 3: "Other"}
    df["age_gender"] = df["age"].astype(str) + "_" + df["gender"].map(gname).fillna("Other")

    # IPF rake (race, education, party, age_gender) then normalize to N
    rake_order = ["ces_race", "education", "anes_party_id", "age_gender"]
    converged = False
    for iteration in range(50):
        max_diff = 0.0
        for var in rake_order:
            for cat, tp in S26_TARGETS[var].items():
                mask = df[var].astype(str) == str(cat)
                cur = df.loc[mask, "weight"].sum() / df["weight"].sum()
                if cur > 0:
                    df.loc[mask, "weight"] *= tp / cur
                    max_diff = max(max_diff, abs(cur - tp))
        if max_diff < 1e-6:
            converged = True
            print(f"  IPF converged after {iteration + 1} iterations")
            break
    if not converged:
        print("  IPF reached max iterations (50)")
    n = len(df)
    df["weight"] *= n / df["weight"].sum()

    # Post-IPF age×party calibration + "Other" downweights
    df["weight_base"] = df["weight"]
    df["adj_factor"] = 1.0
    for age_codes, dem_adj, gop_adj in [
        ([1, 2], 0.90, 1.10),   # 18-29
        ([3], 1.05, 0.95),      # 30-34
        ([4], 1.15, 0.85),      # 35-44
        ([5], 0.90, 1.10),      # 45-64
        ([6], 1.05, 0.95),      # 65+
    ]:
        mask = df["age"].isin(age_codes)
        df.loc[mask & (df["anes_party_id_labels"] == "Democrat"), "adj_factor"] *= dem_adj
        df.loc[mask & (df["anes_party_id_labels"] == "Republican"), "adj_factor"] *= gop_adj
    df.loc[df["2024_recalled_vote_labels"] == "Other", "adj_factor"] *= 0.75
    df.loc[df["anes_party_id_labels"] == "Other", "adj_factor"] *= 0.50
    df["weight"] = df["weight_base"] * df["adj_factor"]

    # Trim at 5× mean, renormalize to N
    cap = 5.0 * df["weight"].mean()
    n_trim = int((df["weight"] > cap).sum())
    if n_trim:
        df.loc[df["weight"] > cap, "weight"] = cap
        print(f"  Trimmed {n_trim} weights above {cap:.2f}")
    df["weight"] *= n / df["weight"].sum()

    out = df[["case_id"]].copy()
    out["weight"] = df["weight"].values
    out["wave"] = "S26"
    print(f"  Mean={out['weight'].mean():.3f}  Std={out['weight'].std():.3f}  "
          f"Min={out['weight'].min():.3f}  Max={out['weight'].max():.3f}")
    print(f"  N={len(out)}  sum(weights)={out['weight'].sum():.1f}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--wave", choices=["F24", "S25", "F25", "S26", "all"], default="all"
    )
    args = parser.parse_args()

    WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)

    waves = ["F24", "S25", "F25", "S26"] if args.wave == "all" else [args.wave]
    for w in waves:
        if w == "S26":
            if not S26_RAW.exists():
                print(f"ERROR: {S26_RAW} not found", file=sys.stderr)
                return 1
            result = weight_s26_official(S26_RAW)
        else:
            src = HARMONIZED_DIR / f"harmonized_{w.lower()}.csv"
            if not src.exists():
                print(f"ERROR: {src} not found — run scripts/crosswalk.py first", file=sys.stderr)
                return 1
            result = weight_one_wave(src, w)
        out_path = WEIGHTS_DIR / f"weights_{w.lower()}.csv"
        result.to_csv(out_path, index=False)
        print(f"  Wrote {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
