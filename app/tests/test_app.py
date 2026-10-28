import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app as appmod  # noqa: E402
import quickml_client  # noqa: E402


def fake_predict(rows):
    # deterministic stand-in for QuickML: the offline estimate, labelled as quickml
    return quickml_client.offline_estimate(rows), {
        "source": "quickml", "method": "batch-records", "latency_s": 0.01, "note": ""}


class ApiTests(unittest.TestCase):
    def setUp(self):
        appmod.app.testing = True
        self.c = appmod.app.test_client()

    @mock.patch.object(quickml_client, "predict_rows", side_effect=fake_predict)
    def test_sample_window_applies_rules_end_to_end(self, _p):
        d = self.c.get("/api/sample?start=0&hours=24").get_json()
        self.assertEqual(len(d["hours"]), 24)
        self.assertEqual(d["source"], "quickml")
        for h in d["hours"]:
            storage = h["demand_kw"] > 160 or 18 <= h["hour"] <= 22
            self.assertEqual(h["mode"] == "Renewable Storage Mode", storage)
            self.assertEqual(h["overload"], h["demand_kw"] >= 200)
        self.assertEqual(d["summary"]["overload_hours"], len(d["alerts"]))

    @mock.patch.object(quickml_client, "predict_rows", side_effect=fake_predict)
    def test_csv_upload_cleans_missing_values(self, _p):
        csv_text = (
            "timestamp,ambient_temp_c,humidity_pct,solar_irradiance_wm2\n"
            "2024-07-01 00:00:00,28.0,60,0\n"
            "2024-07-01 01:00:00,,61,0\n"
            "2024-07-01 02:00:00,30.0,62,0\n"
            "2024-07-01 02:00:00,31.0,62,0\n"
            "2024-07-01 03:00:00,30.0,150,\n"
            "not-a-date,1,2,3\n"
        )
        r = self.c.post("/api/forecast", data={"file": (io.BytesIO(csv_text.encode()), "t.csv")},
                        content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(len(d["hours"]), 4)  # bad date dropped, duplicate removed
        joined = " ".join(d["warnings"])
        self.assertIn("unreadable timestamp", joined)
        self.assertIn("duplicate", joined)
        self.assertIn("Filled", joined)
        self.assertIn("Clipped", joined)  # humidity 150 -> 100

    def test_missing_columns_rejected(self):
        r = self.c.post("/api/forecast", data={"file": (io.BytesIO(b"a,b\n1,2\n"), "x.csv")},
                        content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Missing required column", r.get_json()["error"])

    @mock.patch.object(quickml_client, "predict_rows", side_effect=fake_predict)
    def test_json_manual_rows(self, _p):
        rows = [{"timestamp": "2024-07-01 19:00:00", "ambient_temp_c": 35, "humidity_pct": 50,
                 "solar_irradiance_wm2": 0}]
        d = self.c.post("/api/forecast", json={"rows": rows}).get_json()
        h = d["hours"][0]
        self.assertEqual(h["mode"], "Renewable Storage Mode")
        self.assertTrue(h["overload"])  # 197.55 + 8.5876*11 = 292 kW

    @mock.patch.object(quickml_client, "predict_rows", side_effect=fake_predict)
    def test_predict_proxy_keeps_row_order_and_count(self, _p):
        rows = [{"hour": h, "ambient_temp_c": 30, "humidity_pct": 50, "solar_irradiance_wm2": 0} for h in (3, 19, 3)]
        d = self.c.post("/api/predict", json={"rows": rows}).get_json()
        self.assertEqual(len(d["predictions"]), 3)
        self.assertEqual(d["predictions"][0], d["predictions"][2])
        self.assertGreater(d["predictions"][1], d["predictions"][0])
        self.assertEqual(d["source"], "quickml")
        self.assertEqual(self.c.post("/api/predict", json={"rows": [{"hour": 1}]}).status_code, 400)

    def test_offline_fallback_is_labelled_when_not_configured(self):
        for k in ("QUICKML_ENDPOINT_KEY", "ZOHO_REFRESH_TOKEN"):
            os.environ.pop(k, None)
        d = self.c.get("/api/sample?start=0&hours=3").get_json()
        self.assertEqual(d["source"], "offline-estimate")
        self.assertIn("not configured", d["note"])

    def test_health_and_logs_do_not_leak_secrets(self):
        os.environ["QUICKML_ENDPOINT_KEY"] = "SECRET-KEY-123"
        try:
            self.c.get("/api/sample?start=0&hours=2")
            txt = self.c.get("/api/health").get_data(as_text=True) + self.c.get("/api/logs").get_data(as_text=True)
            self.assertNotIn("SECRET-KEY-123", txt)
        finally:
            os.environ.pop("QUICKML_ENDPOINT_KEY", None)


if __name__ == "__main__":
    unittest.main()
