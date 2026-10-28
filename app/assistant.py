"""Grid Assistant: answers questions about the forecast currently shown on the dashboard.

Two modes:
  * Claude (Anthropic API) when ANTHROPIC_API_KEY is set and the `anthropic` package is installed.
  * Built-in, rule-based answers computed from the same forecast data otherwise (or if the API call
    fails). The response always says which mode produced it, so the UI can label it honestly.

Replies are always in English.
"""
import json
import os
import re

import controller

MODEL = os.environ.get("ASSISTANT_MODEL", "claude-opus-5-5")
MAX_QUESTION = 500
MAX_HISTORY = 8
MAX_HOURS = 168
COOLING_KW_PER_C = 8.6      # from the model exploration in README.md: ~+8.6 kW per degC above 24 degC
COOLING_BASE_C = 24.0

SYSTEM_PROMPT = """You are Grid Assistant, built into a smart-microgrid dashboard (HackSphere 3.0, Zoho Catalyst).
The dashboard forecasts hourly electricity demand (kW) with a QuickML linear-regression model and applies fixed dispatch rules:
- demand > 160 kW, or the hour is 18:00-22:00 inclusive -> Renewable Storage Mode; otherwise Main Grid Mode
- demand >= 200 kW -> Critical Overload Alert (independent of the mode)
- The battery state of charge is an illustrative model; it never changes the mode.
Demand rises about 8.6 kW per degree C above 24 degC (cooling load); humidity and solar irradiance add little.

Each question comes with the forecast currently on screen as JSON. Answer only from that data and the rules above.
If the data does not contain the answer, say so plainly. Never invent numbers.
Keep answers short: at most about 120 words, plain text, no markdown tables. Quote times as HH:MM and power in kW.
Always reply in English, whatever language the question is written in.
For what-if questions you may give a rough estimate using the cooling-load rule, say it is an estimate, and suggest the
What-if panel on the dashboard, which re-runs the real model."""


# --------------------------------------------------------------------------- context
def _f(v, default=0.0):
    try:
        f = float(v)
        return f if f == f else default
    except (TypeError, ValueError):
        return default


def compact_context(ctx):
    """Reduce the dashboard payload to the fields the assistant needs (bounded size)."""
    ctx = ctx if isinstance(ctx, dict) else {}
    hours = []
    for h in (ctx.get("hours") or [])[:MAX_HOURS]:
        if not isinstance(h, dict):
            continue
        ts = str(h.get("timestamp", ""))[:19]
        hours.append({
            "t": ts,
            "hour": int(_f(h.get("hour"), _f(ts[11:13] if len(ts) >= 13 else 0))),
            "temp_c": round(_f(h.get("ambient_temp_c")), 1),
            "solar_wm2": round(_f(h.get("solar_irradiance_wm2"))),
            "demand_kw": round(_f(h.get("demand_kw")), 1),
            "mode": "storage" if h.get("mode") == controller.MODE_STORAGE else "grid",
            "overload": bool(h.get("overload")),
            "soc_pct": round(_f(h.get("soc_pct")), 1),
        })
    s = ctx.get("summary") if isinstance(ctx.get("summary"), dict) else {}
    return {
        "window": str(ctx.get("label", ""))[:160],
        "prediction_source": "QuickML model" if ctx.get("source") == "quickml" else "offline estimate",
        "summary": {k: s.get(k) for k in ("hours", "peak_kw", "peak_timestamp", "avg_kw", "storage_hours",
                                          "grid_hours", "overload_hours", "min_soc_pct", "final_soc_pct",
                                          "battery_low_hours")},
        "hours": hours,
    }


# --------------------------------------------------------------------------- Claude
def claude_available():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def ask_claude(question, history, ctx):
    """Return answer text from Claude. Raises on API/network errors (caller falls back)."""
    import anthropic

    client = anthropic.Anthropic(timeout=40.0, max_retries=1)
    messages = []
    for m in history[-MAX_HISTORY:]:
        role, text = m.get("role"), str(m.get("text", ""))[:1200]
        if role in ("user", "assistant") and text:
            messages.append({"role": role, "content": text})
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    data = json.dumps(compact_context(ctx), separators=(",", ":"))
    messages.append({"role": "user", "content": "Forecast on screen (JSON):\n" + data + "\n\nQuestion: " + question})

    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=messages,
    )
    if resp.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    return text or None


