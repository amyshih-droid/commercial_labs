"""
entity_resolution.py - collapses the combined multi-source table down to
one row per real-world physical facility, using fuzzy name+location
matching (deterministic, rule-based - no LLM needed for this step).

Key design decision: match on NAME + CITY + STATE together, not name alone.
Two rows with an identical company name but DIFFERENT cities are treated
as different physical facilities. Two rows with the same name AND the
same city are merged into one.

Usage:
    python3 scripts/pipeline/entity_resolution.py \
        --in data/phase2_pharma/combined_raw.csv \
        --out-entities data/phase2_pharma/master_entities.csv \
        --out-crosswalk data/phase2_pharma/entity_crosswalk.csv
"""

import argparse
import re
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz

# Common legal-entity suffixes that add noise to name matching without
# adding meaning - stripped only for the MATCHING step, never from the
# actual displayed/stored company_name.
LEGAL_SUFFIXES = [
    r"\bINC\b\.?", r"\bLLC\b\.?", r"\bLLP\b\.?", r"\bLP\b\.?", r"\bLTD\b\.?",
    r"\bCORP(?:ORATION)?\b\.?", r"\bCO\b\.?", r"\bPLLC\b\.?", r"\bPC\b\.?",
]
NAME_NORMALIZE_PATTERN = re.compile(
    "|".join(LEGAL_SUFFIXES) + r"|[^\w\s]", re.IGNORECASE
)


def normalize_name(name: str) -> str:
    """Uppercases, strips legal suffixes and punctuation, collapses
    whitespace - used only for comparison, never for display."""
    if pd.isna(name):
        return ""
    cleaned = NAME_NORMALIZE_PATTERN.sub(" ", str(name).upper())
    return re.sub(r"\s+", " ", cleaned).strip()

def normalize_street(street: str) -> str:
    """Uppercases and strips punctuation for cleaner fuzzy matching."""
    if pd.isna(street):
        return ""
    cleaned = re.sub(r"[^\w\s]", " ", str(street).upper())
    return re.sub(r"\s+", " ", cleaned).strip()

def normalize_city(city: str) -> str:
    if pd.isna(city):
        return ""
    return str(city).strip().upper()


class UnionFind:
    """Minimal disjoint-set structure for clustering matched row indices
    into groups - each group becomes one final master entity."""
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, x, y):
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self.parent[ry] = rx


def resolve_entities(df: pd.DataFrame, name_threshold: int = 90, street_threshold: int = 75) -> pd.DataFrame:
    """Returns df with two new columns: _norm_name, _norm_city (used for
    matching) and _cluster_id (which final entity each row belongs to).
    """
    df = df.reset_index(drop=True).copy()
    df["_norm_name"] = df["company_name"].apply(normalize_name)
    df["_norm_city"] = df["address_city"].apply(normalize_city)
    df["_state"] = df["address_state"].fillna("").str.strip().str.upper()

    df["_norm_street"] = df["address_street"].apply(normalize_street)

    uf = UnionFind(len(df))

    # Blocking: only compare rows within the same state and same city,
    # and only where the first 3 characters of the normalized name match.
    # This keeps the comparison count manageable at scale - full pairwise
    # comparison across a large combined dataset would be far too slow.
    # Tradeoff: a typo in the first 3 characters of a name could cause a
    # missed match - acceptable for now, worth revisiting if recall
    # turns out too low in practice (e.g. add a phonetic blocking key).
    df["_block_key"] = (
        df["_state"] + "|" + df["_norm_city"] + "|" + df["_norm_name"].str[:3]
    )

    for block_key, group in df.groupby("_block_key"):
        if block_key.startswith("||") or len(group) < 2:
            continue  # nothing to compare, or no location info to block on
        indices = group.index.tolist()
        for i in range(len(indices)):
            for j in range(i + 1, len(indices)):
                idx_a, idx_b = indices[i], indices[j]

                name_a = df.at[idx_a, "_norm_name"]
                name_b = df.at[idx_b, "_norm_name"]

                street_a = df.at[idx_a, "_norm_street"]
                street_b = df.at[idx_b, "_norm_street"]

                if not name_a or not name_b:
                    continue
                name_score = fuzz.token_sort_ratio(name_a, name_b)
                if name_score >= name_threshold:
                    if street_a and street_b:
                        street_score = fuzz.token_sort_ratio(street_a, street_b)
                        if street_score < street_threshold:
                            continue

                    uf.union(idx_a, idx_b)

    df["_cluster_id"] = [uf.find(i) for i in range(len(df))]
    return df


