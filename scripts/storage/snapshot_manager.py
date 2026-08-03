from pathlib import Path
from datetime import date
import pandas as pd


def save_snapshot(
    dataframe: pd.DataFrame,
    source: str,
    format: str = "parquet",
) -> Path:
    """
    Save a snapshot of the extracted dataframe.

    Parameters
    ----------
    dataframe : pd.DataFrame
    source : str
        e.g. "fda_hct"
    format : str
        "parquet" or "csv"

    Returns
    -------
    pathlib.Path
        Path to the saved file.
    """

    output_dir = Path("data/phase2_pharma/raw") / source
    output_dir.mkdir(parents=True, exist_ok=True)

    today = date.today().isoformat()

    if format == "parquet":
        output_file = output_dir / f"{today}.parquet"
        dataframe.to_parquet(output_file, index=False)

    elif format == "csv":
        output_file = output_dir / f"{today}.csv"
        dataframe.to_csv(output_file, index=False)

    else:
        raise ValueError(f"Unsupported format: {format}")

    print(f"Saved snapshot to: {output_file}")

    return output_file