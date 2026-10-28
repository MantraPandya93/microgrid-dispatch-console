"""HackSphere 3.0 - demand forecasting predict.py  (V4)

Calls the published Zoho Catalyst QuickML endpoint and returns one predicted demand
value (kW) per input row.

Endpoint contract (QuickML console "Test the model"):
    request : {"data": {"ambient_temp_c":..., "humidity_pct":..., "solar_irradiance_wm2":...,
                        "cooling_load":..., "hour":..., "hour_0":..., ..., "hour_23":...}}
    response: {"result": [<predicted demand kw>]}

Speed: the script first tries to send many rows in ONE call (two possible batch formats,
verified against single-row answers). If the endpoint does not support batching it falls
back to parallel single-row calls with progress output.

Secured endpoint - credentials come from environment variables (never hard-code them):
    QUICKML_ENDPOINT_KEY   -> header X-QUICKML-ENDPOINT-KEY
    CATALYST_ORG           -> header CATALYST-ORG (default below)
    QUICKML_ENVIRONMENT    -> header Environment (default "Development")
    Authorization          -> "Zoho-oauthtoken <token>" from QUICKML_ACCESS_TOKEN, or
                              automatically from ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET /
                              ZOHO_REFRESH_TOKEN (optional ZOHO_ACCOUNTS_URL,
                              default https://accounts.zoho.in)
    QUICKML_ENDPOINT_URL   -> optional override of the URL constant below

Usage:
    python predict.py --input sample_test_input.csv --output predictions.csv
"""
import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests

QUICKML_ENDPOINT_URL = (
    "https://api.catalyst.zoho.in/quickml/v1/project/102666000000013054/endpoints/predict"
)
_PLACEHOLDER = "YOUR_PUBLISHED_QUICKML_ENDPOINT_URL_HERE"
# Public AppSail app that holds the QuickML credentials server-side. Used automatically when this
# machine has no QuickML credentials (e.g. on the evaluator's computer). Override with QUICKML_PROXY_URL.
PROXY_URL = "https://microgrid-50047155770.development.catalystappsail.in/api/predict"
PROXY_CHUNK = 1000
DEFAULT_CATALYST_ORG = "60093257910"

TIMEOUT = (5, 20)  # (connect, read) seconds per call
MAX_WORKERS = 16
RETRIES = 2
BATCH_SIZE = 250
COOLING_THRESHOLD_C = 24.0  # must match training features (see features.py)

# Offline safety net only (same model structure, fit on train_telemetry.csv).
_FALLBACK_SLOPE = 8.5876
_FALLBACK_BASE = [
    89.07, 90.10, 90.82, 89.98, 89.61, 89.91, 132.82, 172.66, 173.79, 174.74,
    144.66, 143.79, 142.98, 143.85, 143.98, 143.27, 144.44, 143.68, 197.55,
    198.70, 198.72, 198.00, 197.33, 109.96,
]


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def _endpoint_url() -> str:
    return os.environ.get("QUICKML_ENDPOINT_URL", QUICKML_ENDPOINT_URL).strip()


