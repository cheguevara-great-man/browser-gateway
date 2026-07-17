from __future__ import annotations

import importlib.util
import tempfile
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
            "model": "gpt-test",
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

    def test_rejects_impossible_totals_and_extra_fields(self) -> None:
        event = self.event()
        event["total_tokens"] = 1
        with self.assertRaises(ValueError):
            MODULE.validate_event(event)
        event = self.event()
        event["prompt"] = "must not be accepted"
        with self.assertRaises(ValueError):
            MODULE.validate_event(event)


if __name__ == "__main__":
    unittest.main()