# --------------------------------------------------------------------------- built-in answers
T = {
    "en": {
        "nodata": "Run a forecast first, then ask me about it.",
        "peak": "Peak demand is {kw} kW at {t}. Average over the window is {avg} kW.",
        "over_none": "No hour reaches the 200 kW overload line in this window. The highest is {kw} kW at {t}.",
        "over": "{n} of {total} hours are at or above 200 kW: {times}.",
        "batt": "The battery ends at {end}% (lowest {mn}%). It is below the {low}% warning level for {lowh} hour(s). This is an illustrative battery model; it never changes the grid mode.",
        "mode": "Storage carries {s} h and the main grid {g} h. The first hour ({t}) runs on {m}.",
        "solar": "Solar irradiance peaks at {w} W/m² at {t}. Solar adds little to demand in this model; it mainly charges the battery.",
        "avg": "Average forecast demand is {avg} kW over {n} hours.",
        "rules": "Rules: demand above 160 kW, or any hour from 18:00 to 22:00, uses Renewable Storage; otherwise Main Grid. Demand at or above 200 kW raises a Critical Overload alert in either mode.",
        "hour": "At {t}: {kw} kW, {mode}. {why}{over}",
        "why_both": "Demand is above 160 kW and the hour is inside the 18–22 evening window.",
        "why_hi": "Demand is above the 160 kW storage threshold.",
        "why_ev": "The hour is inside the 18–22 evening window.",
        "why_grid": "Demand is at or below 160 kW and the hour is outside the evening window.",
        "over_tag": " This hour is a critical overload (≥ 200 kW).",
        "heat": "Rough estimate for {d:+g} °C: overload hours go from {a} to {b} and the peak from {p1} kW to {p2} kW (cooling load ≈ 8.6 kW per °C above 24 °C). Use the What-if panel to re-run the real model.",
        "storage": "Renewable Storage",
        "grid": "Main Grid",
        "help": "I can answer questions about the forecast on screen. Try: “When is the peak?”, “How many overload hours?”, “Why storage at 18:00?”, “Battery status?” or “What if it is 3 °C hotter?”",
    },
}

INTENTS = [
    ("heat", r"what.?if|hotter|heat|warmer|cooler|colder|ગરમી|ઠંડી|તાપમાન.*(વધ|ઘટ)|degree|°"),
    ("rules", r"\brule|how does|how do(es)? it (work|decide)|નિયમ|કેવી રીતે"),
    ("over", r"overload|over.?load|ઓવરલોડ|alert|ચેતવણી"),
    ("peak", r"peak|max|highest|maximum|સૌથી વધુ|પીક|વધારે માં વધારે"),
    ("batt", r"battery|soc|charge|બેટરી|ચાર્જ"),
    ("solar", r"solar|sun|સોલાર|સૂર્ય"),
    ("avg", r"average|mean|સરેરાશ"),
    ("mode", r"\bmode|grid|storage|ગ્રીડ|સ્ટોરેજ"),
]


def _find_hour(q):
    m = re.search(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", q)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(1[0-2]|0?[1-9])\s*(am|pm)\b", q, re.I)
    if m:
        h = int(m.group(1)) % 12
        return h + 12 if m.group(2).lower() == "pm" else h
    m = re.search(r"\b([01]?\d|2[0-3])\s*(?:h\b|hrs?\b|o'?clock|વાગ્યે|વાગે|baje)", q, re.I)
    return int(m.group(1)) if m else None


def _num(v):
    return f"{v:.1f}"


