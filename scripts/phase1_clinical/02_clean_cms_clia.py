"""
Script 02: Clean & Transform CMS CLIA Data
===========================================
Takes the raw CMS download and transforms it into your project schema.

What this script does, in order:
  1. Load the raw national CSV
  2. Rename CMS column names → your schema field names
  3. Add missing fields (lab_id, company_name, lab_type, data_source, etc.)
  4. Standardize values (ZIP codes, state codes, phone numbers)
  5. Replace all NaN/None with "" (your null convention)
  6. Deduplicate on CLIA number
  7. Run a data quality report
  8. Save to data/clean/cms_clia_national_clean.csv

"""

import pandas as pd
import re
import os

# ── Paths ───────────────────────────────────────────────────────────────────────
RAW_FILE   = os.path.join(os.path.dirname(__file__), "..", "..", "data", "phase1_clinical", "raw",   "cms_clia_national_raw.csv")
CLEAN_FILE = os.path.join(os.path.dirname(__file__), "..", "..","data", "phase1_clinical", "clean", "cms_clia_national_clean.csv")

os.makedirs(os.path.dirname(CLEAN_FILE), exist_ok=True)


# ── Step 1: Load raw data ───────────────────────────────────────────────────────
# MENTOR NOTE — always load CSVs with dtype=str when cleaning.
# If you let pandas guess types, it will turn ZIP code "07001" into integer 7001
# and you permanently lose the leading zero. Read everything as string first,
# then convert only the columns you actually need as numbers.

def load_raw(path: str) -> pd.DataFrame:
    print(f"Loading raw file: {path}")
    df = pd.read_csv(path, dtype=str, low_memory=False)
    print(f"  Loaded {len(df):,} rows × {df.shape[1]} columns")
    print(f"  Columns: {list(df.columns)}")
    return df

# ── Step 1b: Filter to commercial labs only ─────────────────────────────────────
# Applied BEFORE renaming columns because the filter uses raw CMS column names.
# See PROJECT_SPEC.md — Filtering decisions section for full rationale.
def filter_commercial(df: pd.DataFrame) -> pd.DataFrame:
    before = len(df)

    df = df[
        (df['PGM_TRMNTN_CD'] == '00') &      # active — not closed/terminated
        (df['GNRL_CNTL_TYPE_CD'] == '04') &  # privately owned — not govt/nonprofit
        (df['SKLTN_REC_SW'] == 'N')           # real record — not ghost entry
    ].copy()

    print(f"  Rows: {before:,} → {len(df):,}")
    print(f"  Removed: {before - len(df):,} non-commercial records")
    return df


# ── Step 2: Rename CMS columns → your schema ───────────────────────────────────
# Only keep columns that map to your schema. Drop the rest.
# If a CMS column doesn't appear in your file, it's silently skipped.

CMS_TO_SCHEMA = {
    "PRVDR_NUM"            : "clia_number",
    "FAC_NAME"             : "lab_name",
    "ST_ADR"               : "street_address",
    "CITY_NAME"            : "city",
    "STATE_CD"             : "state",
    "ZIP_CD"               : "zip_code",
    "PHNE_NUM"            : "phone_number",
    "ACRDTN_TYPE_CD"       : "accreditation",
    "PRVDR_CTGRY_SBTYP_CD" : "specialty",
    "CRTFCTN_DT"           : "date_collected",
}

def rename_columns(df: pd.DataFrame) -> pd.DataFrame:
    # only rename columns that actually exist in this file
    rename_map = {k: v for k, v in CMS_TO_SCHEMA.items() if k in df.columns}
    df = df.rename(columns=rename_map)

    missing = [v for k, v in CMS_TO_SCHEMA.items() if k not in rename_map]
    if missing:
        print(f"  [INFO] These CMS columns were not found (may vary by export): "
              f"{[k for k,v in CMS_TO_SCHEMA.items() if k not in rename_map]}")

    # keep only schema columns that exist; drop all other CMS columns
    keep = [c for c in CMS_TO_SCHEMA.values() if c in df.columns]
    df   = df[keep].copy()
    print(f"  Kept {len(keep)} columns after rename")
    return df


