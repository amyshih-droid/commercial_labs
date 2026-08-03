import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from dotenv import load_dotenv
from urllib.parse import urlencode

import requests


# ============================================================
# Configuration
# ============================================================

ALGOLIA_URL = (
    "https://45bwzj1sgc-dsn.algolia.net/1/indexes/*/queries"
)


load_dotenv()

ALGOLIA_APPLICATION_ID = os.getenv(
    "YC_ALGOLIA_APPLICATION_ID",
    "45BWZJ1SGC",
)

ALGOLIA_API_KEY = os.getenv(
    "YC_ALGOLIA_API_KEY",
    "",
)

ALGOLIA_INDEX = "YCCompany_production"

OUTPUT_DIR = Path("data/phase2_pharma/raw/ycombinator")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# YC filters
# ============================================================

YC_INDUSTRIES = [
    "Drug Discovery and Delivery",
    "Industrial Bio",
    "Therapeutics",
]

YC_REGION = "United States of America"


# ============================================================
# API request
# ============================================================

def build_algolia_params(
    page: int = 0,
    hits_per_page: int = 100,
) -> str:
    """
    Build query parameters for YC Algolia search.
    """
    industry_filters = [
        f"industries:{industry}"
        for industry in YC_INDUSTRIES
    ]

    facet_filters = [
        industry_filters,
        [f"regions:{YC_REGION}"],
    ]

    params = {
        "query": "",
        "page": str(page),
        "hitsPerPage": str(hits_per_page),
        "attributesToHighlight": json.dumps([]),
        "attributesToRetrieve": json.dumps(["*"]),
        "facets": json.dumps([
            "industries",
            "subindustry",
            "regions",
            "batch",
            "isHiring",
            "nonprofit",
            "top_company",
        ], separators=(",", ":")),
        "facetFilters": json.dumps(facet_filters, separators=(",", ":")),
        "tagFilters": json.dumps(["ycdc_public"], separators=(",", ":")),
    }

    parts = [f"{quote(k)}={quote(v)}" for k, v in params.items()]
    return "&".join(parts)

    
def fetch_algolia_page(
    page: int,
    hits_per_page: int = 100,
) -> dict:
    """
    Fetch one page of YC company records from Algolia.
    """

    if not ALGOLIA_API_KEY:
        raise RuntimeError(
            "YC_ALGOLIA_API_KEY is not set. "
            "Set it as an environment variable before running."
        )

    headers = {
        "Content-Type": "application/json",
        "X-Algolia-Application-Id": ALGOLIA_APPLICATION_ID,
        "X-Algolia-API-Key": ALGOLIA_API_KEY,
    }

    params = build_algolia_params(
        page=page,
        hits_per_page=hits_per_page,
    )

    payload = {
        "requests": [
            {
                "indexName": ALGOLIA_INDEX,
                "params": params,
            }
        ]
    }

    response = requests.post(
        ALGOLIA_URL,
        headers=headers,
        json=payload,
        timeout=30,
    )

    response.raise_for_status()

    data = response.json()

    if "results" not in data:
        raise ValueError(
            "Unexpected Algolia response: "
            "missing 'results'."
        )

    return data


# ============================================================
# Company extraction
# ============================================================

def extract_companies(
    algolia_response: dict,
    retrieved_at: str,
) -> list[dict]:
    """
    Extract and annotate company records from an Algolia response.
    """

    results = algolia_response.get("results", [])

    if not results:
        return []

    result = results[0]

    companies = []

    for hit in result.get("hits", []):
        company_id = hit.get("id") or hit.get("objectID")

        slug = hit.get("slug")

        if slug:
            source_url = (
                f"https://www.ycombinator.com/companies/{slug}"
            )
        else:
            source_url = None

        company = {
            "source_name": "Y Combinator",
            "source_record_id": str(company_id)
            if company_id is not None
            else None,
            "source_url": source_url,
            "retrieved_at": retrieved_at,

            # YC company information
            "company_name": hit.get("name"),
            "yc_slug": slug,
            "website_url": hit.get("website"),

            # Location as provided by YC
            "source_location": hit.get("all_locations"),

            # Company description
            "description": hit.get("long_description"),
            "one_liner": hit.get("one_liner"),

            # Company metadata
            "team_size": hit.get("team_size"),
            "industry": hit.get("industry"),
            "subindustry": hit.get("subindustry"),
            "industries": hit.get("industries"),
            "tags": hit.get("tags"),

            # YC metadata
            "batch": hit.get("batch"),
            "company_status": hit.get("status"),
            "stage": hit.get("stage"),
            "regions": hit.get("regions"),

            # Signals
            "is_hiring": hit.get("isHiring"),
            "nonprofit": hit.get("nonprofit"),
            "top_company": hit.get("top_company"),

            # Keep the original YC record ID
            "yc_object_id": hit.get("objectID"),
        }

        companies.append(company)

    return companies


