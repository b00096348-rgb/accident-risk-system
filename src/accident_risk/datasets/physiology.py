from pathlib import Path

import pandas as pd

from accident_risk.datasets.base import BaseDataset


class WhoopVitalsDataset(BaseDataset):
    """Dataset loader for WHOOP physiological vitals and PPG/HRV logs."""
    
    def __init__(self, data_dir: str = "data/raw"):
        self.data_path = Path(data_dir) / "whoop_vitals_continuous.csv"
        
    def load(self) -> pd.DataFrame:
        if not self.data_path.exists():
            raise FileNotFoundError(f"Missing dataset at {self.data_path}")
            
        df = pd.read_csv(self.data_path)
        
        # Standardize timestamp and physiological feature names
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        
        required_cols = ['heart_rate', 'hrv_rmssd', 'skin_temp', 'respiration_rate']
        for col in required_cols:
            if col not in df.columns:
                df[col] = df[col].fillna(method='ffill') if col in df else 0.0
                
        return df.sort_values('timestamp').reset_index(drop=True)