# ── Step 3: Add missing schema fields ──────────────────────────────────────────
# These fields don't exist in CMS — we add them now with defaults.
# Scripts 03 and 04 will fill in lat/lon, contact info, and company_url later.

def add_missing_fields(df: pd.DataFrame) -> pd.DataFrame:
    # generate a unique lab_id for every row
    # format: CLIA_CA_00001, CLIA_CA_00002, ...
    df.insert(0, "lab_id", [f"CLIA_CA_{str(i+1).zfill(5)}" for i in range(len(df))])

    # company_name: CMS has no parent company field
    # for now, copy lab_name — script 04 (AI enrichment) will correct large chains
    if "lab_name" in df.columns:
        df.insert(1, "company_name", df["lab_name"])

    # fields that will be filled by later scripts — default to ""
    df.loc[:,"is_hq"]        = ""   # script 04 will infer this
    df.loc[:,"company_url"]  = ""   # script 04 will find this
    df.loc[:,"contact_name"] = ""   # script 04 will find this
    df.loc[:,"contact_email"]= ""   # script 04 will find this
    df.loc[:,"latitude"]     = ""   # script 03 will geocode this
    df.loc[:,"longitude"]    = ""   # script 03 will geocode this

    # fields we know right now
    df.loc[:,"lab_type"]     = "clinical"   # all CMS CLIA = clinical labs
    df.loc[:,"data_source"]  = "CMS_CLIA"

    print(f"  Added missing schema fields. Total columns: {df.shape[1]}")
    return df


# ── Step 4: Standardize values ──────────────────────────────────────────────────

def standardize_zip(zip_code: str) -> str:
    """
    Ensure ZIP code is exactly 5 digits, zero-padded if short.
    CMS sometimes stores ZIP as integer, losing leading zeros (e.g. NJ ZIPs start 0).
    """
    zip_str = str(zip_code)
    if not zip_code or zip_code.strip() == "":
        return ""
    z = str(zip_code).strip().split("-")[0]  # remove ZIP+4 suffix (e.g. "90210-1234")
    z = re.sub(r"[^0-9]", "", z)            # digits only
    return z.zfill(5) if len(z) <= 5 else z[:5]


def standardize_phone(phone: str) -> str:
    """
    Normalize phone to (XXX) XXX-XXXX format.
    CMS stores phones as 10-digit strings like "3105551234".
    """
    phone_str = str(phone)
    if not phone or phone.strip() == "":
        return ""
    digits = re.sub(r"[^0-9]", "", str(phone))
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    elif len(digits) == 11 and digits[0] == "1":
        return f"({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    return phone.strip()   # return original if format is unexpected


def standardize_values(df: pd.DataFrame) -> pd.DataFrame:
    # strip leading/trailing whitespace from all string columns
    for col in df.select_dtypes(include="object").columns:
        df.loc[:, col] = df[col].astype(str).str.strip()

    # title-case lab names and cities (CMS often uses ALL CAPS)
    for col in ["lab_name", "company_name", "city", "street_address"]:
        if col in df.columns:
            df.loc[:, col] = df[col].astype(str).str.title()

    # uppercase state code
    if "state" in df.columns:
        df.loc[:, "state"] = df["state"].astype(str).str.upper()

    # standardize ZIP
    if "zip_code" in df.columns:
        df.loc[:, "zip_code"] = df["zip_code"].apply(standardize_zip)

    # standardize phone
    if "phone_number" in df.columns:
        df.loc[:, "phone_number"] = df["phone_number"].apply(standardize_phone)

    print("  Standardized: whitespace, title case, ZIP, phone")
    return df


# ── Step 5: Replace all nulls with "" ──────────────────────────────────────────
# pandas uses NaN (float) for missing string values.
# In a CSV, NaN becomes the literal text "nan" which breaks downstream code.
# We replace every NaN with "" to match our null convention from the spec.

def fix_nulls(df: pd.DataFrame) -> pd.DataFrame:
    df = df.fillna("")
    # also catch the string "nan" which sometimes sneaks in
    df = df.replace("nan", "").replace("NaN", "").replace("None", "")
    print("  Replaced all nulls with empty string \"\"")
    return df


