# Project Roadmap & Future Refactoring

## Current Status (Completed)
- [x] added new source for env data
- [x] Initial automated orchestrator in `run_pipeline.py` for pharma/env data
- [x] Basic field (address_street, contact_name, contact_email, is_gmp_facility, is_commercial) inference script `04_llm_infer.py` with web search fallbacks

## Phase 3: Transition to Config-Driven Architecture (Next Up)
- [ ] **Centralize Schema Definitions:** Move all field types, defaults, and validation rules to `config/schema.yaml`.
- [ ] **Harmonize Source Mappings:** Standardize raw-to-schema field names across `config/source_mappings/*.yaml`.
- [ ] **Company-Level Inference Optimization:** Refactor `04_llm_infer.py` to deduplicate by `company_name` before triggering LLM calls (prevents duplicate API queries for chain locations like CSL Plasma/BioLife).
- [ ] **Fix GMP Fallback Overwrites:** Update Step 8 in `04_llm_infer.py` to only query Google if web scraping returns `None` (preventing `"no_explicit_gmp"` from triggering unnecessary web searches).
- [ ] **Pre-Database Validation Gate:** Implement `scripts/pipeline/08_validate_before_db.py` to check primary key uniqueness, lat/long boundary ranges, and column alignments before DB export.