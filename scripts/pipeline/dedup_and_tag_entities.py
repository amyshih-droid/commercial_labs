"""
06_dedup_and_tag_entities.py - Cleans street addresses, normalizes company
and location fields, tags HQ vs. Branch facilities, and creates a unique
composite 'name' column with ZIP-based and fail-safe street disambiguation.

Usage:
    python3 scripts/pipeline/06_dedup_and_tag_entities.py \
        --in data/phase2_pharma/auto_master_entities_enriched.csv \
        --out data/phase2_pharma/auto_master_entities_tagged.csv
"""

import argparse
from pathlib import Path
import re

import pandas as pd

# Standard US State Postal Codes for cross-state validation
US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC"
}


def normalize_company_name(name: str, city: str = "", state: str = "") -> str:
    """Strips legal suffixes, hyphens, DBA noise, and trailing branch city/state modifiers."""
    if not isinstance(name, str) or not name.strip():
        return ""

    s = name.strip()

    # Convert hyphens/dashes to spaces (fixes 'Care-Aurora' vs 'Care - Aurora')
    s = re.sub(r"[-–—]", " ", s)

    # Remove DBA clauses
    s = re.sub(r"\b(dba|d/b/a)\b.*", "", s, flags=re.IGNORECASE)

    # Strip legal corporate suffixes
    s = re.sub(
        r",?\s*\b(Inc|LLC|PC|Corp|Corporation|Ltd|LLP|LP|PLLC)\b.*",
        "",
        s,
        flags=re.IGNORECASE,
    )

    # Strip punctuation & collapse extra spaces
    s = re.sub(r"[^\w\s]", "", s)
    s = " ".join(s.split())

    # Dynamic city stripping
    if city and isinstance(city, str):
        clean_city = re.sub(r"[^\w\s]", "", city).strip()
        if clean_city:
            s = re.sub(
                rf"\b{re.escape(clean_city)}$", "", s, flags=re.IGNORECASE
            ).strip()

    # Dynamic state stripping
    if state and isinstance(state, str):
        clean_state = re.sub(r"[^\w\s]", "", state).strip()
        if clean_state and len(clean_state) == 2:
            s = re.sub(
                rf"\b{re.escape(clean_state)}$", "", s, flags=re.IGNORECASE
            ).strip()

    return " ".join(s.split())


def clean_and_validate_street(row: pd.Series) -> str:
    """Removes hallucinated cross-state addresses and strips trailing city/state text."""
    street = row.get("address_street")
    city = str(row.get("address_city", "") or "").strip()
    target_state = str(row.get("address_state", "") or "").strip().upper()

    if not isinstance(street, str) or not street.strip():
        return None

    cleaned = street.strip()

    # Cross-State Hallucination Check
    found_states = re.findall(r"\b([A-Z]{2})\b", cleaned)
    for st in found_states:
        if st in US_STATES and target_state and st != target_state:
            return None

    # Strip trailing City, State, and Zip format bleed
    if city and target_state:
        city_state_pattern = rf",?\s*{re.escape(city)},?\s*{re.escape(target_state)}.*$"
        cleaned = re.sub(city_state_pattern, "", cleaned, flags=re.IGNORECASE)

    cleaned = re.sub(r",?\s*[A-Za-z\s]+,?\s*[A-Z]{2}\s*\d{5}(-\d{4})?\s*$", "", cleaned)
    cleaned = cleaned.strip(" ,")
    return cleaned if cleaned else None


def tag_hq_or_branch(group: pd.DataFrame) -> pd.DataFrame:
    """Labels rows within a multi-location company group as HQ or Branch."""
    group = group.copy()

    # Single-location company -> HQ
    if len(group) == 1:
        group["is_hq"] = True
        return group

    # Heuristic 1: Check for explicit HQ keywords
    hq_keyword_pattern = r"\b(hq|headquarters|corporate|main|executive)\b"
    has_keyword = group["address_street"].fillna("").str.contains(
        hq_keyword_pattern, case=False
    ) | group["company_name"].fillna("").str.contains(
        hq_keyword_pattern, case=False
    )

    # Heuristic 2: Check for SEC CIK
    has_cik = group["cik"].notna() & (group["cik"].str.strip() != "")

    group["is_hq"] = False

    if has_keyword.any():
        group.loc[group[has_keyword].index[0], "is_hq"] = True
    elif has_cik.any():
        group.loc[group[has_cik].index[0], "is_hq"] = True

    return group


def normalize_city_spelling(city: str) -> str:
    """Normalizes minor city spelling differences (e.g. Harbour -> Harbor)."""
    if not isinstance(city, str):
        return ""
    c = city.lower().strip()
    c = re.sub(r"\bharbour\b", "harbor", c)
    c = re.sub(r"\bsaint\b", "st", c)
    return re.sub(r"[^\w\s]", "", c)


def build_composite_name(
    row: pd.Series,
    zip_counts: dict[tuple[str, str], int],
    city_counts: dict[tuple[str, str], int],
) -> str:
    """Constructs composite name with street disambiguation for local collisions."""
    clean_company = row["company_name_clean"]
    city = str(row.get("address_city", "") or "").strip().title()
    street = str(row.get("address_street", "") or "").strip()
    zip_code = str(row.get("address_zip", "") or "").strip()[:5]
    norm_city = normalize_city_spelling(city)

    if not city:
        city = "Site"

    # Collision checks using both ZIP code and normalized city spelling
    zip_collision = zip_counts.get((clean_company, zip_code), 0) > 1 if zip_code else False
    city_collision = city_counts.get((clean_company, norm_city), 0) > 1

    has_local_collision = zip_collision or city_collision

    if row.get("is_hq", False):
        prefix = "HQ Lab"
    elif has_local_collision and street:
        prefix = f"{street}, {city} Lab"
    else:
        prefix = f"{city} Lab"

    return f"{prefix} @ {clean_company}"

