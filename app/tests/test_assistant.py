import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app as appmod  # noqa: E402
import assistant  # noqa: E402


def ctx():
    hours = []
    for i, (hr, kw, temp) in enumerate([(17, 150.0, 30.0), (18, 210.0, 34.0), (19, 120.0, 25.0), (23, 90.0, 22.0)]):
        mode = "Renewable Storage Mode" if kw > 160 or 18 <= hr <= 22 else "Main Grid Mode"
        hours.append({"timestamp": f"2024-07-01 {hr:02d}:00:00", "hour": hr, "ambient_temp_c": temp,
                      "humidity_pct": 50, "solar_irradiance_wm2": 300 if hr == 17 else 0,
                      "demand_kw": kw, "mode": mode, "overload": kw >= 200, "soc_pct": 80 - i * 10})
    return {"source": "quickml", "label": "test", "hours": hours, "summary": {"peak_kw": 210.0}}


class AssistantTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        appmod.app.testing = True
        self.c = appmod.app.test_client()

    def test_builtin_answers_from_data(self):
        self.assertIn("210.0 kW", assistant.local_answer("When is the peak?", ctx()))
        self.assertIn("1 of 4", assistant.local_answer("How many overload hours?", ctx()))
        a = assistant.local_answer("Why storage at 19:00?", ctx())
        self.assertIn("evening window", a)
        self.assertIn("Main Grid", assistant.local_answer("why 23:00", ctx()))

    def test_answers_are_always_english(self):
        a = assistant.local_answer("સૌથી વધુ માંગ ક્યારે?", ctx())  # Gujarati question, English answer
        self.assertNotRegex(a, r"[઀-૿]")
        self.assertIn("Peak demand is 210.0 kW", a)

    def test_heat_estimate_raises_overloads(self):
        a = assistant.local_answer("What if it is 8 degrees hotter?", ctx())
        self.assertIn("from 1 to 2", a)  # 17:00: 150 + 8*8.6 = 218.8 kW -> new overload; 19:00: 120 + 68.8 < 200
        self.assertIn("estimate", a.lower())

    def test_no_data(self):
        self.assertIn("Run a forecast", assistant.local_answer("peak?", {}))

    def test_endpoint_falls_back_and_labels_source(self):
        r = self.c.post("/api/chat", json={"question": "peak?", "context": ctx()})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["source"], "built-in")
        self.assertIn("ANTHROPIC_API_KEY", d["note"])
        self.assertEqual(self.c.post("/api/chat", json={"question": ""}).status_code, 400)

    def test_rate_limit(self):
        appmod._chat_hits.clear()
        h = {"X-Forwarded-For": "203.0.113.9"}
        codes = [self.c.post("/api/chat", json={"question": "peak?", "context": ctx()}, headers=h).status_code
                 for _ in range(appmod.CHAT_LIMIT + 1)]
        self.assertEqual(codes[-1], 429)
        self.assertEqual(codes[0], 200)


class ClaudePathTests(unittest.TestCase):
    """Claude mode with a fake client: checks the request we send and the fallbacks (no network)."""

    def setUp(self):
        os.environ["ANTHROPIC_API_KEY"] = "test-key"

    def tearDown(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)

    def _fake(self, stop_reason="end_turn", text="Peak is 210 kW at 18:00.", exc=None):
        from types import SimpleNamespace
        from unittest import mock
        calls = {}

        def create(**kw):
            calls.update(kw)
            if exc:
                raise exc
            return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])
        client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
        return calls, mock.patch("anthropic.Anthropic", return_value=client)

    def test_claude_answer_and_request_shape(self):
        calls, patch = self._fake()
        with patch:
            out = assistant.answer("When is the peak?", [{"role": "assistant", "text": "hi"}], ctx())
        self.assertEqual(out["source"], "claude")
        self.assertEqual(calls["model"], "claude-opus-5-5")
        self.assertEqual(calls["fallbacks"], "default")
        self.assertIn("server-side-fallback-2026-07-01", calls["betas"])
        self.assertEqual(calls["messages"][0]["role"], "user")  # leading assistant turn dropped
        self.assertIn("Question: When is the peak?", calls["messages"][-1]["content"])

    def test_refusal_and_errors_fall_back(self):
        _, patch = self._fake(stop_reason="refusal")
        with patch:
            self.assertEqual(assistant.answer("peak?", [], ctx())["source"], "built-in")
        _, patch = self._fake(exc=RuntimeError("network down"))
        with patch:
            out = assistant.answer("peak?", [], ctx())
        self.assertEqual(out["source"], "built-in")
        self.assertIn("Claude unavailable", out["note"])


if __name__ == "__main__":
    unittest.main()