# ── Step 6: Deduplicate ─────────────────────────────────────────────────────────
# MENTOR NOTE — the CMS CLIA file shouldn't have duplicate CLIA numbers,
# but always check. In real data projects, "shouldn't" doesn't mean "won't".

def deduplicate(df: pd.DataFrame) -> pd.DataFrame:
    before = len(df)

    if "clia_number" in df.columns and df["clia_number"].ne("").any():
        # keep the first occurrence of each CLIA number
        df = df.drop_duplicates(subset=["clia_number"], keep="first")
        dupes_removed = before - len(df)
        if dupes_removed > 0:
            print(f"  ⚠ Removed {dupes_removed} duplicate CLIA numbers")
        else:
            print(f"  ✓ No duplicate CLIA numbers found")
    else:
        # fallback: deduplicate on address if no CLIA number
        df = df.drop_duplicates(
            subset=["street_address", "zip_code"],
            keep="first"
        )
        print(f"  Deduplicated on address (no CLIA number available)")

    print(f"  Rows: {before:,} → {len(df):,}")
    return df


# ── Step 7: Data quality report ─────────────────────────────────────────────────
# Print a summary that tells you exactly how complete your cleaned data is.
# Read this carefully — it tells you what to expect in scripts 03 and 04.

def quality_report(df: pd.DataFrame):
    print("\n" + "="*55)
    print("DATA QUALITY REPORT — cleaned national clinical labs")
    print("="*55)
    print(f"Total records : {len(df):,}")
    print(f"Total columns : {df.shape[1]}")
    print()

    important = [
        "lab_name", "street_address", "city", "state", "zip_code",
        "phone_number", "clia_number", "accreditation", "specialty"
    ]

    print(f"{'Field':<20} {'Filled':>8} {'Empty':>8} {'% complete':>12}")
    print("-" * 52)
    for col in df.columns:
        filled  = df[col].ne("").sum()
        empty   = df[col].eq("").sum()
        pct     = (filled / len(df) * 100) if len(df) > 0 else 0
        flag    = " ⚠" if pct < 70 and col in important else ""
        print(f"  {col:<20} {filled:>7,} {empty:>8,} {pct:>10.0f}%{flag}")

    print()
    print("Fields still empty (to be filled by later scripts):")
    empty_fields = ["latitude", "longitude", "company_url",
                    "contact_name", "contact_email", "is_hq"]
    for f in empty_fields:
        script = "script 03" if f in ["latitude","longitude"] else "script 04"
        print(f"  {f:<20} ← {script} will fill this")


# ── Step 8: Save ────────────────────────────────────────────────────────────────
def save_clean(df: pd.DataFrame, path: str):
    # enforce column order matching your schema exactly
    schema_order = [
        "lab_id", "company_name", "lab_name",
        "street_address", "city", "state", "zip_code",
        "is_hq", "company_url",
        "contact_name", "contact_email",
        "latitude", "longitude",
        "lab_type", "data_source",
        "phone_number", "clia_number", "accreditation",
        "specialty", "date_collected",
    ]
    # only include columns that actually exist
    ordered = [c for c in schema_order if c in df.columns]
    df      = df[ordered]

    df.to_csv(path, index=False, encoding="utf-8")
    print(f"\n✓ Saved clean file: {path}")
    print(f"  {len(df):,} rows × {df.shape[1]} columns")
    print(f"  Next: run scripts/03_geocode.py")


# ── Main ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("="*55)
    print("Script 02: Clean & Transform CMS CLIA National")
    print("="*55 + "\n")

    df = load_raw(RAW_FILE)

    print("\n── Step 1b: Filter to commercial labs only ──")
    df = filter_commercial(df)

    print("\n── Step 2: Rename columns ──")
    df = rename_columns(df)

    print("\n── Step 3: Add missing fields ──")
    df = add_missing_fields(df)

    print("\n── Step 4: Standardize values ──")
    df = standardize_values(df)

    print("\n── Step 5: Fix nulls ──")
    df = fix_nulls(df)

    print("\n── Step 6: Deduplicate ──")
    df = deduplicate(df)

    quality_report(df)

    print("\n── Step 8: Save ──")
    save_clean(df, CLEAN_FILE)
