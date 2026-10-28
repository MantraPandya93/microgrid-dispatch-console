import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from controller import (  # noqa: E402
    MODE_GRID, MODE_STORAGE, BatteryConfig, decide, simulate,
)


class DecideTests(unittest.TestCase):
    def test_threshold_160_is_strictly_greater(self):
        self.assertEqual(decide(160.0, 10), (MODE_GRID, False))
        self.assertEqual(decide(160.01, 10), (MODE_STORAGE, False))
        self.assertEqual(decide(159.99, 10), (MODE_GRID, False))

    def test_evening_window_is_inclusive(self):
        for hour in (18, 19, 20, 21, 22):
            self.assertEqual(decide(90.0, hour)[0], MODE_STORAGE, hour)
        for hour in (0, 6, 17, 23):
            self.assertEqual(decide(90.0, hour)[0], MODE_GRID, hour)

    def test_overload_200_is_inclusive_and_independent_of_mode(self):
        self.assertEqual(decide(200.0, 10), (MODE_STORAGE, True))
        self.assertEqual(decide(199.99, 10), (MODE_STORAGE, False))
        self.assertEqual(decide(250.0, 3), (MODE_STORAGE, True))
        self.assertEqual(decide(205.0, 19), (MODE_STORAGE, True))

    def test_low_demand_outside_window_is_grid(self):
        self.assertEqual(decide(92.0, 3), (MODE_GRID, False))


class BatteryTests(unittest.TestCase):
    def rows(self, spec):
        return [
            {"timestamp": f"2024-01-01 {h:02d}:00:00", "hour": h, "demand_kw": d,
             "solar_irradiance_wm2": s}
            for h, d, s in spec
        ]

    def test_modes_follow_rules_and_soc_stays_in_bounds(self):
        cfg = BatteryConfig(capacity_kwh=500, initial_soc_pct=50)
        spec = [(h, 120.0 if h < 18 else 230.0, 0.0) for h in range(24)]
        rows, summary = simulate(self.rows(spec), cfg)
        for r in rows:
            self.assertGreaterEqual(r["soc_pct"], 0.0)
            self.assertLessEqual(r["soc_pct"], 100.0)
            self.assertEqual((r["mode"], r["overload"]), decide(r["demand_kw"], r["hour"]))
        self.assertEqual(summary["hours"], 24)
        self.assertEqual(summary["overload_hours"], 6)  # hours 18..23 at 230 kW

    def test_battery_charges_on_grid_and_discharges_on_storage(self):
        cfg = BatteryConfig(capacity_kwh=1000, initial_soc_pct=50, grid_charge_kw=100)
        rows, _ = simulate(self.rows([(3, 90.0, 0.0), (19, 210.0, 0.0)]), cfg)
        self.assertGreater(rows[0]["soc_pct"], 50.0)
        self.assertLess(rows[1]["soc_pct"], rows[0]["soc_pct"])

    def test_empty_battery_never_goes_negative(self):
        cfg = BatteryConfig(capacity_kwh=100, initial_soc_pct=5)
        rows, _ = simulate(self.rows([(19, 300.0, 0.0), (20, 300.0, 0.0)]), cfg)
        self.assertEqual(rows[-1]["soc_pct"], 0.0)
        self.assertTrue(rows[-1]["battery_low"])


if __name__ == "__main__":
    unittest.main()
