"""
validate_geocode_output.py - sanity checks on the REAL geocoded output,
after a run completes. It checks whether the actual data looks trustworthy, 
not whether the code's logic is correct in isolation.

Usage:
    python3 scripts/pipeline/validate_geocode_output.py \
        --in data/phase2_pharma/master_entities_geocoded_google.csv
"""

import argparse
from pathlib import Path

import pandas as pd

# Rough continental US bounding box (+ Alaska/Hawaii allowance). Not
# exact - just a sanity check to catch obviously-wrong coordinates
# (e.g. a geocoder confusing a US city name with a same-named city
# somewhere else in the world).
US_LAT_RANGE = (17.0, 72.0)   # covers Hawaii (~19N) to northern Alaska
US_LON_RANGE = (-180.0, -65.0)  # covers Alaska's westward extent to New England


def main():
    parser = argparse.ArgumentParser(description="Validate geocoded output data")
    parser.add_argument("--in", dest="input_file", type=Path, required=True)
    parser.add_argument(
        "--lat-col", default="google_latitude",
        help="Latitude column to validate (default: google_latitude)",
    )
    parser.add_argument(
        "--lon-col", default="google_longitude",
        help="Longitude column to validate (default: google_longitude)",
    )
    parser.add_argument(
        "--source-col", default="google_geocode_source",
        help="Source/status column to summarize (default: google_geocode_source)",
    )
    args = parser.parse_args()

    df = pd.read_csv(args.input_file, dtype=str)
    total = len(df)
    print(f"Loaded {total} rows from {args.input_file}\n")

    # --- 1. Overall match rate ---
    lat = pd.to_numeric(df[args.lat_col], errors="coerce")
    lon = pd.to_numeric(df[args.lon_col], errors="coerce")
    matched = lat.notna() & lon.notna()
    print(f"1. Match rate: {matched.sum()}/{total} ({matched.mean():.1%})")

    # --- 2. Coordinate range sanity check ---
    out_of_range = matched & (
        (lat < US_LAT_RANGE[0]) | (lat > US_LAT_RANGE[1]) |
        (lon < US_LON_RANGE[0]) | (lon > US_LON_RANGE[1])
    )
    print(f"\n2. Coordinates outside expected US range: {out_of_range.sum()}")
    if out_of_range.sum() > 0:
        print("   Sample rows (investigate these - likely a geocoding")
        print("   mismatch, e.g. a US city name matched to a place abroad):")
        cols_to_show = [c for c in ["company_name", "address_city", "address_state",
                                     args.lat_col, args.lon_col] if c in df.columns]
        print(df.loc[out_of_range, cols_to_show].head(10).to_string(index=False))

    # --- 3. Null Island check (0, 0) - a classic geocoding failure signature ---
    null_island = matched & (lat.abs() < 0.01) & (lon.abs() < 0.01)
    print(f"\n3. 'Null Island' (0,0) coordinates: {null_island.sum()}")
    if null_island.sum() > 0:
        print("   These almost always indicate a parsing bug, not a real")
        print("   location - worth treating as failures, not matches.")

    # --- 4. Suspicious coordinate clustering ---
    # If many DIFFERENT companies share the EXACT same coordinates, that
    # often means the geocoder fell back to a city-center/ZIP-centroid
    # location rather than a real street-level match, even though it
    # reported success.
    if matched.sum() > 0:
        coord_pairs = df.loc[matched].apply(
            lambda r: (round(float(r[args.lat_col]), 5), round(float(r[args.lon_col]), 5)),
            axis=1,
        )
        dup_counts = coord_pairs.value_counts()
        suspicious = dup_counts[dup_counts >= 5]  # 5+ different companies, identical coords
        print(f"\n4. Coordinate pairs shared by 5+ different rows: {len(suspicious)}")
        if len(suspicious) > 0:
            print("   These may be city/ZIP-centroid fallbacks rather than true")
            print("   street-level matches - worth spot-checking a few:")
            for coords, count in suspicious.head(5).items():
                print(f"     {coords}: {count} rows")

    # --- 5. Source/status breakdown ---
    if args.source_col in df.columns:
        print(f"\n5. '{args.source_col}' breakdown:")
        for val, count in df[args.source_col].value_counts(dropna=False).items():
            print(f"   {count:>6}  {val}")

    print("\nValidation complete.")


if __name__ == "__main__":
    main()