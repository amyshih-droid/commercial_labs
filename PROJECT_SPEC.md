# PROJECT_SPEC.md

## Commercial Lab Mapping & Business Development Database

**Version:** 0.2 (Phase 0 — Scope Definition, all decisions resolved)
**Last updated:** 2026-06-26
**Author:** Amy Shih

---

## 1. Project summary

Build a clean, structured, geocoded database of commercial wet labs across all US states.
The output is a CSV file (and future dashboard) that serves business development prospecting.

---

## 2. Final goal vs. MVP

This project is delivered in phases. The final goal covers all two lab types across
all 50 states. Each phase is a complete, usable deliverable on its own.


| Phase | T                     | Geography            | Status                 |
| ----- | --------------------- | -------------------- | ---------------------- |
| 1     | Clinical / diagnostic | California -> all 50 | On hold (not priority) |
| 2     | Pharma / biotech      | California -> all 50 | In Progress            |


**Why this order:**

- Phase 1 (clinical) uses a single free national download (CMS CLIA database) — fastest to
validate the pipeline and schema with real data.
- Phase 2 (pharma/biotech) has no central registry — requires creative sourcing and is
best attempted with a mature, working pipeline.

---



## 3. Lab type definitions


| Lab type              | What they do                                   | Regulatory body    | Primary data source                        |
| --------------------- | ---------------------------------------------- | ------------------ | ------------------------------------------ |
| Clinical / diagnostic | Test patient specimens (blood, urine, tissue)  | CMS (CLIA program) | CMS CLIA database — data.cms.gov (free)    |
| Pharma / biotech      | Drug R&D, QC testing, contract research (CROs) | FDA (varies)       | Company websites, LinkedIn, paid databases |


---



## 4. Geographic scope

- **MVP (Phase 1):** California only
  - Rationale: largest lab count in the US, good variety of lab types, CMS data is
  complete for CA.
  - Validate the full pipeline (collection → cleaning → geocoding → CSV output)
  on CA before expanding.
- **Scale-up:** Extend the same pipeline to all 50 states after MVP is validated.

---



## Phase 2 - Pharma / biotech



## Pharma / biotech Company websites


| Source                                                                                                                               | Unique value                                                                                          |
| ------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------- |
| FDA                                                                                                                                  | Official manufacturing establishments                                                                 |
| NIH                                                                                                                                  | Early-stage R&D companies receiving federal funding                                                   |
| BioPharmGuy                                                                                                                          | Broad commercial biotech ecosystem (therapeutics, CROs, CDMOs, services)                              |
| SEC Form D                                                                                                                           | Companies that have raised private capital but haven't yet hit FDA registration or NIH funding.       |
| -Incubator/accelerator rosters (Y Combinator, MassChallenge HealthTech, Illumina Accelerator, Berkeley SkyDeck, Activate Fellowship) | Stealth-stage wet-lab startups go through a small set of gatekeepers before they have their own space |
| -LinkedIn job postings                                                                                                               | Real-time signal, catches pre-funding stealth too                                                     |




### Methods and number of collected labs


