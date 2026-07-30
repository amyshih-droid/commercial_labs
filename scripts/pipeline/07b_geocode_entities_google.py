"""
07b_geocode_entities_google.py - Appends latitude/longitude to
deduplicated facilities using the Google Maps Geocoding API.

Usage:
    python3 scripts/pipeline/07b_geocode_entities_google.py \
        --in data/phase2_pharma/master_entities_tagged.csv \
        --out data/phase2_pharma/master_entities_geocoded_google.csv \
        --only-missing
"""

import argparse
import json
import os
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()  # reads GOOGLE_MAPS_API_KEY from a .env file 

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

# Google's documented limit is 50 requests/second, but that assumes a
# high query-per-second quota tier. Default to a conservative delay;
# lower this only if you've confirmed your project's QPS quota supports it.
DEFAULT_DELAY_SECONDS = 0.1
MAX_RETRIES_ON_RATE_LIMIT = 3
RETRY_BACKOFF_SECONDS = 2.0

import json

CACHE_FILE = Path("data/phase2_pharma/geocode_cache_google.json")

def load_geocode_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_geocode_cache(cache: dict):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)

def make_address_key(street: str, city: str, state: str, zip_code: str) -> str:
    """Creates a normalized string key for the address."""
    parts = [p.strip().upper() for p in [street, city, state, zip_code] if p and str(p).strip()]
    return ", ".join(parts)

def geocode_google_single(street: str, city: str, state: str, zip_code: str,
                           api_key: str, retries: int = MAX_RETRIES_ON_RATE_LIMIT) -> tuple:
    """
    Single-address lookup via the Google Maps Geocoding API.
    Returns: (lat, lon, source_label, formatted_address) or
             (None, None, "failed:<reason>", None)

    source_label encodes Google's own location_type (ROOFTOP,
    RANGE_INTERPOLATED, GEOMETRIC_CENTER, APPROXIMATE) so you can judge
    match precision the same way the Census script encodes match_type
    (exact vs non_exact).
    """
    address_parts = [p for p in [street, city, state, zip_code] if p]
    if not address_parts:
        return None, None, "failed:no_address_data", None
    query = ", ".join(address_parts)

    params = {"address": query, "key": api_key, "components": "country:US"}

    for attempt in range(retries + 1):
        try:
            resp = requests.get(GEOCODE_URL, params=params, timeout=10)
            data = resp.json()
            status = data.get("status")

            if status == "OK" and data.get("results"):
                result = data["results"][0]
                location = result["geometry"]["location"]
                location_type = result["geometry"].get("location_type", "UNKNOWN")
                formatted_address = result.get("formatted_address")
                return (location["lat"], location["lng"],
                        f"google_{location_type.lower()}", formatted_address)

            if status == "ZERO_RESULTS":
                return None, None, "failed:zero_results", None

            if status == "OVER_QUERY_LIMIT":
                if attempt < retries:
                    time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                return None, None, "failed:over_query_limit", None

            if status in ("REQUEST_DENIED", "INVALID_REQUEST"):
                # Not worth retrying - these indicate a bad API key/config
                # or a malformed query, not a transient issue.
                return None, None, f"failed:{status.lower()}", None

            return None, None, f"failed:{status.lower() if status else 'unknown'}", None

        except Exception as e:
            if attempt < retries:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            return None, None, f"failed:exception:{type(e).__name__}", None

    return None, None, "failed:exhausted_retries", None


