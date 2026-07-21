from __future__ import annotations

import importlib.util
import http.client
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
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
            "service_tier": "default",
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
            self.assertEqual(result["daily"][0]["estimated_credits"], result["totals"]["estimated_credits"])

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
            page = MODULE.dashboard_page(result, "session", "admin", 30, b"secret", page="models")
            self.assertIn("future-model", page)
            self.assertIn("估算 Credits", page)
            self.assertNotIn("prompt", page)
            for page_name in ("overview", "machines", "models", "settings"):
                rendered = MODULE.dashboard_page(
                    result, "session", "admin", 30, b"secret", page=page_name
                )
                self.assertIn("Codex 用量中心", rendered)

    def test_quota_snapshot_infers_pool_and_custom_period_policy_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "usage.sqlite3"
            MODULE.initialize(database)
            # Usage that happened before tracking starts must not be assigned to devices.
            observed = datetime.now(timezone.utc).replace(microsecond=0)
            pre_start = self.event()
            pre_start["occurred_at"] = (observed - timedelta(minutes=1)).isoformat()
            MODULE.insert_events(database, [pre_start])
            reset_at = int(time.time()) + 3 * 86400
            quota = {
                "machine_id": "machine-1",
                "observed_at": observed.isoformat(),
                "plan_type": "pro",
                "used_percent": 20,
                "allowed": True,
                "limit_reached": False,
                "limit_window_seconds": 604800,
                "reset_at": reset_at,
            }
            latest = MODULE.insert_quota_snapshot(database, quota)
            baseline = MODULE._control_settings(database)
            self.assertEqual(baseline["baseline_used_percent"], 20)
            self.assertEqual(baseline["baseline_at"], observed.isoformat())
            start_date = observed.astimezone(MODULE.BEIJING).date().isoformat()
            initial = MODULE.summary(
                database, start_date=start_date, end_date=latest["period_end"]
            )
            self.assertEqual(initial["totals"]["requests"], 0)

            post_start = self.event("post-start")
            post_start["occurred_at"] = (observed + timedelta(minutes=1)).isoformat()
            MODULE.insert_events(database, [post_start])
            quota["observed_at"] = (observed + timedelta(minutes=2)).isoformat()
            quota["used_percent"] = 40
            latest = MODULE.insert_quota_snapshot(database, quota)
            result = MODULE.summary(
                database, start_date=start_date, end_date=latest["period_end"]
            )
            self.assertEqual(result["quota"]["remaining_percent"], 60)
            self.assertEqual(result["official_baseline"]["remaining_percent"], 80)
            self.assertEqual(result["official_incremental_used_percent"], 20)
            self.assertEqual(result["totals"]["requests"], 1)
            self.assertAlmostEqual(
                result["inferred_budget_credits"], result["totals"]["estimated_credits"] / 0.2 * 0.8,
                places=6,
            )
            self.assertAlmostEqual(
                result["per_machine_target"], result["inferred_budget_credits"] / 6, places=6
            )
            overview = MODULE.dashboard_page(
                result, "session", "admin", 30, b"secret", page="overview"
            )
            self.assertIn("本轮总体进度", overview)
            self.assertIn("25.00%", overview)
            self.assertIn("150.0%", overview)
            self.assertIn("各设备额度使用率", overview)
            self.assertIn("本轮统计起点", overview)
            self.assertIn(MODULE._short_time(observed.isoformat()), overview)
            MODULE.set_control_settings(
                database, mode="official", start_date="", end_date="", budget=None,
                machine_slots=6, hard_cap=True,
            )
            preserved = MODULE._control_settings(database)
            self.assertEqual(preserved["baseline_at"], observed.isoformat())
            self.assertEqual(preserved["baseline_used_percent"], 20)
            official_policy = MODULE.machine_policy(database, "machine-1")
            self.assertTrue(official_policy["blocked"])
            self.assertEqual(official_policy["mode"], "official")
            self.assertAlmostEqual(official_policy["used_account_percent"], 20.0)
            self.assertAlmostEqual(official_policy["limit_account_percent"], 80 / 6, places=6)
            self.assertEqual(official_policy["baseline_used_percent"], 20)
            # A quota refresh that lowers used_percent (for example a reset card)
            # starts a new baseline instead of producing negative consumption.
            quota["observed_at"] = (observed + timedelta(minutes=3)).isoformat()
            quota["used_percent"] = 5
            MODULE.insert_quota_snapshot(database, quota)
            refreshed = MODULE._control_settings(database)
            self.assertEqual(refreshed["baseline_used_percent"], 5)
            self.assertEqual(refreshed["baseline_at"], quota["observed_at"])
            quota["observed_at"] = (observed + timedelta(minutes=4)).isoformat()
            quota["used_percent"] = 8
            quota["reset_at"] = reset_at + 7 * 86400
            MODULE.insert_quota_snapshot(database, quota)
            new_window = MODULE._control_settings(database)
            self.assertEqual(new_window["baseline_used_percent"], 8)
            self.assertEqual(new_window["baseline_at"], quota["observed_at"])
            self.assertEqual(new_window["baseline_reset_at"], quota["reset_at"])
            today = datetime.now(MODULE.BEIJING).date().isoformat()
            MODULE.set_control_settings(
                database, mode="manual", start_date=today, end_date=today, budget=0.001,
                machine_slots=6, hard_cap=True,
            )
            policy = MODULE.machine_policy(database, "machine-1")
            self.assertTrue(policy["blocked"])
            self.assertEqual(policy["reason"], "machine_credit_limit_reached")
            self.assertEqual(policy["mode"], "manual")

    def test_official_window_uses_exact_times_not_midnight_dates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "usage.sqlite3"
            MODULE.initialize(database)
            now = datetime.now(timezone.utc).replace(microsecond=0)
            reset = datetime(2026, 7, 25, 4, 30, tzinfo=timezone.utc)
            exact_start = reset - timedelta(days=7)
            outside = self.event("outside-window")
            outside["occurred_at"] = (exact_start - timedelta(hours=1)).isoformat()
            inside = self.event("inside-window")
            inside["occurred_at"] = (exact_start + timedelta(hours=1)).isoformat()
            MODULE.insert_events(database, [outside, inside])
            quota = MODULE.insert_quota_snapshot(database, {
                "machine_id": "machine-1", "observed_at": now.isoformat(), "plan_type": "pro",
                "used_percent": 10, "allowed": True, "limit_reached": False,
                "limit_window_seconds": 7 * 86400, "reset_at": int(reset.timestamp()),
            })
            # Both events fall on the same Beijing calendar date, but only one is inside
            # the exact rolling window. Date-only filtering would incorrectly count both.
            self.assertEqual(
                (exact_start - timedelta(hours=1)).astimezone(MODULE.BEIJING).date(),
                (exact_start + timedelta(hours=1)).astimezone(MODULE.BEIJING).date(),
            )
            result = MODULE.summary(
                database, start_date=quota["period_start"], end_date=quota["period_end"]
            )
            self.assertEqual(result["totals"]["requests"], 1)
            self.assertEqual(
                MODULE._short_time("2026-07-18T04:30:00+00:00"), "2026-07-18 12:30"
            )

    def test_signed_dashboard_session_expires_and_rejects_tampering(self) -> None:
        token = MODULE.create_session(b"secret", role="admin", now=1_000)
        self.assertEqual(MODULE.validate_session(b"secret", token, now=1_100), "admin")
        self.assertIsNone(MODULE.validate_session(b"secret", token + "x", now=1_100))
        self.assertIsNone(MODULE.validate_session(b"secret", token, now=50_000))

    def test_fast_mode_applies_official_model_multiplier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "usage.sqlite3"
            MODULE.initialize(database)
            standard = self.event("standard")
            standard["model"] = "gpt-5.4"
            fast = self.event("fast")
            fast["model"] = "gpt-5.4"
            fast["service_tier"] = "priority"
            MODULE.insert_events(database, [standard, fast])
            result = MODULE.summary(database, 30)
            credits = {item["service_tier"]: item["estimated_credits"] for item in result["models"]}
            self.assertAlmostEqual(credits["priority"], credits["default"] * 2, delta=0.000002)

    def test_control_modes_are_mutually_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "usage.sqlite3"
            MODULE.initialize(database)
            MODULE.set_control_settings(
                database, mode="official", start_date="ignored", end_date="ignored",
                budget=999, machine_slots=6, hard_cap=True,
            )
            official = MODULE._control_settings(database)
            self.assertEqual(official["mode"], "official")
            self.assertEqual(official["start_date"], "")
            self.assertIsNone(official["budget_credits"])
            with self.assertRaises(ValueError):
                MODULE.set_control_settings(
                    database, mode="manual", start_date="2026-07-01", end_date="2026-07-31",
                    budget=None, machine_slots=6, hard_cap=False,
                )
            MODULE.set_control_settings(
                database, mode="manual", start_date="2026-07-01", end_date="2026-07-31",
                budget=1200, machine_slots=6, hard_cap=False,
            )
            manual = MODULE._control_settings(database)
            self.assertEqual(manual["mode"], "manual")
            self.assertEqual(manual["budget_credits"], 1200)
            page = MODULE.dashboard_page(
                MODULE.summary(database, start_date="2026-07-01", end_date="2026-07-31"),
                "session", "admin", 30, b"secret", page="settings",
            )
            self.assertIn("官方额度自动六等分", page)
            self.assertIn("手动周期与预算", page)

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
            del event["service_tier"]
            self.assertEqual(MODULE.insert_events(database, [event]), 1)
            self.assertEqual(MODULE.summary(database, 30)["models"][0]["model_level"], "default")

    def test_dashboard_login_and_authenticated_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = MODULE.UsageServer(
                ("127.0.0.1", 0), MODULE.Handler,
                database=Path(directory) / "usage.sqlite3",
                report_token="report", admin_token="admin",
                dashboard_admin_username="admin", dashboard_admin_password="admin horse",
                dashboard_viewer_username="viewer", dashboard_viewer_password="correct horse",
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
                self.assertIn("每日 Credits", page)
                self.assertIn("各设备额度使用率", page)

                connection.request("GET", "/dashboard/daily?days=30", headers={"Cookie": cookie})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn("每日 Credits", response.read().decode("utf-8"))

                connection.request("GET", "/v1/usage/summary?days=30", headers={"Cookie": cookie})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn("daily", response.read().decode("utf-8"))

                session_token = cookie.split("=", 1)[1]
                body = (
                    "csrf=" + MODULE.csrf_token(b"session-secret", session_token)
                    + "&days=30&budget=10"
                )
                connection.request(
                    "POST", "/dashboard/budget", body,
                    {"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                self.assertIn("administrator_required", response.read().decode("utf-8"))
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
