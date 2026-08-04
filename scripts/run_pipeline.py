import logging
import subprocess
import datetime
from connectors import fda_hct, fda_blood, biopharmguy_therapeutics, biopharmguy_cmos, biopharmguy_cros, nih_reporter, form_d, ycombinator, fda_drug_registration
from storage.snapshot_manager import save_snapshot

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CONNECTORS = {
    "fda_hct": fda_hct,
    "fda_blood": fda_blood,
    # "biopharmguy_therapeutics": biopharmguy_therapeutics,
    # "biopharmguy_cmos": biopharmguy_cmos,
    # "biopharmguy_cros": biopharmguy_cros,
    # "nih_reporter": nih_reporter,
    # "form_d": form_d,
    # "ycombinator": ycombinator,
    "fda_drug_registration": fda_drug_registration,
}   

def main():
    for source_name, connector in CONNECTORS.items():
        logger.info("==========================================")
        logger.info("Starting extraction for source: %s", source_name)
        logger.info("==========================================")

        try:
            # 1. Extract data in memory
            df = connector.extract(headless=True)

            # 2. Save parquet snapshot to data/phase2_pharma/raw/<source>/YYYY-MM-DD.parquet
            save_snapshot(dataframe=df, source=source_name)
            logger.info("Successfully processed %s (%d rows)", source_name, len(df))

            # 3. Run Standardization
            logger.info("Running standardization using config: %s.yaml", source_name)

            today_str = datetime.date.today().strftime("%Y-%m-%d")
            raw_parquet_path = f"data/phase2_pharma/raw/{source_name}/{today_str}.parquet"

            subprocess.run([
                "python3", "scripts/pipeline/01_standardize.py", 
                "--source", source_name,
                "--raw-file", raw_parquet_path
            ], check=True)
            logger.info("Standardization complete for %s", source_name)
        
        except subprocess.CalledProcessError as e:
            logger.error("Standardization script failed for '%s': %s", source_name, e)
        except Exception as e:
            logger.error("Failed to extract source '%s': %s", source_name, e, exc_info=True)

if __name__ == "__main__":
    main()