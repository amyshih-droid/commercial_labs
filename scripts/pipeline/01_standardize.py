"""
standardize.py — config-driven engine to map any raw source into the
canonical master schema (config/schema.yaml).

This script contains NO source-specific logic. Every difference between
sources (which columns exist, what's missing) lives in
config/source_mappings/<source>.yaml. Adding a 9th, 10th, 11th source
means writing a new YAML file, not touching this script.

IMPORTANT: run this from the project root (the "analysis" folder), not
from inside scripts/pipeline/ - it looks for config/ and data/ relative
to the current working directory, not relative to this script's own
location. This matches how every other script in this project expects
to be run (see scripts/phase2_pharma/*.py for the same convention).

Usage (run from analysis/):
    python3 scripts/pipeline/standardize.py --source nih_reporter \
        --raw-file data/phase2_pharma/raw/03a_nih_reporter_preclinical_raw.csv \
        --out data/phase2_pharma/standardized/nih_reporter_standardized.csv
"""

import argparse
import hashlib
import re
from pathlib import Path

import pandas as pd
import yaml

CONFIG_DIR = Path("config")
SCHEMA_PATH = CONFIG_DIR / "schema.yaml"

# Reusable across any source that stores full US state names instead of
# 2-letter abbreviations (e.g. "Texas" instead of "TX"). Referenced via
# composite_fields field_map entries with normalize: us_state_name_to_abbr
# in a source's mapping YAML - see fda_bio_hctp.yaml for an example.
US_STATE_NAME_TO_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}
US_STATE_ABBRS = set(US_STATE_NAME_TO_ABBR.values())

CANADA_PROVINCE_ABBRS = {
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT",
}


def classify_region_prefix(prefix):
    """Some sources embed a region hint before the city name, separated
    by a dash - e.g. 'Ab - Calgary' (Canadian province), 'Argentina -
    Buenos Aires' (a country name), or 'Ca - Azusa' (US state). This
    figures out which kind of prefix it is and returns (state, country):
      - a 2-letter match against a US state abbreviation -> (state, 'US')
      - a 2-letter match against a Canadian province -> (province, 'Canada')
      - anything else -> (None, <the raw prefix text as the country>)
    Deterministic lookup - no LLM needed for this kind of classification.
    """
    if pd.isna(prefix) or not str(prefix).strip():
        return (None, None)
    p = str(prefix).strip()
    p_upper = p.upper()
    if p_upper in US_STATE_ABBRS:
        return (p_upper, "US")
    if p_upper in CANADA_PROVINCE_ABBRS:
        return (p_upper, "Canada")
    return (None, p)


def classify_sec_state_or_country(code):
    """SEC's EDGAR filings use a state/country code field where US states
    are normal 2-letter abbreviations, but non-US locations get SEC's own
    special codes (e.g. 'A8' = Quebec, Canada). We don't have the full
    SEC code-to-country table built in, so: a recognized US state
    abbreviation -> (state, 'US'); anything else -> (None, 'Non-US') as a
    generic marker. If specific non-US country names are needed later,
    the full SEC state/country code table would need to be added here.
    """
    if pd.isna(code) or not str(code).strip():
        return (None, None)
    c = str(code).strip().upper()
    if c in US_STATE_ABBRS:
        return (c, "US")
    return (None, "Non-US")


def load_yaml(path: Path) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


