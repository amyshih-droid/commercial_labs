"""
SEC Form D Connector

Responsibilities
----------------
- Download and parse SEC Form D quarterly bulk datasets (ZIP/TSV)
- Filter offerings by industryGroupType (e.g. Biotechnology, Pharmaceuticals)
- Enrich unique CIK issuers with official SIC codes from EDGAR submissions API
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

import io
import logging
from pathlib import Path
import tempfile
import time
import zipfile
import pandas as pd
import requests
import yaml

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Config Loader
# ------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path("config/connectors/formd.yaml")


def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> dict:
    """Loads configuration from YAML file."""
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ------------------------------------------------------------------
# Download & File Helpers
# ------------------------------------------------------------------

def download_form_d_quarter(quarter: str, dest_dir: Path, user_agent: str, url_templates: list[str]) -> Path:
    """Downloads and extracts SEC Form D bulk ZIP dataset for a given quarter."""
    headers = {"User-Agent": user_agent}
    last_error = None

    for template in url_templates:
        url = template.format(quarter=quarter.lower())
        logger.info("Downloading Form D dataset from: %s", url)
        try:
            resp = requests.get(url, headers=headers, timeout=60)
            resp.raise_for_status()
            extract_dir = dest_dir / quarter.lower()
            extract_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                zf.extractall(extract_dir)
            logger.info("Successfully extracted %s dataset to %s", quarter, extract_dir)
            return extract_dir
        except requests.exceptions.RequestException as e:
            logger.warning("Failed downloading from %s (%s), trying next pattern...", url, e)
            last_error = e

    raise RuntimeError(f"Could not download Form D data for quarter '{quarter}'. Error: {last_error}")


def find_file_recursive(root: Path, target_name: str) -> Path:
    """Finds a file case-insensitively within target path."""
    target_lower = target_name.lower()
    matches = [p for p in root.rglob("*") if p.is_file() and p.name.lower() == target_lower]
    if not matches:
        all_files = [p for p in root.rglob("*") if p.is_file()]
        raise FileNotFoundError(f"Could not find '{target_name}' under {root}. Present: {all_files}")
    return matches[0]


def load_form_d_tables(extract_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Loads FORMDSUBMISSION, ISSUERS, and OFFERING tables."""
    def read_tsv(name: str) -> pd.DataFrame:
        path = find_file_recursive(extract_dir, name)
        return pd.read_csv(path, sep="\t", dtype=str, encoding="utf-8", low_memory=False)

    submissions = read_tsv("FORMDSUBMISSION.tsv")
    issuers = read_tsv("ISSUERS.tsv")
    offering = read_tsv("OFFERING.tsv")
    return submissions, issuers, offering


# ------------------------------------------------------------------
# Processing & Filtering
# ------------------------------------------------------------------

def filter_to_industries(
    submissions: pd.DataFrame,
    issuers: pd.DataFrame,
    offering: pd.DataFrame,
    industry_values: list[str],
) -> pd.DataFrame:
    """Joins Form D tables on ACCESSIONNUMBER and filters by industryGroupType."""
    offering.columns = [c.strip() for c in offering.columns]
    industry_col = next((c for c in offering.columns if "industrygrouptype" in c.lower()), None)
    if industry_col is None:
        raise KeyError(f"Could not find industryGroupType column. Available: {list(offering.columns)}")

    normalized_targets = {v.strip().lower() for v in industry_values}
    normalized_actual = offering[industry_col].astype(str).str.strip().str.lower()

    matched = offering[normalized_actual.isin(normalized_targets)].copy()

    merged = matched.merge(
        submissions, on="ACCESSIONNUMBER", how="left", suffixes=("_offering", "_submission")
    )
    merged = merged.merge(
        issuers, on="ACCESSIONNUMBER", how="left", suffixes=("", "_issuer")
    )
    return merged


# ------------------------------------------------------------------
# SIC API Enrichment
# ------------------------------------------------------------------

def get_sic_for_cik(cik: str, session: requests.Session, user_agent: str, url_template: str) -> tuple[str | None, str | None]:
    """Fetches official SIC code and description for a given CIK from EDGAR API."""
    if not cik or not str(cik).strip():
        return None, None
    url = url_template.format(cik=str(cik).strip())
    try:
        resp = session.get(url, headers={"User-Agent": user_agent}, timeout=20)
        if resp.status_code != 200:
            return None, None
        data = resp.json()
        return data.get("sic"), data.get("sicDescription")
    except Exception as e:
        logger.warning("Failed to fetch SIC for CIK %s: %s", cik, e)
        return None, None


