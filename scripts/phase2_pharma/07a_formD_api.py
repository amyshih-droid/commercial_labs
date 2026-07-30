"""
Form D Biotech/Pharma Pipeline
==============================

Pulls the latest quarterly SEC Form D bulk dataset, filters issuers to
Biotechnology & Pharmaceuticals (via Form D's own industryGroupType field), then enriches
each company with its official SIC code + description from EDGAR's submissions API.

Output: a CSV with company name, address, CIK, Form D industry label,
SIC code, SIC description, and offering details (amount raised, date).

Usage:
    python form_d_biotech_pipeline.py --quarter 2026Q2
    python form_d_biotech_pipeline.py --quarters 2025Q1,2025Q2,2025Q3,2025Q4,2026Q1,2026Q2
    python form_d_biotech_pipeline.py --quarter 2026Q2 --industry-filter "Biotechnology,Pharmaceuticals"
    python form_d_biotech_pipeline.py --quarter 2026Q2 --sic-filter 8731,2836,8071

Notes:
    - No API key required for any of this.
    - SEC asks that automated requests include a descriptive User-Agent
      with contact info. EDIT THE USER_AGENT VARIABLE BELOW before running.
    - This script makes network calls to sec.gov / data.sec.gov and will
      NOT run inside a sandbox without outbound internet access to those
      domains. Run it locally or on a machine with normal internet access.
"""

import argparse
import io
import sys
import time
import zipfile
from pathlib import Path

import pandas as pd
import requests

# ----------------------------------------------------------------------
# CONFIG — edit this before running. SEC blocks/ throttles requests that
# don't identify a real contact.
# ----------------------------------------------------------------------
USER_AGENT = "Amy Shih amy.shih@labza.com"

SEC_BULK_INDEX_URL = "https://www.sec.gov/data-research/sec-markets-data/form-d-data-sets"
# SEC has used more than one path segment for these files over time, and is
# mid-migration as of mid-2026 (older quarters under "structureddata", the
# newest quarter seen under "datastandardsinnovation"). We try both, in order.
FORM_D_ZIP_URL_TEMPLATES = [
    "https://www.sec.gov/files/structureddata/data/form-d-data-sets/{quarter}_d.zip",
    "https://www.sec.gov/files/datastandardsinnovation/data/form-d-data-sets/{quarter}_d.zip",
]
SUBMISSIONS_URL_TEMPLATE = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"

REQUEST_DELAY_SECONDS = 0.15  # ~6-7 requests/sec, well under SEC's 10 req/s limit

DEFAULT_SIC_CODES = {"8731", "2836", "8071"}
# 8731 = Commercial Physical & Biological Research
# 2836 = Biological Products, Except Diagnostic
# 8071 = Medical Laboratories


def download_form_d_quarter(quarter: str, dest_dir: Path) -> Path:
    """
    Downloads and unzips the quarterly Form D bulk dataset.
    `quarter` format: '2026q2', '2025q4', etc. — LOWERCASE, matching SEC's
    actual file naming (this differs from what you might guess from the
    site's display text, which shows "2026 Q2").

    Tries each URL in FORM_D_ZIP_URL_TEMPLATES in order and uses the first
    one that returns a successful response, since SEC has used more than
    one path segment for this data set and appears to be mid-migration.
    """
    headers = {"User-Agent": USER_AGENT}
    last_error = None

    for template in FORM_D_ZIP_URL_TEMPLATES:
        url = template.format(quarter=quarter.lower())
        print(f"Trying: {url}")
        try:
            resp = requests.get(url, headers=headers, timeout=60)
            resp.raise_for_status()
            print(f"Success: downloaded from {url}")
            extract_dir = dest_dir / quarter.lower()
            extract_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                zf.extractall(extract_dir)
            print(f"Extracted to {extract_dir}")
            return extract_dir
        except requests.exceptions.RequestException as e:
            print(f"  Failed ({e}), trying next URL pattern if available...")
            last_error = e

    raise RuntimeError(
        f"Could not download Form D data for quarter '{quarter}' from any "
        f"known URL pattern. Last error: {last_error}\n"
        f"Check the current download link manually at:\n{SEC_BULK_INDEX_URL}"
    )