def make_hash_id(row: pd.Series, fields: list) -> str:
    """Builds a stable, repeatable ID from a set of fields when no native
    unique identifier exists in the source. Same inputs always produce
    the same ID, so re-running the pipeline doesn't create duplicate IDs
    for the same real-world record (idempotency)."""
    raw = "|".join(str(row.get(f, "") or "") for f in fields)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def apply_direct_mappings(raw_df: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    """Renames raw columns into canonical field names per the mapping
    config. Warns (does not crash) if an expected raw column is missing,
    since schema drift across quarters/sources is expected, not
    exceptional - see prior debugging in this project for examples.

    Also strips leading/trailing whitespace from every value. Some
    sources (e.g. DRLS registration exports) pad every field with spaces
    - e.g. " Multi-Kare, Inc. " - which, left unstripped, would make
    entity resolution treat that and "Multi-Kare, Inc." as two different
    companies. Since data is loaded with dtype=str, this is safe to apply
    universally rather than special-casing it per source.
    """
    out = pd.DataFrame(index=raw_df.index)
    for canonical_field, raw_col in mapping.get("direct_mappings", {}).items():
        if raw_col in raw_df.columns:
            out[canonical_field] = raw_df[raw_col].str.strip()
        else:
            print(f"  Warning: expected raw column '{raw_col}' not found "
                  f"for canonical field '{canonical_field}' - filling with null.")
            out[canonical_field] = None
    return out


def resolve_composite_fields(raw_df: pd.DataFrame, out: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    """Some sources combine several canonical fields into one raw column
    - e.g. a single 'ADDRESS' string like:
        "555 Armour Street, Tifton, Georgia (GA) 31794, United States (USA)"
    instead of separate street/city/state/zip columns. Rather than an LLM
    call (slower, costs money, non-deterministic), this uses a regex
    pattern - declared per-source in the mapping YAML - to split the
    combined value deterministically. Reusable across any source with a
    similarly-structured combined field, not just this one.
    """
    for group_name, cfg in mapping.get("composite_fields", {}).items():
        raw_col = cfg["raw_column"]
        if raw_col not in raw_df.columns:
            print(f"  Warning: composite field raw column '{raw_col}' not found "
                  f"for group '{group_name}' - skipping.")
            continue

        pattern = re.compile(cfg["pattern"])
        field_map = cfg.get("field_map", {})
        values = raw_df[raw_col].str.strip()

        matches = values.apply(lambda v: pattern.match(v) if pd.notna(v) else None)
        unmatched_count = matches.isna().sum()
        if unmatched_count > 0:
            print(f"  Warning: {unmatched_count}/{len(matches)} rows in "
                  f"'{raw_col}' did not match the expected pattern for "
                  f"'{group_name}' - those rows will have null values for "
                  f"the affected fields. Inspect and adjust the regex if "
                  f"this count seems high.")

        for canonical_field, field_spec in field_map.items():
            # field_spec can be a plain string (just the regex group name),
            # or a dict like {"group": "state", "normalize": "us_state_name_to_abbr"}
            # for cases where the raw value needs converting before it's
            # usable - e.g. a full state name like "Texas" instead of "TX".
            if isinstance(field_spec, dict):
                group_key = field_spec["group"]
                normalize = field_spec.get("normalize")
            else:
                group_key = field_spec
                normalize = None

            # Strip whitespace AND stray leading/trailing commas as a
            # defensive backstop - the pattern itself shouldn't produce
            # these anymore (see the [^,()]+? fix for city/state/country),
            # but this guards against any similarly-shaped edge case in
            # data we haven't seen yet.
            values = matches.apply(
                lambda m, g=group_key: m.group(g).strip().strip(",").strip() if m else None
            )

            if normalize == "us_state_name_to_abbr":
                def _normalize_state(v):
                    if not v:
                        return v
                    v_clean = v.strip()
                    if v_clean.lower() in US_STATE_NAME_TO_ABBR:
                        return US_STATE_NAME_TO_ABBR[v_clean.lower()]
                    if len(v_clean) == 2 and v_clean.isalpha():
                        return v_clean.upper()  # already an abbreviation
                    return v_clean  # unrecognized - left as-is, will likely
                                      # fail a downstream US-state filter,
                                      # which is a useful signal to inspect
                values = values.apply(_normalize_state)

            out[canonical_field] = values

        # classify_fields: for cases where ONE extracted group needs to
        # produce TWO canonical fields via a lookup/classification, not
        # just a rename or simple normalize - e.g. a city-prefix like
        # "Ab" that could mean a US state, a Canadian province, or a
        # literal country name depending on what it matches.
        for spec in cfg.get("classify_fields", []):
            group_key = spec["group"]
            classify_fn_name = spec["classify"]
            state_field = spec.get("state_field")
            country_field = spec.get("country_field")

            if classify_fn_name == "region_prefix_us_ca_or_country":
                classify_fn = classify_region_prefix
            elif classify_fn_name == "sec_state_or_country_code":
                classify_fn = classify_sec_state_or_country
            else:
                raise ValueError(f"Unknown classify function '{classify_fn_name}'")

            raw_values = matches.apply(
                lambda m, g=group_key: m.group(g).strip() if (m and m.group(g)) else None
            )
            classified = raw_values.apply(classify_fn)

            if state_field:
                out[state_field] = classified.apply(lambda t: t[0])
            if country_field:
                out[country_field] = classified.apply(lambda t: t[1])
    return out


def resolve_source_record_id(raw_df: pd.DataFrame, out: pd.DataFrame, mapping: dict) -> pd.Series:
    """Three ways a source can supply source_record_id, in priority order:
    1. A native unique column in the raw data (best - e.g. NIH's grant_id,
       FDA's FEI number, SEC's ACCESSIONNUMBER)
    2. A constructed hash of specified fields (fallback for sources with
       no native ID, e.g. BioPharmGuy)
    3. Error if neither is configured - this is a required field, silent
       nulls here would break entity resolution traceability later.
    """
    native_col = mapping.get("source_record_id_field")
    if native_col:
        if native_col in raw_df.columns:
            cleaned = raw_df[native_col].str.strip()
            # Some exports store integer-like IDs as floats (e.g. FEI
            # "3013178227.0" instead of "3013178227"). Strip a trailing
            # ".0" so this doesn't silently corrupt the ID used for
            # cross-source matching later.
            cleaned = cleaned.str.replace(r"\.0$", "", regex=True)
            return cleaned
        print(f"  Warning: configured source_record_id_field '{native_col}' "
              f"not found in raw data - falling back to hash construction.")

    hash_fields = mapping.get("source_record_id_hash_fields")
    if hash_fields:
        return out.apply(lambda r: make_hash_id(r, hash_fields), axis=1)

    raise ValueError(
        "No source_record_id_field or source_record_id_hash_fields "
        "configured for this source - source_record_id is a required "
        "field and cannot be left to chance. Fix the mapping YAML."
    )


def apply_null_placeholders(raw_df: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    """Some sources use a literal placeholder string (e.g. '-', 'N/A',
    'TBD') to mean 'no data' instead of leaving the cell genuinely empty.
    Left alone, that placeholder text would get carried straight into
    the canonical schema, which is misleading (e.g. address_city = '-').
    Declared per-source in the mapping YAML under 'null_placeholders':
    a dict of raw_column -> list of strings to treat as null. Matched
    after stripping whitespace, case-sensitive by default (most
    placeholders like '-' or 'N/A' are consistently cased in practice).
    """
    placeholders = mapping.get("null_placeholders", {})
    for col, values_to_null in placeholders.items():
        if col not in raw_df.columns:
            print(f"  Warning: null_placeholders column '{col}' not found - skipping.")
            continue
        mask = raw_df[col].str.strip().isin(values_to_null)
        count = mask.sum()
        if count > 0:
            raw_df.loc[mask, col] = None
            print(f"  Replaced {count} placeholder value(s) in '{col}' with null "
                  f"(matched: {values_to_null})")
    return raw_df


def apply_concat_fields(raw_df: pd.DataFrame, out: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    """Joins multiple raw columns into one canonical field, skipping any
    that are empty/null for a given row - e.g. STREET1 + STREET2 into one
    address_street ('601 GENOME WAY, SUITE 2001'), where STREET2 is often
    blank. Declared per-source in the mapping YAML under 'concat_fields'.
    """
    for canonical_field, spec in mapping.get("concat_fields", {}).items():
        cols = spec["columns"]
        separator = spec.get("separator", ", ")
        missing = [c for c in cols if c not in raw_df.columns]
        if missing:
            print(f"  Warning: concat_fields columns not found for "
                  f"'{canonical_field}': {missing} - skipping those.")
        existing_cols = [c for c in cols if c in raw_df.columns]

        def _concat(row, cols=existing_cols, sep=separator):
            parts = [str(row[c]).strip() for c in cols
                     if pd.notna(row[c]) and str(row[c]).strip()]
            return sep.join(parts) if parts else None

        out[canonical_field] = raw_df.apply(_concat, axis=1)
    return out

def apply_coalesce_fields(raw_df: pd.DataFrame, out: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    """Takes the first non-empty value from a list of raw columns — e.g.,
    website_url fallback to source_url if website_url is missing."""
    for canonical_field, spec in mapping.get("coalesce_fields", {}).items():
        cols = spec["columns"]
        existing_cols = [c for c in cols if c in raw_df.columns]

        def _first_non_empty(row, cols=existing_cols):
            for c in cols:
                val = row[c]
                if pd.notna(val) and str(val).strip():
                    return str(val).strip()
            return None

        out[canonical_field] = raw_df.apply(_first_non_empty, axis=1)
    return out

def apply_raw_row_filters(raw_df: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    """Filters rows based on a RAW column's value, before any mapping
    happens - e.g. keeping only 'Establishment Status' values of
    Registered/Active/Pre-Registered, regardless of how that source
    capitalizes them. Declared per-source in the mapping YAML under
    'raw_row_filters'. Case-insensitive by design, since real-world
    exports are inconsistent about capitalization."""
    filters = mapping.get("raw_row_filters", [])
    for f in filters:
        col = f["raw_column"]
        keep_values_lower = {v.strip().lower() for v in f["keep_values"]}
        if col not in raw_df.columns:
            print(f"  Warning: raw_row_filter column '{col}' not found - skipping this filter.")
            continue
        before = len(raw_df)
        mask = raw_df[col].str.strip().str.lower().isin(keep_values_lower)
        raw_df = raw_df[mask]
        after = len(raw_df)
        print(f"  Raw filter on '{col}': kept {after}/{before} rows "
              f"(values: {sorted(keep_values_lower)})")
    return raw_df


def apply_canonical_row_filters(out: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    """Filters rows based on an already-mapped CANONICAL field - e.g.
    keeping only rows where address_state is a real US state code, used
    as a proxy for 'US-only' when a source has no explicit country field
    (as is the case here - see fda_hct_blood_registration.yaml notes).
    Must run AFTER composite field resolution, since fields like
    address_state don't exist until that step has parsed them out."""
    filters = mapping.get("canonical_row_filters", [])
    for f in filters:
        field = f["field"]
        keep_values = f["keep_values"]
        case_insensitive = f.get("case_insensitive", True)
        if field not in out.columns:
            print(f"  Warning: canonical_row_filter field '{field}' not found - skipping.")
            continue
        before = len(out)
        if case_insensitive:
            keep_values_norm = {v.strip().upper() for v in keep_values}
            mask = out[field].str.strip().str.upper().isin(keep_values_norm)
        else:
            mask = out[field].isin(keep_values)
        out = out[mask]
        after = len(out)
        print(f"  Canonical filter on '{field}': kept {after}/{before} rows")
    return out


def standardize_source(source_key: str, raw_file: Path, out_file: Path,
                        source_name_override: str = None):
    schema = load_yaml(SCHEMA_PATH)
    mapping_path = CONFIG_DIR / "source_mappings" / f"{source_key}.yaml"
    if not mapping_path.exists():
        raise FileNotFoundError(
            f"No mapping config found at {mapping_path}. "
            f"Every source needs a config/source_mappings/<source>.yaml file."
        )
    mapping = load_yaml(mapping_path)

    effective_source_name = source_name_override or mapping["source_name"]

    print(f"Standardizing '{effective_source_name}' from {raw_file}")
    if source_name_override:
        print(f"  (overriding YAML's declared source_name: '{mapping['source_name']}')")
    raw_df = pd.read_csv(raw_file, dtype=str)
    print(f"  Loaded {len(raw_df)} raw rows, {len(raw_df.columns)} columns")

    raw_df = apply_null_placeholders(raw_df, mapping)
    raw_df = apply_raw_row_filters(raw_df, mapping)

    out = apply_direct_mappings(raw_df, mapping)
    out = apply_concat_fields(raw_df, out, mapping)
    out = apply_coalesce_fields(raw_df, out, mapping)
    out = resolve_composite_fields(raw_df, out, mapping)
    out["source_record_id"] = resolve_source_record_id(raw_df, out, mapping)
    out = apply_canonical_row_filters(out, mapping)

    # Fill in every remaining canonical field not yet set, so every
    # standardized output - regardless of source - has the exact same
    # column set, in the same order. This is what makes 9 different
    # sources concatenable later without special-casing.
    for field_name in schema["fields"]:
        if field_name not in out.columns:
            out[field_name] = None

    out["source_name"] = effective_source_name

    # Enforce canonical column order for readability/consistency
    out = out[list(schema["fields"].keys())]

    # Validate required fields - warn loudly rather than silently ship
    # incomplete data, but don't crash (a partial standardized file is
    # still useful for inspection).
    required_fields = [f for f, spec in schema["fields"].items() if spec.get("required")]
    for field_name in required_fields:
        null_count = out[field_name].isna().sum()
        if null_count > 0:
            print(f"  Warning: required field '{field_name}' has "
                  f"{null_count}/{len(out)} null values.")

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_file, index=False)
    print(f"  Wrote {len(out)} standardized rows to {out_file}")


def main():
    parser = argparse.ArgumentParser(description="Config-driven source standardization engine")
    parser.add_argument("--source", required=True,
                         help="Source key - must match config/source_mappings/<source>.yaml")
    parser.add_argument("--raw-file", type=Path, default=None,
                         help="Optional - defaults to the 'raw_file' path declared in "
                              "that source's mapping YAML, if present")
    parser.add_argument("--out", type=Path, default=None,
                         help="Optional - defaults to "
                              "data/phase2_pharma/standardized/<source>_standardized.csv")
    parser.add_argument("--source-name", type=str, default=None,
                         help="Override the source_name value from the YAML for this "
                              "run only - useful when reusing one shared config across "
                              "multiple raw files that represent differently-named "
                              "sources (e.g. FDA HCT/P vs FDA Blood Component vs FDA "
                              "Plasmapheresis, all sharing one mapping YAML).")
    args = parser.parse_args()

    if not CONFIG_DIR.exists():
        raise FileNotFoundError(
            f"Could not find '{CONFIG_DIR}' from the current directory "
            f"({Path.cwd()}). This script expects to be run from the "
            f"project root (analysis/), e.g.:\n"
            f"  cd /Users/amyshih/Desktop/commercial_labs/analysis\n"
            f"  python3 scripts/pipeline/standardize.py --source {args.source} ..."
        )

    raw_file = args.raw_file
    out_file = args.out

    if raw_file is None or out_file is None:
        mapping_path = CONFIG_DIR / "source_mappings" / f"{args.source}.yaml"
        if not mapping_path.exists():
            raise FileNotFoundError(
                f"No mapping config found at {mapping_path}. "
                f"Every source needs a config/source_mappings/<source>.yaml file."
            )
        mapping = load_yaml(mapping_path)

        if raw_file is None:
            configured = mapping.get("raw_file")
            if configured is None:
                raise ValueError(
                    f"--raw-file not given and no 'raw_file' key found in "
                    f"{mapping_path} - either pass --raw-file explicitly or "
                    f"add a 'raw_file: path/to/file.csv' line to that YAML."
                )
            raw_file = Path(configured)

        if out_file is None:
            out_file = Path(f"data/phase2_pharma/standardized/{args.source}_standardized.csv")

    standardize_source(args.source, raw_file, out_file, source_name_override=args.source_name)


if __name__ == "__main__":
    main()