def _access_token() -> str:
    tok = os.environ.get("QUICKML_ACCESS_TOKEN", "").strip()
    if tok:
        return tok
    cid = os.environ.get("ZOHO_CLIENT_ID", "").strip()
    sec = os.environ.get("ZOHO_CLIENT_SECRET", "").strip()
    ref = os.environ.get("ZOHO_REFRESH_TOKEN", "").strip()
    if not (cid and sec and ref):
        return ""
    base = os.environ.get("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.in").rstrip("/")
    r = requests.post(
        f"{base}/oauth/v2/token",
        data={"grant_type": "refresh_token", "client_id": cid,
              "client_secret": sec, "refresh_token": ref},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if "access_token" not in data:
        raise RuntimeError(f"token refresh failed: {data.get('error', data)}")
    return data["access_token"]


def _headers() -> dict:
    h = {
        "Content-Type": "application/json",
        "CATALYST-ORG": os.environ.get("CATALYST_ORG", DEFAULT_CATALYST_ORG),
        "Environment": os.environ.get("QUICKML_ENVIRONMENT", "Development"),
    }
    key = os.environ.get("QUICKML_ENDPOINT_KEY", "").strip()
    if key:
        h["X-QUICKML-ENDPOINT-KEY"] = key
    token = _access_token()
    if token:
        h["Authorization"] = f"Zoho-oauthtoken {token}"
    return h


def _clean(input_df: pd.DataFrame) -> pd.DataFrame:
    """Validate/clean raw telemetry; keeps row order and row count."""
    df = input_df.copy().reset_index(drop=True)
    ts = pd.to_datetime(df["timestamp"], errors="coerce")
    if ts.isna().any():
        ts = ts.ffill().bfill()
        if ts.isna().all():
            ts = pd.Series(pd.date_range("2000-01-01", periods=len(df), freq="h"))
    df["_ts"] = ts
    for col, lo, hi in (
        ("ambient_temp_c", -60.0, 70.0),
        ("humidity_pct", 0.0, 100.0),
        ("solar_irradiance_wm2", 0.0, 1500.0),
    ):
        s = pd.to_numeric(df[col], errors="coerce").clip(lo, hi)
        s = s.interpolate(limit_direction="both")
        df[col] = s.fillna(s.median() if s.notna().any() else 0.0)
    return df


def _features(df: pd.DataFrame) -> list:
    """One feature dict per row, identical to the training CSV columns."""
    hours = df["_ts"].dt.hour.astype(int).to_numpy()
    temps = df["ambient_temp_c"].to_numpy(dtype=float)
    hums = df["humidity_pct"].to_numpy(dtype=float)
    sols = df["solar_irradiance_wm2"].to_numpy(dtype=float)
    rows = []
    for i in range(len(df)):
        row = {
            "ambient_temp_c": float(temps[i]),
            "humidity_pct": float(hums[i]),
            "solar_irradiance_wm2": float(sols[i]),
            "cooling_load": float(max(0.0, temps[i] - COOLING_THRESHOLD_C)),
            "hour": int(hours[i]),
        }
        for h in range(24):
            row[f"hour_{h}"] = 1 if hours[i] == h else 0
        rows.append(row)
    return rows


def _post(session, url, headers, data):
    r = session.post(url, json={"data": data}, headers=headers, timeout=TIMEOUT)
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    return r.json()["result"]


def _as_float(x) -> float:
    if isinstance(x, (list, tuple)):
        x = x[0]
    v = float(x)
    if not np.isfinite(v):
        raise ValueError("non-finite prediction")
    return v


def _call_one(session, url, headers, row) -> float:
    last = None
    for attempt in range(RETRIES):
        try:
            res = _post(session, url, headers, row)
            return _as_float(res[0] if isinstance(res, (list, tuple)) else res)
        except Exception as e:
            last = e
            if attempt < RETRIES - 1:
                time.sleep(1 + attempt)
    raise RuntimeError(f"QuickML call failed after {RETRIES} tries: {last}")


def _columnar(rows):
    return {k: [r[k] for r in rows] for k in rows[0]}


def _batch_call(session, url, headers, rows, fmt):
    data = _columnar(rows) if fmt == "columnar" else list(rows)
    res = _post(session, url, headers, data)
    out = [_as_float(x) for x in res]
    if len(out) != len(rows):
        raise RuntimeError(f"batch returned {len(out)} values for {len(rows)} rows")
    return out


def _find_batch_format(session, url, headers, rows, singles):
    """Return 'columnar' / 'records' if the endpoint batches correctly, else None."""
    probe = rows[: len(singles)]
    for fmt in ("columnar", "records"):
        try:
            got = _batch_call(session, url, headers, probe, fmt)
            if np.allclose(got, singles, atol=1e-3):
                return fmt
        except Exception:
            continue
    return None


def _fallback(rows: list) -> np.ndarray:
    return np.array(
        [_FALLBACK_BASE[r["hour"]] + _FALLBACK_SLOPE * r["cooling_load"] for r in rows],
        dtype=float,
    )


def _predict_remote(rows, url):
    headers = _headers()
    if "Authorization" not in headers:
        raise RuntimeError("no OAuth token (set ZOHO_CLIENT_ID/SECRET/REFRESH_TOKEN)")
    n = len(rows)
    with requests.Session() as session:
        t0 = time.time()
        first = _call_one(session, url, headers, rows[0])
        _log(f"First call OK ({first:.2f} kW) in {time.time() - t0:.1f}s.")
        if n == 1:
            return np.array([first])

        singles = [first] + [_call_one(session, url, headers, r) for r in rows[1:min(3, n)]]
        preds = list(singles)
        rest = rows[len(singles):]

        fmt = _find_batch_format(session, url, headers, rows, singles) if rest else None
        if fmt and rest:
            _log(f"Batch mode ({fmt}) works: sending {len(rest)} rows in chunks of {BATCH_SIZE}.")
            for i in range(0, len(rest), BATCH_SIZE):
                preds += _batch_call(session, url, headers, rest[i:i + BATCH_SIZE], fmt)
                _log(f"  {len(preds)}/{n} done")
        elif rest:
            _log(f"No batch support: {len(rest)} single calls with {MAX_WORKERS} workers.")
            done = [0]

            def work(r):
                v = _call_one(session, url, headers, r)
                done[0] += 1
                if done[0] % 50 == 0:
                    _log(f"  {len(preds) + done[0]}/{n} done")
                return v

            with ThreadPoolExecutor(MAX_WORKERS) as pool:
                preds += list(pool.map(work, rest))
    return np.array(preds, dtype=float)


def _predict_proxy(rows):
    """Ask the deployed app (which owns the QuickML credentials) for predictions."""
    url = os.environ.get("QUICKML_PROXY_URL", PROXY_URL).strip()
    if not url or url.lower() == "none":
        raise RuntimeError("proxy disabled")
    out = []
    with requests.Session() as session:
        for i in range(0, len(rows), PROXY_CHUNK):
            chunk = [{k: r[k] for k in ("hour", "ambient_temp_c", "humidity_pct", "solar_irradiance_wm2")}
                     for r in rows[i:i + PROXY_CHUNK]]
            resp = session.post(url, json={"rows": chunk}, timeout=(5, 60))
            resp.raise_for_status()
            data = resp.json()
            preds = data["predictions"]
            if len(preds) != len(chunk):
                raise RuntimeError("proxy returned a wrong number of predictions")
            if data.get("source") != "quickml":
                raise RuntimeError(f"proxy is not backed by QuickML (source={data.get('source')})")
            out += [float(p) for p in preds]
    return np.array(out, dtype=float)


def predict(input_df: pd.DataFrame):
    """input_df columns: timestamp, ambient_temp_c, humidity_pct, solar_irradiance_wm2.
    Returns a numpy array with one predicted demand (kW) per input row."""
    rows = _features(_clean(input_df))
    if not rows:
        return np.array([], dtype=float)

    url = _endpoint_url()
    if url and url != _PLACEHOLDER:
        _log(f"Calling QuickML for {len(rows)} rows ...")
        try:
            return _predict_remote(rows, url)
        except Exception as e:
            _log(f"QuickML direct call not possible ({e}); trying the deployed app proxy ...")
            try:
                preds = _predict_proxy(rows)
                _log("Predictions obtained from QuickML via the deployed app.")
                return preds
            except Exception as e2:
                _log(f"WARNING: proxy failed ({e2}); using offline fallback.")
    else:
        _log("WARNING: QUICKML_ENDPOINT_URL not set; using offline fallback.")
    return _fallback(rows)


def main():
    ap = argparse.ArgumentParser(description="Predict hourly demand (kW) via QuickML")
    ap.add_argument("--input", default="sample_test_input.csv")
    ap.add_argument("--output", default="predictions.csv")
    args = ap.parse_args()

    df = pd.read_csv(args.input)
    preds = predict(df)
    out = pd.DataFrame({"timestamp": df["timestamp"], "target_demand_kw": np.round(preds, 4)})
    out.to_csv(args.output, index=False)
    print(f"Wrote {len(out)} predictions to {args.output}. First 5:\n{out.head()}")


if __name__ == "__main__":
    main()
