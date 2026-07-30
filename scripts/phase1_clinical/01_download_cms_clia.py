"""
Script 01: Download & Explore CMS CLIA Database
================================================
Phase 1 — Clinical Labs, California MVP

What this script does:
  1. Downloads the CMS Provider of Services (CLIA) file via their public API
  2. Saves the raw data to data/raw/
  3. Previews what columns exist and what they look like
  4. Maps CMS column names to our project schema

Run this FIRST before any cleaning or geocoding.
"""

import requests
import pandas as pd
import time
import os
import json

# ── paths ──────────────────────────────────────────────────────────────────────
RAW_DIR   = os.path.join(os.path.dirname(__file__), "..", "..", "data", "phase1_clinical", "raw")
RAW_FILE  = os.path.join(RAW_DIR, "cms_clia_national_raw.csv")
CA_FILE   = os.path.join(RAW_DIR, "cms_clia_california_raw.csv")

os.makedirs(RAW_DIR, exist_ok=True)


# ── Step 1: Download from CMS API ──────────────────────────────────────────────
# CMS data API endpoint for CLIA Provider of Services file
# Dataset ID: c6xs-qusr  (Provider of Services File – Clinical Laboratories)
# Docs: https://data.cms.gov/provider-characteristics/hospitals-and-other-facilities/
#       provider-of-services-file-clinical-laboratories

CMS_API_BASE   = "https://data.cms.gov/data-api/v1/dataset/d3eb38ac-d8e9-40d3-b7b7-6205d3d1dc16/data"
BATCH_SIZE     = 5000   # max per request
MENTOR_NOTE    = """
MENTOR NOTE — why paginate?
The CMS API returns a maximum of 1,000 rows by default (up to 5,000 if you ask nicely).
The full CLIA database has ~317,000 labs. We loop with offset to get everything.
This pattern — batch + offset — is used in virtually every real-world data pipeline.
"""

def download_cms_clia(force_redownload=False):
    """Download full CMS CLIA dataset with pagination. Skips if file exists."""

    if os.path.exists(RAW_FILE) and not force_redownload:
        print(f"Raw file already exists at {RAW_FILE}")
        print("Skipping download. Pass force_redownload=True to re-fetch.")
        return pd.read_csv(RAW_FILE, dtype=str, low_memory=False)

    print(MENTOR_NOTE)
    print("Starting download from CMS API...")

    all_records = []
    offset      = 0

    # First, get total count
    stats_url = CMS_API_BASE + "/stats"
    resp = requests.get(stats_url, timeout=30)
    total_rows = resp.json().get("total", "unknown")
    print(f"Total records in dataset: {total_rows}")

    while True:
        url    = f"{CMS_API_BASE}?size={BATCH_SIZE}&offset={offset}"
        resp   = requests.get(url, timeout=60)

        if resp.status_code != 200:
            print(f"Error at offset {offset}: HTTP {resp.status_code}")
            break

        batch = resp.json()
        if not batch:
            print(f"Empty batch at offset {offset} — download complete.")
            break

        all_records.extend(batch)
        print(f"  Downloaded {len(all_records):,} / {total_rows} records...", end="\r")

        if len(batch) < BATCH_SIZE:
            break   # last page

        offset += BATCH_SIZE
        time.sleep(0.5)   # be polite to the API

    print(f"\nTotal records downloaded: {len(all_records):,}")

    df = pd.DataFrame(all_records)
    df.to_csv(RAW_FILE, index=False)
    print(f"Saved to: {RAW_FILE}")
    return df


# ── Step 2: Preview the raw data ───────────────────────────────────────────────
# MENTOR NOTE — always look at raw data BEFORE writing cleaning code.
# You need to know what you're working with: column names, data types, nulls, quirks.

def preview_raw_data(df):
    """Print a structured preview of the raw CMS CLIA dataframe."""
    print("\n" + "="*60)
    print("RAW DATA PREVIEW")
    print("="*60)

    print(f"\nShape: {df.shape[0]:,} rows × {df.shape[1]} columns")

    print("\n── Column names (all) ──")
    for i, col in enumerate(df.columns):
        sample = str(df[col].dropna().iloc[0]) if df[col].notna().any() else "ALL NULL"
        null_pct = df[col].isna().mean() * 100
        print(f"  {i+1:2}. {col:<35} null={null_pct:.0f}%   sample: {sample[:50]}")

    print("\n── State distribution (top 10) ──")
    if "STATE_CD" in df.columns:
        print(df["STATE_CD"].value_counts().head(10).to_string())

    print("\n── First 3 rows (raw) ──")
    print(df.head(3).to_string())


# ── Step 3: Column mapping — CMS names → our schema ───────────────────────────
# MENTOR NOTE — this is one of the most important steps in any data project.
# Source column names are rarely what you want in the final schema.
# Document the mapping explicitly; don't just rename silently.

