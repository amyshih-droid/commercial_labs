"""
Script 03: Fix Data Issues + Geocode Addresses
===============================================
Picks up where script 02 left off. Does two things:

  PART A — Fix two data issues spotted in the clean output:
    1. specialty column contains CMS codes ("01") not labels ("Routine Chemistry")
    2. title-case broke ordinal numbers: "34Th St" → "34th St"

  PART B — Geocode every address:
    - Uses US Census Geocoder (free, no API key, batch processing)
    - Processed in chunks of 9,999 rows to avoid API rate limits

SETUP:
  pip install censusgeocode tqdm
"""

import pandas as pd
import os
import re
import time
import tempfile
import censusgeocode as cg
from tqdm import tqdm

# ── Paths ───────────────────────────────────────────────────────────────────────
CLEAN_FILE    = os.path.join(os.path.dirname(__file__), "..", "..", "data", "phase1_clinical", "clean", "cms_clia_national_clean.csv")
GEOCODED_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "phase1_clinical", "clean", "cms_clia_national_geocoded.csv")

# ── PART A: Fix data issues ─────────────────────────────────────────────────────

# MENTOR NOTE — always decode lookup codes in your cleaning step, not at query time.
# Storing "01" in the CSV means every person who opens it needs to look up the codebook.
# Storing "Routine Chemistry" means the data is self-explanatory.

CLIA_SPECIALTY_CODES = {
    "01": "Routine Chemistry",
    "02": "Hematology",
    "03": "Immunohematology (Blood Banking)",
    "04": "Bacteriology",
    "05": "Mycobacteriology",
    "06": "Mycology",
    "07": "Parasitology",
    "08": "Virology",
    "09": "Syphilis Serology",
    "10": "General Immunology",
    "11": "Radiobioassay",
    "12": "Cytogenetics",
    "13": "Diagnostic Immunohematology",
    "14": "Pathology (Cytology / Histopathology)",
    "15": "Clinical Cytogenetics",
    "16": "Histocompatibility",
    "17": "Clinical Pharmacology",
    "18": "Dermatopathology",
    "19": "Oral Pathology",
    "20": "Ophthalmic Pathology",
    "21": "Neuropathology",
    "22": "Toxicology",
    "23": "Urinalysis",
    "24": "Endocrinology",
    "99": "Other / General Laboratory",
}


def fix_specialty_codes(df: pd.DataFrame) -> pd.DataFrame:
    """Replace numeric CLIA specialty codes with human-readable labels."""
    if "specialty" not in df.columns:
        print("  [SKIP] No specialty column found")
        return df

    before_unique = df["specialty"].nunique()
    df.loc[:, "specialty"] = df["specialty"].map(CLIA_SPECIALTY_CODES).fillna(df["specialty"])    
    # fillna keeps the original value if the code isn't in our lookup
    # (so unknown codes like "25" stay as "25" rather than becoming "")

    after_unique = df["specialty"].nunique()
    decoded = df["specialty"].isin(CLIA_SPECIALTY_CODES.values()).sum()
    print(f"  Specialty: {decoded:,} rows decoded  |  {before_unique} codes → {after_unique} unique values")
    return df


def fix_title_case_ordinals(text: str) -> str:
    """
    Fix title-case breaking ordinal numbers.
    "34Th St" → "34th St" | "101St Ave" → "101st Ave" | "2Nd Floor" → "2nd Floor"

    MENTOR NOTE — this is a regex. The pattern r'(\\d+)(St|Nd|Rd|Th)\\b'
    matches a number followed by an ordinal suffix:
      \\d+   = one or more digits
      (St|Nd|Rd|Th) = one of these ordinal suffixes (case-insensitive)
      \\b    = word boundary (so "Sth" in "South" isn't affected)
    The replacement lowercases the suffix: \\1 keeps the digits, \\2.lower() lowercases the suffix.
    """
    if not isinstance(text, str) or text == "":
        return text
    return re.sub(
        r'(\d+)(St|Nd|Rd|Th)\b',
        lambda m: m.group(1) + m.group(2).lower(),
        text
    )


def fix_address_formatting(df: pd.DataFrame) -> pd.DataFrame:
    """Fix ordinal number casing in street addresses."""
    if "street_address" not in df.columns:
        return df

    before = df["street_address"].head(5).tolist()
    df.loc[:, "street_address"] = df["street_address"].apply(fix_title_case_ordinals)
    after  = df["street_address"].head(5).tolist()

    changed = sum(1 for b, a in zip(before, after) if b != a)
    print(f"  Address ordinals: fixed in first 5 sample rows ({changed} changes shown)")
    return df


# ── ── PART B: Geocoding — US Census Geocoder ───────────────────────────────────────────────────────────

def build_census_input(df: pd.DataFrame) -> pd.DataFrame:
    """
    Build the input dataframe the Census Geocoder expects.
    Required columns: id, street, city, state, zip
    All values must be strings — no nulls.
    """
    return pd.DataFrame({
        "id"    : df["lab_id"].astype(str),
        "street": df["street_address"].fillna("").astype(str),
        "city"  : df["city"].fillna("").astype(str),
        "state" : df["state"].fillna("").astype(str),
        "zip"   : df["zip_code"].fillna("").astype(str),
    })