# ============================================================
# Save functions
# ============================================================

def save_json(
    data,
    filename: str,
):
    """
    Save JSON data to the raw data directory.
    """

    path = OUTPUT_DIR / filename

    with open(
        path,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            data,
            f,
            indent=2,
            ensure_ascii=False,
        )

    print(f"Saved JSON: {path}")


def save_csv(
    companies: list[dict],
    filename: str,
):
    """
    Save flattened company records to CSV.
    """

    if not companies:
        print("No companies to save.")
        return

    path = OUTPUT_DIR / filename

    fieldnames = list(companies[0].keys())

    with open(
        path,
        "w",
        newline="",
        encoding="utf-8",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
        )

        writer.writeheader()

        for company in companies:
            row = company.copy()

            # Convert lists to JSON strings for CSV
            for field in [
                "industries",
                "tags",
                "regions",
            ]:
                if isinstance(row.get(field), list):
                    row[field] = json.dumps(
                        row[field],
                        ensure_ascii=False,
                    )

            writer.writerow(row)

    print(f"Saved CSV: {path}")


# ============================================================
# Main collection function
# ============================================================

def collect_all_companies(
    hits_per_page: int = 100,
) -> tuple[list[dict], list[dict]]:
    """
    Retrieve all YC companies matching the filters.

    Returns:
        companies:
            Flattened company records.

        raw_responses:
            Complete raw Algolia responses.
    """

    retrieved_at = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    all_companies = []
    raw_responses = []

    page = 0

    while True:
        print(
            f"Fetching Algolia page {page}..."
        )

        response = fetch_algolia_page(
            page=page,
            hits_per_page=hits_per_page,
        )

        raw_responses.append(response)

        results = response.get(
            "results",
            [],
        )

        if not results:
            break

        result = results[0]

        hits = result.get(
            "hits",
            [],
        )

        nb_hits = result.get(
            "nbHits",
            0,
        )

        nb_pages = result.get(
            "nbPages",
            0,
        )

        print(
            f"  Retrieved: {len(hits)} companies"
        )

        print(
            f"  Total matching companies: {nb_hits}"
        )

        print(
            f"  Page: {page + 1}/{nb_pages}"
        )

        companies = extract_companies(
            response,
            retrieved_at,
        )

        all_companies.extend(
            companies
        )

        # Stop when we've retrieved
        # the final page.
        if (
            page >= nb_pages - 1
            or not hits
        ):
            break

        page += 1

    return (
        all_companies,
        raw_responses,
    )


# ============================================================
# Main
# ============================================================

def main():

    print("=" * 60)
    print("Y COMBINATOR COMPANY COLLECTION")
    print("=" * 60)

    print(
        "Industries:"
    )

    for industry in YC_INDUSTRIES:
        print(
            f"  - {industry}"
        )

    print(
        f"Region: {YC_REGION}"
    )

    print(
        f"Algolia index: {ALGOLIA_INDEX}"
    )

    print("=" * 60)

    companies, raw_responses = (
        collect_all_companies(
            hits_per_page=100
        )
    )

    timestamp = datetime.now(
        timezone.utc
    ).strftime(
        "%Y%m%dT%H%M%SZ"
    )

    # --------------------------------------------------------
    # Save complete raw Algolia response
    # --------------------------------------------------------

    save_json(
        raw_responses,
        f"yc_algolia_raw_{timestamp}.json",
    )

    # --------------------------------------------------------
    # Save flattened company records
    # --------------------------------------------------------

    save_json(
        companies,
        f"yc_companies_{timestamp}.json",
    )

    # --------------------------------------------------------
    # Save CSV for later standardization
    # --------------------------------------------------------

    save_csv(
        companies,
        f"yc_companies_{timestamp}.csv",
    )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    print()
    print("=" * 60)
    print("COLLECTION COMPLETE")
    print("=" * 60)

    print(
        f"Total companies collected: "
        f"{len(companies):,}"
    )

    print(
        f"Raw API responses: "
        f"{len(raw_responses):,}"
    )

    print(
        f"Output directory: "
        f"{OUTPUT_DIR}"
    )


if __name__ == "__main__":
    main()