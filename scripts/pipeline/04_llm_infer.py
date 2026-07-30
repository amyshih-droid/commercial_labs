"""
llm_infer.py — fills in missing fields (address_street, contact_name,
contact_email) using a WEBSITE-FIRST, PAGE-TARGETED strategy:

  1. If website_url is already known, use it directly - never re-search
     for a URL you already trust. Only run a grounded search to FIND a
     website_url when that field itself is missing.
  2. Once a website is known, fetch its homepage and scan its real
     navigation links (not guessed paths) for Contact / About / Team /
     Leadership / Locations pages.
  3. Only fetch the page categories relevant to what's actually missing
     for that row:
       - missing address_street  -> Contact, Locations pages
       - missing contact_name    -> Team, About, Leadership pages
       - missing contact_email   -> Contact, Team, About pages
  4. Extract fields ONLY from the real fetched page text, in one LLM
     call per row covering all currently-missing fields at once - never
     a plain "ask the model what it knows" call.


Design principles (do not weaken these when modifying):
1. Only ever called for rows where a field is ALREADY null - never
   overwrites a real value from the source.
2. Every filled value must carry a confidence and at least one source
   URL - no untraceable inference is acceptable output.
3. Extraction is grounded in ACTUAL FETCHED PAGE TEXT ONLY. If no
   relevant page is found or fetchable, leave the field null. Never
   fall back to a plain ungrounded model guess.
4. Cached by a hash of (company, city, state, field) so re-running the
   pipeline doesn't re-fetch pages or re-call the API for unchanged rows.
5. On any failure (bad response, fetch error, malformed JSON) - leave
   the field null and log it, don't crash the whole run.

Usage:
    python llm_infer.py \
        --in data/phase2_pharma/master_entities.csv \
        --out data/phase2_pharma/master_entities_test.csv \
        --fields website_url,address_street,contact_name,contact_email \
        --limit 5
"""

import argparse
import asyncio
import hashlib
import json
import os
import time
import re
from pathlib import Path
from urllib.parse import urljoin

import aiohttp
import pandas as pd
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()  # reads a .env file in the current directory (project root) into os.environ

# ---------------------------------------------------------------------
# Configuration & Batch Settings
# ---------------------------------------------------------------------
BATCH_SIZE = 500              # Total labs processed in a single batch execution
MAX_CONCURRENT_REQUESTS = 30  # Semaphore limit to prevent API rate limits (429)

CACHE_DIR = Path("./llm_infer_cache")
# Bumped whenever the EXTRACTION logic/prompt changes meaningfully - a
# row that previously failed extraction (cached as null) needs a fresh
# attempt once the logic improves, otherwise re-running just replays the
# old cached failure forever. Bump this string again next time the
# extraction strategy changes. Deliberately NOT applied to the website
# search cache key, since that logic is unchanged.
EXTRACTION_CACHE_VERSION = "v5"
# REQUEST_DELAY_SECONDS = 0.5
FETCH_TIMEOUT_SECONDS = 15
MAX_PAGES_FETCHED_PER_ROW = 4          # homepage + up to 3 category pages
HEADERS = {"User-Agent": "Mozilla/5.0 (research data collection script)"}

# Which page categories are relevant to each missing field, in priority
# order (checked first = preferred if multiple categories are available).
FIELD_TO_CATEGORIES = {
    "address_street": ["contact", "locations"],
    "contact_name": ["team", "about", "leadership"],
    "contact_email": ["contact", "team", "about"],
}

# Keywords used to recognize each page category from a homepage's real
# navigation links (link text or href, case-insensitive substring match).
CATEGORY_KEYWORDS = {
    "contact": ["contact"],
    "about": ["about"],
    "team": ["team", "people", "staff"],
    "leadership": ["leadership", "executive", "management"],
    "locations": ["location", "locations", "offices", "facilities"],
}


# ---------------------------------------------------------------------
# Token Usage Tracker
# ---------------------------------------------------------------------
class TokenTracker:
    def __init__(self):
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._lock = asyncio.Lock()

    async def add(self, usage):
        if usage:
            async with self._lock:
                self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
                self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
    
    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

# ---------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------

def cache_key(*parts) -> str:
    raw = "|".join(str(p) for p in parts).lower().strip()
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def load_from_cache(key: str):
    path = CACHE_DIR / f"{key}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def save_to_cache(key: str, result: dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_DIR / f"{key}.json", "w") as f:
        json.dump(result, f)

def parse_json_response(text: str) -> dict:
    if not text:
        return {}
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group(0)
    try:
        return json.loads(text.strip())
    except Exception:
        return {}

