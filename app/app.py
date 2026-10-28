"""HackSphere 3.0 - Smart Energy Microgrid & Demand Forecasting (Catalyst AppSail / Flask).

Endpoints
    GET  /                 dashboard
    GET  /api/health       service status (booleans only, never secrets)
    GET  /api/sample-info  windows available from the bundled sample telemetry
    GET  /api/sample       forecast on a bundled 24h window   (?start=<row>&hours=24)
    POST /api/forecast     forecast from CSV upload (multipart "file") or JSON {"rows": [...]}
    GET  /api/logs         recent system log lines

Start command (AppSail):  python3 -u app.py   (listens on X_ZOHO_CATALYST_LISTEN_PORT, default 9000)
"""
import csv
import io
import os
import threading
import time
from collections import deque
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory

import assistant
import controller
import quickml_client

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLE_CSV = os.path.join(BASE_DIR, "data", "sample_test_input.csv")
MAX_ROWS = 168            # up to one week of hourly rows
MAX_UPLOAD_BYTES = 1_000_000
REQUIRED = ["timestamp", "ambient_temp_c", "humidity_pct", "solar_irradiance_wm2"]
LIMITS = {
    "ambient_temp_c": (-60.0, 70.0),
    "humidity_pct": (0.0, 100.0),
    "solar_irradiance_wm2": (0.0, 1500.0),
}

app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

_logs = deque(maxlen=200)
_log_lock = threading.Lock()


def log(level, msg):
    line = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "level": level, "message": msg}
    with _log_lock:
        _logs.append(line)
    print(f"[{line['time']}] {level}: {msg}", flush=True)


class BadInput(ValueError):
    pass


# ----------------------------------------------------------------- ingestion & cleaning
_TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
               "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M")


