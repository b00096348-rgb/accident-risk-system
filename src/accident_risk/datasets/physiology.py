"""Read existing WHOOP CSV columns without fabricated sensor values."""

from pathlib import Path

import pandas as pd


class WhoopVitalsDataset:
    """Legacy CSV inspection loader; training uses explicitly mapped adapters."""

    def __init__(self, data_dir: str = "data/raw"):
        self.data_path = Path(data_dir) / "whoop_vitals_continuous.csv"

    def load(self) -> pd.DataFrame:
        if not self.data_path.exists():
            raise FileNotFoundError(f"Missing dataset at {self.data_path}")
        data = pd.read_csv(self.data_path)
        if "timestamp" not in data:
            raise ValueError("WHOOP CSV needs an explicitly recorded timestamp column")
        return data.sort_values("timestamp").reset_index(drop=True)
