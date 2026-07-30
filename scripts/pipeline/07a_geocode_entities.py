"""
07_geocode_entities.py - Appends latitude and longitude to deduplicated facilities.

Primary Geocoder: US Census Bureau API (Fast Batch Mode, 100% Free)
Fallback Geocoder: OpenStreetMap / Nominatim (1 req/sec rate limit)

Usage:
    python3 scripts/pipeline/07_geocode_entities.py \
        --in data/phase2_pharma/master_entities_tagged.csv \
        --out data/phase2_pharma/master_entities_geocoded.csv
"""

import argparse
import csv
import io
from pathlib import Path
import time

import pandas as pd
import requests

# Batch size for US Census API (Census max limit is 10,000; 5,000 prevents timeouts)
CENSUS_BATCH_SIZE = 1000

# Nominatim OSM policy requires a custom User-Agent
NOMINATIM_HEADERS = {
    "User-Agent": "PharmaResearchPipeline/1.0 (facility_geocoding_script)"
}


def geocode_census_batch(df_chunk: pd.DataFrame) -> dict:
    """
    Submits a batch of rows to the US Census Bureau Batch Geocoder.
    Returns: {entity_id: (lat, lon, geocode_source)}
    """
    results = {}
    
    # Census CSV payload requirement: ID, Street, City, State, ZIP
    census_input = pd.DataFrame({
        "id": df_chunk["_temp_id"],
        "street": df_chunk["address_street"].fillna(""),
        "city": df_chunk["address_city"].fillna(""),
        "state": df_chunk["address_state"].fillna(""),
        "zip": df_chunk["address_zip"].fillna("")
    })

    # Convert chunk to CSV buffer
    csv_buffer = io.StringIO()
    census_input.to_csv(csv_buffer, index=False, header=False)
    csv_buffer.seek(0)

    url = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
    files = {"addressFile": ("batch.csv", csv_buffer.getvalue(), "text/csv")}
    data = {"benchmark": "Public_AR_Current"}

    try:
        response = requests.post(url, files=files, data=data, timeout=180)
        if response.status_code == 200:
            reader = csv.reader(io.StringIO(response.text))
            for row in reader:
                if len(row) >= 6:
                    row_id = row[0]
                    status = row[2]       # 'Match', 'No_Match', or 'Tie'
                    match_type = row[3]   # 'Exact' or 'Non_Exact'
                    lon_lat = row[5]      # Format: "-89.6501,39.7817"

                    if status in ("Match", "Tie") and lon_lat and "," in lon_lat:
                        lon_str, lat_str = lon_lat.split(",")
                        try:
                            source = f"census_{match_type.lower()}"
                            results[row_id] = (float(lat_str), float(lon_str), source)
                        except ValueError:
                            pass
    except Exception as e:
        print(f"  Warning: Census Batch API request failed: {e}")

    return results