def parse_ts(value):
    s = str(value).strip().replace("Z", "")
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def to_float(value):
    if value is None:
        return None
    s = str(value).strip()
    if s == "" or s.lower() in ("nan", "null", "none", "na", "n/a", "-"):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def clean_rows(raw_rows):
    """Validate and clean telemetry. Returns (rows, warnings). Raises BadInput for fatal problems."""
    warnings = []
    if not raw_rows:
        raise BadInput("No data rows were provided.")
    first = raw_rows[0]
    missing_cols = [c for c in REQUIRED if c not in first]
    if missing_cols:
        raise BadInput("Missing required column(s): " + ", ".join(missing_cols))

    parsed, bad_ts = [], []
    for i, r in enumerate(raw_rows, start=1):
        ts = parse_ts(r.get("timestamp"))
        if ts is None:
            bad_ts.append(i)
            continue
        parsed.append({"ts": ts, **{c: to_float(r.get(c)) for c in LIMITS}})
    if bad_ts:
        shown = ", ".join(map(str, bad_ts[:5])) + ("..." if len(bad_ts) > 5 else "")
        warnings.append(f"Dropped {len(bad_ts)} row(s) with an unreadable timestamp (row {shown}).")
    if not parsed:
        raise BadInput("No rows with a valid timestamp (expected YYYY-MM-DD HH:MM:SS).")

    parsed.sort(key=lambda r: r["ts"])
    dedup = {}
    for r in parsed:
        dedup[r["ts"]] = r
    if len(dedup) != len(parsed):
        warnings.append(f"Removed {len(parsed) - len(dedup)} duplicate timestamp(s).")
    rows = list(dedup.values())

    if len(rows) > MAX_ROWS:
        warnings.append(f"Only the first {MAX_ROWS} hourly rows are used.")
        rows = rows[:MAX_ROWS]

    for col, (lo, hi) in LIMITS.items():
        vals = [r[col] for r in rows]
        known = sorted(v for v in vals if v is not None)
        if not known:
            raise BadInput(f"Column '{col}' has no usable numeric values.")
        median = known[len(known) // 2]
        filled = 0
        for i, r in enumerate(rows):
            if r[col] is None:
                prev = next((rows[j][col] for j in range(i - 1, -1, -1) if rows[j][col] is not None), None)
                nxt = next((rows[j][col] for j in range(i + 1, len(rows)) if rows[j][col] is not None), None)
                if prev is not None and nxt is not None:
                    r[col] = (prev + nxt) / 2.0
                else:
                    r[col] = prev if prev is not None else (nxt if nxt is not None else median)
                filled += 1
        clipped = 0
        for r in rows:
            v = min(hi, max(lo, r[col]))
            if v != r[col]:
                clipped += 1
            r[col] = v
        if filled:
            warnings.append(f"Filled {filled} missing value(s) in {col} by interpolation.")
        if clipped:
            warnings.append(f"Clipped {clipped} out-of-range value(s) in {col} to [{lo:g}, {hi:g}].")

    for r in rows:
        r["timestamp"] = r["ts"].strftime("%Y-%m-%d %H:%M:%S")
        r["hour"] = r["ts"].hour
    gaps = sum(1 for a, b in zip(rows, rows[1:]) if (b["ts"] - a["ts"]).total_seconds() != 3600)
    if gaps:
        warnings.append(f"{gaps} timestamp gap(s): rows are not strictly hourly.")
    return rows, warnings


def read_csv_text(text):
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise BadInput("The CSV file is empty.")
    reader.fieldnames = [f.strip() for f in reader.fieldnames]
    return [{(k or "").strip(): v for k, v in row.items()} for row in reader]


# ----------------------------------------------------------------- forecast pipeline
def battery_cfg_from(args):
    cfg = controller.BatteryConfig()

    def num(name, lo, hi, default):
        v = to_float(args.get(name))
        return default if v is None else min(hi, max(lo, v))

    cfg.capacity_kwh = num("battery_capacity_kwh", 50.0, 100000.0, cfg.capacity_kwh)
    cfg.initial_soc_pct = num("initial_soc_pct", 0.0, 100.0, cfg.initial_soc_pct)
    return cfg


def run_forecast(raw_rows, cfg, origin):
    t0 = time.time()
    rows, warnings = clean_rows(raw_rows)
    log("INFO", f"{origin}: cleaned {len(rows)} row(s), {len(warnings)} warning(s).")

    preds, meta = quickml_client.predict_rows(rows)
    if meta["source"] == "quickml":
        log("INFO", f"QuickML ok ({meta['method']}, {meta['latency_s']}s) for {len(rows)} row(s).")
    else:
        log("WARN", f"Using offline estimate. {meta['note']}")

    hours = [{
        "timestamp": r["timestamp"], "hour": r["hour"],
        "ambient_temp_c": round(r["ambient_temp_c"], 2),
        "humidity_pct": round(r["humidity_pct"], 2),
        "solar_irradiance_wm2": round(r["solar_irradiance_wm2"], 2),
        "demand_kw": round(float(p), 2),
    } for r, p in zip(rows, preds)]

    decided, summary = controller.simulate(hours, cfg)

    alerts = []
    for h in decided:
        if h["overload"]:
            alerts.append({"type": "critical_overload", "timestamp": h["timestamp"],
                           "demand_kw": h["demand_kw"],
                           "message": f"Critical overload: {h['demand_kw']:.1f} kW >= "
                                      f"{controller.OVERLOAD_THRESHOLD_KW:.0f} kW at {h['timestamp']}"})
    if summary.get("overload_hours"):
        log("ALERT", f"{summary['overload_hours']} critical overload hour(s); peak {summary['peak_kw']} kW.")
    if summary.get("battery_low_hours"):
        log("WARN", f"Battery below {cfg.low_soc_pct:.0f}% for {summary['battery_low_hours']} hour(s).")
    log("INFO", f"Dispatch: {summary['storage_hours']} h storage, {summary['grid_hours']} h main grid.")

    return {
        "source": meta["source"], "method": meta["method"], "note": meta["note"],
        "quickml_latency_s": meta["latency_s"], "total_latency_s": round(time.time() - t0, 2),
        "hours": decided, "summary": summary, "alerts": alerts, "warnings": warnings,
        "rules": {"storage_above_kw": controller.STORAGE_THRESHOLD_KW,
                  "overload_at_kw": controller.OVERLOAD_THRESHOLD_KW,
                  "evening_window": [controller.EVENING_START_HOUR, controller.EVENING_END_HOUR]},
        "battery": {"capacity_kwh": cfg.capacity_kwh, "initial_soc_pct": cfg.initial_soc_pct,
                    "low_soc_pct": cfg.low_soc_pct},
    }


def _sample_rows():
    with open(SAMPLE_CSV, encoding="utf-8") as f:
        return read_csv_text(f.read())


# ----------------------------------------------------------------- routes
@app.errorhandler(BadInput)
def _bad_input(e):
    log("WARN", f"Rejected input: {e}")
    return jsonify({"error": str(e)}), 400


@app.errorhandler(413)
def _too_large(_e):
    return jsonify({"error": f"File too large (max {MAX_UPLOAD_BYTES // 1000} KB)."}), 413


@app.errorhandler(Exception)
def _unexpected(e):
    if hasattr(e, "code") and isinstance(e.code, int) and e.code < 500:
        return jsonify({"error": getattr(e, "description", str(e))}), e.code
    log("ERROR", f"Unexpected error: {type(e).__name__}: {e}")
    return jsonify({"error": "Internal error while building the forecast."}), 500


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "quickml_configured": quickml_client.configured(),
                    "assistant": "claude" if assistant.claude_available() else "built-in",
                    "time": datetime.now().isoformat(timespec="seconds")})


@app.get("/api/sample-info")
def sample_info():
    rows = _sample_rows()
    return jsonify({"total_rows": len(rows), "first": rows[0]["timestamp"], "last": rows[-1]["timestamp"]})


