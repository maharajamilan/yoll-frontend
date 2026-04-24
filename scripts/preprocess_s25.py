"""
Preprocess the Yale Youth Poll Spring 2025 Qualtrics export into a compact
format the Next.js frontend can consume directly.

Inputs (from the S25 Dataverse replication package):
  - yyp2025_official_values.csv    (numeric response codes)
  - yyp2025_official_labels.csv    (text response labels)

Outputs (written to public/data/):
  - codebook_s25.json   { columns: { name: { question, options, type, ... } } }
  - data_s25.json       { wave, n, columns, rows, weights }

Weights: S25 replication package does NOT ship a weight column. For now we emit
a uniform weight of 1.0 for every respondent. When the S25 weighting pipeline
has been run, re-run this script and pass --weights-csv to pick up real weights.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPO_DIR = Path("/Users/milansingh/Downloads/yyp s25 repo")
OUTPUT_DIR = REPO_ROOT / "public" / "data"

# Columns we never expose (PII, admin, free-text, Prolific metadata).
DROP_EXACT = {
    "StartDate", "EndDate", "Status", "IPAddress", "Progress",
    "Duration (in seconds)", "Finished", "RecordedDate", "ResponseId",
    "RecipientLastName", "RecipientFirstName", "RecipientEmail",
    "ExternalReference", "LocationLatitude", "LocationLongitude",
    "DistributionChannel", "UserLanguage", "Consent_Age", "Consent",
    "RV_Screen", "PROLIFIC_PID", "STUDY_ID", "SESSION_ID", "comments",
}
DROP_SUFFIXES = ("_TEXT",)

# A column is "categorical" if it has a small set of repeated text labels and
# the values file has small integer codes. Use this threshold to classify.
CATEGORICAL_MAX_OPTIONS = 30


def is_dropped(col: str) -> bool:
    if col in DROP_EXACT:
        return True
    if any(col.endswith(s) for s in DROP_SUFFIXES):
        return True
    return False


def load_qualtrics(path: Path) -> tuple[pd.DataFrame, list[str]]:
    """Read a Qualtrics CSV export.

    Row 0 is variable name, row 1 is question text, row 2 is JSON metadata,
    rows 3+ are data. We return (data_df, question_texts) aligned to columns.
    """
    header = pd.read_csv(path, nrows=2)
    question_texts = header.iloc[0].astype(str).tolist()
    df = pd.read_csv(path, skiprows=[1, 2], low_memory=False)
    return df, question_texts


def build_codebook(
    values: pd.DataFrame,
    labels: pd.DataFrame,
    question_texts: dict[str, str],
) -> dict:
    columns: dict[str, dict] = {}
    for col in values.columns:
        if is_dropped(col):
            continue
        v = values[col]
        l = labels[col] if col in labels.columns else None
        non_null = v.dropna()
        if len(non_null) == 0:
            continue

        # Decide categorical vs numeric. The S25 survey is almost entirely
        # categorical; even Age is pre-bucketed.
        unique_vals = non_null.unique()
        if l is not None and len(unique_vals) <= CATEGORICAL_MAX_OPTIONS:
            # Build {code -> label} by matching rows
            code_label: dict[float, str] = {}
            paired = pd.DataFrame({"v": v, "l": l}).dropna()
            for code, group in paired.groupby("v"):
                lab = group["l"].astype(str).mode()
                code_label[float(code)] = lab.iloc[0] if len(lab) else str(code)
            options = [
                {"code": int(c) if float(c).is_integer() else c, "label": code_label[c]}
                for c in sorted(code_label.keys())
            ]
            columns[col] = {
                "label": col,
                "question": question_texts.get(col, col),
                "type": "categorical",
                "options": options,
                "waves": ["S25"],
            }
        else:
            columns[col] = {
                "label": col,
                "question": question_texts.get(col, col),
                "type": "numeric",
                "waves": ["S25"],
            }
    return {
        "waves": {
            "S25": {
                "label": "Spring 2025",
                "n": int(len(values)),
                "note": "Placeholder weights (1.0). Re-run with real S25 weights once pipeline has been run.",
            }
        },
        "columns": columns,
    }


def build_data_payload(
    values: pd.DataFrame,
    keep_columns: list[str],
    weights: pd.Series,
) -> dict:
    kept = values[keep_columns]
    rows: list[list] = []
    for _, row in kept.iterrows():
        out: list = []
        for v in row:
            if pd.isna(v):
                out.append(None)
            elif isinstance(v, float) and v.is_integer():
                out.append(int(v))
            else:
                out.append(v)
        rows.append(out)
    return {
        "wave": "S25",
        "n": int(len(values)),
        "columns": keep_columns,
        "rows": rows,
        "weights": [float(w) for w in weights.tolist()],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=DEFAULT_REPO_DIR,
        help="Path to the yyp s25 repo directory",
    )
    parser.add_argument(
        "--weights-csv",
        type=Path,
        default=None,
        help="Optional CSV with columns [ResponseId, weight] to join onto data",
    )
    args = parser.parse_args()

    values_path = args.repo_dir / "yyp2025_official_values.csv"
    labels_path = args.repo_dir / "yyp2025_official_labels.csv"
    for p in (values_path, labels_path):
        if not p.exists():
            print(f"ERROR: missing {p}", file=sys.stderr)
            return 1

    print(f"Loading {values_path.name}...")
    values, qtext_list = load_qualtrics(values_path)
    print(f"Loading {labels_path.name}...")
    labels, _ = load_qualtrics(labels_path)

    question_texts = dict(zip(values.columns, qtext_list))

    codebook = build_codebook(values, labels, question_texts)
    keep_columns = list(codebook["columns"].keys())

    if args.weights_csv and args.weights_csv.exists():
        w_df = pd.read_csv(args.weights_csv)
        if "ResponseId" not in w_df.columns or "weight" not in w_df.columns:
            print("ERROR: --weights-csv must have ResponseId,weight cols", file=sys.stderr)
            return 1
        merged = values[["ResponseId"]].merge(w_df, on="ResponseId", how="left")
        weights = merged["weight"].fillna(1.0)
        codebook["waves"]["S25"]["note"] = f"Weighted via {args.weights_csv.name}"
    else:
        weights = pd.Series([1.0] * len(values))

    data_payload = build_data_payload(values, keep_columns, weights)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cb_path = OUTPUT_DIR / "codebook_s25.json"
    data_path = OUTPUT_DIR / "data_s25.json"
    cb_path.write_text(json.dumps(codebook, indent=2))
    data_path.write_text(json.dumps(data_payload, separators=(",", ":")))

    print(f"Wrote {cb_path} ({cb_path.stat().st_size/1024:.1f} KB)")
    print(f"Wrote {data_path} ({data_path.stat().st_size/1024:.1f} KB)")
    print(f"Columns exposed: {len(keep_columns)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