def run_batch_geocoding(df: pd.DataFrame) -> pd.DataFrame:
    """
    Send all addresses to the US Census Geocoder in one batch call.
 
    MENTOR NOTE — batch geocoding is always faster than row-by-row.
    The Census API accepts up to 10,000 addresses per request.
    At 1,862 rows, this completes in ~30 seconds vs 31 minutes for Nominatim.
 
    Returns df with latitude and longitude columns filled.
    """

    print(f"\n  Preparing {len(df):,} records for batch geocoding...")
    
    # 1. Create latitude/longitude columns if they don't exist
    if "latitude" not in df.columns:
        df["latitude"] = ""
    if "longitude" not in df.columns:
        df["longitude"] = ""
        
    # 2. Define chunk size (Census absolute limit is 10,000. We use 9,999)
    CHUNK_SIZE = 9999
    chunks = [df[i:i+CHUNK_SIZE] for i in range(0, df.shape[0], CHUNK_SIZE)]
    
    print(f"  Splitting dataset into {len(chunks)} chunks of ~{CHUNK_SIZE} rows.")
    
    for i, chunk in enumerate(chunks):
        print(f"\n  Processing Chunk {i+1}/{len(chunks)}...")
        
        # Checkpoint skip logic: if this chunk already has coordinates, skip it!
        if chunk["latitude"].replace("", pd.NA).notna().sum() > (len(chunk) * 0.8):
             print(f"    -> Chunk {i+1} already heavily geocoded. Skipping to save time.")
             continue
        
        # 3. Format exactly as Census requires: No Headers. [ID, Street, City, State, ZIP]
        batch_input = chunk[["lab_id", "street_address", "city", "state", "zip_code"]]
        
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.csv') as temp:
            batch_input.to_csv(temp.name, index=False, header=False)
            temp_filepath = temp.name
            
        try:
            # 4. Send the file directly to Census servers
            print(f"    -> Sending {len(chunk):,} addresses to US Census API (takes ~5-15 mins)...")
            results = cg.addressbatch(temp_filepath)
            
            # 5. Parse results back into our dataframe
            matched_in_chunk = 0
            for res in results:
                lab_id = res.get('id')
                lat = res.get('lat')
                lon = res.get('lon')
                
                if pd.notna(lat) and pd.notna(lon):
                    df.loc[df["lab_id"] == lab_id, "latitude"] = str(lat)
                    df.loc[df["lab_id"] == lab_id, "longitude"] = str(lon)
                    matched_in_chunk += 1
                    
            print(f"    ✓ Found coordinates for {matched_in_chunk:,} labs in Chunk {i+1}!")
            
            # 6. SAVE PROGRESS IMMEDIATELY. If the script crashes, we don't lose this chunk!
            df.to_csv(GEOCODED_FILE, index=False)
            
            # Rest for 15 seconds so the government server doesn't block us
            time.sleep(15) 
            
        except Exception as e:
            print(f"   Chunk {i+1} Failed: {e}")
            print("    Waiting 60 seconds before trying the next chunk...")
            time.sleep(60)
        finally:
            if os.path.exists(temp_filepath):
                os.remove(temp_filepath)
 
    return df


def geocode_quality_report(df: pd.DataFrame):
    """Print a summary of geocoding results."""
    total    = len(df)
    matched  = df["latitude"].ne("").sum()
    missing  = df["latitude"].eq("").sum()
 
    print(f"\n  ── Geocoding quality report ──")
    print(f"  Total records   : {total:,}")
    print(f"  Geocoded        : {matched:,}  ({matched/total*100:.1f}%)")
    print(f"  Not geocoded    : {missing:,}  ({missing/total*100:.1f}%)")
 
    if matched / total >= 0.80:
        print(f"  Meets success criterion (≥80% geocoded)")
    else:
        print(f"  Below 80% target — review unmatched addresses")
 
    if missing > 0:
        print(f"\n  Sample unmatched addresses:")
        sample = df[df["latitude"] == ""][
            ["lab_name", "street_address", "city", "zip_code"]
        ].head(10)
        print(sample.to_string(index=False))


# ── Main ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("="*55)
    print("Script 03: Fix Issues + Geocode — National Batch")
    print("="*55)

    # ── Part A: Fix data issues ──
    print("\n── Part A: Fix data issues ──")
    
    # Check if a partially geocoded file already exists so we can resume!
    if os.path.exists(GEOCODED_FILE):
        print(f"  Found existing geocoded file. Resuming progress...")
        df = pd.read_csv(GEOCODED_FILE, dtype=str).fillna("")
    else:
        df = pd.read_csv(CLEAN_FILE, dtype=str).fillna("")
        print(f"  Loaded {len(df):,} rows from clean file")
        
        # Only run these fixes if we are starting fresh
        # (Assuming you still have fix_specialty_codes and fix_address_formatting defined above)
        df = fix_specialty_codes(df)
        df = fix_address_formatting(df)
        df.to_csv(CLEAN_FILE, index=False)
        print(f"  Saved fixes back to: {CLEAN_FILE}")

    # ── Part B: Geocode ──
    print("\n── Part B: Geocoding ──")
    df = run_batch_geocoding(df)
    
    # Calculate final stats
    total = len(df)
    matched = df["latitude"].ne("").sum()
    missing = df["latitude"].eq("").sum()
    
    print("\n" + "="*55)
    print("GEOCODING REPORT")
    print("="*55)
    print(f"  Total records: {total:,}")
    print(f"  Geocoded     : {matched:,}  ({matched/total*100:.1f}%)")
    print(f"  Missing      : {missing:,}  ({missing/total*100:.1f}%)")
    
    print(f"\n✓ Saved final geocoded file to: {GEOCODED_FILE}")