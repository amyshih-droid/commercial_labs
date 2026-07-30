"""
Script 04: AI Data Enrichment (Full Production Run)
=======================================================
- Processes the full dataset (~1,800 rows).
- Automatically skips already-processed rows (safe to restart).
- Includes 503 High Demand retry logic and auto-fallback.
- Uses curl_cffi to bypass firewalls.
- Mathematically foolproof JSON extraction.
"""

import pandas as pd
import json
import time
import os
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from tqdm import tqdm
from urllib.parse import urljoin

# Fix imports to prevent naming collisions
import requests as standard_requests 
from curl_cffi import requests as stealth_requests

# Import the NEW Google GenAI SDK
from google import genai
from google.genai import types

# Load .env file
load_dotenv()

SERPER_API_KEY = os.getenv("SERPER_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Configure the NEW Gemini Client
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

INPUT_FILE  = "../../data/phase1_clinical/clean/cms_clia_california_final.csv"
OUTPUT_FILE = "../../data/phase1_clinical/clean/cms_clia_california_final.csv"

# ── STEP 1: Smarter Google Search ──────────────────────────────────────
def get_company_url(company_name, city):
    """Uses Serper.dev with a natural language query."""
    clean_name = str(company_name).replace("/", " ").replace("-", " ")
    search_query = f'{clean_name} {city} California official website'
    url = "https://google.serper.dev/search"
    
    payload = json.dumps({"q": search_query, "num": 3})
    headers = {
        'X-API-KEY': SERPER_API_KEY,
        'Content-Type': 'application/json'
    }
    
    try:
        response = standard_requests.post(url, headers=headers, data=payload, timeout=10)
        
        if response.status_code != 200:
            print(f"\n  [!] GOOGLE SEARCH ERROR: Status {response.status_code} - {response.text}")
            return ""
            
        results = response.json()
        
        if "organic" in results and len(results["organic"]) > 0:
            return results["organic"][0].get("link", "")
    except Exception as e:
        print(f"\n  [!] GOOGLE SEARCH CRASHED: {e}")
    
    return ""

# ── STEP 2: Deeper, Stealthier Scraping ────────────────────────────────
def scrape_website_text(base_url):
    """Scrapes the main URL, extracts hidden emails, and hunts for a Contact page."""
    if not base_url or ".pdf" in base_url.lower():
        return ""
    
    combined_text = ""
    try:
        # 1. Scrape the primary URL
        response = stealth_requests.get(base_url, impersonate="chrome120", timeout=12)
        if response.status_code != 200:
            print(f"  [!] SCRAPER BLOCKED: Status {response.status_code} on main page.")
            return ""
            
        soup = BeautifulSoup(response.text, "html.parser")
        
        # 2. Extract hidden mailto: emails safely using .get()
        hidden_emails = []
        for a_tag in soup.find_all('a', href=True):
            href = a_tag.get('href', '').lower()
            if "mailto:" in href:
                email_raw = a_tag['href'].split("mailto:")[-1].split("?")[0].strip()
                if email_raw and email_raw not in hidden_emails:
                    hidden_emails.append(email_raw)
        
        if hidden_emails:
            combined_text += f"HIDDEN EMAILS FOUND IN HTML: {', '.join(hidden_emails)}\n\n"
            
        # Add the main page text
        combined_text += f"--- MAIN PAGE TEXT ---\n{soup.get_text(separator=' ', strip=True)}\n\n"
        
        # 3. Prioritize "Contact" over "About"
        contact_url = None
        about_url = None
        for a_tag in soup.find_all('a', href=True):
            link_text = a_tag.get_text().lower()
            href = a_tag.get('href', '').lower()
            
            if "contact" in link_text or "contact" in href:
                contact_url = urljoin(base_url, a_tag['href'])
                break 
            elif ("about" in link_text or "about" in href) and not about_url:
                about_url = urljoin(base_url, a_tag['href'])
                
        target_url = contact_url if contact_url else about_url

        if target_url and target_url != base_url:
            print(f"  -> Deep Scraping Secondary Page: {target_url}")
            try:
                time.sleep(2) 
                contact_response = stealth_requests.get(target_url, impersonate="chrome120", timeout=10)
                if contact_response.status_code == 200:
                    contact_soup = BeautifulSoup(contact_response.text, "html.parser")
                    combined_text += f"--- CONTACT/ABOUT PAGE TEXT ---\n{contact_soup.get_text(separator=' ', strip=True)}"
            except Exception:
                pass 

        return combined_text[:15000]
        
    except Exception as e:
        print(f"  [!] SCRAPER ERROR: {e}")
        return ""

# ── STEP 3: Enhanced Gemini Prompt (With 503 Retry Logic) ──────────────
def extract_contact_info_with_ai(company_name, website_text):
    """Feeds deep website text to Gemini with an auto-fallback loop and 503 retries."""
    if not website_text or len(website_text) < 50:
        return "", ""

    prompt = f"""
    You are an expert data extraction assistant. Read the following text scraped from a medical laboratory or clinic website.
    Your job is to find:
    1. The primary contact email address. PAY SPECIAL ATTENTION to the top of the text if it says "HIDDEN EMAILS FOUND IN HTML".
    2. The primary Contact Name (e.g., Clinic Director, Lead Doctor, Manager).
    
    Return ONLY a raw JSON object with no markdown formatting. Example:
    {{"email": "contact@lab.com", "contact_name": "Dr. John Smith"}}
    If you cannot find one of them, return an empty string "" for that field. Do not invent information.
    
    Company: {company_name}
    
    Website Text: 
    {website_text}
    """
    
    env_model = os.getenv("GENAI_MODEL", "gemini-1.5-flash").strip('"\' ')
    
    models_to_try = [
        env_model,
        "gemini-1.5-flash-latest",
        "gemini-2.5-flash",
        "gemini-1.5-pro"
    ]
    models_to_try = list(dict.fromkeys(models_to_try))
    
    last_error = ""
    
    # Retry loop: Try up to 3 times if we get a 503 error
    for attempt in range(3):
        for model_name in models_to_try:
            try:
                response = gemini_client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.0
                    )
                )
                
                # Robust parsing: Find the first '{' and last '}'
                raw_text = response.text.strip()
                start_idx = raw_text.find('{')
                end_idx = raw_text.rfind('}')
                
                if start_idx != -1 and end_idx != -1:
                    raw_text = raw_text[start_idx:end_idx+1]
                    
                ai_result = json.loads(raw_text)
                
                email = ai_result.get("email") or ""
                contact_name = ai_result.get("contact_name") or ""
                
                return str(email).strip(), str(contact_name).strip()
                
            except Exception as e:
                last_error = str(e)
                if "404" in last_error:
                    # Model not found/allowed, silently try the next model
                    continue 
                elif "503" in last_error:
                    print(f"  [!] High demand (503). Pausing for 30s before retry {attempt + 1}/3...")
                    time.sleep(30)
                    break # Break out of the inner loop, wait 30s, then restart the model list
                else:
                    print(f"  [!] GEMINI AI ERROR: {last_error}")
                    return "", ""

    print(f"  [!] ALL RETRIES FAILED. Last error: {last_error}")
    return "", ""

