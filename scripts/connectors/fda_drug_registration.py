"""
FDA Drug Establishment Current Registration Site (DECRS) Connector

Responsibilities
----------------
- Parse FDA DECRS landing page HTML to dynamically discover active ZIP download link
- Download and unpack bulk ZIP dataset in memory
- Load raw registration dataset into a pandas DataFrame using Latin-1 encoding

This connector DOES NOT:
- save files to disk
- standardize final schema columns
- deduplicate across sources
- perform entity resolution
- call LLMs
- geocode
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
import zipfile
import pandas as pd
import requests
import yaml
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("config/connectors/fda_drug_registration.yaml")


def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> dict:
    """Loads configuration from YAML file."""
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def discover_download_url(page_url: str, headers: dict) -> str:
    """Scrapes the landing page HTML to find the annual registration status ZIP link."""
    logger.info("Fetching FDA DECRS landing page: %s", page_url)
    resp = requests.get(page_url, headers=headers, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    download_link = None

    for a_tag in soup.find_all("a", href=True):
        text = a_tag.get_text(strip=True).lower()
        if "annual registration status download file" in text or ("annual registration status" in text and "zip" in text):
            if "excluded" not in text:
                download_link = a_tag["href"]
                break

    if not download_link:
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"].lower()
            if "/media/" in href and "download" in href:
                download_link = a_tag["href"]
                break

    if not download_link:
        raise ValueError(f"Could not locate DECRS ZIP download link on page: {page_url}")

    if download_link.startswith("/"):
        download_link = f"https://www.fda.gov{download_link}"

    logger.info("Discovered active download URL: %s", download_link)
    return download_link


def extract(*, headless: bool = True, config_path: Path = DEFAULT_CONFIG_PATH) -> pd.DataFrame:
    """Extract FDA DECRS registered drug establishments directly from bulk ZIP."""
    config = load_config(config_path)
    page_url = config.get(
        "page_url",
        "https://www.fda.gov/drugs/drug-approvals-and-databases/drug-establishments-current-registration-site-decrs",
    )
    headers = config.get(
        "headers",
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        },
    )

    # 1. Dynamically locate active ZIP URL
    download_url = discover_download_url(page_url, headers)

    # 2. Download binary payload
    logger.info("Downloading FDA DECRS bulk ZIP dataset...")
    response = requests.get(download_url, headers=headers, timeout=60)
    response.raise_for_status()

    # 3. Extract and parse in memory with Latin-1 encoding
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        namelist = zf.namelist()
        if not namelist:
            raise ValueError("FDA DECRS ZIP archive is empty.")

        target_file = next((f for f in namelist if f.endswith((".csv", ".tsv", ".txt"))), namelist[0])
        logger.info("Parsing dataset file '%s' from ZIP payload...", target_file)

        with zf.open(target_file) as extracted_file:
            # latin1 encoding handles legacy FDA text dumps with special/accented characters
            df = pd.read_csv(
                extracted_file,
                sep='\t',
                index_col=False,
                dtype=str,
                encoding="latin1",
            )

    if df.empty:
        raise ValueError("FDA DECRS DataFrame is empty.")

    logger.info("Successfully extracted %d FDA registered drug establishments.", len(df))
    return df


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    df = extract()
    logger.info(df.head())
    logger.info("Total Rows Extracted: %d", len(df))