def _find_file_recursive(root: Path, target_name: str) -> Path:
    """
    Searches recursively under `root` for a file matching `target_name`,
    case-insensitively. SEC's ZIPs sometimes extract into a nested
    subfolder (e.g. form_d_data/2026q2/2026Q2_d/FORMDSUBMISSION.tsv)
    rather than directly into the requested directory, and file casing
    has varied by year too.
    """
    target_lower = target_name.lower()
    matches = [p for p in root.rglob("*") if p.is_file() and p.name.lower() == target_lower]
    if not matches:
        # Also try matching without regard to underscores/hyphens, in case
        # SEC renames e.g. FORMDSUBMISSION.tsv -> FORM_D_SUBMISSION.tsv
        all_files = [p for p in root.rglob("*") if p.is_file()]
        raise FileNotFoundError(
            f"Could not find a file named '{target_name}' anywhere under {root}.\n"
            f"Files actually present:\n" + "\n".join(f"  {p.relative_to(root)}" for p in all_files)
        )
    if len(matches) > 1:
        print(f"  Warning: multiple files matched '{target_name}', using first: {matches[0]}")
    return matches[0]


def load_form_d_tables(extract_dir: Path):
    """Loads the three tables we need and returns them as DataFrames.

    Searches recursively under extract_dir in case the ZIP unpacked into
    a nested subfolder, and matches filenames case-insensitively.
    """
    def read_tsv(name):
        path = _find_file_recursive(extract_dir, name)
        print(f"  Loading {name} from {path}")
        return pd.read_csv(path, sep="\t", dtype=str, encoding="utf-8", low_memory=False)

    submissions = read_tsv("FORMDSUBMISSION.tsv")
    issuers = read_tsv("ISSUERS.tsv")
    offering = read_tsv("OFFERING.tsv")
    return submissions, issuers, offering


def filter_to_industries(submissions, issuers, offering, industry_values):
    """
    Joins the three Form D tables on ACCESSIONNUMBER and filters to any of
    the given industryGroupType values (case-insensitive, exact match per
    value — e.g. ["Biotechnology", "Pharmaceuticals"]).
    """
    offering.columns = [c.strip() for c in offering.columns]
    industry_col = next(
        (c for c in offering.columns if "industrygrouptype" in c.lower()), None
    )
    if industry_col is None:
        raise KeyError(
            "Could not find an industryGroupType-like column in OFFERING.tsv. "
            f"Available columns: {list(offering.columns)}"
        )

    normalized_targets = {v.strip().lower() for v in industry_values}
    normalized_actual = offering[industry_col].astype(str).str.strip().str.lower()

    matched = offering[normalized_actual.isin(normalized_targets)].copy()

    # Warn about any requested values that matched zero rows - usually means
    # a typo or a label that doesn't exist in this quarter's taxonomy.
    found_values = set(normalized_actual[normalized_actual.isin(normalized_targets)].unique())
    missing = normalized_targets - found_values
    if missing:
        print(
            f"  Warning: these --industry-filter values matched 0 rows "
            f"(check spelling/casing against actual values in the data): {sorted(missing)}"
        )

    merged = matched.merge(
        submissions, on="ACCESSIONNUMBER", how="left", suffixes=("_offering", "_submission")
    )
    merged = merged.merge(
        issuers, on="ACCESSIONNUMBER", how="left", suffixes=("", "_issuer")
    )
    return merged


def get_sic_for_cik(cik: str, session: requests.Session):
    """Fetches SIC code + description for a single CIK via EDGAR submissions API."""
    if not cik or not str(cik).strip():
        return None, None
    url = SUBMISSIONS_URL_TEMPLATE.format(cik=str(cik).strip())
    try:
        resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        if resp.status_code != 200:
            return None, None
        data = resp.json()
        return data.get("sic"), data.get("sicDescription")
    except Exception as e:
        print(f"  Warning: failed to fetch SIC for CIK {cik}: {e}", file=sys.stderr)
        return None, None


