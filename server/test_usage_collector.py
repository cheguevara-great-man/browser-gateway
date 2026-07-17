from __future__ import annotations

import importlib.util
import http.client
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "usage_collector", Path(__file__).with_name("usage_collector.py")
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CollectorTests(unittest.TestCase):
    def event(self, event_id: str = "event-1") -> dict[str, object]:
        return {
            "event_id": event_id,
            "machine_id": "machine-1",
            "machine_name": "PC-1",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "route": "chatgpt-codex",
            "model": "gpt-5.3-codex",
            "model_level": "high",
            "input_tokens": 100,
            "cached_input_tokens": 50,
            "output_tokens": 20,
            "reasoning_output_tokens": 5,
            "total_tokens": 120,
        }

    def test_deduplicates_and_aggregates_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "usage.sqlite3"
            MODULE.initialize(database)
            self.assertEqual(MODULE.insert_events(database, [self.event()]), 1)
            self.assertEqual(MODULE.insert_events(database, [self.event()]), 0)
            result = MODULE.summary(database, 30)
            self.assertEqual(result["totals"]["requests"], 1)
            self.assertEqual(result["totals"]["total_tokens"], 120)
            self.assertEqual(result["machines"][0]["machine_name"], "PC-1")
            self.assertAlmostEqual(result["totals"]["estimated_credits"], 0.009406, places=6)
            self.assertEqual(result["models"][0]["model_level"], "high")

    def test_custom_rate_budget_and_dashboard_fairness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "usage.sqlite3"
            MODULE.initialize(database)
            event = self.event()
            event["model"] = "future-model"
            MODULE.insert_events(database, [event])
            self.assertEqual(MODULE.summary(database, 30)["totals"]["unrated_tokens"], 120)
            MODULE.set_rate(database, "future-model", 100, 10, 500)
            MODULE.set_budget(database, 30, 4.0)
            result = MODULE.summary(database, 30)
            self.assertAlmostEqual(result["totals"]["estimated_credits"], 0.0155)
            self.assertEqual(result["per_machine_target"], 4.0)
            page = MODULE.dashboard_page(result, "session", 30, b"secret")
            self.assertIn("future-model", page)
            self.assertIn("估算 Credits", page)
            self.assertNotIn("prompt", page)

    def test_signed_dashboard_session_expires_and_rejects_tampering(self) -> None:
        token = MODULE.create_session(b"secret", now=1_000)
        self.assertTrue(MODULE.validate_session(b"secret", token, now=1_100))
        self.assertFalse(MODULE.validate_session(b"secret", token + "x", now=1_100))
        self.assertFalse(MODULE.validate_session(b"secret", token, now=50_000))

    def test_rejects_impossible_totals_and_extra_fields(self) -> None:
        event = self.event()
        event["total_tokens"] = 1
        with self.assertRaises(ValueError):
            MODULE.validate_event(event)
        event = self.event()
        event["prompt"] = "must not be accepted"
        with self.assertRaises(ValueError):
            MODULE.validate_event(event)

    def test_accepts_queued_events_from_bridge_2_7(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "usage.sqlite3"
            MODULE.initialize(database)
            event = self.event()
            del event["model_level"]
            self.assertEqual(MODULE.insert_events(database, [event]), 1)
            self.assertEqual(MODULE.summary(database, 30)["models"][0]["model_level"], "default")

    def test_dashboard_login_and_authenticated_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = MODULE.UsageServer(
                ("127.0.0.1", 0), MODULE.Handler,
                database=Path(directory) / "usage.sqlite3",
                report_token="report", admin_token="admin",
                dashboard_username="viewer", dashboard_password="correct horse",
                session_secret="session-secret",
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                connection.request("GET", "/dashboard")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn("Token 用量中心", response.read().decode("utf-8"))

                body = "username=viewer&password=correct+horse"
                connection.request(
                    "POST", "/login", body,
                    {"Content-Type": "application/x-www-form-urlencoded"},
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 303)
                cookie = response.getheader("Set-Cookie").split(";", 1)[0]
                response.read()

                connection.request("GET", "/dashboard", headers={"Cookie": cookie})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                page = response.read().decode("utf-8")
                self.assertIn("机器额度平衡", page)
                self.assertIn("模型、档位与 Credits 费率", page)
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
