"""Shared feature engineering for QuickML training AND inference (must stay identical)."""
import numpy as np
import pandas as pd

COOLING_THRESHOLD_C = 24.0  # demand rises ~8.5 kW per degC above this


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """df needs: timestamp, ambient_temp_c, humidity_pct, solar_irradiance_wm2."""
    ts = pd.to_datetime(df["timestamp"])
    out = pd.DataFrame(index=df.index)
    out["timestamp"] = df["timestamp"].astype(str)
    out["ambient_temp_c"] = pd.to_numeric(df["ambient_temp_c"], errors="coerce")
    out["humidity_pct"] = pd.to_numeric(df["humidity_pct"], errors="coerce")
    out["solar_irradiance_wm2"] = pd.to_numeric(df["solar_irradiance_wm2"], errors="coerce")
    out["cooling_load"] = np.maximum(0.0, out["ambient_temp_c"] - COOLING_THRESHOLD_C)
    out["hour"] = ts.dt.hour
    for h in range(24):
        out[f"hour_{h}"] = (ts.dt.hour == h).astype(int)
    return out
