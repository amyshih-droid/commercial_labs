import pandas as pd
from pathlib import Path

def extract(headless=True) -> pd.DataFrame:
    """
    Extracts the TNI LAMS environmental lab data.
    Currently reads from a locally downloaded CSV export.
    """
    file_path = Path("data/tmp/env/export_search.csv")
    
    if not file_path.exists():
        raise FileNotFoundError(f"Could not find the environmental labs file at {file_path}")
        
    df = pd.read_csv(file_path, dtype=str)
    
    return df