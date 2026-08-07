"""
combine_standardized.py - concatenates all standardized/*.csv files into
one raw combined table, before any dedup/entity resolution happens.

This step does NOT dedup anything - it just stacks every standardized
source on top of each other and records which file each row came from,
so entity resolution (the next step) has full traceability.

Usage:
    python3 scripts/pipeline/combine_standardized.py \
        --input-dir data/phase2_pharma/standardized \
        --out data/phase2_pharma/combined_raw.csv
"""

import argparse
from pathlib import Path

import pandas as pd

def combine_csvs(input_dir: Path, out_path: Path):
    """Core logic to concatenate standardized CSVs."""
    files = sorted(input_dir.glob("*.csv"))
    if not files:
        raise FileNotFoundError(f"No CSV files found in {input_dir}")

    frames = []
    for f in files:
        df = pd.read_csv(f, dtype=str)
        df["source_file"] = f.name 
        print(f"  {f.name}: {len(df)} rows")
        frames.append(df)

    combined = pd.concat(frames, ignore_index=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(out_path, index=False)
    print(f"\nCombined total: {len(combined)} rows from {len(files)} files")
    print(f"Wrote to {out_path}")

def main():
    parser = argparse.ArgumentParser(description="Combine all standardized source files")
    parser.add_argument("--input-dir", type=Path,
                         default=Path("data/phase2_pharma/standardized"))
    parser.add_argument("--out", type=Path,
                         default=Path("data/phase2_pharma/auto_combined_raw.csv"))
    args = parser.parse_args()

    # Call the core logic function
    combine_csvs(args.input_dir, args.out)

if __name__ == "__main__":
    main()