def main():
    parser = argparse.ArgumentParser(
        description="Geocode facilities using the Google Maps Geocoding API"
    )
    parser.add_argument(
        "--in", dest="input_file", type=Path,
        default=Path("data/phase2_pharma/master_entities_tagged.csv"),
        help="Path to input CSV file",
    )
    parser.add_argument(
        "--out", dest="output_file", type=Path,
        default=Path("data/phase2_pharma/master_entities_geocoded_google.csv"),
        help="Path to output geocoded CSV file",
    )
    parser.add_argument(
        "--api-key-env", default="GOOGLE_MAPS_API_KEY",
        help="Environment variable name holding your API key (default: GOOGLE_MAPS_API_KEY)",
    )
    parser.add_argument(
        "--only-missing", action="store_true",
        help="Only geocode rows where latitude is currently null (e.g. rows "
             "the Census/Nominatim script in 07a couldn't match) - use this "
             "to fill remaining gaps rather than re-geocoding everything.",
    )
    parser.add_argument(
        "--delay", type=float, default=DEFAULT_DELAY_SECONDS,
        help=f"Seconds to wait between requests (default: {DEFAULT_DELAY_SECONDS})",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Only process the first N rows needing geocoding - use this "
             "for a cheap smoke test before a full (billed) run",
    )
    args = parser.parse_args()

    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        raise EnvironmentError(
            f"No API key found in environment variable '{args.api_key_env}'. "
            f"Set it in a .env file at your project root, e.g.:\n"
            f"  {args.api_key_env}=your-real-key-here"
        )

    if not args.input_file.exists():
        raise FileNotFoundError(f"Input file not found: {args.input_file}")

    print(f"Reading input file: {args.input_file}")
    df = pd.read_csv(args.input_file, dtype=str)

    for col in ["google_latitude", "google_longitude", "google_geocode_source",
                "google_geocode_formatted_address"]:
        if col not in df.columns:
            df[col] = None

    if args.only_missing:
        rows_to_process = df[df["google_latitude"].isna()].index
        print(f"--only-missing set: {len(rows_to_process)}/{len(df)} rows "
              f"currently have no google_latitude, will attempt those only.")
    else:
        rows_to_process = df.index
        print(f"Geocoding all {len(rows_to_process)} rows via Google "
              f"(existing google_latitude/google_longitude will be "
              f"overwritten; your Census/Nominatim latitude/longitude "
              f"columns are untouched, enabling side-by-side comparison).")

    if args.limit:
        rows_to_process = rows_to_process[: args.limit]
        print(f"--limit set: only processing the first {len(rows_to_process)} rows.")

    geocode_cache = load_geocode_cache()
    print(f"Loaded {len(geocode_cache)} cached geocode entries from {CACHE_FILE}")

    matched_count = 0
    cache_hits = 0
    api_calls = 0
    failed_reasons = {}

    for count, idx in enumerate(rows_to_process, 1):
        street = str(df.loc[idx, "address_street"] or "").strip()
        city = str(df.loc[idx, "address_city"] or "").strip()
        state = str(df.loc[idx, "address_state"] or "").strip()
        zip_code = str(df.loc[idx, "address_zip"] or "").strip()

        addr_key = make_address_key(street, city, state, zip_code)

        if not addr_key:
            lat, lon, source, formatted = None, None, "failed:no_address_data", None
        elif addr_key in geocode_cache:
            # CACHE HIT: Retrieve stored results for FREE
            cached_entry = geocode_cache[addr_key]
            lat = cached_entry.get("lat")
            lon = cached_entry.get("lon")
            source = cached_entry.get("source")
            formatted = cached_entry.get("formatted")
            cache_hits += 1
        else:
            lat, lon, source, formatted = geocode_google_single(
                street, city, state, zip_code, api_key
            )
            api_calls += 1
            # Store in cache immediately (even if zero_results, so we don't re-query missing locations)
            geocode_cache[addr_key] = {
                "lat": lat,
                "lon": lon,
                "source": source,
                "formatted": formatted,
            }
            save_geocode_cache(geocode_cache)
            time.sleep(args.delay)
        
        if lat is not None and lon is not None:
            df.at[idx, "google_latitude"] = lat
            df.at[idx, "google_longitude"] = lon
            df.at[idx, "google_geocode_source"] = source
            df.at[idx, "google_geocode_formatted_address"] = formatted
            matched_count += 1
        else:
            df.at[idx, "google_geocode_source"] = source
            failed_reasons[source] = failed_reasons.get(source, 0) + 1

        if count % 25 == 0 or count == len(rows_to_process):
            print(f"  Progress: {count}/{len(rows_to_process)} processed "
                  f"({matched_count} matched so far)...")


    args.output_file.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output_file, index=False)

    print("\n" + "=" * 50)
    print("GOOGLE GEOCODING SUMMARY")
    print("=" * 50)
    print(f"Rows attempted:      {len(rows_to_process)}")
    print(f"Matched:             {matched_count}")
    print(f"Failed:              {len(rows_to_process) - matched_count}")
    if failed_reasons:
        print("Failure breakdown:")
        for reason, count in sorted(failed_reasons.items(), key=lambda x: -x[1]):
            print(f"  {count:>5}  {reason}")
    print("=" * 50)
    print(f"Wrote results to {args.output_file}")


if __name__ == "__main__":
    main()