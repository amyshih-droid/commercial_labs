"""
Script 06: BioPharmGuy Directory Scraper (Stealth Edition)
===========================================================
- Uses curl_cffi for browser impersonation to bypass Cloudflare firewalls.
- Targets the pre-clinical CRO ecosystem: Contract Research & Scientific Services.
- Dynamically locates company anchor links and parses locations flexibly.
- Harvests verified company website URLs directly from the HTML hrefs.
- Outputs to a dedicated biopharmguy_cros_services.csv file.
"""

import pandas as pd
import time
import os
from bs4 import BeautifulSoup

# Try to use curl_cffi for stealth scraping (bypassing Cloudflare)
try:
    from curl_cffi import requests as stealth_requests
    USE_STEALTH = True
except ImportError:
    import requests as standard_requests
    USE_STEALTH = False

# Output Configuration
OUTPUT_DIR = "data/clean"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "biopharmguy_cros_services.csv")
os.makedirs(OUTPUT_DIR, exist_ok=True)

BASE_URL = "https://biopharmguy.com"

# The exact master list endpoints for the Pre-Clinical CROs and Services
TARGET_ENDPOINTS = {
    "Contract Research (Pre-Clinical)": "/links/company-by-location-contract-research.php",
    "Scientific Services": "/links/company-by-location-services.php"
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
}

def scrape_biopharmguy_category(category_name, endpoint):
    """
    Scrapes a specific BioPharmGuy sub-directory page and parses company text grids.
    """
    target_url = f"{BASE_URL}{endpoint}"
    print(f"📡 Requesting payload from: {target_url}")
    
    try:
        # Use stealth requests to bypass the bot detection wall
        if USE_STEALTH:
            print("  🔒 Initializing stealth connection via curl_cffi (Chrome 120)...")
            response = stealth_requests.get(target_url, impersonate="chrome120", timeout=15)
        else:
            print("  ⚠️  Warning: curl_cffi not detected. Falling back to standard requests...")
            response = standard_requests.get(target_url, headers=HEADERS, timeout=15)
            
        if response.status_code != 200:
            print(f"  [!] Failed to pull page. HTTP Status Code: {response.status_code}")
            return []
            
        soup = BeautifulSoup(response.text, "html.parser")
        
        # Guardrail against Cloudflare Captcha Walls
        page_text = soup.get_text().lower()
        if "cloudflare" in page_text or "captcha" in page_text or "turnstile" in page_text or "security check" in page_text:
            print("\n  [!] CLOUDFLARE BLOCK DETECTED! Returning 0 records for this segment.")
            return []
            
        companies_parsed = []
        
        tables = soup.find_all("table")
        if not tables:
            print("  [!] Warning: No data grids (<table> elements) detected on this layout.")
            return []
            
        for table in tables:
            rows = table.find_all("tr")
            for row in rows:
                cols_td = row.find_all("td")
                
                # We need at least Company and Location columns
                if len(cols_td) < 2:
                    continue
                
                # 1. Safely extract the company name and website from the first column
                company_td = cols_td[0]
                name = ""
                href = ""
                link_col_idx = 0
                
                # Scan all anchor tags in the cell to bypass invisible alphabetical anchors
                for a_tag in company_td.find_all("a"):
                    tag_text = a_tag.get_text(strip=True)
                    if len(tag_text) > 1:
                        name = tag_text.upper()
                        href = a_tag.get("href", "").strip()
                        break
                
                # Fallback: If no valid link was found, extract raw text
                if not name:
                    name = company_td.get_text(strip=True).upper()
                
                # Skip pagination, table headers, and alphabetical sorting links
                if not name or name in ["COMPANY", "LOCATION", "NAME", "WEBSITE", "BACK TO TOP", "TOP"] or len(name) < 2:
                    continue
                if href.startswith("#") or not href or href == "/":
                    href = ""
                
                # Skip structural directory navigation filters that are not actual companies
                if href and any(nav_term in href for nav_term in ["company-by-location", "biotech-company-directory", "biotech-service-provider", "links/company-by"]):
                    href = ""
                
                # Determine absolute website URL
                resolved_url = ""
                if href:
                    if href.startswith("/"):
                        resolved_url = f"{BASE_URL}{href}"
                    else:
                        resolved_url = href
                
                # 2. Extract location coordinates
                city = ""
                state = ""
                if len(cols_td) > link_col_idx + 1:
                    city = cols_td[link_col_idx + 1].text.strip().title()
                if len(cols_td) > link_col_idx + 2:
                    state = cols_td[link_col_idx + 2].text.strip().upper()
                
                # Save the clean record
                companies_parsed.append({
                    "lab_name": name,
                    "category_focus": category_name,
                    "city": city,
                    "state": state,
                    "street_address": "",
                    "contact_name": "",
                    "contact_email": "",
                    "phone_number": "",
                    "website_url": resolved_url,
                    "source_registry": "BioPharmGuy"
                })
                
        return companies_parsed

    except Exception as e:
        print(f"  [!] Connection interruption during thread task: {e}")
        return []

if __name__ == "__main__":
    print("="*60)
    print("Script 06: BioPharmGuy CRO & Services Ingestion")
    print("="*60)
    
    if not USE_STEALTH:
        print("[!] PREflight Check: curl_cffi is missing. Attempting standard run...")
    else:
        print("✓ PREflight Check: curl_cffi successfully loaded. Stealth mode active.")
        
    master_list = []
    
    for category, endpoint in TARGET_ENDPOINTS.items():
        print(f"\n🚀 Initiating harvest loop segment for target: {category}")
        records = scrape_biopharmguy_category(category, endpoint)
        print(f"✅ Successfully captured {len(records)} entries for segment.")
        master_list.extend(records)
        
        # Voluntary pause to prevent server-side automated request blocking
        time.sleep(3)
        
    if master_list:
        df = pd.DataFrame(master_list)
        
        # Deduplicate companies listed under multiple service types
        # Keep the first assigned category
        df_clean = df.drop_duplicates(subset=["lab_name", "city", "state"], keep="first")
        
        df_clean.to_csv(OUTPUT_FILE, index=False)
        print("\n" + "="*60)
        print(f"🎉 HARVEST COMPLETE: Aggregated {len(df_clean)} unique CRO & Service entities!")
        print(f"Saved unified data layer directly to: {OUTPUT_FILE}")
        print("="*60)
        print("\nSample Lookahead Rows:")
        print(df_clean[["lab_name", "category_focus", "city", "state", "website_url"]].head(10))
    else:
        print("\n❌ Script terminated. No targets parsed out successfully.")