| Data Source(s)                                   | Included                                                                                 | Filtering                                                                                                                                   | # of labs                | data retrieval                                                                                                                                                   |
| ------------------------------------------------ | ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| FDA Drug Establishment Registration              | Pharma & Therapeutics (Commercial/Manufacturing) & CMOs & CDMOs (Contract Manufacturing) | 1. MANUFACTURE & API MANUFACTURE 2. only in USA                                                                                             | 10,310 -> 7,459 -> 3,543 | manual download                                                                                                                                                  |
| HUMAN CELL AND TISSUE ESTABLISHMENT REGISTRATION | Pharma & Therapeutics & CMOs & CDMOs                                                     | 1. Establishment Function = Process 2. Establishment Status = Registered or Pre-registered                                                  | 5,280 -> 2,097 -> 1,111  | web scraping                                                                                                                                                     |
| Blood Establishment Registration (BER) database  | Pharma & Therapeutics & CMOs & CDMOs                                                     | 1. PRODUCT TESTING LABORATORY, PLASMAPHERESIS CENTER, COMPONENT PREPARATION FACILITY 2. Establishment Status = Registered or Pre-registered | 2038 -> 1,295            | web scraping                                                                                                                                                     |
| NIH RePORTER API                                 | Pharma & Therapeutics (Pure R&D / Pre-Clinical)                                          | activity_codes: "R41", "R42", "R43", "R44"                                                                                                  | 180                      | API                                                                                                                                                              |
| BioPharmGuy                                      | Direct VC-Backed Startups, Corporate Spin-offs & Incubators                              | 1. Biologics + RNA, Peptide & Gene Therapy, Antibodies, Small Molecules, Stem Cells, Radiopharmaceuticals, Tissue Engineering 2. only US    | 6,287 -> 3,094           | web scraping                                                                                                                                                     |
| BioPharmGuy                                      | CMOs & CDMOs                                                                             | 1. (All Contract Manufacturing) 2. only US                                                                                                  | 1,190 -> 514             | web scraping                                                                                                                                                     |
| BioPharmGuy                                      | CROs                                                                                     | 1. (All Contract Research) (All Scientific Services) & 2. only US                                                                           | 1,177 -> 515             | web scraping                                                                                                                                                     |
| SEC Form D                                       | Direct VC-Backed Startups, Corporate Spin-offs, Early-stage/Stealth Startups             | industryGroupType = "Biotechnology" or "Pharmaceuticals" & SIC code (8731, 2836, 8071, 2834) & 2023Q1–2026Q2                                | 1,967 -> 1,835           | (1) bulk download of SEC's quarterly Form D structured data ZIP (2) API calls to data.sec.gov/submissions/CIK##########.json per company for SIC code enrichment |
| Incubator: Y Combinator                          | Stealth-stage wet-lab startups                                                           | Drug Discovery and Delivery & Industrial Bio & Therapeutics & United States of America                                                      | 135 -> 124               | API                                                                                                                                                              |
| OpenAI Web Search                                | Specialized Biotech & Private CDMOs (e.g., cell therapy startups)                        |                                                                                                                                             |                          | gpt-5.4-nano model inference for missing values                                                                                                                  |




### Pipeline for preprocessing and standardizing



#### Step 1 — Standardize

This step maps any raw source into the canonical master schema (config/schema.yaml)

#### Step 2 - Combine standardized datasets from different sources

