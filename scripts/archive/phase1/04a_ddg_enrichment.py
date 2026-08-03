"""
Script 04b: URL Retrieval Only (DuckDuckGo Test Run)
=======================================================
- Uses DuckDuckGo for 100% FREE searches to find website URLs.
- SKIPS website scraping and SKIPS Gemini AI extraction.
- Fast, lightweight, and bypasses daily AI quota limits.
"""

import pandas as pd
import time
from tqdm import tqdm
from duckduckgo_search import DDGS

INPUT_FILE  = "../../data/phase1_clinical/clean/cms_clia_california_geocoded.csv"
OUTPUT_FILE = "../../data/phase1_clinical/clean/cms_clia_california_ddg_enriched.csv"

# ── STEP 1: 100% Free DuckDuckGo Search ────────────────────────────────
# ── STEP 1: Smarter Free DuckDuckGo Search ────────────────────────────────
def get_company_url(company_name, city):
    """Uses DuckDuckGo to find the company website, filtering out directories."""
    clean_name = str(company_name).replace("/", " ").replace("-", " ")
    
    # Removed "official website" as it sometimes confuses non-Google engines
    search_query = f'{clean_name} {city} California clinic lab'
    
    # List of domains we DO NOT want
    junk_domains = [
        "yelp.com", "healthgrades.com", "vitals.com", "zocdoc.com", 
        "webmd.com", "doximity.com", "sharecare.com", "mapquest.com",
        "yellowpages.com", "facebook.com", "linkedin.com"
    ]
    
    try:
        # Ask for the top 5 results instead of just 1
        results = DDGS().text(search_query, max_results=5)
        
        if not results:
            return ""
            
        # Check each result
        for result in results:
            link = result.get("href", "").lower()
            
            # If the link doesn't contain any junk domains, assume it's the real site!
            is_junk = any(junk in link for junk in junk_domains)
            if not is_junk and link.startswith("http"):
                return result.get("href", "") # Return the original, un-lowercased link
                
    except Exception as e:
        print(f"\n  [!] DUCKDUCKGO RATE LIMIT/ERROR: {e}")
    
    return ""

# ── MAIN EXECUTION ────────────────────────────────────────────────────
if __name__ == "__main__":
    print("="*60)
    print("Script 04b: URL Retrieval Only (DuckDuckGo Test)")
    print("="*60)

    df = pd.read_csv(INPUT_FILE, dtype=str).fillna("")
    
    if "website_url" not in df.columns:
        df["website_url"] = ""

    # Test mode: Still testing first 10
    test_df = df.head(10).copy()
    print(f"Processing {len(test_df)} rows...\n")
    
    save_interval = 1

    for index, row in tqdm(test_df.iterrows(), total=len(test_df)):
        company_name = row["lab_name"]
        city = row["city"]
        
        print(f"\n--- [{index}] {company_name} ---")
        
        # Skip if URL already exists
        if str(row.get("website_url", "")).strip() not in ["", "NOT FOUND"]:
            print("  -> Already has URL. Skipping.")
            continue

        url = get_company_url(company_name, city)
        
        if url:
            print(f"  -> Found URL: {url}")
            test_df.at[index, "website_url"] = url
        else:
            print("  -> NO URL FOUND!")
            test_df.at[index, "website_url"] = "NOT FOUND"
        
        # A 3-second pause to prevent DuckDuckGo from banning your IP
        time.sleep(3) 
            
        if index % save_interval == 0:
            test_df.to_csv(OUTPUT_FILE, index=False)

    test_df.to_csv(OUTPUT_FILE, index=False)
    print(f"\n Test complete! Check your terminal output above for errors.")