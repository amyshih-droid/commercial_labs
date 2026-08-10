import logging
import datetime
import subprocess
from pathlib import Path
import sys
import argparse
import time

# Connectors
from connectors import fda_hct, fda_blood, biopharmguy_therapeutics, biopharmguy_cmos, biopharmguy_cros, nih_reporter, form_d, ycombinator, fda_drug_registration, env_labs
from storage.snapshot_manager import save_snapshot

# Pipeline Imports
sys.path.append('scripts/pipeline')
from standardize import standardize_source
from combine_standardized import combine_csvs
from entity_resolution import run_entity_resolution
from llm_infer import run_llm_infer
from dedup_and_tag_entities import run_dedup_and_tag
from geocode_entities_google import run_geocode_google
from validate_before_db import validate_dataset

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DOMAIN_CONNECTORS = {
    "pharma":{
    #     "fda_hct": fda_hct,
    #     "fda_blood": fda_blood,
    #     "biopharmguy_therapeutics": biopharmguy_therapeutics,
    #     "biopharmguy_cmos": biopharmguy_cmos,
    #     "biopharmguy_cros": biopharmguy_cros,
    #     "nih_reporter": nih_reporter,
    #     "form_d": form_d,
    #     "ycombinator": ycombinator,
    #     "fda_drug_registration": fda_drug_registration,
    },
    "env": {
        "env_labs": env_labs,
    }
}   

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", choices=["pharma", "env"], required=True)
    args = parser.parse_args()

    start_time = time.time()

    domain = args.domain
    DATA_DIR = Path(f"data/{domain}")
    active_connectors = DOMAIN_CONNECTORS[domain]

    (DATA_DIR / "raw").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "standardized").mkdir(parents=True, exist_ok=True)

    for source_name, connector in active_connectors.items():
        logger.info("==========================================")
        logger.info("Starting extraction for source: {source_name}")
        logger.info("==========================================")

        try:
            # 1. Extract data in memory
            df = connector.extract(headless=True)

            # 2. Save parquet snapshot explicitly to data/env/raw/...
            today_str = datetime.date.today().strftime("%Y-%m-%d")
            raw_dir = DATA_DIR / "raw" / source_name
            raw_dir.mkdir(parents=True, exist_ok=True)
            
            raw_parquet_path = raw_dir / f"{today_str}.parquet"
            df.to_parquet(raw_parquet_path, index=False)
            logger.info("Successfully saved snapshot for %s (%d rows) to %s", source_name, len(df), raw_parquet_path)

            # 3. Run Standardization
            config_name = "biopharmguy" if source_name.startswith("biopharmguy") else source_name
            logger.info("Running standardization using config: %s.yaml", config_name)

            output_csv_path = DATA_DIR / "standardized" / f"{source_name}_standardized.csv"

            standardize_source(
                source_key=config_name,
                raw_file=Path(raw_parquet_path),
                out_file=Path(output_csv_path),
                source_name_override=source_name
            )
        
        except subprocess.CalledProcessError as e:
            logger.error("Standardization script failed for '%s': %s", source_name, e)
        except Exception as e:
            logger.error("Failed to extract source '%s': %s", source_name, e, exc_info=True)

    # =========================================================================
    # 4. Combine all standardized files into a single raw table
    # =========================================================================
    logger.info("==========================================")
    logger.info("Starting combination of all standardized files")
    logger.info("==========================================")

    try:
        input_directory = DATA_DIR / "standardized"
        output_file = DATA_DIR / "auto_combined_raw.csv"

        # Call the native Python function directly!
        combine_csvs(input_directory, output_file)
        logger.info("Successfully combined all files into %s", output_file)
    except Exception as e:
        logger.error("Unexpected error during combination: %s", e, exc_info=True)

    # =========================================================================
    # 5. Entity Resolution
    # =========================================================================
    logger.info("==========================================")
    logger.info("Starting Entity Resolution")
    logger.info("==========================================")

    try:
        in_file = DATA_DIR / "auto_combined_raw.csv" 
        out_entities = DATA_DIR / "auto_master_entities.csv"
        out_crosswalk = DATA_DIR / "auto_entity_crosswalk.csv"

        # Call the native Python function directly!
        run_entity_resolution(
            input_file=in_file,
            out_entities=out_entities,
            out_crosswalk=out_crosswalk,
            name_threshold=90,
            street_threshold=75
        )
        logger.info("Successfully completed Entity Resolution!")
    except Exception as e:
        logger.error("Unexpected error during entity resolution: %s", e, exc_info=True)
    
    # =========================================================================
    # 6. LLM Inference & Data Enrichment
    # =========================================================================
    logger.info("==========================================")
    logger.info("Starting LLM Field Inference & Enrichment")
    logger.info("==========================================")

    try:
        in_master = DATA_DIR / "auto_master_entities.csv"
        out_enriched = DATA_DIR / "auto_master_entities_enriched.csv"
        domain_llm_cache = DATA_DIR / "llm_cache"

        run_llm_infer(
            input_file=in_master,
            output_file=out_enriched,
            cache_dir=domain_llm_cache,
            # if new field is needed by llm, add it here
            fields=["website_url", "address_street", "contact_name", "contact_email", "is_gmp_facility", "is_commercial", "services"],
            limit=None  # Set to e.g. 10 for a small subset
        )

        logger.info("Successfully completed LLM Inference!")
    except Exception as e:
        logger.error("Unexpected error during LLM inference: %s", e, exc_info=True)

    # =========================================================================
    # 7. Dedup, Tag Entities & Build Composite Names
    # =========================================================================
    logger.info("==========================================")
    logger.info("Starting Dedup & Entity Tagging")
    logger.info("==========================================")

    try:
        in_enriched = DATA_DIR / "auto_master_entities_enriched.csv"
        out_tagged = DATA_DIR / "auto_master_entities_tagged.csv"

        run_dedup_and_tag(
            input_file=in_enriched,
            output_file=out_tagged
        )

        logger.info("Successfully completed Dedup & Tagging!")
    except Exception as e:
        logger.error("Unexpected error during dedup & tagging: %s", e, exc_info=True)

    #  =========================================================================
    # 8. Geocode Entities (Google)
    # =========================================================================
    logger.info("==========================================")
    logger.info("Starting Google Maps Geocoding")
    logger.info("==========================================")

    try:
        in_tagged = DATA_DIR / "auto_master_entities_tagged.csv"
        out_geocoded = DATA_DIR / "auto_master_entities_geocoded.csv"
        domain_cache = DATA_DIR / "geocode_cache_google.json"

        run_geocode_google(
            input_file=in_tagged,
            output_file=out_geocoded,
            only_missing=True, # Will safely skip already geocoded rows
            limit=None
        )

        logger.info("Successfully completed Google Geocoding!")
    except Exception as e:
        logger.error("Unexpected error during Google geocoding: %s", e, exc_info=True)
    
    # =========================================================================
    # 9. Final Validation
    # =========================================================================
    logger.info("==========================================")
    logger.info("Starting Final Data Validation")
    logger.info("==========================================")

    try:
        final_output = DATA_DIR / "auto_master_entities_geocoded.csv"
        validate_dataset(final_output)
    except Exception as e:
        logger.error("Validation failed! %s", e)

    end_time = time.time()
    elapsed_seconds = end_time - start_time
    elapsed_formatted = time.strftime("%H:%M:%S", time.gmtime(elapsed_seconds))

    logger.info(f"PIPELINE COMPLETION TIME: {elapsed_formatted}")

    

if __name__ == "__main__":
    main()