def local_answer(question, ctx):
    L = T["en"]
    c = compact_context(ctx)
    hours, s = c["hours"], c["summary"]
    if not hours:
        return L["nodata"]
    q = question.lower()
    hh = lambda h: h["t"][11:16]

    hour = _find_hour(q)
    if hour is not None and not re.search(INTENTS[0][1], q):
        h = next((x for x in hours if x["hour"] == hour), None)
        if h:
            ev = controller.EVENING_START_HOUR <= h["hour"] <= controller.EVENING_END_HOUR
            hi = h["demand_kw"] > controller.STORAGE_THRESHOLD_KW
            why = L["why_both"] if hi and ev else L["why_hi"] if hi else L["why_ev"] if ev else L["why_grid"]
            return L["hour"].format(t=hh(h), kw=_num(h["demand_kw"]), mode=L[h["mode"]], why=why,
                                    over=L["over_tag"] if h["overload"] else "")

    intent = next((name for name, pat in INTENTS if re.search(pat, q)), "help")
    peak = max(hours, key=lambda x: x["demand_kw"])
    if intent == "peak":
        avg = sum(x["demand_kw"] for x in hours) / len(hours)
        return L["peak"].format(kw=_num(peak["demand_kw"]), t=hh(peak), avg=_num(avg))
    if intent == "over":
        over = [x for x in hours if x["overload"]]
        if not over:
            return L["over_none"].format(kw=_num(peak["demand_kw"]), t=hh(peak))
        times = ", ".join(f"{hh(x)} ({x['demand_kw']:.0f} kW)" for x in over[:12]) + (f" +{len(over) - 12}" if len(over) > 12 else "")
        return L["over"].format(n=len(over), total=len(hours), times=times)
    if intent == "batt":
        socs = [x["soc_pct"] for x in hours]
        return L["batt"].format(end=_num(socs[-1]), mn=_num(min(socs)), low=f"{controller.BatteryConfig.low_soc_pct:.0f}",
                                lowh=sum(1 for v in socs if v < controller.BatteryConfig.low_soc_pct))
    if intent == "mode":
        st = sum(1 for x in hours if x["mode"] == "storage")
        return L["mode"].format(s=st, g=len(hours) - st, t=hh(hours[0]), m=L[hours[0]["mode"]])
    if intent == "solar":
        sp = max(hours, key=lambda x: x["solar_wm2"])
        return L["solar"].format(w=sp["solar_wm2"], t=hh(sp))
    if intent == "avg":
        return L["avg"].format(avg=_num(sum(x["demand_kw"] for x in hours) / len(hours)), n=len(hours))
    if intent == "rules":
        return L["rules"]
    if intent == "heat":
        m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*(?:°|deg|degree|ડિગ્રી|c\b)", q)
        d = float(m.group(1)) if m else 3.0
        if re.search(r"cool|cold|ઠંડ|ઘટ", q) and d > 0:
            d = -d
        d = max(-10.0, min(10.0, d))

        def shifted(x):
            before = max(0.0, x["temp_c"] - COOLING_BASE_C)
            after = max(0.0, x["temp_c"] + d - COOLING_BASE_C)
            return x["demand_kw"] + COOLING_KW_PER_C * (after - before)
        new = [shifted(x) for x in hours]
        return L["heat"].format(d=d, a=sum(1 for x in hours if x["overload"]),
                                b=sum(1 for v in new if v >= controller.OVERLOAD_THRESHOLD_KW),
                                p1=_num(peak["demand_kw"]), p2=_num(max(new)))
    return L["help"]


# --------------------------------------------------------------------------- entry point
def answer(question, history, ctx):
    """Return {"answer", "source", "note"}; never raises for normal inputs."""
    question = str(question or "").strip()[:MAX_QUESTION]
    history = [m for m in (history or []) if isinstance(m, dict)]
    if claude_available():
        try:
            text = ask_claude(question, history, ctx)
            if text:
                return {"answer": text, "source": "claude", "note": ""}
            note = "Claude declined this question; showing a built-in answer."
        except Exception as e:  # network/API problems must not break the dashboard
            note = f"Claude unavailable ({type(e).__name__}); showing a built-in answer."
    else:
        note = "AI mode is off (ANTHROPIC_API_KEY not set); built-in answers."
    return {"answer": local_answer(question, ctx), "source": "built-in", "note": note}