@app.get("/api/sample")
def sample():
    rows = _sample_rows()
    hours = int(min(MAX_ROWS, max(1, to_float(request.args.get("hours")) or 24)))
    start = int(max(0, to_float(request.args.get("start")) or 0))
    if start >= len(rows):
        start = 0
    window = rows[start:start + hours]
    if len(window) < hours and start > 0:  # wrap around so rolling windows never run short
        window += rows[: hours - len(window)]
    return jsonify(run_forecast(window, battery_cfg_from(request.args), f"sample[{start}:{start + hours}]"))


@app.post("/api/forecast")
def forecast():
    cfg = battery_cfg_from(request.values if request.files else (request.get_json(silent=True) or {}))
    if "file" in request.files:
        f = request.files["file"]
        try:
            text = f.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            raise BadInput("The file must be a UTF-8 CSV.")
        return jsonify(run_forecast(read_csv_text(text), cfg, f"upload:{(f.filename or 'csv')[:60]}"))
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("rows"), list):
        raise BadInput('Send a CSV file (field "file") or JSON {"rows": [...]}.')
    raw = [r for r in body["rows"] if isinstance(r, dict)]
    return jsonify(run_forecast(raw, cfg, "manual-entry"))


MAX_PREDICT_ROWS = 5000


@app.post("/api/predict")
def predict_api():
    """Stateless prediction proxy used by predict.py when it has no QuickML credentials.
    Body: {"rows": [{"hour": 0-23, "ambient_temp_c": x, "humidity_pct": y, "solar_irradiance_wm2": z}, ...]}
    Returns one prediction per row, in order. Credentials stay on the server."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("rows"), list) or not body["rows"]:
        raise BadInput('Send JSON {"rows": [{"hour":..,"ambient_temp_c":..,"humidity_pct":..,"solar_irradiance_wm2":..}]}.')
    if len(body["rows"]) > MAX_PREDICT_ROWS:
        raise BadInput(f"At most {MAX_PREDICT_ROWS} rows per request.")
    rows = []
    for r in body["rows"]:
        try:
            hour = int(r["hour"])
            rows.append({
                "hour": hour % 24,
                "ambient_temp_c": min(70.0, max(-60.0, float(r["ambient_temp_c"]))),
                "humidity_pct": min(100.0, max(0.0, float(r["humidity_pct"]))),
                "solar_irradiance_wm2": min(1500.0, max(0.0, float(r["solar_irradiance_wm2"]))),
            })
        except (KeyError, TypeError, ValueError):
            raise BadInput("Every row needs numeric hour, ambient_temp_c, humidity_pct, solar_irradiance_wm2.")
    preds, meta = quickml_client.predict_rows(rows)
    log("INFO", f"/api/predict: {len(rows)} row(s) via {meta['source']}.")
    return jsonify({"predictions": [round(float(p), 4) for p in preds], "source": meta["source"]})


CHAT_LIMIT = 30            # questions per visitor ...
CHAT_WINDOW_S = 600        # ... per 10 minutes (keeps a public demo from running up API costs)
_chat_hits = {}
_chat_lock = threading.Lock()


def _chat_allowed(key):
    now = time.time()
    with _chat_lock:
        hits = [t for t in _chat_hits.get(key, []) if now - t < CHAT_WINDOW_S]
        if len(hits) >= CHAT_LIMIT:
            _chat_hits[key] = hits
            return False
        hits.append(now)
        _chat_hits[key] = hits
        if len(_chat_hits) > 5000:   # bound memory
            for k in list(_chat_hits)[:1000]:
                _chat_hits.pop(k, None)
        return True


@app.post("/api/chat")
def chat():
    """Grid Assistant. Body: {"question": str, "history": [{role, text}], "context": <forecast response>}."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not str(body.get("question", "")).strip():
        raise BadInput('Send JSON {"question": "...", "context": {...}}.')
    who = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "?")
    if not _chat_allowed(who):
        return jsonify({"error": "Too many questions. Please wait a few minutes."}), 429
    history = body.get("history") if isinstance(body.get("history"), list) else []
    ctx = body.get("context") if isinstance(body.get("context"), dict) else {}
    t0 = time.time()
    out = assistant.answer(body.get("question"), history, ctx)
    log("INFO", f"Assistant answered via {out['source']} in {time.time() - t0:.2f}s.")
    return jsonify(out)


@app.get("/api/logs")
def logs():
    with _log_lock:
        return jsonify({"logs": list(_logs)[-60:][::-1]})


@app.after_request
def _headers(resp):
    resp.headers["Cache-Control"] = "no-store" if request.path.startswith("/api/") else "public, max-age=300"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


if __name__ == "__main__":
    port = int(os.environ.get("X_ZOHO_CATALYST_LISTEN_PORT", "9000"))
    log("INFO", f"Starting on port {port}; QuickML configured: {quickml_client.configured()}")
    app.run(host="0.0.0.0", port=port, threaded=True)
