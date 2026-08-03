"""
Y Combinator Directory Connector

Responsibilities
----------------
- Connect to YC Algolia Search API
- Extract active biotech and life-science YC portfolio companies
- Return a pandas DataFrame

This connector DOES NOT:
- save files to disk
- standardize final schema columns
- deduplicate across sources
- perform entity resolution
- call LLMs
- geocode
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
import pandas as pd
import requests
import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Load local .env file if available
load_dotenv()

# ------------------------------------------------------------------
# Config Loader
# ------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path("config/connectors/ycombinator.yaml")


def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> dict:
    """Loads configuration from YAML file."""
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ------------------------------------------------------------------
# Algolia API Helpers
# ------------------------------------------------------------------

def build_algolia_params(
    config: dict,
    page: int = 0,
    hits_per_page: int = 100,
) -> str:
    """Build query parameters for YC Algolia search."""
    industries = config.get("industries", [])
    region = config.get("region", "United States of America")

    industry_filters = [f"industries:{ind}" for ind in industries]
    facet_filters = [
        industry_filters,
        [f"regions:{region}"],
    ]

    params = {
        "query": "",
        "page": str(page),
        "hitsPerPage": str(hits_per_page),
        "attributesToHighlight": json.dumps([]),
        "attributesToRetrieve": json.dumps(["*"]),
        "facets": json.dumps(
            [
                "industries",
                "subindustry",
                "regions",
                "batch",
                "isHiring",
                "nonprofit",
                "top_company",
            ],
            separators=(",", ":"),
        ),
        "facetFilters": json.dumps(facet_filters, separators=(",", ":")),
        "tagFilters": json.dumps(["ycdc_public"], separators=(",", ":")),
    }

    parts = [f"{quote(k)}={quote(v)}" for k, v in params.items()]
    return "&".join(parts)


def fetch_algolia_page(
    config: dict,
    api_key: str,
    page: int,
    hits_per_page: int = 100,
) -> dict:
    """Fetch one page of YC company records from Algolia."""
    url = config.get("algolia_url", "https://45bwzj1sgc-dsn.algolia.net/1/indexes/*/queries")
    app_id = config.get("algolia_app_id", "45BWZJ1SGC")
    index_name = config.get("algolia_index", "YCCompany_production")
    timeout = config.get("request_timeout", 30)

    headers = {
        "Content-Type": "application/json",
        "X-Algolia-Application-Id": app_id,
        "X-Algolia-API-Key": api_key,
    }

    params = build_algolia_params(config, page=page, hits_per_page=hits_per_page)

    payload = {
        "requests": [
            {
                "indexName": index_name,
                "params": params,
            }
        ]
    }

    response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    response.raise_for_status()

    data = response.json()
    if "results" not in data:
        raise ValueError("Unexpected Algolia response: missing 'results'.")

    return data


# ------------------------------------------------------------------
# Record Parsing
# ------------------------------------------------------------------

def extract_companies(algolia_response: dict, retrieved_at: str) -> list[dict]:
    """Extract and annotate company records from an Algolia response."""
    results = algolia_response.get("results", [])
    if not results:
        return []

    result = results[0]
    companies = []

    for hit in result.get("hits", []):
        company_id = hit.get("id") or hit.get("objectID")
        slug = hit.get("slug")
        source_url = f"https://www.ycombinator.com/companies/{slug}" if slug else None

        # Convert list fields to JSON strings to ensure smooth Parquet serialization
        industries_val = hit.get("industries")
        tags_val = hit.get("tags")
        regions_val = hit.get("regions")

        company = {
            "source_name": "Y Combinator",
            "source_record_id": str(company_id) if company_id is not None else None,
            "source_url": source_url,
            "retrieved_at": retrieved_at,
            # YC company info
            "company_name": hit.get("name"),
            "yc_slug": slug,
            "website_url": hit.get("website"),
            # Location
            "source_location": hit.get("all_locations"),
            # Descriptions
            "description": hit.get("long_description"),
            "one_liner": hit.get("one_liner"),
            # Metadata
            "team_size": hit.get("team_size"),
            "industry": hit.get("industry"),
            "subindustry": hit.get("subindustry"),
            "industries": json.dumps(industries_val) if isinstance(industries_val, list) else industries_val,
            "tags": json.dumps(tags_val) if isinstance(tags_val, list) else tags_val,
            "batch": hit.get("batch"),
            "company_status": hit.get("status"),
            "stage": hit.get("stage"),
            "regions": json.dumps(regions_val) if isinstance(regions_val, list) else regions_val,
            # Signals
            "is_hiring": hit.get("isHiring"),
            "nonprofit": hit.get("nonprofit"),
            "top_company": hit.get("top_company"),
            "yc_object_id": hit.get("objectID"),
        }
        companies.append(company)

    return companies


# ------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------

def validate(df: pd.DataFrame, config: dict) -> None:
    """Validates extracted DataFrame structure and critical fields."""
    if df.empty:
        raise ValueError("Y Combinator connector returned zero rows.")

    required_cols = set(config.get("required_columns", []))
    missing = required_cols - set(df.columns)

    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    if not df.columns.is_unique:
        raise ValueError("Duplicate columns detected in Y Combinator DataFrame.")

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
    """Extract Y Combinator biotech portfolio companies driven by YAML config."""
    config = load_config(config_path)

    env_var_name = config.get("algolia_api_key_env_var", "YC_ALGOLIA_API_KEY")
    api_key = os.getenv(env_var_name, "")

    if not api_key:
        raise RuntimeError(
            f"Environment variable '{env_var_name}' is not set. "
            "Please set your YC Algolia API key in your .env file."
        )

    hits_per_page = config.get("hits_per_page", 100)
    retrieved_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    all_companies = []
    page = 0

    while True:
        logger.info("Fetching YC Algolia page %d...", page)
        response = fetch_algolia_page(config, api_key, page, hits_per_page=hits_per_page)
        companies = extract_companies(response, retrieved_at)
        all_companies.extend(companies)

        results = response.get("results", [])
        if not results:
            break

        result = results[0]
        nb_pages = result.get("nbPages", 0)
        hits = result.get("hits", [])

        if page >= nb_pages - 1 or not hits:
            break

        page += 1

    df = pd.DataFrame(all_companies)
    validate(df, config)
    logger.info("Successfully extracted %d Y Combinator portfolio companies.", len(df))
    return df


# ------------------------------------------------------------------
# Development Entry Point
# ------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    df = extract(headless=True)
    logger.info(df.head())
    logger.info("Total Rows Extracted: %d", len(df))