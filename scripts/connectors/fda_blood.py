"""
FDA Blood Establishment Registration (BER) Connector

Responsibilities
----------------
- Connect to FDA Blood Establishment Registration database
- Execute queries across configured establishment types and country parameters
- Extract and combine raw tabular results
- Return a single pandas DataFrame

This connector DOES NOT:
- save files to disk
- standardize columns
- deduplicate
- perform entity resolution
- call LLMs
- geocode
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
import pandas as pd
import yaml

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Config Loader
# ------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path("config/connectors/fda_blood.yaml")


def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> dict:
    """Loads configuration from YAML file."""
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ------------------------------------------------------------------
# Driver Initialization
# ------------------------------------------------------------------

def create_driver(headless: bool = True) -> webdriver.Chrome:
    """
    Create a Chrome webdriver instance.

    Parameters
    ----------
    headless : bool
        Run browser without GUI.
    """
    options = Options()

    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    return webdriver.Chrome(options=options)


def wait_for(
    driver: webdriver.Chrome,
    by: By,
    value: str,
    timeout: int = 20,
):
    """Wait until an element is present in the DOM."""
    return WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((by, value))
    )


# ------------------------------------------------------------------
# FDA Search Form Navigation
# ------------------------------------------------------------------

def open_search_page(
    driver: webdriver.Chrome,
    config: dict,
    type_value: str,
) -> None:
    """Navigates to the search portal and submits query for a target establishment type."""
    driver.get(config["url"])

    # Select target establishment type
    est_dropdown_elem = wait_for(driver, By.NAME, "EstablishmentType")
    est_dropdown = Select(est_dropdown_elem)
    est_dropdown.select_by_value(type_value)

    # Configure Country restriction if specified
    country_code = config.get("country", "US")
    try:
        country_dropdown = Select(driver.find_element(By.NAME, "Country"))
        if country_dropdown.is_multiple:
            country_dropdown.deselect_all()
        country_dropdown.select_by_value(country_code)
    except Exception as e:
        logger.warning("Could not set country filter to '%s': %s", country_code, e)

    # Set records per page layout option
    try:
        records_dropdown = Select(driver.find_element(By.NAME, "nrecords"))
        records_per_page = str(config.get("records_per_page", 100))
        records_dropdown.select_by_value(records_per_page)
    except Exception as e:
        logger.warning("Could not set records_per_page to 100: %s", e)

    # Submit query
    driver.find_element(By.XPATH, "//input[@type='submit']").click()
    wait_for(driver, By.TAG_NAME, "table")


# ------------------------------------------------------------------
# HTML Parsing
# ------------------------------------------------------------------

def parse_current_page(driver: webdriver.Chrome, required_cols: list[str]) -> pd.DataFrame:
    """Parses HTML table payload from the current page source."""
    tables = pd.read_html(io.StringIO(driver.page_source))

    for table in tables:
        table.columns = [str(c).strip().replace("\n", " ") for c in table.columns]

        if any(col in table.columns for col in required_cols):
            return table

    raise RuntimeError("FDA data table not found on the page.")


# ------------------------------------------------------------------
# Pagination Loop
# ------------------------------------------------------------------

def scrape_pages(driver: webdriver.Chrome, required_cols: list[str]) -> pd.DataFrame:
    """Iterates through paginated web tables until exhaustion."""
    pages = []
    while True:
        pages.append(parse_current_page(driver, required_cols))
        try:
            next_button = driver.find_element(By.ID, "Display next")
            next_button.click()
            wait_for(driver, By.TAG_NAME, "table")
        except (NoSuchElementException, TimeoutException):
            break

    return pd.concat(pages, ignore_index=True)


# ------------------------------------------------------------------
# Data Validation
# ------------------------------------------------------------------

def validate(df: pd.DataFrame, config: dict) -> None:
    """Validates extracted DataFrame structure and key fields."""
    if df.empty:
        raise ValueError("FDA BER connector returned zero rows.")

    required_cols = set(config.get("required_columns", []))
    missing = required_cols - set(df.columns)

    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    if not df.columns.is_unique:
        raise ValueError("Duplicate columns detected in extracted DataFrame.")

    primary_keys = config.get("primary_key", [])
    for pk in primary_keys:
        if pk in df.columns and df[pk].isna().all():
            raise ValueError(f"Primary key column '{pk}' is completely empty.")


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def extract(
    *,
    headless: bool = True,
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> pd.DataFrame:
    """Extract FDA Blood Establishments driven by YAML config."""
    config = load_config(config_path)
    establishment_types = config.get("establishment_types", {})

    driver = create_driver(headless)
    collected_frames = []

    try:
        for type_value, type_name in establishment_types.items():
            logger.info("Starting harvest for establishment type: %s (%s)", type_name, type_value)
            open_search_page(driver, config, type_value)
            df_type = scrape_pages(driver, config.get("required_columns", []))
            collected_frames.append(df_type)

        if not collected_frames:
            raise ValueError("No establishment types were processed.")

        combined_df = pd.concat(collected_frames, ignore_index=True)
        validate(combined_df, config)
        logger.info("Successfully extracted %d total FDA blood establishments.", len(combined_df))
        return combined_df

    finally:
        driver.quit()


# ------------------------------------------------------------------
# Development Entry Point
# ------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    df = extract(headless=False)
    logger.info(df.head())
    logger.info("Total Rows: %d", len(df))