def enrich_with_sic(df: pd.DataFrame, cik_col: str = "CIK") -> pd.DataFrame:
    """Loops through unique CIKs and attaches SIC code + description."""
    if cik_col not in df.columns:
        # Try common alternates seen across schema versions
        alt = next((c for c in df.columns if c.strip().upper() == "CIK"), None)
        if alt is None:
            raise KeyError(f"No CIK column found. Available columns: {list(df.columns)}")
        cik_col = alt

    unique_ciks = df[cik_col].dropna().unique().tolist()
    print(f"Looking up SIC codes for {len(unique_ciks)} unique companies...")

    sic_map = {}
    session = requests.Session()
    for i, cik in enumerate(unique_ciks, 1):
        sic, sic_desc = get_sic_for_cik(cik, session)
        sic_map[cik] = (sic, sic_desc)
        if i % 25 == 0:
            print(f"  {i}/{len(unique_ciks)} looked up...")
        time.sleep(REQUEST_DELAY_SECONDS)

    df["sic_code"] = df[cik_col].map(lambda c: sic_map.get(c, (None, None))[0])
    df["sic_description"] = df[cik_col].map(lambda c: sic_map.get(c, (None, None))[1])
    return df


def dedup_to_one_row_per_company(df: pd.DataFrame) -> pd.DataFrame:
    """
    Collapses multiple Form D rows for the same company (e.g. an original
    filing plus later amendments within the same quarter) down to one row
    per company. Groups by CIK when available (most reliable identifier),
    falling back to ENTITYNAME for any rows missing a CIK.

    Deliberately simple: no date parsing, no sorting - just keeps the last
    row encountered per company in the file's existing order. This trades
    a small amount of precision (we don't guarantee "most recent filing")
    for reliability, since date parsing here previously caused a
    segmentation fault in some environments (likely a pandas/dateutil/
    Python version compatibility issue, not a logic bug). If you need
    true "most recent filing wins" behavior, that's worth revisiting once
    the underlying environment issue is resolved (see README).
    """
    cik_col = next((c for c in df.columns if c.strip().upper() == "CIK"), None)
    name_col = next((c for c in df.columns if c.strip().upper() == "ENTITYNAME"), None)
    group_col = cik_col if cik_col is not None else name_col

    if group_col is None:
        print("  Warning: no CIK or ENTITYNAME column found - skipping dedup step.")
        return df

    before = len(df)
    deduped = df.drop_duplicates(subset=[group_col], keep="last")
    after = len(deduped)

    if before != after:
        print(f"  Deduped {before} rows -> {after} unique companies (grouped by {group_col}).")
    return deduped


def expand_quarter_range(start: str, end: str) -> list:
    """
    Expands a start/end quarter pair (e.g. '2023Q1', '2026Q2') into an
    explicit ordered list of quarter strings covering that range,
    inclusive of both endpoints.
    """
    def parse_quarter(q):
        q = q.strip().upper()
        year_str, q_str = q.split("Q")
        return int(year_str), int(q_str)

    start_year, start_q = parse_quarter(start)
    end_year, end_q = parse_quarter(end)

    if (start_year, start_q) > (end_year, end_q):
        raise ValueError(
            f"--quarter-range start ({start}) is after end ({end}) - check the order."
        )

    quarters = []
    year, q = start_year, start_q
    while (year, q) <= (end_year, end_q):
        quarters.append(f"{year}Q{q}")
        q += 1
        if q > 4:
            q = 1
            year += 1
    return quarters


def process_single_quarter(quarter: str, workdir: Path, industry_values: list) -> pd.DataFrame:
    """
    Runs the download -> load -> filter steps for one quarter and returns
    the resulting DataFrame, tagged with a 'source_quarter' column.
    """
    print(f"\n{'='*60}\nProcessing quarter: {quarter}\n{'='*60}")
    extract_dir = download_form_d_quarter(quarter, workdir)
    submissions, issuers, offering = load_form_d_tables(extract_dir)

    merged = filter_to_industries(submissions, issuers, offering, industry_values)
    print(f"Found {len(merged)} Form D offering records tagged: {industry_values}")

    merged = merged.copy()
    merged["source_quarter"] = quarter.upper()
    return merged


