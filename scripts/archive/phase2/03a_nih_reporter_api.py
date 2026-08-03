"""
Script 05: NIH RePORTER API Extraction (Max Contact Info)
===========================================================
- Queries the NIH RePORTER v2 Projects API for active corporate grants.
- Legally guarantees commercial small business outputs.
- Extracts PI Contact Names and directly parses the public Contact Emails.
- Pre-formats empty 'phone_number' and 'website_url' columns for the AI Agent.
"""

import pandas as pd
import requests
import time
import os
from tqdm import tqdm

# Output Configuration
OUTPUT_DIR  = "data/phase2_pharma/raw"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "nih_reporter_preclinical_raw.csv")
os.makedirs(OUTPUT_DIR, exist_ok=True)

API_URL = "https://api.reporter.nih.gov/v2/projects/search"

def query_nih_reporter_with_retry(payload):
    delays = [1, 2, 4, 8, 16]
    for delay in delays:
        try:
            response = requests.post(API_URL, json=payload, timeout=20)
            if response.status_code == 200:
                return response.json()
            elif response.status_code in [429, 500, 502, 503, 504]:
                time.sleep(delay)
                continue
        except Exception:
            time.sleep(delay)
            continue
    return None

def fetch_strict_commercial_biotechs(years=[2021, 2022, 2023, 2024, 2025], max_records=200):
    all_projects = []
    offset = 0
    limit = 50
    
    print(f"Extracting Small Business Grants & Contact Info for Years: {years}...")
    pbar = tqdm(total=max_records, desc="Harvesting Data")
    
    while len(all_projects) < max_records:
        payload = {
            "criteria": {
                "activity_codes": ["R41", "R42", "R43", "R44"],
                "fiscal_years": years
            },
            "limit": limit,
            "offset": offset,
            "sort_field": "project_start_date",
            "sort_order": "desc"
        }
        
        data = query_nih_reporter_with_retry(payload)
        if not data or "results" not in data:
            break
            
        results = data.get("results", [])
        if not results:
            break
            
        for project in results:
            org_data = project.get("organization", {})
            pis = project.get("principal_investigators", [])
            org_name = org_data.get("org_name")
            
            if not org_name:
                continue
                
            # Local university/hospital firewall
            bad_keywords = ["UNIVERSITY", "COLLEGE", "INSTITUTE", "HOSPITAL", "FOUNDATION", "CLINIC", "SCHOOL OF MEDICINE"]
            if any(keyword in org_name.upper() for keyword in bad_keywords):
                continue
                
            # Deep parsing the Principal Investigator details
            pi_name = ""
            pi_email = ""
            if pis:
                # Grab the primary contact PI
                contact_pi = pis[0]
                pi_name = contact_pi.get("full_name", "")
                
                # Dig for the email string safely
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
                "contact_email": pi_email, # Raw harvested NIH email
                "phone_number": "",        # Missing field -> To be found by OpenAI Search
                "website_url": "",         # Missing field -> To be found by OpenAI Search
                "award_amount": project.get("total_cost", 0),
                "grant_id": project.get("project_num", "")
            }
            all_projects.append(project_info)
            
        pbar.update(len(results))
        offset += limit
        time.sleep(1)
        
    pbar.close()
    
    raw_df = pd.DataFrame(all_projects)
    if not raw_df.empty:
        # Keep one unique row per company
        cleaned_df = raw_df.drop_duplicates(subset=["lab_name"], keep="first")
        return cleaned_df
    return pd.DataFrame()

if __name__ == "__main__":
    df_biotech = fetch_strict_commercial_biotechs(years=[2024, 2025, 2026], max_records=200)
    
    if not df_biotech.empty:
        df_biotech.to_csv(OUTPUT_FILE, index=False)
        print(f"\n🎉 SUCCESS: Saved {len(df_biotech)} commercial biotechs to CSV!")
        print("\nReviewing Harvested Contact Data:")
        print(df_biotech[["lab_name", "contact_name", "contact_email", "phone_number", "website_url"]].head(10))