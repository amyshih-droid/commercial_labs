"""
BioPharmGuy CMO / CDMO Directory Connector

Responsibilities
----------------
- Connect to BioPharmGuy Contract Manufacturing sub-directories via HTTP requests
- Extract CMO/CDMO company listings
- Return a pandas DataFrame

This connector DOES NOT:
- save files
- standardize columns
- deduplicate across sources
- perform entity resolution
- call LLMs
- geocode
"""

from __future__ import annotations

import logging
from pathlib import Path
import time
import pandas as pd
import yaml
from bs4 import BeautifulSoup

# Try to use curl_cffi for browser impersonation (bypassing Cloudflare), fallback to standard requests
try:
    from curl_cffi import requests as stealth_requests
    USE_STEALTH = True
except ImportError:
    import requests as standard_requests
    USE_STEALTH = False

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Config Loader
# ------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path("config/connectors/biopharmguy_cmos.yaml")


def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> dict:
    """Loads configuration from YAML file."""
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ------------------------------------------------------------------
# Page Fetcher
# ------------------------------------------------------------------

def fetch_page_html(url: str) -> str:
    """Fetches HTML page content using stealth requests or standard HTTP requests fallback."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    }

    if USE_STEALTH:
        response = stealth_requests.get(url, impersonate="chrome120", timeout=15)
    else:
        logger.warning("curl_cffi not installed. Using standard requests fallback.")
        response = standard_requests.get(url, headers=headers, timeout=15)

    if response.status_code != 200:
        raise RuntimeError(f"HTTP fetch failed for {url} with status code {response.status_code}")

    html_text = response.text
    page_text_lower = html_text.lower()

    if any(bot_term in page_text_lower for bot_term in ["cloudflare", "captcha", "turnstile", "security check"]):
        raise PermissionError(f"Cloudflare/Anti-bot challenge detected when requesting {url}")

    return html_text


# ------------------------------------------------------------------
# HTML Parser
# ------------------------------------------------------------------

def parse_category_table(html: str, category_name: str, base_url: str) -> list[dict]:
    """Parses BioPharmGuy CMO table grids into structured raw records."""
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")

    if not tables:
        logger.warning("No <table> elements found for category '%s'.", category_name)
        return []

    records = []
    for table in tables:
        rows = table.find_all("tr")
        for row in rows:
            cols_td = row.find_all("td")

            if len(cols_td) < 2:
                continue

            # 1. Parse company name and website URL
            company_td = cols_td[0]
            name = ""
            href = ""

            for a_tag in company_td.find_all("a"):
                tag_text = a_tag.get_text(strip=True)
                if len(tag_text) > 1:
                    name = tag_text.upper()
                    href = a_tag.get("href", "").strip()
                    break

            if not name:
                name = company_td.get_text(strip=True).upper()

            if not name or len(name) < 2 or name in ["COMPANY", "NAME", "LOCATION"]:
                continue

            if href and (href.startswith("#") or href == "/"):
                href = ""
            if href and any(nav in href for nav in ["company-by-location", "biotech-company-directory"]):
                href = ""

            resolved_url = ""
            if href:
                resolved_url = f"{base_url}{href}" if href.startswith("/") else href

            # 2. Parse location
            location_td = cols_td[1]
            location_text = location_td.get_text(strip=True)
            city, state = "", ""

            if "," in location_text:
                parts = location_text.split(",", 1)
                city = parts[0].strip().title()
                state = parts[1].strip().upper()
            else:
                city = location_text.title()

            records.append({
                "lab_name": name,
                "category_focus": category_name,
                "city": city,
                "state": state,
                "street_address": "",
                "contact_name": "",
                "contact_email": "",
                "phone_number": "",
                "website_url": resolved_url,
                "source_registry": "BioPharmGuy",
            })

    return records


# ------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------

def validate(df: pd.DataFrame, config: dict) -> None:
    """Validates extracted DataFrame structure and critical fields."""
    if df.empty:
        raise ValueError("BioPharmGuy CMO connector returned zero rows.")

    required_cols = set(config.get("required_columns", []))
    missing = required_cols - set(df.columns)

    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    if not df.columns.is_unique:
        raise ValueError("Duplicate columns detected in BioPharmGuy CMO DataFrame.")

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
    """Extract BioPharmGuy CMO directory listings driven by YAML config."""
    config = load_config(config_path)
    base_url = config.get("base_url", "https://biopharmguy.com")
    endpoints = config.get("endpoints", {})
    delay = config.get("request_delay_seconds", 2)

    all_records = []

    for category_name, endpoint in endpoints.items():
        target_url = f"{base_url}{endpoint}"
        logger.info("Extracting BioPharmGuy CMO category: %s (%s)", category_name, target_url)

        try:
            html = fetch_page_html(target_url)
            category_records = parse_category_table(html, category_name, base_url)
            all_records.extend(category_records)
            logger.info("Parsed %d records for category '%s'", len(category_records), category_name)
        except Exception as e:
            logger.error("Failed to extract category '%s': %s", category_name, e)

        time.sleep(delay)

    df = pd.DataFrame(all_records)
    validate(df, config)
    logger.info("Successfully extracted %d total BioPharmGuy CMO entries.", len(df))
    return df


# ------------------------------------------------------------------
# Development Entry Point
# ------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    df = extract(headless=True)
    logger.info(df.head())
    logger.info("Total Rows Extracted: %d", len(df))