def merge_cluster(rows: pd.DataFrame, canonical_fields: list) -> dict:
    """Collapses a cluster of matched rows into one merged record - for
    each canonical field, takes the first non-null value found across
    the cluster's member rows. Simple by design; a future improvement
    could prefer values from higher-confidence sources instead of just
    first-non-null."""
    merged = {}
    for field in canonical_fields:
        non_null = rows[field].dropna()
        merged[field] = non_null.iloc[0] if len(non_null) > 0 else None
    return merged


def run_entity_resolution(input_file: Path, out_entities: Path, out_crosswalk: Path, name_threshold: int = 90, street_threshold: int = 75):
    df = pd.read_csv(input_file, dtype=str)
    print(f"Loaded {len(df)} combined rows")

    df = resolve_entities(df, name_threshold=name_threshold, street_threshold=street_threshold)
    n_clusters = df["_cluster_id"].nunique()
    print(f"Resolved into {n_clusters} unique entities "
          f"(from {len(df)} raw rows - {len(df) - n_clusters} merged as duplicates)")

    canonical_fields = [c for c in df.columns if not c.startswith("_") and c != "source_file"]

    master_rows = []
    crosswalk_rows = []
    for i, (cluster_id, group) in enumerate(df.groupby("_cluster_id"), 1):
        entity_id = f"ENT-{i:06d}"
        merged = merge_cluster(group, canonical_fields)
        merged["entity_id"] = entity_id
        merged["num_source_files"] = group["source_file"].nunique()
        merged["source_files"] = "; ".join(sorted(group["source_file"].unique()))
        master_rows.append(merged)

        for _, row in group.iterrows():
            crosswalk_rows.append({
                "entity_id": entity_id,
                "source_file": row["source_file"],
                "source_record_id": row["source_record_id"],
                "company_name": row["company_name"],
                "address_street": row.get("address_street"),
                "address_city": row.get("address_city"),
                "address_state": row.get("address_state"),
            })

    master_df = pd.DataFrame(master_rows)
    id_cols = ["entity_id", "num_source_files", "source_files"]
    other_cols = [c for c in canonical_fields if c != "entity_id"]
    master_df = master_df[id_cols + other_cols]

    crosswalk_df = pd.DataFrame(crosswalk_rows)

    out_entities.parent.mkdir(parents=True, exist_ok=True)
    master_df.to_csv(out_entities, index=False)
    crosswalk_df.to_csv(out_crosswalk, index=False)

    print(f"Wrote {len(master_df)} master entities to {out_entities}")
    print(f"Wrote {len(crosswalk_df)} crosswalk rows to {out_crosswalk}")

    multi_source = master_df[master_df["num_source_files"] > 1]
    if len(multi_source) > 0:
        print(f"\n{len(multi_source)} entities confirmed by more than one source file:")
        print(multi_source[["company_name", "num_source_files", "source_files"]].to_string(index=False))

def main():
    parser = argparse.ArgumentParser(description="Fuzzy entity resolution across combined sources")
    parser.add_argument("--in", dest="input_file", type=Path,
                         default=Path("data/phase2_pharma/combined_raw.csv"))
    parser.add_argument("--out-entities", type=Path,
                         default=Path("data/phase2_pharma/master_entities.csv"))
    parser.add_argument("--out-crosswalk", type=Path,
                         default=Path("data/phase2_pharma/entity_crosswalk.csv"))
    parser.add_argument("--name-threshold", type=int, default=90,
                         help="Minimum fuzzy match score (0-100) to merge two "
                              "rows in the same city/state block")
    args = parser.parse_args()

    # Pass the arguments into the new core function
    run_entity_resolution(args.input_file, args.out_entities, args.out_crosswalk, args.name_threshold)

if __name__ == "__main__":
    main()