def run_dedup_and_tag(input_file: Path, output_file: Path):
    """Helper entry point for running dedup and tagging directly from run_pipeline.py"""
    if not input_file.exists():
        raise FileNotFoundError(f"Input file not found: {input_file}")

    print(f"Reading input file: {input_file}")
    df = pd.read_csv(input_file, dtype=str)

    print("Cleaning and validating street addresses...")
    df["address_street"] = df.apply(clean_and_validate_street, axis=1)

    print("Normalizing company names...")
    df["company_name_clean"] = df.apply(
        lambda r: normalize_company_name(
            r.get("company_name"), r.get("address_city"), r.get("address_state")
        ),
        axis=1,
    )

    print("Tagging HQ vs. Branch locations...")
    df = (
        df.groupby("company_name_clean", group_keys=False)
        .apply(tag_hq_or_branch)
        .reset_index(drop=True)
    )

    # Pre-calculate counts by 5-digit ZIP code and normalized city
    df["_zip5"] = df["address_zip"].fillna("").str.strip().str[:5]
    df["_norm_city"] = df["address_city"].apply(normalize_city_spelling)

    zip_counts = df.groupby(["company_name_clean", "_zip5"]).size().to_dict()
    city_counts = df.groupby(["company_name_clean", "_norm_city"]).size().to_dict()

    print("Building composite 'name' column...")
    df["name"] = df.apply(
        lambda r: build_composite_name(r, zip_counts, city_counts), axis=1
    )

    # FAIL-SAFE PASS: Disambiguate any lingering identical composite names
    dup_mask = df.duplicated(subset=["name"], keep=False)
    if dup_mask.any():
        print(f"Resolving {dup_mask.sum()} duplicate 'name' entries with fail-safe street pass...")
        for idx in df[dup_mask].index:
            street = str(df.loc[idx, "address_street"] or "").strip()
            city = str(df.loc[idx, "address_city"] or "").strip().title()
            company = df.loc[idx, "company_name_clean"]
            if street:
                df.loc[idx, "name"] = f"{street}, {city} Lab @ {company}"

    # Clean up temporary helper columns
    df.drop(columns=["_zip5", "_norm_city"], inplace=True)

    output_file.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_file, index=False)

    hq_count = df["is_hq"].sum()
    branch_count = len(df) - hq_count

    print(f"\nProcessed total: {len(df)} rows")
    print(f"Tagged: {hq_count} HQ locations, {branch_count} Branch locations")
    print(f"Wrote to {output_file}")

def main():
    parser = argparse.ArgumentParser(
        description="Normalize company names, clean addresses, and tag HQ vs. Branch facilities"
    )
    parser.add_argument(
        "--in",
        dest="input_file",
        type=Path,
        default=Path("data/phase2_pharma/master_entities_llm_infer_v2.csv"),
        help="Path to input CSV file",
    )
    parser.add_argument(
        "--out",
        dest="output_file",
        type=Path,
        default=Path("data/phase2_pharma/master_entities_tagged.csv"),
        help="Path to output CSV file",
    )
    args = parser.parse_args()

    if not args.input_file.exists():
        raise FileNotFoundError(f"Input file not found: {args.input_file}")

    print(f"Reading input file: {args.input_file}")
    df = pd.read_csv(args.input_file, dtype=str)

    print("Cleaning and validating street addresses...")
    df["address_street"] = df.apply(clean_and_validate_street, axis=1)

    print("Normalizing company names...")
    df["company_name_clean"] = df.apply(
        lambda r: normalize_company_name(
            r.get("company_name"), r.get("address_city"), r.get("address_state")
        ),
        axis=1,
    )

    print("Tagging HQ vs. Branch locations...")
    df = (
        df.groupby("company_name_clean", group_keys=False)
        .apply(tag_hq_or_branch)
        .reset_index(drop=True)
    )

    # Pre-calculate counts by 5-digit ZIP code and normalized city
    df["_zip5"] = df["address_zip"].fillna("").str.strip().str[:5]
    df["_norm_city"] = df["address_city"].apply(normalize_city_spelling)

    zip_counts = df.groupby(["company_name_clean", "_zip5"]).size().to_dict()
    city_counts = df.groupby(["company_name_clean", "_norm_city"]).size().to_dict()

    print("Building composite 'name' column...")
    df["name"] = df.apply(
        lambda r: build_composite_name(r, zip_counts, city_counts), axis=1
    )

    # FAIL-SAFE PASS: Disambiguate any lingering identical composite names
    dup_mask = df.duplicated(subset=["name"], keep=False)
    if dup_mask.any():
        print(f"Resolving {dup_mask.sum()} duplicate 'name' entries with fail-safe street pass...")
        for idx in df[dup_mask].index:
            street = str(df.loc[idx, "address_street"] or "").strip()
            city = str(df.loc[idx, "address_city"] or "").strip().title()
            company = df.loc[idx, "company_name_clean"]
            if street:
                df.loc[idx, "name"] = f"{street}, {city} Lab @ {company}"

    # Clean up temporary helper columns
    df.drop(columns=["_zip5", "_norm_city"], inplace=True)

    args.output_file.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output_file, index=False)

    hq_count = df["is_hq"].sum()
    branch_count = len(df) - hq_count

    print(f"\nProcessed total: {len(df)} rows")
    print(f"Tagged: {hq_count} HQ locations, {branch_count} Branch locations")
    print(f"Wrote to {args.output_file}")


if __name__ == "__main__":
    main()