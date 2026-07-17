#!/usr/bin/env python3
"""Small authenticated HTTPS service for central token-usage aggregation."""

from __future__ import annotations

import argparse
import hmac
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


MAX_BODY = 256 * 1024


class UsageServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, *, database: Path, report_token: str, admin_token: str):
        self.database = database
        self.report_token = report_token
        self.admin_token = admin_token
        super().__init__(address, handler)
        initialize(database)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BrowserGatewayUsage/1"
    sys_version = ""

    def do_GET(self):  # noqa: N802
        path = urlsplit(self.path)
        if path.path == "/health":
            self._json(200, {"status": "ok"})
            return
        if path.path != "/v1/usage/summary":
            self._json(404, {"error": "not_found"})
            return
        if not self._authorized(self.server.admin_token):
            self._json(401, {"error": "unauthorized"})
            return
        try:
            days = int(parse_qs(path.query).get("days", ["30"])[0])
        except ValueError:
            days = 30
        days = min(max(days, 1), 366)
        self._json(200, summary(self.server.database, days))

    def do_POST(self):  # noqa: N802
        if urlsplit(self.path).path != "/v1/usage/events":
            self._json(404, {"error": "not_found"})
            return
        if not self._authorized(self.server.report_token):
            self._discard_body()
            self._json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if not 1 <= length <= MAX_BODY:
                raise ValueError("invalid body length")
            raw = self.rfile.read(length)
            value = json.loads(raw)
            events = value if isinstance(value, list) else [value]
            if not 1 <= len(events) <= 100:
                raise ValueError("invalid event count")
            accepted = insert_events(self.server.database, events)
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {"error": "invalid_event"})
            return
        self._json(202, {"accepted": accepted})

    def _authorized(self, expected: str) -> bool:
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(supplied, f"Bearer {expected}")

    def _discard_body(self) -> None:
        try:
            length = min(max(int(self.headers.get("Content-Length", "0")), 0), MAX_BODY)
            if length:
                self.rfile.read(length)
        except ValueError:
            return

    def _json(self, status: int, value: object) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def initialize(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(path)) as database, database:
        database.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS usage_events(
                event_id TEXT PRIMARY KEY,
                machine_id TEXT NOT NULL,
                machine_name TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                route TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                cached_input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                reasoning_output_tokens INTEGER NOT NULL,
                total_tokens INTEGER NOT NULL,
                received_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_usage_machine_time
                ON usage_events(machine_id, occurred_at);
            """
        )


def insert_events(path: Path, events: list[object]) -> int:
    rows = [validate_event(event) for event in events]
    accepted = 0
    received_at = datetime.now(timezone.utc).isoformat()
    with closing(connect(path)) as database, database:
        for row in rows:
            cursor = database.execute(
                """INSERT OR IGNORE INTO usage_events(
                    event_id,machine_id,machine_name,occurred_at,route,model,
                    input_tokens,cached_input_tokens,output_tokens,
                    reasoning_output_tokens,total_tokens,received_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (*row, received_at),
            )
            accepted += cursor.rowcount
    return accepted


def validate_event(value: object) -> tuple[object, ...]:
    if not isinstance(value, dict) or set(value) != {
        "event_id", "machine_id", "machine_name", "occurred_at", "route", "model",
        "input_tokens", "cached_input_tokens", "output_tokens",
        "reasoning_output_tokens", "total_tokens",
    }:
        raise ValueError("invalid fields")
    strings = []
    for name, maximum in (
        ("event_id", 64), ("machine_id", 64), ("machine_name", 128),
        ("occurred_at", 64), ("route", 64), ("model", 128),
    ):
        item = value[name]
        if not isinstance(item, str) or not item or len(item) > maximum:
            raise ValueError("invalid string")
        strings.append(item)
    parsed = datetime.fromisoformat(strings[3].replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp needs timezone")
    numbers = []
    for name in (
        "input_tokens", "cached_input_tokens", "output_tokens",
        "reasoning_output_tokens", "total_tokens",
    ):
        item = value[name]
        if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 2_000_000_000:
            raise ValueError("invalid token count")
        numbers.append(item)
    if numbers[4] <= 0 or numbers[4] < numbers[0] + numbers[2]:
        raise ValueError("invalid total")
    return (*strings, *numbers)


def summary(path: Path, days: int) -> dict[str, object]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with closing(connect(path)) as database, database:
        rows = database.execute(
            """SELECT machine_id,machine_name,COUNT(*),SUM(input_tokens),
                      SUM(cached_input_tokens),SUM(output_tokens),
                      SUM(reasoning_output_tokens),SUM(total_tokens),MAX(occurred_at)
                 FROM usage_events WHERE occurred_at >= ?
                 GROUP BY machine_id,machine_name ORDER BY SUM(total_tokens) DESC""",
            (since,),
        ).fetchall()
        daily = database.execute(
            """SELECT substr(occurred_at,1,10),SUM(total_tokens)
                 FROM usage_events WHERE occurred_at >= ?
                 GROUP BY substr(occurred_at,1,10) ORDER BY 1""",
            (since,),
        ).fetchall()
    machines = [
        {
            "machine_id": row[0], "machine_name": row[1], "requests": row[2],
            "input_tokens": row[3], "cached_input_tokens": row[4],
            "output_tokens": row[5], "reasoning_output_tokens": row[6],
            "total_tokens": row[7], "last_seen": row[8],
        }
        for row in rows
    ]
    return {
        "days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "machines": machines,
        "daily": [{"date": row[0], "total_tokens": row[1]} for row in daily],
        "totals": {
            "machines": len(machines),
            "requests": sum(item["requests"] for item in machines),
            "input_tokens": sum(item["input_tokens"] for item in machines),
            "cached_input_tokens": sum(item["cached_input_tokens"] for item in machines),
            "output_tokens": sum(item["output_tokens"] for item in machines),
            "reasoning_output_tokens": sum(item["reasoning_output_tokens"] for item in machines),
            "total_tokens": sum(item["total_tokens"] for item in machines),
        },
    }


def connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path, timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19443)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    args = parser.parse_args()
    credentials = json.loads(args.credentials.read_text(encoding="utf-8"))
    server = UsageServer(
        (args.listen, args.port), Handler, database=args.database,
        report_token=credentials["report_token"], admin_token=credentials["admin_token"],
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
