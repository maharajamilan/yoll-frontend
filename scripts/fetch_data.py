"""
Fetch the raw YYP replication files this pipeline needs straight from the
Yale Dataverse and store them in the repo (data-raw/source/<wave>/), so the
project no longer depends on files sitting in anyone's ~/Downloads.

Usage:
    python scripts/fetch_data.py                # fetch any missing files
    python scripts/fetch_data.py --force        # re-download everything
    python scripts/fetch_data.py --wave S26     # one wave
    python scripts/fetch_data.py --list         # list all YYP datasets on Dataverse
                                                 # (spot new waves to add to WAVES)

Adding a future wave: run `--list`, find the new dataset's DOI + filenames,
add an entry to WAVES below, and re-run. Then run crosswalk.py / rake_weights.py
/ preprocess.py as usual and commit the regenerated public/data JSON.

Files that Dataverse ingested into its tabular `.tab` format are downloaded in
their *original* format (the source CSV) via `?format=original`; native files
(already-CSV / XLSX) download directly. Only the files the pipeline actually
reads are fetched — not the notebooks / PDFs / instruments.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
import urllib.error
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = REPO_ROOT / "data-raw" / "source"
DATAVERSE = "https://dataverse.yale.edu"
COLLECTION = "YYP"

# Per wave: the Dataverse dataset DOI and the files the pipeline reads, mapped
# (dataverse filename -> local filename under data-raw/source/<wave>/).
WAVES: dict[str, dict] = {
    "F24": {
        "doi": "doi:10.60600/YU/GGSZBD",
        "files": {
            "data_yyp_F24.tab": "data_yyp_F24.csv",
            "qualtrics_id_mappings_to_columns_F24.tab": "qualtrics_id_mappings_to_columns_F24.csv",
        },
    },
    "S25": {
        "doi": "doi:10.60600/YU/NB31JO",
        "files": {
            "yyp2025_official_values.tab": "yyp2025_official_values.csv",
            "yyp2025_official_labels.tab": "yyp2025_official_labels.csv",
        },
    },
    "F25": {
        "doi": "doi:10.60600/YU/DUHYAX",
        "files": {
            "yypfall25dat_withweights.csv": "yypfall25dat_withweights.csv",
            "2025-138a_codebook.xlsx": "2025-138a_codebook.xlsx",
        },
    },
    "S26": {
        "doi": "doi:10.60600/YU/WXIIS6",
        "files": {
            "2025-138b_client.tab": "2025-138b_client.csv",
            "2025-138b_codebook.xlsx": "2025-138b_codebook.xlsx",
        },
    },
}


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def dataset_files(doi: str) -> dict[str, dict]:
    """Return {filename: dataFile-metadata} for a dataset's latest version."""
    url = f"{DATAVERSE}/api/datasets/:persistentId/?persistentId={doi}"
    data = _get_json(url)["data"]["latestVersion"]
    out = {}
    for f in data["files"]:
        df = f["dataFile"]
        out[df["filename"]] = df
    return out


def download_file(datafile: dict, dest: Path) -> None:
    """Download one datafile to dest. Tabular-ingested files (those with an
    originalFileName) are fetched in original format so we get the source CSV."""
    fid = datafile["id"]
    ingested = bool(datafile.get("originalFileName")) or (
        datafile.get("originalFileFormat") not in (None, "", "UNKNOWN")
        and str(datafile.get("filename", "")).endswith(".tab")
    )
    url = f"{DATAVERSE}/api/access/datafile/{fid}"
    if ingested:
        url += "?format=original"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=300) as resp, open(tmp, "wb") as fh:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
    tmp.replace(dest)


def fetch_wave(wave: str, force: bool) -> None:
    cfg = WAVES[wave]
    print(f"\n===== {wave}  ({cfg['doi']}) =====")
    files = dataset_files(cfg["doi"])
    for dv_name, local_name in cfg["files"].items():
        dest = SOURCE_DIR / wave / local_name
        if dest.exists() and not force:
            print(f"  ✓ {local_name} (have it; --force to refresh)")
            continue
        if dv_name not in files:
            print(f"  ! {dv_name} not found in dataset — available: {sorted(files)}",
                  file=sys.stderr)
            continue
        print(f"  ↓ {dv_name} -> {local_name}")
        download_file(files[dv_name], dest)
        print(f"    {dest.stat().st_size/1024/1024:.1f} MB")


def list_collection() -> None:
    url = f"{DATAVERSE}/api/dataverses/{COLLECTION}/contents"
    items = _get_json(url)["data"]
    print(f"Datasets in the {COLLECTION} Dataverse collection:")
    for it in items:
        doi = f"doi:{it['authority']}{it['separator']}{it['identifier']}"
        try:
            meta = _get_json(
                f"{DATAVERSE}/api/datasets/:persistentId/?persistentId={doi}"
            )["data"]["latestVersion"]["metadataBlocks"]["citation"]["fields"]
            title = next(f["value"] for f in meta if f["typeName"] == "title")
        except Exception:
            title = "(title unavailable)"
        mapped = next((w for w, c in WAVES.items() if c["doi"] == doi), None)
        tag = f"-> {mapped}" if mapped else "-> (not mapped; add to WAVES)"
        print(f"  {doi}  {tag}\n      {title}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wave", choices=list(WAVES) + ["all"], default="all")
    ap.add_argument("--force", action="store_true", help="re-download existing files")
    ap.add_argument("--list", action="store_true", help="list YYP datasets and exit")
    args = ap.parse_args()

    if args.list:
        list_collection()
        return 0

    waves = list(WAVES) if args.wave == "all" else [args.wave]
    for w in waves:
        try:
            fetch_wave(w, args.force)
        except urllib.error.HTTPError as e:
            print(f"  HTTP error for {w}: {e}", file=sys.stderr)
            return 1
    print(f"\nDone. Raw files in {SOURCE_DIR.relative_to(REPO_ROOT)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
