## Commercial Labs Analysis

AI-assisted pipeline for discovering, validating, and geocoding commercial wet labs in the United States.

## Key Features
- Multi-Source Ingestion: Connectors for FDA HCT/P, Blood Registrations, SEC Form D, YCombinator, NIH Reporter, Biopharmguy, and Environmental Labs.
- Entity Resolution: Cross-source clustering using weighted string similarity and address blocking.
- Page-Targeted LLM Extraction: Async navigation scraping to extract missing attributes (website_url, contact_name, services, is_gmp_facility, is_commercial).
- Google Maps Geocoding: Validates physical addresses and appends latitude and longitude coordinates with caching.
- Pre-Database Quality Validation: Automatic pre-load check measuring field fill rates and primary key integrity.

## Project Structure
```text
.
├── config
│   ├── connectors
│   │   ├── biopharmguy_cmos.yaml
│   │   ├── biopharmguy_cros.yaml
│   │   ├── biopharmguy_therapeutics.yaml
│   │   ├── fda_blood.yaml
│   │   ├── fda_drug_registration.yaml
│   │   ├── fda_hct.yaml
│   │   ├── form_d.yaml
│   │   ├── nih_reporter.yaml
│   │   └── ycombinator.yaml
│   ├── schema.yaml
│   └── source_mappings
│       ├── biopharmguy.yaml
│       ├── env_labs.yaml
│       ├── fda_blood.yaml
│       ├── fda_drug_registration.yaml
│       ├── fda_hct.yaml
│       ├── form_d.yaml
│       ├── nih_reporter.yaml
│       └── ycombinator.yaml
├── PROJECT_SPEC.md
├── README.md
├── requirements.txt
├── ROADMAP.md
├── scripts
│   ├── connectors
│   │   ├── biopharmguy_cmos.py
│   │   ├── biopharmguy_cros.py
│   │   ├── biopharmguy_therapeutics.py
│   │   ├── env_labs.py
│   │   ├── fda_blood.py
│   │   ├── fda_drug_registration.py
│   │   ├── fda_hct.py
│   │   ├── form_d.py
│   │   ├── nih_reporter.py
│   │   └── ycombinator.py
│   ├── pipeline
│   │   ├── combine_standardized.py
│   │   ├── dedup_and_tag_entities.py
│   │   ├── entity_resolution.py
│   │   ├── geocode_entities_google.py
│   │   ├── llm_infer.py
│   │   ├── standardize.py
│   │   └── validate_before_db.py
│   ├── run_pipeline.py
│   └── storage
│       └── snapshot_manager.py
└── tests
    ├── test_fda_hct.py
    └── test_geocode_google.py
```

## Run Pipeline Orchestrator
```text
# Run Environmental Labs Domain
python3 scripts/run_pipeline.py --domain env

# Run Pharma / Biotech Domain
python3 scripts/run_pipeline.py --domain pharma
```

## Pipeline Architecture

```text
[ Connectors / Extractors ] (APIs, Web Scrapers, SEC Form D, FDA)
            │
            ▼
[ Stage 1: Standardization ] (Schema mapping via config/source_mappings/*.yaml)
            │
            ▼
[ Stage 2: Stacking ] (Combines standardized outputs into auto_combined_raw.csv)
            │
            ▼
[ Stage 3: Entity Resolution ] (Blocking & string similarity matching across sources)
            │
            ▼
[ Stage 4: LLM Inference ] (Async page scraping & field enrichment via gpt-5.4-nano)
            │
            ▼
[ Stage 5: Dedup & Tagging ] (Company name normalization, HQ vs. Branch tagging)
            │
            ▼
[ Stage 6: Google Geocoding ] (Rooftop/approximate lat/lng coordinate resolution)
            │
            ▼
[ Stage 7: Quality Gate ] (Validate completeness, PK uniqueness, & fill rate bounds)
```

## How to add a new data source
1. Connector Configuration (config/connectors/<source_id>.yaml): Define URLs, API headers, or scraper CSS/XPath selectors.
2. Connector Script (scripts/connectors/<source_id>.py): Implement the standard extract(headless=True) -> pd.DataFrame function.
3. Register Source (scripts/run_pipeline.py): Import the new script and register it inside the DOMAIN_CONNECTORS dictionary ("pharma" or "env").
4. Source Mapping (config/source_mappings/<source_id>.yaml): Map the raw output headers from the script to the standardized field names defined in schema.yaml.

## How to add a new field
1. Central Schema (config/schema.yaml): Append the new field name, data type, and default fallback value (null).
2. Source Mappings (config/source_mappings/*.yaml): Map raw column names from applicable sources to your new target field key.
3. Downstream Pipeline Updates:

3.1 If extracted via LLM (scripts/pipeline/llm_infer.py):
(1) Add the field to relevant category pages in FIELD_TO_CATEGORIES
(2) Ensure all referenced categories exist in CATEGORY_KEYWORDS (to avoid KeyErrors during page discovery)
(3) Add extraction instructions and JSON shape rules in build_extraction_prompt()
(4) Pass the new field name in the fields=[...] list inside run_pipeline.py

3.2 If checked before DB export (scripts/pipeline/validate_before_db.py): Add the field name to target_fields so the validator measures its fill rate and logs its AI status breakdown