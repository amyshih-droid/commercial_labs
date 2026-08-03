"""
FDA HCT Connector

Responsibilities
----------------
- Connect to FDA website
- Extract commercial HCT/P establishment table
- Return a pandas DataFrame

This connector DOES NOT:
- save files
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
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Config Loader
# ------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path("config/connectors/fda_hct.yaml")


def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> dict:
    """Loads configuration from YAML file."""
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

FDA_URL = (
    "https://www.accessdata.fda.gov/scripts/"
    "cber/CFAppsPub/tiss/Index.cfm"
)

REQUIRED_COLUMNS = {
    "FEI",
    "Establishment Status",
}


# ------------------------------------------------------------------
# Driver
# ------------------------------------------------------------------

def create_driver(headless: bool = True) -> webdriver.Chrome:
    """
    Create a Chrome webdriver.

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
    """Wait until an element is present."""

    return WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((by, value))
    )

# ------------------------------------------------------------------
# FDA Navigation
# ------------------------------------------------------------------

def open_search_page(driver: webdriver.Chrome, config: dict) -> None:

    driver.get(config["url"])

    dropdown = wait_for(driver, By.XPATH, "//select[@name='Establishment Function' or contains(@name, 'Function')]")
    process_option = dropdown.find_element(By.XPATH, "//option[@value='e']")
    process_option.click()

    try:
        records_dropdown = Select(driver.find_element(By.NAME, "nrecords"))
        records_per_page = str(config.get("records_per_page", 100))
        records_dropdown.select_by_value(records_per_page)
    except Exception as e:
        logger.warning("Could not set page size to 100: %s", e)

    driver.find_element(By.XPATH, "//input[@type='submit']").click()
    wait_for(driver, By.TAG_NAME, "table")


# ------------------------------------------------------------------
# Parsing
# ------------------------------------------------------------------

def parse_current_page(driver: webdriver.Chrome, required_cols: list[str]) -> pd.DataFrame:
    tables = pd.read_html(io.StringIO(driver.page_source))

    for table in tables:
        table.columns = [str(c).strip().replace("\n", " ") for c in table.columns]

        if any(col in table.columns for col in required_cols):
            return table

    raise RuntimeError("FDA data table not found.")


# ------------------------------------------------------------------
# Pagination
# ------------------------------------------------------------------

def scrape_pages(driver: webdriver.Chrome, required_cols: list[str]) -> pd.DataFrame:
    pages = []
    while True:
        pages.append(parse_current_page(driver, required_cols))
        try:
            driver.find_element(By.ID, "Display next").click()
            wait_for(driver, By.TAG_NAME, "table")
        except Exception:
            break

    return pd.concat(pages, ignore_index=True)


# ------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------

def validate(df: pd.DataFrame, config: dict) -> None:

    if df.empty:
        raise ValueError("FDA connector returned zero rows.")

    required_cols = set(config.get("required_columns", []))
    missing = required_cols - set(df.columns)

    if missing:
        raise ValueError(f"Missing required columns: {missing}")
    
    if not df.columns.is_unique:
        raise ValueError("Duplicate columns detected.")

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
    """Extract FDA HCT establishments driven by YAML config."""
    config = load_config(config_path)

    driver = create_driver(headless)

    try:
        open_search_page(driver, config)
        df = scrape_pages(driver, config.get("required_columns", []))
        validate(df, config)
        logger.info("Extracted %d FDA establishments.", len(df))
        return df

    finally:
        driver.quit()


# ------------------------------------------------------------------
# Development entry point
# ------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    df = extract(headless=False)
    logger.info(df.head())
    logger.info("Rows: %d", len(df))