def enrich_with_sic(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    """Enriches DataFrame with SIC code and SIC description per unique CIK."""
    cik_col = next((c for c in df.columns if c.strip().upper() == "CIK"), None)
    if cik_col is None:
        raise KeyError(f"No CIK column found in DataFrame. Available: {list(df.columns)}")

    user_agent = config.get("user_agent", "Anonymous user@example.com")
    url_template = config.get("submissions_url_template", "https://data.sec.gov/submissions/CIK{cik:0>10}.json")
    delay = config.get("request_delay_seconds", 0.15)
    target_sic_codes = set(config.get("target_sic_codes", []))

    unique_ciks = df[cik_col].dropna().unique().tolist()
    logger.info("Enriching %d unique CIKs via EDGAR submissions API...", len(unique_ciks))

    sic_map = {}
    session = requests.Session()
    for i, cik in enumerate(unique_ciks, 1):
        sic, sic_desc = get_sic_for_cik(cik, session, user_agent, url_template)
        sic_map[cik] = (sic, sic_desc)
        if i % 50 == 0 or i == len(unique_ciks):
            logger.info("Looked up SIC for %d/%d CIKs", i, len(unique_ciks))
        time.sleep(delay)

    df["sic_code"] = df[cik_col].map(lambda c: sic_map.get(c, (None, None))[0])
    df["sic_description"] = df[cik_col].map(lambda c: sic_map.get(c, (None, None))[1])
    if target_sic_codes:
        df["sic_matches_target_list"] = df["sic_code"].isin(target_sic_codes)

    return df


# ------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------

def validate(df: pd.DataFrame, config: dict) -> None:
    """Validates extracted DataFrame structure and critical fields."""
    if df.empty:
        raise ValueError("Form D connector returned zero rows.")

    required_cols = set(config.get("required_columns", []))
    missing = required_cols - set(df.columns)

    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    if not df.columns.is_unique:
        raise ValueError("Duplicate columns detected in Form D DataFrame.")

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
    """Extract SEC Form D offerings driven by YAML config."""
    config = load_config(config_path)

    user_agent = config.get("user_agent", "Anonymous user@example.com")
    url_templates = config.get("form_d_zip_url_templates", [])
    quarters = config.get("quarters", ["2026Q1", "2026Q2"])
    industry_filters = config.get("industry_filters", ["Biotechnology", "Pharmaceuticals"])
    skip_sic_lookup = config.get("skip_sic_lookup", False)

    all_quarter_frames = []

    with tempfile.TemporaryDirectory() as temp_dir_str:
        workdir = Path(temp_dir_str)

        for q in quarters:
            logger.info("Processing Form D quarter: %s", q)
            try:
                extract_dir = download_form_d_quarter(q, workdir, user_agent, url_templates)
                submissions, issuers, offering = load_form_d_tables(extract_dir)
                merged_q = filter_to_industries(submissions, issuers, offering, industry_filters)
                merged_q["source_quarter"] = q.upper()
                all_quarter_frames.append(merged_q)
                logger.info("Extracted %d records for quarter %s", len(merged_q), q)
            except Exception as e:
                logger.error("Failed processing Form D quarter %s: %s", q, e)

        if not all_quarter_frames:
            raise ValueError("No Form D quarters were successfully processed.")

        df_combined = pd.concat(all_quarter_frames, ignore_index=True)

        if not skip_sic_lookup:
            df_combined = enrich_with_sic(df_combined, config)

        keep_candidates = [
            "ACCESSIONNUMBER", "ENTITYNAME", "CIK", "STREET1", "STREET2", "CITY",
            "STATEORCOUNTRY", "ZIPCODE", "industryGroupType", "sic_code",
            "sic_description", "sic_matches_target_list", "TOTALAMOUNTSOLD",
            "TOTALOFFERINGAMOUNT", "DATEOFFIRSTSALE", "FILING_DATE", "source_quarter",
        ]
        keep_cols = [c for c in keep_candidates if c in df_combined.columns]
        final_df = df_combined[keep_cols] if keep_cols else df_combined

        validate(final_df, config)
        logger.info("Successfully extracted %d total Form D records.", len(final_df))
        return final_df


# ------------------------------------------------------------------
# Development Entry Point
# ------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    df = extract(headless=True)
    logger.info(df.head())
    logger.info("Total Rows Extracted: %d", len(df))