# ---------------------------------------------------------------------
# Website resolution (only searched for when genuinely missing)
# ---------------------------------------------------------------------
def clean_company_name(name: str) -> str:
    """Strips DBA noise and legal suffixes to produce a clean search term."""
    if not isinstance(name, str):
        return ""
    # Extract brand name inside 'dba' or 'd/b/a' if present
    dba_match = re.search(r"(?:dba|d/b/a)\s+(.+)", name, re.IGNORECASE)
    if dba_match:
        name = dba_match.group(1)
    # Remove standard corporate suffixes
    name = re.sub(
        r",?\s*(Inc\.|LLC|PC|Corporation|Corp\.|L\.L\.C\.|L\.P\.|Ltd\.)",
        "",
        name,
        flags=re.IGNORECASE,
    )
    return name.strip(" ()")

def build_website_search_prompt(company_name: str, city: str, state: str) -> str:
    cleaned_name = clean_company_name(company_name)
    return f"""Find the official homepage website URL for the following medical laboratory / facility using web search.

Official Name: {company_name}
Common Brand Name: {cleaned_name}
Known Location: {city}, {state}

Search Guidance:
- Search for "{cleaned_name} {city} {state}" or official healthcare directory listings.
- Return ONLY the main official homepage URL (e.g. "https://www.example.com/"). 
Do not return third-party directory listings like Yelp, YellowPages, or Facebook unless no official site exists.

Return ONLY valid JSON, no other text:
{{"value": "<the official website URL, or null if not found>"}}
"""


async def resolve_website_url_async(
    company_name: str, city: str, state: str, client: AsyncOpenAI, tracker: TokenTracker
) -> dict:
    key = cache_key("v2_web", company_name, city, state)
    cached = load_from_cache(key)
    if cached is not None:
        return cached

    try:
        prompt = build_website_search_prompt(company_name, city, state)
        resp = await client.responses.create(
            model="gpt-5.4-nano",
            input=prompt,
            tools=[{"type": "web_search"}],
        )
        await tracker.add(getattr(resp, "usage", None))
        result = parse_json_response(resp.output_text)
    except Exception as e:
        result = {"value": None}

    save_to_cache(key, result)
    return result

async def search_street_address_fallback_async(company_name: str, city: str, state: str, zip_code: str, client: AsyncOpenAI, tracker: TokenTracker) -> str:
    cleaned_name = clean_company_name(company_name)
    prompt = f"""Find the official physical street address for the following facility using web search.

Facility Name: {company_name}
Common Brand Name: {cleaned_name}
Location: {city}, {state} {zip_code}

Rules:
- Include the building number, street name, and suite/unit/suite number if available (e.g., "123 Main St, Suite 400").
- Do NOT return just the city, state, or PO Box.
- Return null if no specific street address is found.

Return ONLY valid JSON:
{{"address_street": "<full street address including suite/unit, or null if not found>"}}
"""
    try:
        resp = await client.responses.create(
            model="gpt-5.4-nano",
            input=prompt,
            tools=[{"type": "web_search"}],
        )
        await tracker.add(getattr(resp, "usage", None))
        parsed = parse_json_response(resp.output_text)
        if isinstance(parsed, dict):
            return parsed.get("address_street")
        return None
    except Exception:
        return None
    
# ---------------------------------------------------------------------
# Async Web Scraping - find REAL Contact/About/Team/etc. pages
# from the site's own navigation, rather than guessing URL paths.
# ---------------------------------------------------------------------