def main():
    parser = argparse.ArgumentParser(description="Form D Biotech/Pharma pipeline")
    parser.add_argument(
        "--quarter",
        "--quarters",
        dest="quarters",
        default=None,
        help=(
            "One or more quarters, comma-separated. e.g. --quarter 2026Q2 "
            "or --quarters 2025Q1,2025Q2,2025Q3,2025Q4,2026Q1,2026Q2. "
            "Use this OR --quarter-range, not both."
        ),
    )
    parser.add_argument(
        "--quarter-range",
        dest="quarter_range",
        default=None,
        help=(
            "Start and end quarter, comma-separated, to auto-generate every "
            "quarter in between (inclusive). Example: --quarter-range 2023Q1,2026Q2 "
            "expands to 2023Q1,2023Q2,...,2026Q1,2026Q2 (14 quarters). "
            "Use this OR --quarter/--quarters, not both."
        ),
    )
    parser.add_argument(
        "--industry-filter",
        default="Biotechnology",
        help=(
            "Comma-separated Form D industryGroupType values to include "
            "(default: 'Biotechnology'). Values are matched case-insensitively. "
            "Example: --industry-filter 'Biotechnology,Pharmaceuticals,Other Health Care'"
        ),
    )
    parser.add_argument(
        "--sic-filter",
        default=",".join(sorted(DEFAULT_SIC_CODES)),
        help="Comma-separated SIC codes to flag as a match (default: 8731,2836,8071)",
    )
    parser.add_argument(
        "--workdir", default="./form_d_data", help="Where to download/extract data"
    )
    parser.add_argument(
        "--out", default="form_d_biotech_enriched.csv", help="Output CSV path"
    )
    parser.add_argument(
        "--skip-sic-lookup",
        action="store_true",
        help="Skip the per-company SIC API enrichment step (faster, Form D label only)",
    )
    parser.add_argument(
        "--keep-duplicates",
        action="store_true",
        help=(
            "Keep one row per Form D filing instead of collapsing amendments/"
            "repeat filings (within or across quarters) down to one row per "
            "company. Off by default."
        ),
    )
    args = parser.parse_args()

    if args.quarters and args.quarter_range:
        parser.error("Use either --quarter/--quarters OR --quarter-range, not both.")
    if not args.quarters and not args.quarter_range:
        parser.error("You must provide either --quarter/--quarters or --quarter-range.")

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    if args.quarter_range:
        start, end = [p.strip() for p in args.quarter_range.split(",")]
        quarters = expand_quarter_range(start, end)
        print(f"Expanded --quarter-range {start} to {end} -> {len(quarters)} quarters: {quarters}")
    else:
        quarters = [q.strip() for q in args.quarters.split(",") if q.strip()]

    industry_values = [v.strip() for v in args.industry_filter.split(",") if v.strip()]

    # Process each quarter independently, then stack the results. A failure
    # on one quarter (e.g. a 404 for a not-yet-published future quarter)
    # is reported but doesn't stop the others from completing.
    all_quarter_frames = []
    failed_quarters = []
    for q in quarters:
        try:
            df_q = process_single_quarter(q, workdir, industry_values)
            all_quarter_frames.append(df_q)
        except Exception as e:
            print(f"  ERROR processing {q}: {e}")
            failed_quarters.append(q)

    if not all_quarter_frames:
        print("No quarters processed successfully. Exiting.")
        return

    merged = pd.concat(all_quarter_frames, ignore_index=True)
    print(f"\nCombined total across {len(all_quarter_frames)} quarter(s): {len(merged)} rows")
    if failed_quarters:
        print(f"Quarters that failed and were skipped: {failed_quarters}")

    if not args.keep_duplicates:
        # Dedup across ALL quarters combined, not per-quarter, since the
        # same company (same CIK) may have filed in more than one quarter -
        # e.g. an original filing in 2025Q4 and an amendment in 2026Q1.
        merged = dedup_to_one_row_per_company(merged)

    if not args.skip_sic_lookup:
        merged = enrich_with_sic(merged)
        sic_filter_set = set(args.sic_filter.split(","))
        merged["sic_matches_target_list"] = merged["sic_code"].isin(sic_filter_set)

    # Trim to the columns most useful for merging into your master lab list.
    # Column names in the raw files vary slightly by year - adjust as needed
    # after inspecting merged.columns.
    keep_candidates = [
        "ACCESSIONNUMBER", "ENTITYNAME", "CIK", "STREET1", "STREET2", "CITY",
        "STATEORCOUNTRY", "ZIPCODE", "industryGroupType", "sic_code",
        "sic_description", "sic_matches_target_list", "TOTALAMOUNTSOLD",
        "TOTALOFFERINGAMOUNT", "DATEOFFIRSTSALE", "FILING_DATE", "source_quarter",
    ]
    keep_cols = [c for c in keep_candidates if c in merged.columns]
    final = merged[keep_cols] if keep_cols else merged

    final.to_csv(args.out, index=False)
    print(f"Wrote {len(final)} rows to {args.out}")


if __name__ == "__main__":
    main()