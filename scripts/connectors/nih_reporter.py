"""
NIH RePORTER API Connector

Responsibilities
----------------
- Connect to NIH RePORTER v2 Projects Search API
- Extract active small business innovation research grants (SBIR/STTR)
- Parse principal investigator and organization details into a pandas DataFrame

This connector DOES NOT:
- save files to disk
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
import requests
import yaml

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Config Loader
# ------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path("config/connectors/nih_reporter.yaml")


def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> dict:
    """Loads configuration from YAML file."""
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ------------------------------------------------------------------
# API Requester
# ------------------------------------------------------------------

def query_nih_reporter_with_retry(
    url: str,
    payload: dict,
    timeout: int = 20,
) -> dict | None:
    """Queries NIH RePORTER API with exponential backoff on retries."""
    delays = [1, 2, 4, 8, 16]
    for delay in delays:
        try:
            response = requests.post(url, json=payload, timeout=timeout)
            if response.status_code == 200:
                return response.json()
            elif response.status_code in [429, 500, 502, 503, 504]:
                logger.warning("NIH API HTTP %d. Retrying in %ds...", response.status_code, delay)
                time.sleep(delay)
                continue
        except requests.RequestException as e:
            logger.warning("NIH API request error: %s. Retrying in %ds...", e, delay)
            time.sleep(delay)
            continue
    return None


# ------------------------------------------------------------------
# Extractor Logic
# ------------------------------------------------------------------

def fetch_projects(config: dict) -> list[dict]:
    """Iterates through paginated API responses and parses raw project payloads."""
    api_url = config.get("api_url", "https://api.reporter.nih.gov/v2/projects/search")
    activity_codes = config.get("activity_codes", ["R41", "R42", "R43", "R44"])
    fiscal_years = config.get("fiscal_years", [2024, 2025, 2026])
    limit = config.get("page_limit", 50)
    max_records = config.get("max_records", 500)
    timeout = config.get("request_timeout", 20)
    delay = config.get("request_delay_seconds", 1)

    all_projects = []
    offset = 0

    bad_keywords = [
        "UNIVERSITY",
        "COLLEGE",
        "INSTITUTE",
        "HOSPITAL",
        "FOUNDATION",
        "CLINIC",
        "SCHOOL OF MEDICINE",
    ]

    while len(all_projects) < max_records:
        payload = {
            "criteria": {
                "activity_codes": activity_codes,
                "fiscal_years": fiscal_years,
            },
            "limit": limit,
            "offset": offset,
            "sort_field": "project_start_date",
            "sort_order": "desc",
        }

        data = query_nih_reporter_with_retry(api_url, payload, timeout=timeout)
        if not data or "results" not in data:
            logger.warning("No data returned from NIH RePORTER at offset %d.", offset)
            break

        results = data.get("results", [])
        if not results:
            break

        for project in results:
            org_data = project.get("organization", {})
            pis = project.get("principal_investigators", [])
            org_name = org_data.get("org_name", "")

            if not org_name:
                continue

            # Exclude non-commercial academic/institutional recipients
            if any(keyword in org_name.upper() for keyword in bad_keywords):
                continue

            pi_name = ""
            pi_email = ""
            if pis:
                contact_pi = pis[0]
                pi_name = contact_pi.get("full_name", "")
                raw_email = contact_pi.get("email", "")
                if raw_email and "@" in str(raw_email):
                    pi_email = str(raw_email).strip().lower()

            project_info = {
                "lab_name": org_name.strip().upper(),
                "project_title": project.get("project_title", ""),
                "fiscal_year": project.get("fiscal_year", ""),
                "street_address": org_data.get("street_address1", ""),
                "city": org_data.get("org_city", "").strip().title(),
                "state": org_data.get("org_state", "").strip().upper(),
                "zip_code": org_data.get("org_zipcode", ""),
                "contact_name": pi_name.strip().title(),
                "contact_email": pi_email,
                "phone_number": "",
                "website_url": "",
                "award_amount": project.get("total_cost", 0),
                "grant_id": project.get("project_num", ""),
            }
            all_projects.append(project_info)

        logger.info("Extracted %d NIH grant records so far...", len(all_projects))
        offset += limit
        time.sleep(delay)

    return all_projects


# ------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------

def validate(df: pd.DataFrame, config: dict) -> None:
    """Validates extracted DataFrame structure and critical fields."""
    if df.empty:
        raise ValueError("NIH RePORTER connector returned zero rows.")

    required_cols = set(config.get("required_columns", []))
    missing = required_cols - set(df.columns)

    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    if not df.columns.is_unique:
        raise ValueError("Duplicate columns detected in NIH RePORTER DataFrame.")

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
    """Extract NIH RePORTER grants driven by YAML config."""
    config = load_config(config_path)

    projects_list = fetch_projects(config)
    df = pd.DataFrame(projects_list)

    validate(df, config)
    logger.info("Successfully extracted %d total NIH RePORTER commercial grants.", len(df))
    return df


# ------------------------------------------------------------------
# Development Entry Point
# ------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    df = extract(headless=True)
    logger.info(df.head())
    logger.info("Total Rows Extracted: %d", len(df))