def geocode_nominatim_single(street: str, city: str, state: str, zip_code: str) -> tuple:
    """
    Single-address lookup fallback using OpenStreetMap Nominatim API.
    Returns: (lat, lon, source) or (None, None, "failed")
    """
    url = "https://nominatim.openstreetmap.org/search"
    
    # Try full street query first if street exists
    if street:
        query = f"{street}, {city}, {state} {zip_code}".strip(", ")
        source_label = "nominatim_street"
    else:
        query = f"{city}, {state} {zip_code}".strip(", ")
        source_label = "nominatim_city_fallback"

    params = {
        "q": query,
        "format": "json",
        "limit": 1,
        "countrycodes": "us"
    }

    try:
        resp = requests.get(url, headers=NOMINATIM_HEADERS, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data:
                lat = float(data[0]["lat"])
                lon = float(data[0]["lon"])
                return lat, lon, source_label
            
            # If street query failed, retry with City + State + ZIP
            if street:
                time.sleep(1.0)
                fallback_query = f"{city}, {state} {zip_code}".strip(", ")
                params["q"] = fallback_query
                resp = requests.get(url, headers=NOMINATIM_HEADERS, params=params, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    if data:
                        return float(data[0]["lat"]), float(data[0]["lon"]), "nominatim_city_fallback"
    except Exception:
        pass

    return None, None, "failed"


def main():
    parser = argparse.ArgumentParser(
        description="Geocode facilities using US Census API with Nominatim fallback"
    )
    parser.add_argument(
        "--in",
        dest="input_file",
        type=Path,
        default=Path("data/phase2_pharma/master_entities_tagged.csv"),
        help="Path to input tagged CSV file",
    )
    parser.add_argument(
        "--out",
        dest="output_file",
        type=Path,
        default=Path("data/phase2_pharma/master_entities_geocoded.csv"),
        help="Path to output geocoded CSV file",
    )
    args = parser.parse_args()

    if not args.input_file.exists():
        raise FileNotFoundError(f"Input file not found: {args.input_file}")

    print(f"Reading input file: {args.input_file}")
    df = pd.read_csv(args.input_file, dtype=str)

    # Temporary unique row identifier for matching batch responses
    df["_temp_id"] = [f"ROW_{i}" for i in range(len(df))]
    
    # Initialize output columns if not present
    if "latitude" not in df.columns:
        df["latitude"] = None
    if "longitude" not in df.columns:
        df["longitude"] = None
    if "geocode_source" not in df.columns:
        df["geocode_source"] = None

    # Step 1: US Census Bureau Batch Geocoding
    print("\n--- Phase 1: US Census Bureau Batch Geocoding ---")
    census_matched_count = 0

    for i in range(0, len(df), CENSUS_BATCH_SIZE):
        chunk = df.iloc[i : i + CENSUS_BATCH_SIZE]
        print(f"Processing Census batch {i // CENSUS_BATCH_SIZE + 1} ({len(chunk)} rows)...")
        
        batch_results = geocode_census_batch(chunk)
        
        for idx in chunk.index:
            row_id = df.loc[idx, "_temp_id"]
            if row_id in batch_results:
                lat, lon, source = batch_results[row_id]
                df.at[idx, "latitude"] = lat
                df.at[idx, "longitude"] = lon
                df.at[idx, "geocode_source"] = source
                census_matched_count += 1

    print(f"Census Batch complete: Geocoded {census_matched_count} / {len(df)} rows.")

    # Step 2: Nominatim Fallback for Unmatched Rows
    unmatched_mask = df["latitude"].isna()
    unmatched_indices = df[unmatched_mask].index
    print(f"\n--- Phase 2: OpenStreetMap / Nominatim Fallback ({len(unmatched_indices)} rows) ---")

    nominatim_matched_count = 0
    if len(unmatched_indices) > 0:
        print("Running Nominatim lookups (rate limited to 1 request/second)...")
        for count, idx in enumerate(unmatched_indices, 1):
            street = str(df.loc[idx, "address_street"] or "").strip()
            city = str(df.loc[idx, "address_city"] or "").strip()
            state = str(df.loc[idx, "address_state"] or "").strip()
            zip_code = str(df.loc[idx, "address_zip"] or "").strip()

            lat, lon, source = geocode_nominatim_single(street, city, state, zip_code)
            
            if lat and lon:
                df.at[idx, "latitude"] = lat
                df.at[idx, "longitude"] = lon
                df.at[idx, "geocode_source"] = source
                nominatim_matched_count += 1

            if count % 20 == 0 or count == len(unmatched_indices):
                print(f"  Progress: {count}/{len(unmatched_indices)} fallbacks checked...")

            # Strict 1 second delay to adhere to Nominatim usage policy
            time.sleep(1.0)

    # Remove temporary tracking ID
    df.drop(columns=["_temp_id"], inplace=True)

    # Save output CSV
    args.output_file.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output_file, index=False)

    # Final Summary Report
    total_geocoded = df["latitude"].notna().sum()
    failed_count = len(df) - total_geocoded

    print("\n" + "=" * 50)
    print("GEOCODING SUMMARY")
    print("=" * 50)
    print(f"Total Facilities:          {len(df)}")
    print(f"Geocoded via US Census:    {census_matched_count}")
    print(f"Geocoded via Nominatim:    {nominatim_matched_count}")
    print(f"Failed / Unmatched:        {failed_count}")
    print(f"Overall Coverage:          {(total_geocoded / len(df)) * 100:.1f}%")
    print("=" * 50)
    print(f"Wrote results to {args.output_file}")


if __name__ == "__main__":
    main()