This step concatenates all standardized/*.csv files into one raw combined table, before any dedup/entity resolution happens.

#### Step 3 - Remove duplicates

The step collapses the combined multi-source table down to one row per real-world physical facility, using fuzzy Company name + city matching 

#### Step 4 - Use LLM to infer missing values in url, address, email, contact name

The step fills in missing fields (address_street, contact_name, contact_email) using a WEBSITE-FIRST, PAGE-TARGETED strategy

#### Step 5 - Evaluate the output of LLM inference (modifying prompts and adding fallback)



##### 1. I performed an iterative **A/B Comparative Evaluation** between:

- **Baseline (v1):** Standard HTML scraper + naive web search.
- **Modified (v2):** Enhanced pipeline with legal name cleaning (`clean_company_name`), suite-preserving address prompts, and multi-field grounded search fallback for contact details.



##### 2. This is the Quantitative Results & Metrics


| Field            | Baseline (v1) Filled | Modified (v2) Filled | Net Gain | Final Coverage | Key Quality Metric                                        |
| ---------------- | -------------------- | -------------------- | -------- | -------------- | --------------------------------------------------------- |
| `website_url`    | 26 / 30              | 26 / 30              | +0       | **86.7%**      | Eliminated false positive; found missing domain (`AFMC`). |
| `address_street` | 27 / 30              | 27 / 30              | +0       | **90.0%**      | Suite/Floor granularity jumped from **3.3% to 53.3%**.    |
| `contact_name`   | 18 / 30              | 24 / 30              | **+6**   | **80.0%**      | **+20.0%** gain; upgraded roles to Lead MDs/Directors.    |
| `contact_email`  | 8 / 30               | 13 / 30              | **+5**   | **43.3%**      | **+16.6%** gain; recovered JS/Cloudflare blocked emails.  |




##### 3. Key Pipeline Improvements Verified



###### A. Legal Entity Name Cleaning (`clean_company_name`)

- **Problem:** Database strings like `"A2CL Services LLC (dba ACL Laboratories)"` distorted search engine queries.
- **Fix:** Stripped legal boilerplate before running web search queries.
- **Result:** Unlocked previously missed domains (e.g., `AFMC Diamond Lab` $\rightarrow$ `https://afmc.com/locations/diamond-bar/`).



###### B. Address Suite & Unit Preservation

- **Problem:** Prompt rules stripped suite/unit numbers, returning generic street names.
- **Fix:** Updated extraction and search fallback prompts to mandate suite/floor retention.
- **Result:** 16 out of 30 addresses now contain exact room/suite numbers (e.g., `200 Quinn Drive, Suite 120`).



###### C. Grounded Contact Search Fallback

- **Problem:** HTML scrapers failed on JavaScript-heavy websites or Cloudflare-protected pages, leaving contact names/emails blank.
- **Fix:** Added `search_contact_fallback_async()` to query search indexes when HTML scraping returns `null`.
- **Result:** Discovered 6 new contact names and 5 new contact emails while maintaining 100% precision.



##### 4. Sample Verification Audit


| Entity ID      | Facility Name              | Verified Output (`llm_infer_v2`)                                                | Status / Source                   |
| -------------- | -------------------------- | ------------------------------------------------------------------------------- | --------------------------------- |
| **ENT-000002** | AFMC Diamond Lab           | `Dr. Rafif Z. Moussa`                                                           | Extracted via HTML (`afmc.com`)   |
| **ENT-000004** | AHN Center for Repro. Med. | `200 Quinn Drive, Suite 120` `Dr. Terrence D. Lewis, MD, PhD`                   | Found via Fallback Search         |
| **ENT-000005** | ALLcare Fertility Center   | `369 Lexington Ave, Floor 6` `Dr. Mingxue Yang` `info@allcarefertility.com`     | Extracted + Fallback Email Search |
| **ENT-000012** | Adore Fertility LLC        | `1280 Hospital Drive, Suite 300` `Dr. Jeris Cox, MD` `admin@adorefertility.com` | Found via Fallback Search         |
| **ENT-000026** | Advocate Lutheran Hospital | `Dr. Arth Srivastava` `LGH-PatientRelations@aah.org`                            | Extracted + Fallback Email Search |




#### 5. Conclusion & Decision

**Decision:** Approved for full production run across the 10,781 master dataset.
**Run Summary:** 


| Metric            | Result     |
| ----------------- | ---------- |
| Runtime           | 01:15:01   |
| Prompt Tokens     | 12,250,501 |
| Completion Tokens | 479,276    |
| Total Tokens      | 12,729,777 |
| Total expense     | ~$0.80     |



| Field          | Total Processed | Missing | Missing Rate |
| -------------- | --------------- | ------- | ------------ |
| Website URL    | 7,012           | 602     | 8.6%         |
| Street Address | 5,690           | 228     | 4.0%         |
| Contact Name   | 7,307           | 2,452   | 33.6%        |
| Contact Email  | 7,487           | 1,495   | 20.0%        |




#### Step 6 - Postprocessing and geocoding

Appends latitude/longitude to deduplicated facilities using the Google Maps Geocoding API.

#### Step 7 - Turn the existing pipeline into a measurable, testable, reproducible system.

1. tests/
  └── Unit + integration tests
2. evaluation/
  └── Golden dataset + automated evaluation
3. reports/
  └── Data quality + cost + coverage reports
4. run_pipeline.py
  └── One-command end-to-end execution



## Phase 1 - Clinical / diagnostic



## 5. Data schema



### 5a. Primary fields (collect from day one)


| Field name         | Type    | Null value | Description                                                      | Example                            |
| ------------------ | ------- | ---------- | ---------------------------------------------------------------- | ---------------------------------- |
| `lab_id`           | string  | —          | Unique ID (generated: source_state_rownum). Never null.          | `CLIA_CA_001`                      |
| `company_name`     | string  | —          | Legal name of the parent company. Never null.                    | `Quest Diagnostics Inc.`           |
| `lab_name`         | string  | `""`       | Name of this specific location (may equal company_name)          | `Quest Diagnostics – Burbank`      |
| `street_address`   | string  | `""`       | Street number and name only                                      | `1234 Olive Ave`                   |
| `city`             | string  | `""`       | City name                                                        | `Burbank`                          |
| `state`            | string  | —          | 2-letter state code. Never null.                                 | `CA`                               |
| `zip_code`         | string  | `""`       | 5-digit ZIP — stored as string to preserve leading zeros         | `91502`                            |
| `is_hq`            | boolean | `""`       | TRUE if this location is company headquarters, FALSE if branch   | `FALSE`                            |
| `company_url`      | string  | `""`       | Company or lab website URL                                       | `https://www.questdiagnostics.com` |
| `contact_name`(-)  | string  | `""`       | Name of the BD-reachable contact (lab director or sales contact) | `Dr. Jane Smith`                   |
| `contact_email`(-) | string  | `""`       | Email address for BD outreach. Use `""` if not found.            | `jsmith@questdiag.com`             |
| `latitude`         | float   | `""`       | Geocoded latitude — populated via Nominatim in Phase 3 (clean)   | `34.1808`                          |
| `longitude`        | float   | `""`       | Geocoded longitude — populated via Nominatim in Phase 3 (clean)  | `-118.3090`                        |
| `lab_type`         | string  | —          | One of: clinical / environmental / pharma. Never null.           | `clinical`                         |
| `data_source`      | string  | —          | Origin of this record. Never null.                               | `CMS_CLIA`                         |




### 5b. Secondary fields (add when available, leave `""` if not)


| Field name       | Type   | Null value | Description                                    |
| ---------------- | ------ | ---------- | ---------------------------------------------- |
| `phone_number`   | string | `""`       | Main phone number for the lab location         |
| `clia_number`    | string | `""`       | CLIA certification number (clinical labs only) |
| `accreditation`  | string | `""`       | Accreditation body (CAP, A2LA, etc.)           |
| `specialty`      | string | `""`       | Lab specialty (e.g. microbiology, pathology)   |
| `date_collected` | string | `""`       | ISO date when this record was collected        |




### 5c. Null value convention

All missing or unknown values use an **empty string** `""` in the CSV.

- Do NOT use `NA`, `N/A`, `null`, `NULL`, `[]`, or `None`.
- Reason: `""` is read as `NaN` by Python/Pandas automatically, displays as a blank
cell in Excel, and is unambiguous across tools.
- Fields marked "Never null" (`lab_id`, `company_name`, `state`, `lab_type`,
`data_source`) must always have a value. If they can't be filled, the record
should be flagged for review rather than included.

---



## 6. HQ, branches, and what counts as a duplicate

some branches were wrongly identified as duplicates by the same city name but different states. When using tag city@company, it may cause issues.

### HQ vs. branch — NOT duplicates

A company may have one headquarters and many branch locations.
These are **distinct physical places** and each gets its own row.
The `is_hq` flag distinguishes them so BD reps know which locations
are decision-making hubs vs. collection/testing sites.

```
company_name          | lab_name                    | street_address   | city     | is_hq
Quest Diagnostics Inc | Quest Diagnostics – HQ      | 500 Plaza Dr     | Secaucus | TRUE
Quest Diagnostics Inc | Quest Diagnostics – Burbank | 1234 Olive Ave   | Burbank  | FALSE
Quest Diagnostics Inc | Quest Diagnostics – Fresno  | 789 Cedar St     | Fresno   | FALSE
```

All three rows are valid and should be kept.

### True duplicates — same physical location, two sources

A duplicate occurs when the **same physical lab** appears twice because two
different data sources both listed it.

**Detection strategy (in order of reliability):**

1. Match on `clia_number` (exact match = definite duplicate, for clinical labs)
2. Match on `street_address` + `zip_code` (same building = likely duplicate)
3. Match on `company_name` + `city` + `state` (fuzzy — use as a flag for review)

**Resolution:** Keep the record with the most complete fields. Merge any
fields the other record has that this one is missing.

---



## 7. Geocoding


| Decision       | Choice               | Rationale                                                  |
| -------------- | -------------------- | ---------------------------------------------------------- |
| Geocoding tool | US Census Geocoder   | Free, no API key required, no rate limit, batch processing |
| Python library | `censusgeocode`      | `import census`                                            |
| Input          | Batch csv            | id, street, city, state, zip - sent as one batch call      |
| On failure     | `""`                 | retry with simplified input, then `""` if still unmatched  |
| Phase 1 volume | 23,323 national labs | Batch complete in 60 seconds                               |


---



## 8. Intended users and their needs


| User           | What they do with the data                              | Key fields they rely on                                       |
| -------------- | ------------------------------------------------------- | ------------------------------------------------------------- |
| BD / sales rep | Cold-outreach to labs to sell equipment, reagents, SaaS | contact_name, contact_email, phone_number, company_url, is_hq |


---



## 9. Deliverables


| Deliverable    | Format          | Description                                            |
| -------------- | --------------- | ------------------------------------------------------ |
| Primary output | CSV             | Clean, geocoded lab database, one row per lab location |
| Documentation  | README.md       | Explains sources, schema, and how to reproduce         |
| This file      | PROJECT_SPEC.md | Lives in repo root. Version-controlled.                |


---



## 10. Out of scope

- Academic / university research labs
- Hospital labs not independently listed
- Labs outside the United States
- Real-time data updates (static snapshot only for MVP)
- Financial data (revenue, employee count)
- Contact emails
- Environmental testing labs

---



## 11. Success criteria

Phase 1 is complete when:

- [x] All California clinical labs from CMS CLIA are collected and parsed
- [x] Zero duplicate `lab_id` values in the final CSV
- [x] All primary schema fields present (empty string `""` for nulls — no mixed conventions)
- [x] `street_address`, `city`, `state`, `zip_code` are in separate columns (not combined)
- [x] ≥ 80% of records have valid geocoordinates (lat/lon not empty)
- [x] CSV is UTF-8 encoded, comma-delimited, readable in Excel and Python
- [x] README explains how to reproduce the dataset from scratch
- [ ] Example of name: HQ Lab @ company name; TX @ company name

---



## 12. Resolved decisions log


| Decision                             | Resolution                                               | Resolved |
| ------------------------------------ | -------------------------------------------------------- | -------- |
| Lab type scope                       | All 3 types; phased — clinical first                     | ✓        |
| Geographic MVP                       | California → scale to all 50                             | ✓        |
| Address format                       | Split: street_address, city, state, zip_code             | ✓        |
| contact_name / contact_email meaning | BD-reachable contact (lab director or sales contact)     | ✓        |
| Null value convention                | Empty string `""` throughout                             | ✓        |
| HQ vs. branch = duplicates?          | No — keep both. Deduplicate on address/CLIA number.      | ✓        |
| Geocoding service                    | Nominatim via geopy (free; 1 req/sec rate limit)         | ✓        |
| Commercial lab filter                | PGM_TRMNTN_CD=00 + GNRL_CNTL_TYPE_CD=04 + SKLTN_REC_SW=N | ✓        |
| ELGBLTY_SW filter                    | Dropped — only affects 2 records, negligible impact      | ✓        |




## 13. Filtering decisions — CMS CLIA California



### Definition of commercial lab (this project)

A commercial lab is a standalone, privately operated testing facility
that is not affiliated with a government body, academic institution,
or non-profit hospital system.

Specifically excluded:

- Government public health labs
- University / academic research labs (UCSD, UCLA, UCI etc.)
- Hospital internal labs (Kindred, Kaiser, St. Francis etc.)
- Physician office labs doing basic in-house testing only
- Community health clinics
- Non-profit health systems (MemorialCare, Sutter, Providence etc.)
- Research institutes



### Structural filters — final version


| Filter          | Column            | Value | Rationale                                 |
| --------------- | ----------------- | ----- | ----------------------------------------- |
| Active only     | PGM_TRMNTN_CD     | '00'  | Removes closed/terminated labs            |
| Privately owned | GNRL_CNTL_TYPE_CD | '04'  | Removes govt and non-profit orgs          |
| Real record     | SKLTN_REC_SW      | 'N'   | Removes ghost/incomplete skeleton entries |




### Known data limitation

GNRL_CNTL_TYPE_CD = '04' means privately owned (CMS definition),
not commercial reference lab (BD definition). These are different:

- For-profit hospitals (Kindred, Concentra) are correctly coded 04
by CMS but their internal labs are not standalone commercial targets
- Some non-profit entities (Kaiser) may be miscoded as 04 in CMS —
a known data quality issue in the source database itself
- CMS has no field for "accepts external specimens" — the core
definition of a standalone commercial reference lab



### Phase 1 - Clinical / diagnostic from CMS CLIA database


| Row counts at each filter stage                        | Rows    |
| ------------------------------------------------------ | ------- |
| Raw National records (NY, CA, WA)                      | 676,051 |
| cleaning (Active only + Privately owned + Real record) | 23,323  |
| Geocoded                                               | 20,547  |
| company url                                            | on hold |