CMS_TO_SCHEMA = {
    # CMS column name      : our schema field name
    "PRVDR_NUM"            : "clia_number",       # CLIA certification number
    "FAC_NAME"             : "lab_name",           # Facility name
    "ST_ADR"               : "street_address",     # Street address
    "CITY_NAME"            : "city",               # City
    "STATE_CD"             : "state",              # 2-letter state code
    "ZIP_CD"               : "zip_code",           # ZIP code
    "PHONE_NUM"            : "phone_number",       # Phone (secondary field)
    "ACRDTN_TYPE_CD"       : "accreditation",      # Accreditation type
    "PRVDR_CTGRY_SBTYP_CD" : "specialty",          # Lab specialty/subtype
    "CRTFCTN_DT"           : "date_collected",     # Certification date (proxy)
}

# Fields in our schema NOT in CMS CLIA — must be added manually or from other sources
SCHEMA_GAPS = {
    "company_name"  : "CMS has FAC_NAME (lab name) but not parent company name. "
                      "Must enrich from Google Places or business registry.",
    "is_hq"         : "Not in CMS. Infer: if only one location per company_name → likely HQ. "
                      "Otherwise, mark FALSE and flag for manual review.",
    "company_url"   : "Not in CMS. Enrich from Google Places API or company website scraping.",
    "contact_name"  : "Not in CMS. CMS has no contact person. "
                      "Enrich from LinkedIn, lab website, or Hunter.io.",
    "contact_email" : "Not in CMS. Same enrichment sources as contact_name.",
    "latitude"      : "Not in CMS. Geocode using Nominatim from address fields.",
    "longitude"     : "Not in CMS. Geocode using Nominatim from address fields.",
}


def print_schema_mapping():
    print("\n" + "="*60)
    print("COLUMN MAPPING: CMS → Our Schema")
    print("="*60)

    print("\n── Fields we GET from CMS ──")
    for cms_col, schema_col in CMS_TO_SCHEMA.items():
        print(f"  {cms_col:<30} → {schema_col}")

    print("\n── Fields we MUST ADD ourselves (schema gaps) ──")
    for field, note in SCHEMA_GAPS.items():
        print(f"\n  {field}:")
        print(f"    {note}")


# ── Step 4: Filter to California ───────────────────────────────────────────────
# def filter_to_california(df):
#     """Keep only CA records and save."""
#     if "STATE_CD" not in df.columns:
#         raise ValueError("Column STATE_CD not found. Check your column mapping.")

#     ca_df = df[df["STATE_CD"].str.strip().str.upper() == "CA"].copy()
#     ca_df.to_csv(CA_FILE, index=False)

#     print(f"\n── California filter ──")
#     print(f"  National total : {len(df):,} labs")
#     print(f"  California only: {len(ca_df):,} labs")
#     print(f"  Saved to       : {CA_FILE}")
#     return ca_df


# ── Step 5: Quick data quality check ───────────────────────────────────────────
# MENTOR NOTE — always run a data quality check right after download.
# Catch problems early, before they silently corrupt your cleaning steps.

def data_quality_check(df, label="dataset"):
    print(f"\n── Data quality check: {label} ──")

    checks = {
        "Total rows"                  : len(df),
        "Rows with no lab name"       : df["FAC_NAME"].isna().sum() if "FAC_NAME" in df.columns else "col missing",
        "Rows with no street address" : df["ST_ADR"].isna().sum() if "ST_ADR" in df.columns else "col missing",
        "Rows with no ZIP code"       : df["ZIP_CD"].isna().sum() if "ZIP_CD" in df.columns else "col missing",
        "Rows with no phone"          : df["PHONE_NUM"].isna().sum() if "PHONE_NUM" in df.columns else "col missing",
        "Unique CLIA numbers"         : df["PRVDR_NUM"].nunique() if "PRVDR_NUM" in df.columns else "col missing",
        "Duplicate CLIA numbers"      : df["PRVDR_NUM"].duplicated().sum() if "PRVDR_NUM" in df.columns else "col missing",
    }

    for check, result in checks.items():
        flag = " ⚠" if isinstance(result, int) and result > 0 and "Total" not in check and "Unique" not in check else ""
        print(f"  {check:<35}: {result}{flag}")


# ── Main ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Step 1: Download
    df_national = download_cms_clia(force_redownload=False)

    # Step 2: Preview
    preview_raw_data(df_national)

    # Step 3: Print schema mapping
    print_schema_mapping()

    # Step 4: Filter to CA
    # df_ca = filter_to_california(df_national)

    # Step 5: Quality check
    data_quality_check(df_national, label="National")
    # data_quality_check(df_ca, label="California")

    print("\n✓ Script 01 complete. Next: run scripts/02_clean_cms_clia.py")