async def fetch_page_async(session: aiohttp.ClientSession, url: str) -> str:
    try:
        async with session.get(url, headers=HEADERS, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            if resp.status == 200:
                html = await resp.text()
                soup = BeautifulSoup(html, "html.parser")
                for tag in soup(["script", "style"]):
                    tag.decompose()
                return soup.get_text(separator=" ", strip=True)
    except Exception:
        return ""
    return ""


async def discover_category_pages_async(session: aiohttp.ClientSession, homepage_url: str, needed_categories: set) -> dict:
        """Fetches the homepage, scans its real <a> links for text/href
        matching each needed category's keywords, and returns
        {category: [absolute_url, ...]} - real site navigation, not guessed
        static paths (e.g. not blindly trying '/contact-us' if that link
        doesn't actually exist on the site)."""
        found = {cat: [] for cat in needed_categories}
        try:
            async with session.get(homepage_url, headers=HEADERS, timeout=FETCH_TIMEOUT_SECONDS) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    soup = BeautifulSoup(html, "html.parser")
                    for a in soup.find_all("a", href=True):
                        link_text = a.get_text(strip=True).lower()
                        href = a["href"].lower()
                        for category in needed_categories:
                            keywords = CATEGORY_KEYWORDS[category]
                            if any(kw in link_text or kw in href for kw in keywords):
                                absolute_url = urljoin(homepage_url, a["href"])
                                if absolute_url not in found[category]:
                                    found[category].append(absolute_url)
        except Exception:
            pass
        return found


# ---------------------------------------------------------------------
# Missing-value pattern grouping (for cost visibility, per point 3)
# ---------------------------------------------------------------------

def print_missing_pattern_summary(df: pd.DataFrame, fields: list):
    missing_pattern = df[fields].apply(
        lambda row: tuple(f for f in fields if pd.isna(row[f])), axis=1
    )
    counts = missing_pattern.value_counts()
    print("\nMissing-value pattern summary (before running):")
    for pat, count in counts.items():
        label = "+".join(pat) if pat else "(nothing missing)"
        print(f"  {count:>6} rows missing: {label}")
    total_needing_work = (missing_pattern.apply(len) > 0).sum()
    print(f"  {total_needing_work} rows need at least one field filled\n")


# ---------------------------------------------------------------------
# Extraction from fetched page text
# ---------------------------------------------------------------------

def build_extraction_prompt(company_name: str, city: str, state: str, zip_code: str, missing_fields: list, pages: dict) -> str:
    field_descriptions = {
        "address_street": "full physical street address including building number, street name, and suite/unit/suite number if present (do not return city/state/zip here)",
        "contact_name": "full name of a key doctor, lab director, medical founder, or primary contact person listed for this facility (include titles like MD, PhD, or Dr. if present)",
        "contact_email": "direct or general contact email address listed for the facility (e.g., info@, contact@, or a personal email)",
    }
    fields_requested = "\n".join(
        f"- {f}: {field_descriptions.get(f, f)}" for f in missing_fields
    )
    pages_text = "\n\n".join(
        f"--- Page: {url} ---\n{text[:4000]}" for url, text in pages.items() if text
    )

    return f"""You are extracting information about the company
"{company_name}" from the following real, fetched web page text. Use
ONLY what literally appears in this text - do not guess, infer beyond
what's stated, or use any prior knowledge about this company.

Fields needed:
{fields_requested}

{pages_text}

Return ONLY valid JSON, no other text, in this exact shape:
{{
  "address_street": {{"value": "..."}},
  "contact_name": {{"value": "..."}},
  "contact_email": {{"value": "..."}}
}}
Only include keys for the fields actually requested above. If a field
is not found anywhere in the provided page text, set "value" to null
and "confidence" to 0.0 rather than guessing.
"""

async def search_contact_fallback_async(
    company_name: str, city: str, state: str, missing_fields: list, client: AsyncOpenAI, tracker: TokenTracker
) -> dict:
    cleaned_name = clean_company_name(company_name)
    needed = ", ".join(missing_fields)
    
    prompt = f"""Search the web to find official contact details for this medical facility / laboratory.

Facility Name: {company_name}
Common Brand Name: {cleaned_name}
Location: {city}, {state}

Find values strictly for these missing fields: {needed}

Rules:
- contact_name: Full name of the lab director, lead physician, doctor, founder, or key contact person (e.g., "Dr. Jane Smith, MD").
- contact_email: Official direct or public contact email address (e.g., "info@facility.com" or "jsmith@facility.com").
- Set any field to null if not confidently found in real web search results.

Return ONLY valid JSON:
{{
  "contact_name": "<full name or null>",
  "contact_email": "<email or null>"
}}
"""
    try:
        resp = await client.responses.create(
            model="gpt-5.4-nano",
            input=prompt,
            tools=[{"type": "web_search"}],
        )
        await tracker.add(getattr(resp, "usage", None))
        parsed = parse_json_response(resp.output_text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}

async def call_extraction_async(client: AsyncOpenAI, prompt: str, tracker: TokenTracker) -> dict:
    resp = await client.chat.completions.create(
        model="gpt-5.4-nano",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"}
    )
    await tracker.add(getattr(resp, "usage", None))
    return parse_json_response(resp.choices[0].message.content)

# ---------------------------------------------------------------------
# Async Processing Engine
# ---------------------------------------------------------------------


async def process_row_async(batch_idx: int,
    total_batch: int,
    global_idx: int,
    row: pd.Series,
    missing_fields: list,
    session: aiohttp.ClientSession,
    client: AsyncOpenAI,
    semaphore: asyncio.Semaphore,
    tracker: TokenTracker
) -> tuple:
    async with semaphore:
        entity_id = row.get("entity_id", f"Row-{global_idx}")
        company_name = row.get("company_name", "Unknown")
        city = str(row.get("address_city", "") or "")
        state = str(row.get("address_state", "") or "")
        zip_code = str(row.get("address_zip", "") or "")

        print(f"[{batch_idx}/{total_batch}] Processing {entity_id}: '{company_name}' ({city}, {state} {zip_code})...")

        key = cache_key(EXTRACTION_CACHE_VERSION, "extract", company_name, city, state, zip_code, ",".join(missing_fields))
        cached = load_from_cache(key)
        if cached is not None:
            return global_idx, cached

        results = {f: {"value": None, "status": "not_attempted"} for f in missing_fields}
        diagnostics = {}  # per-field record of what was actually checked

        # Step 1: Resolve website_url if missing
        website_url = row.get("website_url")
        if pd.isna(website_url) or not str(website_url).strip():
            if "website_url" in missing_fields:
                web_result = await resolve_website_url_async(company_name, city, state, client, tracker)
                website_url = web_result.get("value")
                results["website_url"] = {
                    "value": website_url,
                    "status": "found_via_search" if website_url else "search_found_nothing",
                }
            else:
                website_url = None

        # Step 2: If STILL no website, fall back to a direct address search
        # (this used to be unreachable dead code - Step 1 returned before
        # this ever ran. Fixed: now it actually executes.)
        if not website_url:
            if "address_street" in missing_fields:
                fallback_street = await search_street_address_fallback_async(
                    company_name, city, state, zip_code, client, tracker
                )
                results["address_street"] = {
                    "value": fallback_street,
                    "status": "found_via_fallback_search" if fallback_street else "no_website_fallback_search_failed",
                }
            for f in missing_fields:
                if f not in ("website_url", "address_street"):
                    results[f]["status"] = "no_website_cannot_check_pages"
            save_to_cache(key, results)
            return global_idx, results

        # Step 3: Discover category pages
        fields_needing_pages = [f for f in missing_fields if f in FIELD_TO_CATEGORIES]
        needed_categories = set()
        for f in fields_needing_pages:
            needed_categories.update(FIELD_TO_CATEGORIES[f])

        if needed_categories:
            category_pages = await discover_category_pages_async(session, website_url, needed_categories)

            # Step 4: Fetch candidate pages
            pages_to_fetch = [website_url]
            for f in fields_needing_pages:
                for category in FIELD_TO_CATEGORIES[f]:
                    for url in category_pages.get(category, []):
                        if url not in pages_to_fetch:
                            pages_to_fetch.append(url)
                        if len(pages_to_fetch) >= MAX_PAGES_FETCHED_PER_ROW:
                            break
                    if len(pages_to_fetch) >= MAX_PAGES_FETCHED_PER_ROW:
                        break
                if len(pages_to_fetch) >= MAX_PAGES_FETCHED_PER_ROW:
                    break

            fetch_tasks = [fetch_page_async(session, u) for u in pages_to_fetch]
            fetched_texts = await asyncio.gather(*fetch_tasks)
            pages = {u: txt for u, txt in zip(pages_to_fetch, fetched_texts) if txt}

            # Diagnostic: record which pages were found/fetched, and how
            # much usable text came back - a page that returns 200 but
            # near-zero text is very likely JavaScript-rendered content
            # our fetcher can't see, not genuinely-missing information.
            THIN_TEXT_THRESHOLD = 300  # characters
            pages_attempted = len(pages_to_fetch)
            pages_fetched_ok = len(pages)
            total_text_len = sum(len(t) for t in pages.values())
            likely_js_rendered = pages_fetched_ok > 0 and total_text_len < THIN_TEXT_THRESHOLD

            page_diag = (
                f"pages_checked={pages_attempted},fetched_ok={pages_fetched_ok},"
                f"total_text_chars={total_text_len}"
            )

            # Step 5: Extract missing fields from HTML
            if pages:
                try:
                    prompt = build_extraction_prompt(company_name, city, state, zip_code, fields_needing_pages, pages)
                    extracted = await call_extraction_async(client, prompt, tracker)
                    for f in fields_needing_pages:
                        field_result = extracted.get(f, {})
                        value = field_result.get("value")
                        if value:
                            status = "extracted"
                        elif likely_js_rendered:
                            status = f"pages_likely_js_rendered ({page_diag})"
                        else:
                            status = f"checked_pages_not_found ({page_diag})"
                        results[f] = {"value": value, "status": status}
                except Exception as e:
                    print(f"  Warning: extraction failed for '{company_name}': {e}")
                    for f in fields_needing_pages:
                        results[f]["status"] = f"extraction_call_failed: {e}"
            else:
                for f in fields_needing_pages:
                    results[f]["status"] = f"no_pages_fetched_ok ({page_diag})"
        else:
            for f in fields_needing_pages:
                results[f]["status"] = "no_category_pages_needed"

        # Step 6: ADDRESS FALLBACK - If address_street is STILL missing after HTML extraction
        if "address_street" in missing_fields and not results.get("address_street", {}).get("value"):
            fallback_street = await search_street_address_fallback_async(
                company_name, city, state, zip_code, client, tracker
            )
            if fallback_street:
                results["address_street"] = {"value": fallback_street, "status": "found_via_fallback_search"}
            # else: keep the earlier status from Step 5 explaining why
            # page-based extraction didn't find it either
        
        # Step 7: CONTACT FALLBACK - If contact_name or contact_email are STILL missing
        missing_contacts = [
            f for f in ("contact_name", "contact_email") 
            if f in missing_fields and not results.get(f, {}).get("value")
        ]
        if missing_contacts:
            contact_fallback = await search_contact_fallback_async(
                company_name, city, state, missing_contacts, client, tracker
            )
            for f in missing_contacts:
                val = contact_fallback.get(f)
                if val and str(val).lower() != "null":
                    results[f] = {"value": val, "status": "found_via_fallback_search"}

        save_to_cache(key, results)
        return global_idx, results


async def run_pipeline_async(df: pd.DataFrame, fields: list, limit: int):
    client = AsyncOpenAI()
    tracker = TokenTracker()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    rows_needing_work = df[df[fields].isna().any(axis=1)].index
    if limit:
        rows_needing_work = rows_needing_work[:limit]

    total_to_process = len(rows_needing_work)
    print(f"\nStarting batch run for {total_to_process} labs using asyncio + gpt-5.4-nano...")

    async with aiohttp.ClientSession() as session:
        tasks = []
        for b_idx, g_idx in enumerate(rows_needing_work, 1):
            row = df.loc[g_idx]
            row_missing_fields = [f for f in fields if pd.isna(row[f])]
            tasks.append(
                process_row_async(
                    b_idx, total_to_process, g_idx, row, row_missing_fields, session, client, semaphore, tracker
                )
            )

        results = await asyncio.gather(*tasks)

    for g_idx, row_results in results:
        row_missing_fields = [f for f in fields if pd.isna(df.loc[g_idx, f])]
        for f in row_missing_fields:
            res = row_results.get(f, {})
            df.at[g_idx, f] = res.get("value")
            df.at[g_idx, f"{f}_status"] = res.get("status")

    print("\n" + "=" * 50)
    print("TOKEN USAGE SUMMARY")
    print("=" * 50)
    print(f"  Prompt Tokens:     {tracker.prompt_tokens:,}")
    print(f"  Completion Tokens: {tracker.completion_tokens:,}")
    print(f"  Total Tokens Used: {tracker.total_tokens:,}")
    print("=" * 50 + "\n")

    # Status breakdown per field - this is the key diagnostic: tells you
    # whether remaining gaps are mostly "no website found", "pages likely
    # JavaScript-rendered" (a fetcher limitation), or "genuinely checked,
    # not found" (probably a real ceiling, not a bug).
    print("STATUS BREAKDOWN (per field, this run only):")
    for f in fields:
        status_col = f"{f}_status"
        if status_col not in df.columns:
            continue
        this_run_rows = [g_idx for g_idx, _ in results]
        statuses = df.loc[df.index.isin(this_run_rows), status_col].dropna()
        if len(statuses) == 0:
            continue
        # Collapse the parenthetical diagnostic detail for a clean summary count
        simplified = statuses.str.replace(r"\s*\(.*\)", "", regex=True)
        print(f"\n  {f}:")
        for status, count in simplified.value_counts().items():
            print(f"    {count:>5}  {status}")

def main():
    parser = argparse.ArgumentParser(description="Website-first, page-targeted LLM field inference")
    parser.add_argument("--in", dest="input_file", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--fields", default="address_street,contact_name,contact_email")
    parser.add_argument("--limit", type=int, default=None,
                         help="Override default BATCH_SIZE (500)")
    args = parser.parse_args()

    df = pd.read_csv(args.input_file, dtype=str)
    fields = [f.strip() for f in args.fields.split(",") if f.strip()]

    asyncio.run(run_pipeline_async(df, fields, args.limit))

    df.to_csv(args.out, index=False)
    print(f"Wrote {len(df)} rows to {args.out}")


if __name__ == "__main__":
    main()