# ── MAIN EXECUTION ────────────────────────────────────────────────────
if __name__ == "__main__":
    print("="*60)
    print("Script 04: AI Enrichment (Production Run - Full Dataset)")
    print("="*60)

    df = pd.read_csv(INPUT_FILE, dtype=str).fillna("")
    
    for col in ["website_url", "contact_email"]:
        if col not in df.columns:
            df[col] = ""

    # FULL DATASET RUN
    run_df = df.copy()
    print(f"Total rows in dataset: {len(run_df)}\n")
    
    save_interval = 5 # Save progress every 5 rows

    for index, row in tqdm(run_df.iterrows(), total=len(run_df)):
        
        # --- CRITICAL SKIP LOGIC ---
        # If this row already has a website_url, skip it so we don't re-process it!
        if str(row.get("website_url", "")).strip() != "":
            continue
            
        company_name = row["lab_name"]
        city = row["city"]
        
        print(f"\n--- [{index}] {company_name} ---")
        
        url = get_company_url(company_name, city)
        
        # If no URL is found, we write "NOT FOUND" so we know to skip it next time
        run_df.at[index, "website_url"] = url if url else "NOT FOUND"
        
        if url:
            print(f"  -> Found URL: {url}")
            web_text = scrape_website_text(url)
            
            # Increased sleep timer to 10 seconds to aggressively prevent 503 errors
            time.sleep(10) 
            
            email, ai_name = extract_contact_info_with_ai(company_name, web_text)
            
            print(f"  -> Found Email: {email if email else 'NONE'}")
            print(f"  -> Found Name : {ai_name if ai_name else 'NONE'}")
            
            run_df.at[index, "contact_email"] = email
            
            if row.get("official_name", "") == "" and ai_name != "":
                run_df.at[index, "official_name"] = ai_name
                run_df.at[index, "official_title"] = "Found via Website"
        else:
            print("  -> NO URL FOUND! Skipping scrape.")
            
        if index % save_interval == 0:
            run_df.to_csv(OUTPUT_FILE, index=False)

    # Final save
    run_df.to_csv(OUTPUT_FILE, index=False)
    print(f"\n Full Production Run complete! Check {OUTPUT_FILE} for results.")