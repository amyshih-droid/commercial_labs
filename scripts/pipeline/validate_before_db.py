import pandas as pd
import logging
import argparse
from pathlib import Path
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

def validate_dataset(file_path: Path):
    if not file_path.exists():
        logger.error(f"File not found: {file_path}")
        sys.exit(1)

    logger.info(f"Loading data from {file_path}...")
    df = pd.read_csv(file_path, dtype=str)
    
    total_rows = len(df)
    logger.info(f"Total records to validate: {total_rows}")
    
    if total_rows == 0:
        logger.error("Dataset is empty.")
        sys.exit(1)

    # 1. Check Required Base Fields
    logger.info("-" * 40)
    logger.info("Check Required Base Fields")
    logger.info("-" * 40)
    required_fields = ["company_name", "entity_id"]
    fatal_errors = False
    
    for field in required_fields:
        if field not in df.columns:
            logger.error(f"CRITICAL: Missing required column '{field}'")
            fatal_errors = True
        else:
            missing_count = df[field].isna().sum()
            if missing_count > 0:
                logger.error(f"CRITICAL: {missing_count} rows are missing '{field}'")
                fatal_errors = True

    # 2. Check for Duplicate IDs
    logger.info("-" * 40)
    logger.info("Check for Duplicate IDs")
    logger.info("-" * 40)

    if "entity_id" in df.columns:
        dupes = df.duplicated(subset=["source_record_id"]).sum()
        if dupes > 0:
            logger.error(f"CRITICAL: Found {dupes} duplicate source_record_ids.")
            fatal_errors = True

    if fatal_errors:
        logger.error("Validation FAILED due to critical errors. Do not ingest.")
        sys.exit(1)

    # 3. Calculate Fill Rates
    logger.info("-" * 40)
    logger.info("FIELD FILL RATES (Quality Check)")
    logger.info("-" * 40)
    
    # Fields we want to measure quality on
    target_fields = [
        "website_url", 
        "google_geocode_formatted_address",
        "contact_name",
        "contact_email", 
        "is_commercial", 
        "is_gmp_facility",
        "google_latitude",
        "google_longitude",
        "services", 
    ]
    
    for field in target_fields:
        if field in df.columns:
            filled_count = df[field].notna().sum()
            fill_rate = (filled_count / total_rows) * 100
            logger.info(f"{field:<18}: {filled_count}/{total_rows} ({fill_rate:.1f}%)")
        else:
            logger.warning(f"{field:<18}: Column completely missing from dataset!")

    logger.info("-" * 40)
    logger.info("Validation PASSED. Data is ready for database ingestion.")

    # 4. Status Breakdown
    logger.info("-" * 40)
    logger.info("STATUS BREAKDOWN (per AI field):")
    logger.info("-" * 40)
    
    # Find all columns that end with '_status'
    status_columns = [col for col in df.columns if str(col).endswith('_status')]
    
    if not status_columns:
        logger.info("  No status columns found in the dataset.")
    
    for col in status_columns:
        field_name = col.replace('_status', '')
        logger.info(f"\n  {field_name}:")
        
        statuses = df[col].dropna()
        if len(statuses) == 0:
            logger.info("      (No status data available)")
            continue
        
        # Collapse the parenthetical diagnostic detail for a clean summary count
        simplified = statuses.str.replace(r"\s*\(.*\)", "", regex=True)
        counts = simplified.value_counts()
        
        for status, count in counts.items():
            logger.info(f"    {count:>5}  {status}")
        
    logger.info("Validation PASSED. Data is ready for database ingestion.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate output data before DB insertion.")
    parser.add_argument("--domain", required=True, choices=["pharma", "env"], help="Domain to validate (pharma or env)")
    args = parser.parse_args()

    # Determine file path based on domain pattern
    file_path = Path(f"data/{args.domain}/auto_master_entities_geocoded.csv")
    
    validate_dataset(file_path)