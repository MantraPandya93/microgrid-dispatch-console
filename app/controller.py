"""Smart-grid transition controller (HackSphere 3.0, section 6) + simple battery model.

Rules, evaluated per forecast hour using predicted demand (kW) and the timestamp hour:
    demand > 160 kW  OR  18 <= hour <= 22 (inclusive)  -> Renewable Storage Mode
    otherwise                                          -> Main Grid Mode
    demand >= 200 kW                                   -> Critical Overload Alert
                                                          (independent of grid mode)
"""
from dataclasses import dataclass

STORAGE_THRESHOLD_KW = 160.0   # strictly greater than
OVERLOAD_THRESHOLD_KW = 200.0  # greater than or equal
EVENING_START_HOUR = 18        # inclusive
EVENING_END_HOUR = 22          # inclusive

MODE_STORAGE = "Renewable Storage Mode"
MODE_GRID = "Main Grid Mode"


def decide(demand_kw: float, hour: int):
    """Return (mode, overload) for one forecast hour."""
    in_evening = EVENING_START_HOUR <= hour <= EVENING_END_HOUR
    storage = demand_kw > STORAGE_THRESHOLD_KW or in_evening
    mode = MODE_STORAGE if storage else MODE_GRID
    overload = demand_kw >= OVERLOAD_THRESHOLD_KW
    return mode, overload


@dataclass
class BatteryConfig:
    """Illustrative microgrid battery assumptions (all adjustable from the dashboard/API)."""
    capacity_kwh: float = 4000.0
    initial_soc_pct: float = 90.0
    low_soc_pct: float = 15.0          # raise a warning below this level
    grid_charge_kw: float = 100.0      # charging power available in Main Grid Mode
    solar_kw_per_wm2: float = 0.15     # PV output (kW) per W/m2 of irradiance


def simulate(hours, cfg: BatteryConfig):
    """Run the controller over a forecast window and track battery state of charge.

    hours: list of dicts with keys: hour, demand_kw, solar_irradiance_wm2
    Returns (decorated hours, summary). Each hour gains: mode, overload, soc_pct,
    battery_kw (negative = discharging, positive = charging), solar_kw, battery_low.
    The grid mode itself always follows the section-6 rules; the battery model is
    informational and never overrides it.
    """
    cap = max(1.0, float(cfg.capacity_kwh))
    soc = min(100.0, max(0.0, float(cfg.initial_soc_pct)))
    out = []
    for h in hours:
        demand = float(h["demand_kw"])
        mode, overload = decide(demand, int(h["hour"]))
        solar_kw = max(0.0, float(h.get("solar_irradiance_wm2", 0.0))) * cfg.solar_kw_per_wm2

        if mode == MODE_STORAGE:
            wanted = max(0.0, demand - solar_kw)            # kWh drawn from battery this hour
            available = soc / 100.0 * cap
            drawn = min(wanted, available)
            battery_kw = -drawn
        else:
            room = (100.0 - soc) / 100.0 * cap
            battery_kw = min(cfg.grid_charge_kw + solar_kw, room)
        soc = min(100.0, max(0.0, soc + battery_kw / cap * 100.0))

        row = dict(h)
        row.update(
            mode=mode,
            overload=overload,
            solar_kw=round(solar_kw, 2),
            battery_kw=round(battery_kw, 2),
            soc_pct=round(soc, 2),
            battery_low=soc < cfg.low_soc_pct,
        )
        out.append(row)

    summary = summarize(out)
    return out, summary


def summarize(rows):
    if not rows:
        return {}
    peak = max(rows, key=lambda r: r["demand_kw"])
    return {
        "hours": len(rows),
        "peak_kw": round(peak["demand_kw"], 2),
        "peak_timestamp": peak["timestamp"],
        "avg_kw": round(sum(r["demand_kw"] for r in rows) / len(rows), 2),
        "storage_hours": sum(1 for r in rows if r["mode"] == MODE_STORAGE),
        "grid_hours": sum(1 for r in rows if r["mode"] == MODE_GRID),
        "overload_hours": sum(1 for r in rows if r["overload"]),
        "min_soc_pct": round(min(r["soc_pct"] for r in rows), 2),
        "final_soc_pct": rows[-1]["soc_pct"],
        "battery_low_hours": sum(1 for r in rows if r["battery_low"]),
        "first_mode": rows[0]["mode"],
    }
