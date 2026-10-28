"""Pure-python client for the published Zoho Catalyst QuickML demand-forecast endpoint.

Credentials are read from environment variables only (set them in the AppSail console):
    QUICKML_ENDPOINT_URL   (optional, default below)
    QUICKML_ENDPOINT_KEY   -> header X-QUICKML-ENDPOINT-KEY
    CATALYST_ORG           -> header CATALYST-ORG
    QUICKML_ENVIRONMENT    -> header Environment (default "Development")
    ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET / ZOHO_REFRESH_TOKEN
                           -> used to mint short-lived OAuth access tokens (cached ~50 min)
    ZOHO_ACCOUNTS_URL      -> default https://accounts.zoho.in
    QUICKML_ACCESS_TOKEN   -> optional static token instead of the refresh flow (testing)

The model was trained on these features (must match exactly):
    ambient_temp_c, humidity_pct, solar_irradiance_wm2, cooling_load, hour, hour_0..hour_23
    where cooling_load = max(0, ambient_temp_c - 24).
"""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

DEFAULT_URL = (
    "https://api.catalyst.zoho.in/quickml/v1/project/102666000000013054/endpoints/predict"
)
DEFAULT_ORG = "60093257910"
COOLING_THRESHOLD_C = 24.0
TIMEOUT = (5, 40)
BATCH_SIZE = 250
MAX_WORKERS = 8

# Offline estimate (same structure as the trained model). Only used when QuickML is
# unreachable or not configured, and always reported to the UI as "offline-estimate".
_FALLBACK_SLOPE = 8.5876
_FALLBACK_BASE = [
    89.07, 90.10, 90.82, 89.98, 89.61, 89.91, 132.82, 172.66, 173.79, 174.74,
    144.66, 143.79, 142.98, 143.85, 143.98, 143.27, 144.44, 143.68, 197.55,
    198.70, 198.72, 198.00, 197.33, 109.96,
]

_lock = threading.Lock()
_token = {"value": "", "expires": 0.0}
_batch_format = {"value": None}  # discovered once: "records" | "columnar" | "single"


def endpoint_url() -> str:
    return os.environ.get("QUICKML_ENDPOINT_URL", DEFAULT_URL).strip() or DEFAULT_URL


def configured() -> bool:
    has_key = bool(os.environ.get("QUICKML_ENDPOINT_KEY", "").strip())
    has_token = bool(os.environ.get("QUICKML_ACCESS_TOKEN", "").strip()) or all(
        os.environ.get(k, "").strip()
        for k in ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN")
    )
    return has_key and has_token


def _access_token() -> str:
    static = os.environ.get("QUICKML_ACCESS_TOKEN", "").strip()
    if static:
        return static
    with _lock:
        if _token["value"] and time.time() < _token["expires"]:
            return _token["value"]
        base = os.environ.get("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.in").rstrip("/")
        r = requests.post(
            f"{base}/oauth/v2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": os.environ["ZOHO_CLIENT_ID"].strip(),
                "client_secret": os.environ["ZOHO_CLIENT_SECRET"].strip(),
                "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"].strip(),
            },
            timeout=TIMEOUT,
        )
        data = r.json()
        if "access_token" not in data:
            raise RuntimeError(f"OAuth token refresh failed: {data.get('error', 'unknown error')}")
        _token["value"] = data["access_token"]
        _token["expires"] = time.time() + 50 * 60
        return _token["value"]


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "X-QUICKML-ENDPOINT-KEY": os.environ["QUICKML_ENDPOINT_KEY"].strip(),
        "Authorization": f"Zoho-oauthtoken {_access_token()}",
        "CATALYST-ORG": os.environ.get("CATALYST_ORG", DEFAULT_ORG).strip(),
        "Environment": os.environ.get("QUICKML_ENVIRONMENT", "Development").strip(),
    }


def build_features(row: dict) -> dict:
    """row: {'hour': int, 'ambient_temp_c': float, 'humidity_pct': float, 'solar_irradiance_wm2': float}"""
    temp = float(row["ambient_temp_c"])
    hour = int(row["hour"])
    feats = {
        "ambient_temp_c": temp,
        "humidity_pct": float(row["humidity_pct"]),
        "solar_irradiance_wm2": float(row["solar_irradiance_wm2"]),
        "cooling_load": max(0.0, temp - COOLING_THRESHOLD_C),
        "hour": hour,
    }
    for h in range(24):
        feats[f"hour_{h}"] = 1 if hour == h else 0
    return feats


def _post(headers, data):
    r = requests.post(endpoint_url(), json={"data": data}, headers=headers, timeout=TIMEOUT)
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json()["result"]


def _num(x) -> float:
    if isinstance(x, (list, tuple)):
        x = x[0]
    v = float(x)
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError("non-finite prediction")
    return v


def _single(headers, feats):
    last = None
    for attempt in range(2):
        try:
            res = _post(headers, feats)
            return _num(res[0] if isinstance(res, (list, tuple)) else res)
        except Exception as e:  # retry once
            last = e
            time.sleep(1)
    raise RuntimeError(f"QuickML single call failed: {last}")


def _batch(headers, feats_list, fmt):
    if fmt == "records":
        data = feats_list
    else:  # columnar
        data = {k: [f[k] for f in feats_list] for k in feats_list[0]}
    res = _post(headers, data)
    out = [_num(x) for x in res]
    if len(out) != len(feats_list):
        raise RuntimeError(f"batch returned {len(out)} values for {len(feats_list)} rows")
    return out


def offline_estimate(rows) -> list:
    return [
        _FALLBACK_BASE[int(r["hour"])]
        + _FALLBACK_SLOPE * max(0.0, float(r["ambient_temp_c"]) - COOLING_THRESHOLD_C)
        for r in rows
    ]


def predict_rows(rows):
    """Return (predictions, meta). meta = {source, method, latency_s, note}."""
    t0 = time.time()
    if not configured():
        return offline_estimate(rows), {
            "source": "offline-estimate", "method": "local",
            "latency_s": 0.0,
            "note": "QuickML credentials are not configured on the server.",
        }
    try:
        headers = _headers()
        feats = [build_features(r) for r in rows]
        preds, method = None, None

        for fmt in ([_batch_format["value"]] if _batch_format["value"] else ["records", "columnar"]):
            if fmt in (None, "single"):
                break
            try:
                preds = []
                for i in range(0, len(feats), BATCH_SIZE):
                    preds += _batch(headers, feats[i:i + BATCH_SIZE], fmt)
                method = f"batch-{fmt}"
                _batch_format["value"] = fmt
                break
            except Exception:
                preds = None
        if preds is None:
            _batch_format["value"] = "single"
            with ThreadPoolExecutor(MAX_WORKERS) as pool:
                preds = list(pool.map(lambda f: _single(headers, f), feats))
            method = "single-calls"
        return preds, {
            "source": "quickml", "method": method,
            "latency_s": round(time.time() - t0, 2), "note": "",
        }
    except Exception as e:
        return offline_estimate(rows), {
            "source": "offline-estimate", "method": "local",
            "latency_s": round(time.time() - t0, 2),
            "note": f"QuickML call failed: {str(e)[:200]}",
        }
