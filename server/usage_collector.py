#!/usr/bin/env python3
"""Small authenticated HTTPS service for central token-usage aggregation."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import json
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit


MAX_BODY = 256 * 1024
BEIJING = timezone(timedelta(hours=8))

# Official Codex token-based rate card, credits per one million tokens.
# Unknown future models deliberately remain unrated until configured in the dashboard.
DEFAULT_MODEL_RATES = {
    "gpt-5.6-sol": (125.0, 12.5, 750.0),
    "gpt-5.6-terra": (62.5, 6.25, 375.0),
    "gpt-5.6-luna": (25.0, 2.5, 150.0),
    "gpt-5.5": (125.0, 12.5, 750.0),
    "gpt-5.5-cyber": (500.0, 50.0, 3000.0),
    "gpt-5.4": (62.5, 6.25, 375.0),
    "gpt-5.4-mini": (18.75, 1.875, 113.0),
    "gpt-5.3-codex": (43.75, 4.375, 350.0),
    "gpt-5.2": (43.75, 4.375, 350.0),
}


class UsageServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, address, handler, *, database: Path, report_token: str,
        admin_token: str, dashboard_admin_username: str, dashboard_admin_password: str,
        dashboard_viewer_username: str, dashboard_viewer_password: str, session_secret: str,
    ):
        self.database = database
        self.report_token = report_token
        self.admin_token = admin_token
        self.dashboard_admin_username = dashboard_admin_username
        self.dashboard_admin_password = dashboard_admin_password
        self.dashboard_viewer_username = dashboard_viewer_username
        self.dashboard_viewer_password = dashboard_viewer_password
        self.session_secret = session_secret.encode("utf-8")
        super().__init__(address, handler)
        initialize(database)

    def handle_error(self, request, client_address) -> None:
        # Browsers routinely close idle HTTP/1.1 connections. Do not turn that
        # harmless lifecycle event into a traceback in the systemd journal.
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BrowserGatewayUsage/1"
    sys_version = ""

    def do_GET(self):  # noqa: N802
        path = urlsplit(self.path)
        if path.path == "/":
            self._redirect("/dashboard")
            return
        if path.path.startswith("/dashboard"):
            session = self._session()
            if session is None:
                self._html(200, login_page(error=parse_qs(path.query).get("error") == ["1"]))
                return
            days, start_date, end_date = _query_scope(path.query, self.server.database)
            page = {
                "/dashboard": "overview",
                "/dashboard/daily": "overview",
                "/dashboard/machines": "machines",
                "/dashboard/models": "models",
                "/dashboard/settings": "settings",
                "/dashboard/machine": "machine",
            }.get(path.path)
            if page is None:
                self._json(404, {"error": "not_found"})
                return
            query = parse_qs(path.query)
            self._html(200, dashboard_page(
                summary(self.server.database, days, start_date, end_date), session[0], session[1], days,
                self.server.session_secret, page=page,
                machine_id=query.get("id", [""])[0],
            ))
            return
        if path.path == "/logout":
            self._redirect("/dashboard", clear_session=True)
            return
        if path.path == "/health":
            self._json(200, {"status": "ok"})
            return
        if path.path == "/v1/usage/policy":
            if not self._authorized(self.server.report_token):
                self._json(401, {"error": "unauthorized"})
                return
            machine_id = parse_qs(path.query).get("machine_id", [""])[0]
            try:
                self._json(200, machine_policy(self.server.database, machine_id))
            except ValueError:
                self._json(400, {"error": "invalid_machine"})
            return
        if path.path != "/v1/usage/summary":
            self._json(404, {"error": "not_found"})
            return
        if not self._authorized(self.server.admin_token) and self._session() is None:
            self._json(401, {"error": "unauthorized"})
            return
        days, start_date, end_date = _query_scope(path.query, self.server.database)
        self._json(200, summary(self.server.database, days, start_date, end_date))

    def do_POST(self):  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/login":
            self._login()
            return
        if path in {"/dashboard/rate", "/dashboard/budget", "/dashboard/control"}:
            self._dashboard_update(path)
            return
        if path == "/v1/usage/quota":
            if not self._authorized(self.server.report_token):
                self._discard_body()
                self._json(401, {"error": "unauthorized"})
                return
            try:
                value = self._json_body()
                quota = insert_quota_snapshot(self.server.database, value)
            except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
                self._json(400, {"error": "invalid_quota"})
                return
            self._json(202, {"accepted": True, "quota": quota})
            return
        if path != "/v1/usage/events":
            self._json(404, {"error": "not_found"})
            return
        if not self._authorized(self.server.report_token):
            self._discard_body()
            self._json(401, {"error": "unauthorized"})
            return
        try:
            value = self._json_body()
            events = value if isinstance(value, list) else [value]
            if not 1 <= len(events) <= 100:
                raise ValueError("invalid event count")
            accepted = insert_events(self.server.database, events)
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {"error": "invalid_event"})
            return
        self._json(202, {"accepted": accepted})

    def _json_body(self) -> object:
        length = int(self.headers.get("Content-Length", "-1"))
        if not 1 <= length <= MAX_BODY:
            raise ValueError("invalid body length")
        return json.loads(self.rfile.read(length))

    def _login(self) -> None:
        try:
            form = self._form(8192)
        except ValueError:
            self._redirect("/dashboard?error=1")
            return
        username = form.get("username", [""])[0]
        password = form.get("password", [""])[0]
        role = None
        if (
            hmac.compare_digest(username.encode("utf-8"), self.server.dashboard_admin_username.encode("utf-8"))
            and hmac.compare_digest(password.encode("utf-8"), self.server.dashboard_admin_password.encode("utf-8"))
        ):
            role = "admin"
        elif (
            hmac.compare_digest(username.encode("utf-8"), self.server.dashboard_viewer_username.encode("utf-8"))
            and hmac.compare_digest(password.encode("utf-8"), self.server.dashboard_viewer_password.encode("utf-8"))
        ):
            role = "viewer"
        if role is None:
            time.sleep(0.25)
            self._redirect("/dashboard?error=1")
            return
        token = create_session(self.server.session_secret, role=role)
        self.send_response(303)
        self.send_header("Location", "/dashboard")
        self.send_header(
            "Set-Cookie",
            f"bg_usage_session={token}; Path=/; Max-Age=43200; Secure; HttpOnly; SameSite=Strict",
        )
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _dashboard_update(self, path: str) -> None:
        session = self._session()
        if session is None:
            self._redirect("/dashboard")
            return
        if session[1] != "admin":
            self._json(403, {"error": "administrator_required"})
            return
        days = 30
        try:
            form = self._form(16384)
            if not hmac.compare_digest(
                form.get("csrf", [""])[0].encode("utf-8"),
                csrf_token(self.server.session_secret, session[0]).encode("ascii"),
            ):
                raise ValueError("csrf")
            days = min(max(int(form.get("days", ["30"])[0]), 1), 366)
            if path == "/dashboard/rate":
                set_rate(
                    self.server.database,
                    form.get("model", [""])[0],
                    float(form.get("input_rate", [""])[0]),
                    float(form.get("cached_rate", [""])[0]),
                    float(form.get("output_rate", [""])[0]),
                )
            elif path == "/dashboard/budget":
                raw_budget = form.get("budget", [""])[0].strip()
                set_budget(self.server.database, days, float(raw_budget) if raw_budget else None)
            else:
                set_control_settings(
                    self.server.database,
                    mode=form.get("control_mode", ["official"])[0],
                    start_date=form.get("start_date", [""])[0],
                    end_date=form.get("end_date", [""])[0],
                    budget=float(form.get("budget", [""])[0]) if form.get("budget", [""])[0] else None,
                    machine_slots=int(form.get("machine_slots", ["6"])[0]),
                    hard_cap=form.get("hard_cap", [""])[0] == "on",
                )
        except (ValueError, OverflowError):
            self._redirect(f"/dashboard?{urlencode({'days': days, 'error': 1})}")
            return
        self._redirect(f"/dashboard?days={days}")

    def _session(self) -> tuple[str, str] | None:
        for item in self.headers.get("Cookie", "").split(";"):
            name, separator, value = item.strip().partition("=")
            if separator and name == "bg_usage_session":
                role = validate_session(self.server.session_secret, value)
                if role is not None:
                    return value, role
        return None

    def _form(self, maximum: int) -> dict[str, list[str]]:
        if self.headers.get_content_type() != "application/x-www-form-urlencoded":
            raise ValueError("invalid content type")
        length = int(self.headers.get("Content-Length", "-1"))
        if not 0 <= length <= maximum:
            raise ValueError("invalid length")
        return parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)

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

    def _html(self, status: int, value: str) -> None:
        body = value.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str, *, clear_session: bool = False) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        if clear_session:
            self.send_header(
                "Set-Cookie",
                "bg_usage_session=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict",
            )
        self.send_header("Content-Length", "0")
        self.end_headers()

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
                model_level TEXT NOT NULL DEFAULT 'default',
                service_tier TEXT NOT NULL DEFAULT 'default',
                input_tokens INTEGER NOT NULL,
                cached_input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                reasoning_output_tokens INTEGER NOT NULL,
                total_tokens INTEGER NOT NULL,
                received_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_usage_machine_time
                ON usage_events(machine_id, occurred_at);
            CREATE TABLE IF NOT EXISTS model_rates(
                model TEXT PRIMARY KEY,
                input_rate REAL NOT NULL,
                cached_input_rate REAL NOT NULL,
                output_rate REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dashboard_settings(
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quota_snapshots(
                observed_at TEXT NOT NULL,
                machine_id TEXT NOT NULL,
                plan_type TEXT NOT NULL,
                used_percent REAL NOT NULL,
                allowed INTEGER NOT NULL,
                limit_reached INTEGER NOT NULL,
                window_seconds INTEGER NOT NULL,
                reset_at INTEGER NOT NULL,
                received_at TEXT NOT NULL,
                PRIMARY KEY(observed_at, machine_id)
            );
            CREATE INDEX IF NOT EXISTS idx_quota_observed_at
                ON quota_snapshots(observed_at DESC);
            """
        )
        columns = {row[1] for row in database.execute("PRAGMA table_info(usage_events)")}
        if "model_level" not in columns:
            database.execute(
                "ALTER TABLE usage_events ADD COLUMN model_level TEXT NOT NULL DEFAULT 'default'"
            )
        if "service_tier" not in columns:
            database.execute(
                "ALTER TABLE usage_events ADD COLUMN service_tier TEXT NOT NULL DEFAULT 'default'"
            )


def insert_events(path: Path, events: list[object]) -> int:
    rows = [validate_event(event) for event in events]
    accepted = 0
    received_at = datetime.now(timezone.utc).isoformat()
    with closing(connect(path)) as database, database:
        for row in rows:
            cursor = database.execute(
                """INSERT OR IGNORE INTO usage_events(
                    event_id,machine_id,machine_name,occurred_at,route,model,model_level,service_tier,
                    input_tokens,cached_input_tokens,output_tokens,
                    reasoning_output_tokens,total_tokens,received_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (*row, received_at),
            )
            accepted += cursor.rowcount
    return accepted


def validate_event(value: object) -> tuple[object, ...]:
    required = {
        "event_id", "machine_id", "machine_name", "occurred_at", "route", "model", "model_level", "service_tier",
        "input_tokens", "cached_input_tokens", "output_tokens",
        "reasoning_output_tokens", "total_tokens",
    }
    bridge_2_8_required = required - {"service_tier"}
    legacy_required = required - {"model_level", "service_tier"}
    if not isinstance(value, dict) or frozenset(value) not in {
        frozenset(required), frozenset(bridge_2_8_required), frozenset(legacy_required)
    }:
        raise ValueError("invalid fields")
    if "model_level" not in value:
        # Keep rolling upgrades safe: Bridge 2.7 events already queued on any of
        # the six machines remain valid after the central collector is upgraded.
        value = {**value, "model_level": "default"}
    if "service_tier" not in value:
        value = {**value, "service_tier": "default"}
    strings = []
    for name, maximum in (
        ("event_id", 64), ("machine_id", 64), ("machine_name", 128),
        ("occurred_at", 64), ("route", 64), ("model", 128), ("model_level", 64),
        ("service_tier", 64),
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


def insert_quota_snapshot(path: Path, value: object) -> dict[str, object]:
    required = {
        "machine_id", "observed_at", "plan_type", "used_percent", "allowed",
        "limit_reached", "limit_window_seconds", "reset_at",
    }
    if not isinstance(value, dict) or frozenset(value) != frozenset(required):
        raise ValueError("invalid quota fields")
    machine_id = value["machine_id"]
    observed_at = value["observed_at"]
    plan_type = value["plan_type"]
    if not all(isinstance(item, str) and item for item in (machine_id, observed_at, plan_type)):
        raise ValueError("invalid quota strings")
    if len(machine_id) > 64 or len(observed_at) > 64 or len(plan_type) > 32:
        raise ValueError("quota string too long")
    observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    if observed.tzinfo is None:
        raise ValueError("quota timestamp needs timezone")
    used_percent = value["used_percent"]
    if isinstance(used_percent, bool) or not isinstance(used_percent, (int, float)) or not 0 <= used_percent <= 100:
        raise ValueError("invalid used percent")
    allowed = value["allowed"]
    limit_reached = value["limit_reached"]
    if not isinstance(allowed, bool) or not isinstance(limit_reached, bool):
        raise ValueError("invalid quota flags")
    window_seconds = value["limit_window_seconds"]
    reset_at = value["reset_at"]
    if (
        isinstance(window_seconds, bool) or not isinstance(window_seconds, int)
        or not 60 <= window_seconds <= 31 * 86400
        or isinstance(reset_at, bool) or not isinstance(reset_at, int)
        or reset_at <= 0
    ):
        raise ValueError("invalid quota window")
    received_at = datetime.now(timezone.utc).isoformat()
    with closing(connect(path)) as database, database:
        database.execute(
            """INSERT OR REPLACE INTO quota_snapshots(
                observed_at,machine_id,plan_type,used_percent,allowed,limit_reached,
                window_seconds,reset_at,received_at
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                observed_at, machine_id, plan_type[:32], float(used_percent), int(allowed),
                int(limit_reached), window_seconds, reset_at, received_at,
            ),
        )
        database.execute(
            "DELETE FROM quota_snapshots WHERE received_at < ?",
            ((datetime.now(timezone.utc) - timedelta(days=90)).isoformat(),),
        )
    return latest_quota(path) or {}


def latest_quota(path: Path) -> dict[str, object] | None:
    with closing(connect(path)) as database:
        row = database.execute(
            """SELECT observed_at,machine_id,plan_type,used_percent,allowed,limit_reached,
                      window_seconds,reset_at,received_at
                 FROM quota_snapshots ORDER BY observed_at DESC LIMIT 1"""
        ).fetchone()
    if row is None:
        return None
    reset = datetime.fromtimestamp(row[7], timezone.utc)
    start = reset - timedelta(seconds=row[6])
    observed = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
    return {
        "observed_at": row[0], "machine_id": row[1], "plan_type": row[2],
        "used_percent": round(float(row[3]), 2),
        "remaining_percent": round(max(100.0 - float(row[3]), 0.0), 2),
        "allowed": bool(row[4]), "limit_reached": bool(row[5]),
        "limit_window_seconds": int(row[6]), "reset_at": reset.isoformat(),
        "period_start_at": start.isoformat(), "period_end_at": reset.isoformat(),
        "period_start": start.astimezone(BEIJING).date().isoformat(),
        "period_end": reset.astimezone(BEIJING).date().isoformat(),
        "stale": datetime.now(timezone.utc) - observed > timedelta(minutes=20),
    }


def _control_settings(path: Path) -> dict[str, object]:
    with closing(connect(path)) as database:
        settings = dict(database.execute(
            "SELECT setting_key,setting_value FROM dashboard_settings WHERE setting_key LIKE 'control_%'"
        ))
    legacy_manual = bool(settings.get("control_start_date") and settings.get("control_end_date"))
    return {
        "mode": settings.get("control_mode", "manual" if legacy_manual else "official"),
        "start_date": settings.get("control_start_date", ""),
        "end_date": settings.get("control_end_date", ""),
        "budget_credits": float(settings["control_budget_credits"]) if settings.get("control_budget_credits") else None,
        "machine_slots": int(settings.get("control_machine_slots", "6")),
        "hard_cap": settings.get("control_hard_cap", "false") == "true",
    }


def set_control_settings(
    path: Path, *, mode: str = "manual", start_date: str, end_date: str, budget: float | None,
    machine_slots: int, hard_cap: bool,
) -> None:
    if mode not in {"official", "manual"}:
        raise ValueError("invalid control mode")
    if not 1 <= machine_slots <= 100:
        raise ValueError("invalid machine slots")
    if mode == "manual":
        start = _date_value(start_date)
        end = _date_value(end_date)
        if start > end:
            raise ValueError("start after end")
        if (end - start).days > 366:
            raise ValueError("period too long")
        if budget is None or not 0.000001 <= budget <= 10_000_000_000:
            raise ValueError("manual mode requires budget")
        stored_start, stored_end, stored_budget = start.isoformat(), end.isoformat(), str(budget)
    else:
        stored_start = stored_end = stored_budget = ""
    values = {
        "control_mode": mode,
        "control_start_date": stored_start,
        "control_end_date": stored_end,
        "control_budget_credits": stored_budget,
        "control_machine_slots": str(machine_slots),
        "control_hard_cap": "true" if hard_cap else "false",
    }
    with closing(connect(path)) as database, database:
        for key, value in values.items():
            database.execute(
                "INSERT INTO dashboard_settings(setting_key,setting_value) VALUES (?,?) "
                "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value",
                (key, value),
            )


def summary(
    path: Path, days: int = 30, start_date: str | None = None, end_date: str | None = None,
) -> dict[str, object]:
    since, until, resolved_start, resolved_end = _time_bounds(days, start_date, end_date)
    current_quota = latest_quota(path)
    if (
        start_date and end_date and current_quota
        and current_quota["period_start"] == resolved_start
        and current_quota["period_end"] == resolved_end
    ):
        since = str(current_quota["period_start_at"])
        until = str(current_quota["period_end_at"])
    with closing(connect(path)) as database, database:
        rows = database.execute(
            """SELECT machine_id,machine_name,model,model_level,service_tier,COUNT(*),SUM(input_tokens),
                      SUM(cached_input_tokens),SUM(output_tokens),
                      SUM(reasoning_output_tokens),SUM(total_tokens),MAX(occurred_at)
                 FROM usage_events WHERE occurred_at >= ? AND occurred_at < ?
                 GROUP BY machine_id,machine_name,model,model_level,service_tier""",
            (since, until),
        ).fetchall()
        daily_rows = database.execute(
            """SELECT date(occurred_at,'+8 hours'),machine_id,machine_name,model,model_level,
                      service_tier,COUNT(*),SUM(input_tokens),SUM(cached_input_tokens),
                      SUM(output_tokens),SUM(reasoning_output_tokens),SUM(total_tokens)
                 FROM usage_events WHERE occurred_at >= ? AND occurred_at < ?
                 GROUP BY date(occurred_at,'+8 hours'),machine_id,machine_name,model,model_level,service_tier
                 ORDER BY 1""",
            (since, until),
        ).fetchall()
        configured_rates = {
            _model_key(row[0]): (float(row[1]), float(row[2]), float(row[3]))
            for row in database.execute(
                "SELECT model,input_rate,cached_input_rate,output_rate FROM model_rates"
            )
        }
        budget_row = database.execute(
            "SELECT setting_value FROM dashboard_settings WHERE setting_key = ?", (f"budget_{days}",)
        ).fetchone()
    machine_map: dict[tuple[str, str], dict[str, object]] = {}
    model_map: dict[tuple[str, str, str], dict[str, object]] = {}
    for row in rows:
        machine_id, machine_name, model, model_level, service_tier = row[:5]
        requests, input_tokens, cached_tokens, output_tokens, reasoning_tokens, total_tokens = row[5:11]
        rates = configured_rates.get(_model_key(model), DEFAULT_MODEL_RATES.get(_model_key(model)))
        speed_multiplier = _speed_multiplier(model, service_tier)
        estimated_credits = _estimate_credits(
            input_tokens, cached_tokens, output_tokens, rates, speed_multiplier
        )
        machine = machine_map.setdefault(
            (machine_id, machine_name),
            {
                "machine_id": machine_id, "machine_name": machine_name,
                "requests": 0, "input_tokens": 0, "cached_input_tokens": 0,
                "output_tokens": 0, "reasoning_output_tokens": 0,
                "total_tokens": 0, "estimated_credits": 0.0, "unrated_tokens": 0,
                "last_seen": row[11],
                "models": [],
            },
        )
        machine["requests"] += requests
        machine["input_tokens"] += input_tokens
        machine["cached_input_tokens"] += cached_tokens
        machine["output_tokens"] += output_tokens
        machine["reasoning_output_tokens"] += reasoning_tokens
        machine["total_tokens"] += total_tokens
        if estimated_credits is None:
            machine["unrated_tokens"] += total_tokens
        else:
            machine["estimated_credits"] = round(machine["estimated_credits"] + estimated_credits, 6)
        machine["last_seen"] = max(machine["last_seen"], row[11])
        machine["models"].append(
            {
                "model": model, "model_level": model_level, "service_tier": service_tier,
                "rates": rates, "speed_multiplier": speed_multiplier,
                "requests": requests, "input_tokens": input_tokens,
                "cached_input_tokens": cached_tokens, "output_tokens": output_tokens,
                "reasoning_output_tokens": reasoning_tokens, "total_tokens": total_tokens,
                "estimated_credits": estimated_credits,
            }
        )
        model_item = model_map.setdefault(
            (model, model_level, service_tier),
            {
                "model": model, "model_level": model_level, "service_tier": service_tier,
                "rates": rates, "speed_multiplier": speed_multiplier,
                "requests": 0, "input_tokens": 0, "cached_input_tokens": 0,
                "output_tokens": 0, "reasoning_output_tokens": 0,
                "total_tokens": 0, "estimated_credits": 0.0,
                "unrated": rates is None,
            },
        )
        model_item["requests"] += requests
        model_item["input_tokens"] += input_tokens
        model_item["cached_input_tokens"] += cached_tokens
        model_item["output_tokens"] += output_tokens
        model_item["reasoning_output_tokens"] += reasoning_tokens
        model_item["total_tokens"] += total_tokens
        if estimated_credits is not None:
            model_item["estimated_credits"] = round(model_item["estimated_credits"] + estimated_credits, 6)
    machines = sorted(machine_map.values(), key=lambda item: item["estimated_credits"], reverse=True)
    for machine in machines:
        machine["models"].sort(
            key=lambda item: item["estimated_credits"] if item["estimated_credits"] is not None else -1,
            reverse=True,
        )
        machine["cache_hit_rate"] = round(
            machine["cached_input_tokens"] / machine["input_tokens"] * 100, 1
        ) if machine["input_tokens"] else 0.0
    models = sorted(model_map.values(), key=lambda item: item["estimated_credits"], reverse=True)
    average = round(sum(item["estimated_credits"] for item in machines) / len(machines), 6) if machines else 0.0
    highest = max((item["estimated_credits"] for item in machines), default=0.0)
    control = _control_settings(path)
    quota = current_quota
    manual_matches = bool(
        control["mode"] == "manual"
        and control["start_date"] == resolved_start and control["end_date"] == resolved_end
    )
    quota_matches = bool(
        quota and quota["period_start"] == resolved_start and quota["period_end"] == resolved_end
    )
    official_matches = bool(control["mode"] == "official" and quota_matches)
    control_matches = manual_matches or official_matches
    legacy_budget = float(budget_row[0]) if budget_row is not None and not control_matches else None
    budget = control["budget_credits"] if manual_matches else legacy_budget
    slots = int(
        control["machine_slots"] if control_matches
        else max(len(machines), 1) if budget_row is not None
        else 6
    )
    tracked_credits = round(sum(item["estimated_credits"] for item in machines), 6)
    inferred_budget = None
    if quota_matches and quota and 0 < float(quota["used_percent"]) <= 100:
        inferred_budget = round(tracked_credits / (float(quota["used_percent"]) / 100), 6)
    allocation_budget = (
        control["budget_credits"] if manual_matches
        else inferred_budget if official_matches
        else legacy_budget
    )
    target = round(allocation_budget / slots, 6) if allocation_budget is not None else None
    target_account_percent = round(100.0 / slots, 6) if official_matches else None
    for machine in machines:
        machine["deviation_percent"] = (
            round((machine["estimated_credits"] / average - 1) * 100, 1) if average else 0.0
        )
        machine["catch_up_to_highest"] = round(highest - machine["estimated_credits"], 6)
        machine["budget_remaining"] = (
            round(max(target - machine["estimated_credits"], 0), 6) if target is not None else None
        )
        machine["account_quota_percent"] = (
            round(float(quota["used_percent"]) * machine["estimated_credits"] / tracked_credits, 4)
            if official_matches and quota and tracked_credits > 0 else None
        )
        machine["allocation_status"] = (
            "limit" if target_account_percent is not None and machine["account_quota_percent"] is not None
                and machine["account_quota_percent"] >= target_account_percent
            else "reduce" if target_account_percent is not None and machine["account_quota_percent"] is not None
                and machine["account_quota_percent"] >= target_account_percent * 0.9
            else "limit" if manual_matches and target is not None and machine["estimated_credits"] >= target
            else "reduce" if manual_matches and target is not None and machine["estimated_credits"] >= target * 0.9
            else "available" if target is not None else "unconfigured"
        )
    daily_map: dict[str, dict[str, object]] = {}
    for row in daily_rows:
        date, machine_id, machine_name, model, model_level, service_tier = row[:6]
        requests, input_tokens, cached_tokens, output_tokens, reasoning_tokens, total_tokens = row[6:12]
        rates = configured_rates.get(_model_key(model), DEFAULT_MODEL_RATES.get(_model_key(model)))
        credits = _estimate_credits(
            input_tokens, cached_tokens, output_tokens, rates,
            _speed_multiplier(model, service_tier),
        )
        day = daily_map.setdefault(date, {
            "date": date, "requests": 0, "input_tokens": 0, "cached_input_tokens": 0,
            "output_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": 0,
            "estimated_credits": 0.0, "unrated_tokens": 0, "machines": {},
        })
        day["requests"] += requests
        day["input_tokens"] += input_tokens
        day["cached_input_tokens"] += cached_tokens
        day["output_tokens"] += output_tokens
        day["reasoning_output_tokens"] += reasoning_tokens
        day["total_tokens"] += total_tokens
        if credits is None:
            day["unrated_tokens"] += total_tokens
        else:
            day["estimated_credits"] = round(day["estimated_credits"] + credits, 6)
        daily_machine = day["machines"].setdefault(machine_id, {
            "machine_id": machine_id, "machine_name": machine_name, "requests": 0,
            "total_tokens": 0, "estimated_credits": 0.0, "unrated_tokens": 0,
        })
        daily_machine["requests"] += requests
        daily_machine["total_tokens"] += total_tokens
        if credits is None:
            daily_machine["unrated_tokens"] += total_tokens
        else:
            daily_machine["estimated_credits"] = round(
                daily_machine["estimated_credits"] + credits, 6
            )
    daily = []
    for day in daily_map.values():
        day["machines"] = sorted(
            day["machines"].values(), key=lambda item: item["estimated_credits"], reverse=True
        )
        daily.append(day)
    daily.sort(key=lambda item: item["date"])
    return {
        "days": days,
        "start_date": resolved_start,
        "end_date": resolved_end,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "machines": machines,
        "models": models,
        "budget_credits": budget,
        "allocation_budget_credits": allocation_budget,
        "inferred_budget_credits": inferred_budget,
        "allocation_mode": control["mode"] if control_matches else "legacy" if legacy_budget else "none",
        "machine_slots": slots,
        "hard_cap_enabled": bool(
            control["hard_cap"] and control_matches
            and (manual_matches or (quota and not quota["stale"] and tracked_credits > 0))
        ),
        "per_machine_target": target,
        "per_machine_target_percent": target_account_percent,
        "average_estimated_credits": average,
        "quota": quota,
        "control": control,
        "daily": daily,
        "totals": {
            "machines": len(machines),
            "requests": sum(item["requests"] for item in machines),
            "input_tokens": sum(item["input_tokens"] for item in machines),
            "cached_input_tokens": sum(item["cached_input_tokens"] for item in machines),
            "output_tokens": sum(item["output_tokens"] for item in machines),
            "reasoning_output_tokens": sum(item["reasoning_output_tokens"] for item in machines),
            "total_tokens": sum(item["total_tokens"] for item in machines),
            "estimated_credits": tracked_credits,
            "unrated_tokens": sum(item["unrated_tokens"] for item in machines),
        },
    }


def set_rate(path: Path, model: str, input_rate: float, cached_rate: float, output_rate: float) -> None:
    if not model or len(model) > 128:
        raise ValueError("invalid model")
    if any(not 0 <= rate <= 100_000 for rate in (input_rate, cached_rate, output_rate)):
        raise ValueError("invalid rate")
    with closing(connect(path)) as database, database:
        database.execute(
            "INSERT INTO model_rates(model,input_rate,cached_input_rate,output_rate) VALUES (?,?,?,?) "
            "ON CONFLICT(model) DO UPDATE SET input_rate=excluded.input_rate,"
            "cached_input_rate=excluded.cached_input_rate,output_rate=excluded.output_rate",
            (_model_key(model), input_rate, cached_rate, output_rate),
        )


def set_budget(path: Path, days: int, budget: float | None) -> None:
    if budget is not None and not 0.000001 <= budget <= 10_000_000_000:
        raise ValueError("invalid budget")
    with closing(connect(path)) as database, database:
        if budget is None:
            database.execute("DELETE FROM dashboard_settings WHERE setting_key = ?", (f"budget_{days}",))
        else:
            database.execute(
                "INSERT INTO dashboard_settings(setting_key,setting_value) VALUES (?,?) "
                "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value",
                (f"budget_{days}", str(budget)),
            )


def _model_key(model: str) -> str:
    return model.strip().lower().replace("_", "-")


def _estimate_credits(
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    rates: tuple[float, float, float] | None,
    multiplier: float = 1.0,
) -> float | None:
    if rates is None:
        return None
    uncached = max(input_tokens - cached_tokens, 0)
    return round(
        ((uncached * rates[0] + cached_tokens * rates[1] + output_tokens * rates[2])
         / 1_000_000) * multiplier,
        6,
    )


def _speed_multiplier(model: str, service_tier: str) -> float:
    if service_tier.strip().lower() not in {"fast", "priority"}:
        return 1.0
    key = _model_key(model)
    if key.startswith(("gpt-5.6", "gpt-5.5")):
        return 2.5
    if key.startswith("gpt-5.4"):
        return 2.0
    return 1.0


def create_session(secret: bytes, role: str = "viewer", now: int | None = None) -> str:
    if role not in {"admin", "viewer"}:
        raise ValueError("invalid role")
    issued = int(time.time()) if now is None else now
    nonce = base64.urlsafe_b64encode(hashlib.sha256(f"{issued}-{time.time_ns()}".encode()).digest()[:12]).decode().rstrip("=")
    payload = f"{issued}.{role}.{nonce}"
    signature = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def validate_session(secret: bytes, token: str, now: int | None = None) -> str | None:
    try:
        issued_text, role, nonce, supplied = token.split(".", 3)
        issued = int(issued_text)
    except (ValueError, TypeError):
        return None
    current = int(time.time()) if now is None else now
    if role not in {"admin", "viewer"} or not 0 <= current - issued <= 43200 or not 8 <= len(nonce) <= 64:
        return None
    expected = hmac.new(secret, f"{issued}.{role}.{nonce}".encode(), hashlib.sha256).hexdigest()
    return role if hmac.compare_digest(supplied.encode("utf-8"), expected.encode("ascii")) else None


def csrf_token(secret: bytes, session: str) -> str:
    return hmac.new(secret, f"csrf:{session}".encode(), hashlib.sha256).hexdigest()


def _query_days(query: str) -> int:
    try:
        return min(max(int(parse_qs(query).get("days", ["30"])[0]), 1), 366)
    except ValueError:
        return 30


def _date_value(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ValueError("invalid date") from None


def _time_bounds(
    days: int, start_date: str | None, end_date: str | None,
) -> tuple[str, str, str, str]:
    if start_date and end_date:
        start = _date_value(start_date)
        end = _date_value(end_date)
        if start > end or (end - start).days > 366:
            raise ValueError("invalid date range")
        start_local = datetime.combine(start, datetime.min.time(), BEIJING)
        end_local = datetime.combine(end + timedelta(days=1), datetime.min.time(), BEIJING)
    else:
        end_local = datetime.now(BEIJING)
        start_local = end_local - timedelta(days=min(max(days, 1), 366))
        start = start_local.date()
        end = end_local.date()
    return (
        start_local.astimezone(timezone.utc).isoformat(),
        end_local.astimezone(timezone.utc).isoformat(),
        start.isoformat(), end.isoformat(),
    )


def _query_scope(query: str, database: Path) -> tuple[int, str | None, str | None]:
    values = parse_qs(query)
    start = values.get("start", [""])[0]
    end = values.get("end", [""])[0]
    if start or end:
        try:
            start_date = _date_value(start).isoformat()
            end_date = _date_value(end).isoformat()
            if _date_value(start_date) > _date_value(end_date):
                raise ValueError
            return min((_date_value(end_date) - _date_value(start_date)).days + 1, 366), start_date, end_date
        except ValueError:
            pass
    if "days" in values:
        return _query_days(query), None, None
    control = _control_settings(database)
    if control["mode"] == "manual" and control["start_date"] and control["end_date"]:
        return 30, str(control["start_date"]), str(control["end_date"])
    quota = latest_quota(database)
    if quota is not None:
        return 30, str(quota["period_start"]), str(quota["period_end"])
    return 30, None, None


def machine_policy(path: Path, machine_id: str) -> dict[str, object]:
    if not machine_id or len(machine_id) > 64:
        raise ValueError("invalid machine id")
    control = _control_settings(path)
    if not control["hard_cap"]:
        return {"blocked": False, "reason": "hard_cap_disabled", "mode": control["mode"]}
    if control["mode"] == "official":
        quota = latest_quota(path)
        if quota is None:
            return {"blocked": False, "reason": "official_quota_unavailable", "mode": "official"}
        if quota["stale"]:
            return {"blocked": False, "reason": "official_quota_stale", "mode": "official"}
        data = summary(
            path, start_date=str(quota["period_start"]), end_date=str(quota["period_end"])
        )
        machine = next((item for item in data["machines"] if item["machine_id"] == machine_id), None)
        used_percent = float(machine["account_quota_percent"] or 0) if machine else 0.0
        limit_percent = 100.0 / int(control["machine_slots"])
        if data["totals"]["estimated_credits"] <= 0:
            return {"blocked": False, "reason": "insufficient_tracked_usage", "mode": "official"}
        blocked = used_percent >= limit_percent
        return {
            "blocked": blocked,
            "reason": "machine_credit_limit_reached" if blocked else "within_machine_credit_limit",
            "mode": "official", "used_account_percent": round(used_percent, 6),
            "limit_account_percent": round(limit_percent, 6),
            "remaining_account_percent": round(max(limit_percent - used_percent, 0), 6),
            "official_used_percent": quota["used_percent"],
            "period_start": quota["period_start"], "period_end": quota["period_end"],
            "hard_cap_enabled": True,
        }
    if not control["start_date"] or not control["end_date"] or control["budget_credits"] is None:
        return {"blocked": False, "reason": "manual_budget_incomplete", "mode": "manual"}
    data = summary(path, start_date=str(control["start_date"]), end_date=str(control["end_date"]))
    machine = next((item for item in data["machines"] if item["machine_id"] == machine_id), None)
    used = float(machine["estimated_credits"]) if machine else 0.0
    target = float(data["per_machine_target"] or 0)
    blocked = bool(control["hard_cap"] and target > 0 and used >= target)
    return {
        "blocked": blocked,
        "reason": "machine_credit_limit_reached" if blocked else "within_machine_credit_limit",
        "mode": "manual",
        "used_credits": round(used, 6), "limit_credits": round(target, 6),
        "remaining_credits": round(max(target - used, 0), 6),
        "period_start": control["start_date"], "period_end": control["end_date"],
        "hard_cap_enabled": bool(control["hard_cap"]),
    }


def login_page(*, error: bool = False) -> str:
    warning = '<p class="error">账号或密码不正确</p>' if error else ""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Token 用量中心</title>
<style>{_CSS}</style></head><body class="login-body"><main class="login-card">
<h1>Token 用量中心</h1><p class="muted">Browser Gateway 私有统计面板</p>{warning}
<form method="post" action="/login"><label>用户名<input name="username" autocomplete="username" required></label>
<label>密码<input type="password" name="password" autocomplete="current-password" required></label>
<button type="submit">登录</button></form></main></body></html>"""


def dashboard_page(
    data: dict[str, object], session: str, role: str, days: int, secret: bytes,
    *, page: str = "overview", machine_id: str = "",
) -> str:
    content = {
        "overview": lambda: _overview_page(data, days),
        "machines": lambda: _machines_page(data, days),
        "models": lambda: _models_page(data),
        "machine": lambda: _machine_page(data, machine_id),
        "settings": lambda: _settings_page(data, session, role, days, secret),
    }[page]()
    page_names = {
        "overview": "总览", "machines": "设备额度", "models": "计费明细",
        "machine": "机器详情", "settings": "设置",
    }
    primary = [
        ("overview", "/dashboard", "总览"),
        ("machines", "/dashboard/machines", "设备额度"),
        ("models", "/dashboard/models", "计费明细"),
        ("settings", "/dashboard/settings", "设置"),
    ]
    scope = urlencode({"start": data["start_date"], "end": data["end_date"]})
    nav = "".join(
        f'<a class="{"active" if page == key or (page == "machine" and key == "machines") else ""}" '
        f'href="{url}?{scope}">{label}</a>' for key, url, label in primary
    )
    filter_path = _page_url(page, days, machine_id).split("?", 1)[0]
    hidden_machine = f'<input type="hidden" name="id" value="{html.escape(machine_id, quote=True)}">' if machine_id else ""
    date_filter = f'''<form class="date-filter" method="get" action="{filter_path}">{hidden_machine}
<label>开始<input type="date" name="start" value="{html.escape(data['start_date'])}" required></label>
<label>结束<input type="date" name="end" value="{html.escape(data['end_date'])}" required></label><button>统计</button></form>'''
    role_label = "管理员" if role == "admin" else "只读"
    total = data["totals"]
    warning = (
        f'<div class="notice">有 {_number(total["unrated_tokens"])} Token 尚未匹配费率，Credits 合计不包含这部分。</div>'
        if total["unrated_tokens"] else ""
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{page_names[page]} · Codex 用量中心</title><style>{_CSS}</style></head>
<body><header><div><h1>Codex 用量中心</h1><p>{html.escape(data['start_date'])} 至 {html.escape(data['end_date'])} · 更新于 {html.escape(_short_time(data['generated_at']))}</p></div>
<div class="identity"><span>{role_label}</span><a href="/logout">退出</a></div></header>
<main><div class="toolbar"><nav class="primary">{nav}</nav>{date_filter}</div>{warning}{content}
<p class="foot">Credits 由模型响应中的 usage、官方标准费率和 Fast 倍率估算；官方剩余额度来自 Codex 自身 Usage 接口的脱敏快照。</p></main></body></html>"""


def _overview_page(data: dict[str, object], days: int) -> str:
    quota = data.get("quota")
    if quota:
        stale = " · 数据超过 20 分钟未更新" if quota["stale"] else ""
        inferred = (
            f' · 推算总额度 {_credits(data["inferred_budget_credits"])} Credits'
            if data["inferred_budget_credits"] is not None else ""
        )
        quota_html = f'''<section class="quota-strip"><div><span>官方当前窗口</span><strong>已用 {quota['used_percent']:g}% · 剩余 {quota['remaining_percent']:g}%</strong></div>
<div><span>重置时间</span><strong>{html.escape(_short_time(quota['reset_at']))}</strong></div><p>{html.escape(quota['plan_type'])}{inferred}{stale}</p></section>'''
    else:
        quota_html = '<section class="quota-strip muted"><div><strong>等待任一 Bridge 同步官方 Usage 快照</strong></div></section>'
    line_chart = _machine_line_chart(data)
    total_chart = _machine_total_chart(data)
    return f"""{quota_html}<section class="dashboard-grid"><article class="panel chart-panel"><div class="panel-head"><div><h2>每日 Credits</h2><p>每条曲线代表一台设备</p></div></div>{line_chart}</article>
<article class="panel total-panel"><div class="panel-head"><div><h2>设备额度进度</h2><p>已用与本机均分剩余 · 总计 {_credits(data['totals']['estimated_credits'])} Credits</p></div></div>{total_chart}</article></section>"""


def _machine_line_chart(data: dict[str, object]) -> str:
    machines = data["machines"]
    if not machines:
        return '<div class="empty">尚无设备用量数据</div>'
    start = _date_value(data["start_date"])
    end = min(_date_value(data["end_date"]), datetime.now(BEIJING).date())
    dates = []
    cursor = start
    while cursor <= end:
        dates.append(cursor.isoformat())
        cursor += timedelta(days=1)
    if not dates:
        dates = [start.isoformat()]
    daily_lookup: dict[tuple[str, str], float] = {}
    for day in data["daily"]:
        for machine in day["machines"]:
            daily_lookup[(day["date"], machine["machine_id"])] = float(machine["estimated_credits"])
    series = {
        machine["machine_id"]: [daily_lookup.get((date, machine["machine_id"]), 0.0) for date in dates]
        for machine in machines
    }
    maximum = max((value for values in series.values() for value in values), default=0.0) or 1.0
    left, top, width, height = 58.0, 20.0, 822.0, 242.0
    x = lambda index: left + (width * index / max(len(dates) - 1, 1))
    y = lambda value: top + height - (height * value / maximum)
    colors = ("#2463eb", "#e45756", "#17a673", "#f59e0b", "#805ad5", "#0891b2", "#db2777", "#64748b")
    grid = "".join(
        f'<line x1="{left}" y1="{top + height * step / 4:.1f}" x2="{left + width}" y2="{top + height * step / 4:.1f}" class="gridline"/>'
        for step in range(5)
    )
    y_labels = "".join(
        f'<text x="48" y="{top + height * step / 4 + 4:.1f}" text-anchor="end">{maximum * (4-step) / 4:.2f}</text>'
        for step in range(5)
    )
    label_indexes = sorted(set(round((len(dates) - 1) * step / min(4, max(len(dates) - 1, 1))) for step in range(min(5, len(dates)))))
    x_labels = "".join(
        f'<text x="{x(index):.1f}" y="286" text-anchor="middle">{html.escape(dates[index][5:])}</text>'
        for index in label_indexes
    )
    lines = []
    legends = []
    for index, machine in enumerate(machines):
        color = colors[index % len(colors)]
        points = " ".join(f"{x(i):.1f},{y(value):.1f}" for i, value in enumerate(series[machine["machine_id"]]))
        dots = ""
        if len(dates) <= 31:
            dots = "".join(
                f'<circle cx="{x(i):.1f}" cy="{y(value):.1f}" r="3" fill="{color}"><title>{html.escape(machine["machine_name"])} {dates[i]}: {_credits(value)}</title></circle>'
                for i, value in enumerate(series[machine["machine_id"]])
            )
        lines.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>{dots}')
        legends.append(f'<span><i style="background:{color}"></i>{html.escape(machine["machine_name"])}</span>')
    return f'''<div class="line-chart"><svg viewBox="0 0 900 300" role="img" aria-label="各设备每日 Credits 折线图">{grid}{y_labels}{x_labels}{''.join(lines)}</svg>
<div class="legend">{''.join(legends)}</div></div>'''


def _machine_total_chart(data: dict[str, object]) -> str:
    machines = data["machines"]
    if not machines:
        return '<div class="empty">尚无设备用量数据</div>'
    target = data.get("per_machine_target")
    rows = []
    for item in machines:
        css = item["allocation_status"]
        if data["allocation_mode"] == "official" and item["account_quota_percent"] is not None:
            used = float(item["account_quota_percent"])
            limit = float(data["per_machine_target_percent"])
            remaining = max(float(data["per_machine_target_percent"]) - float(item["account_quota_percent"]), 0)
            over = max(used - limit, 0)
            advice = (
                f'已用 {used:.2f}% · 超出 {over:.2f}%'
                if over else f'已用 {used:.2f}% · 剩余 {remaining:.2f}%'
            )
            progress = min(used / limit * 100, 100) if limit else 0
            bar_title = f"已用账户总额度 {used:.2f}%，本机均分上限 {limit:.2f}%"
        elif data["allocation_mode"] in {"manual", "legacy"} and target is not None:
            used = float(item["estimated_credits"])
            limit = float(target)
            remaining = max(limit - used, 0)
            over = max(used - limit, 0)
            advice = (
                f'已用 {_credits(used)} · 超出 {_credits(over)} Credits'
                if over else f'已用 {_credits(used)} · 剩余 {_credits(remaining)} Credits'
            )
            progress = min(used / limit * 100, 100) if limit else 0
            bar_title = f"已用 {_credits(used)} Credits，本机均分上限 {_credits(limit)} Credits"
        else:
            advice, css, progress = "尚未取得额度标准", "neutral", 0
            bar_title = "等待额度标准"
        remaining_width = max(100 - progress, 0)
        rows.append(f'''<div class="total-row"><div><strong>{html.escape(item['machine_name'])}</strong><span class="advice {css}">{advice}</span></div>
<div class="total-bar {css}" title="{html.escape(bar_title, quote=True)}"><i class="used" style="width:{progress:.1f}%"></i><i class="remaining" style="width:{remaining_width:.1f}%"></i></div><b>{_credits(item['estimated_credits'])}</b></div>''')
    if data["allocation_mode"] == "official" and data["per_machine_target_percent"] is not None:
        target_note = f'<p class="target-note">官方窗口自动 {data["machine_slots"]} 等分：每台最多占账户总额度 {data["per_machine_target_percent"]:.2f}%（按已记录用量比例估算）</p>'
    elif target is not None:
        target_note = f'<p class="target-note">手动预算 {data["machine_slots"]} 等分：每台 {_credits(target)} Credits</p>'
    else:
        target_note = '<p class="target-note">等待官方 Usage 快照或完整的手动预算设置</p>'
    legend = '<div class="quota-legend"><span><i class="used"></i>已用</span><span><i class="remaining"></i>剩余至本机均分上限</span></div>' if target is not None else ""
    return f'<div class="total-chart">{legend}{"".join(rows)}{target_note}</div>'


def _daily_page(data: dict[str, object]) -> str:
    rows = []
    for item in reversed(data["daily"]):
        machine_mix = "、".join(
            f'{html.escape(machine["machine_name"])} {_credits(machine["estimated_credits"])}'
            for machine in item["machines"]
        )
        rows.append(f"""<tr><td><strong>{html.escape(item['date'])}</strong></td><td>{_number(item['requests'])}</td>
<td>{_number(item['input_tokens'])}</td><td>{_number(item['cached_input_tokens'])}</td><td>{_number(item['output_tokens'])}</td>
<td>{_number(item['reasoning_output_tokens'])}</td><td><strong>{_credits(item['estimated_credits'])}</strong></td><td class="mix">{machine_mix}</td></tr>""")
    return f"""{_cards(data['totals'])}<section class="panel"><div class="panel-head"><div><h2>每日明细</h2><p>按北京时间自然日归档；机器列显示当日各机器 Credits。</p></div></div>
<div class="table-wrap"><table><thead><tr><th>日期（北京时间）</th><th>请求</th><th>输入</th><th>缓存输入</th><th>输出</th><th>推理输出</th><th>Credits</th><th>机器分布</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan="8" class="empty">尚无每日数据</td></tr>'}</tbody></table></div></section>"""


def _machines_page(data: dict[str, object], days: int) -> str:
    rows = []
    for item in data["machines"]:
        link = "/dashboard/machine?" + urlencode({"id": item["machine_id"], "start": data["start_date"], "end": data["end_date"]})
        top_names = list(dict.fromkeys(model["model"] for model in item["models"]))[:3]
        top = "、".join(html.escape(model) for model in top_names)
        status = {"limit": "已到上限", "reduce": "接近上限", "available": "可继续使用"}.get(item["allocation_status"], "待计算")
        allocation = (
            f'{item["account_quota_percent"]:.2f}% / {data["per_machine_target_percent"]:.2f}%'
            if data["allocation_mode"] == "official" and item["account_quota_percent"] is not None
            else f'{_credits(item["estimated_credits"])} / {_credits(data["per_machine_target"])} Credits'
            if data["per_machine_target"] is not None else "—"
        )
        rows.append(f"""<tr><td><a class="name" href="{link}">{html.escape(item['machine_name'])}</a><small>{html.escape(item['machine_id'])}</small></td>
<td><span class="advice {item['allocation_status']}">{status}</span></td><td>{allocation}</td><td>{_credits(item['estimated_credits'])}</td>
<td>{_number(item['requests'])}</td><td>{item['cache_hit_rate']:.1f}%</td><td class="mix">{top}</td><td>{html.escape(_short_time(item['last_seen']))}</td></tr>""")
    return f"""{_cards(data['totals'])}<section class="panel"><div class="panel-head"><div><h2>设备额度</h2><p>用于判断哪台设备可以多用、哪台应减量；点击名称查看用量组成。</p></div></div>
<div class="table-wrap"><table><thead><tr><th>设备</th><th>状态</th><th>已用 / 均分上限</th><th>Credits</th><th>请求</th><th>缓存率</th><th>主要模型</th><th>最后使用</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan="8" class="empty">尚无设备数据</td></tr>'}</tbody></table></div></section>"""


def _models_page(data: dict[str, object]) -> str:
    rows = []
    total_credits = data["totals"]["estimated_credits"] or 1
    for item in data["models"]:
        rates = item["rates"]
        rate_text = "未定价" if rates is None else f"{rates[0]:g} / {rates[1]:g} / {rates[2]:g}"
        share = item["estimated_credits"] / total_credits * 100 if not item["unrated"] else 0
        rows.append(f"""<tr><td><strong>{html.escape(item['model'])}</strong></td><td>{html.escape(item['model_level'])}</td>
<td>{html.escape(_tier_label(item['service_tier']))}</td><td>{item['speed_multiplier']:g}×</td><td>{_number(item['requests'])}</td>
<td>{_number(item['input_tokens'])}</td><td>{_number(item['cached_input_tokens'])}</td><td>{_number(item['output_tokens'])}</td>
<td>{_number(item['reasoning_output_tokens'])}</td><td>{_credits(item['estimated_credits']) if not item['unrated'] else '未定价'}</td><td>{share:.1f}%</td><td>{rate_text}</td></tr>""")
    return f"""{_cards(data['totals'])}<section class="panel"><div class="panel-head"><div><h2>计费明细</h2><p>用于核对 Credits 为什么增加，以及费率、推理档位和 Fast 倍数是否正确；未定价模型会在页面顶部报警。</p></div></div>
<div class="table-wrap"><table><thead><tr><th>模型</th><th>推理</th><th>速度</th><th>倍数</th><th>请求</th><th>输入</th><th>缓存</th><th>输出</th><th>推理输出</th><th>Credits</th><th>占比</th><th>标准费率 输入/缓存/输出</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan="12" class="empty">尚无模型数据</td></tr>'}</tbody></table></div></section>"""


def _machine_page(data: dict[str, object], machine_id: str) -> str:
    machine = next((item for item in data["machines"] if item["machine_id"] == machine_id), None)
    if machine is None:
        return '<section class="panel"><div class="empty">找不到这台机器，可能当前时间范围内没有记录。</div></section>'
    model_rows = "".join(
        f"""<tr><td>{html.escape(item['model'])}</td><td>{html.escape(item['model_level'])}</td><td>{html.escape(_tier_label(item['service_tier']))}</td>
<td>{_number(item['requests'])}</td><td>{_number(item['input_tokens'])}</td><td>{_number(item['cached_input_tokens'])}</td><td>{_number(item['output_tokens'])}</td>
<td>{_credits(item['estimated_credits']) if item['estimated_credits'] is not None else '未定价'}</td></tr>""" for item in machine["models"]
    )
    daily_rows = []
    for day in reversed(data["daily"]):
        item = next((entry for entry in day["machines"] if entry["machine_id"] == machine_id), None)
        if item:
            daily_rows.append(f'<tr><td>{html.escape(day["date"])}</td><td>{_number(item["requests"])}</td><td>{_number(item["total_tokens"])}</td><td>{_credits(item["estimated_credits"])}</td></tr>')
    local_totals = {
        "machines": 1, "requests": machine["requests"], "total_tokens": machine["total_tokens"],
        "estimated_credits": machine["estimated_credits"],
    }
    back_query = urlencode({"start": data["start_date"], "end": data["end_date"]})
    return f"""<div class="back"><a href="/dashboard/machines?{back_query}">← 返回机器列表</a></div><div class="title-row"><div><h2>{html.escape(machine['machine_name'])}</h2><p>{html.escape(machine['machine_id'])} · 最后使用 {html.escape(_short_time(machine['last_seen']))}</p></div></div>
{_cards(local_totals)}<section class="split"><article class="panel"><div class="panel-head"><div><h2>模型组合</h2><p>缓存率 {machine['cache_hit_rate']:.1f}%</p></div></div><div class="table-wrap"><table><thead><tr><th>模型</th><th>推理</th><th>速度</th><th>请求</th><th>输入</th><th>缓存</th><th>输出</th><th>Credits</th></tr></thead><tbody>{model_rows}</tbody></table></div></article>
<article class="panel"><div class="panel-head"><div><h2>每日历史</h2><p>仅显示有使用记录的日期</p></div></div><div class="table-wrap"><table><thead><tr><th>日期</th><th>请求</th><th>Token</th><th>Credits</th></tr></thead><tbody>{''.join(daily_rows)}</tbody></table></div></article></section>"""


def _settings_page(data: dict[str, object], session: str, role: str, days: int, secret: bytes) -> str:
    if role != "admin":
        return '<section class="panel"><div class="panel-head"><div><h2>只读账号</h2><p>你可以查看全部统计，但修改预算和费率需要管理员账号。</p></div></div></section>'
    csrf = csrf_token(secret, session)
    control = data["control"]
    budget = "" if control["budget_credits"] is None else str(control["budget_credits"])
    hard_checked = " checked" if control["hard_cap"] else ""
    official_checked = " checked" if control["mode"] == "official" else ""
    manual_checked = " checked" if control["mode"] == "manual" else ""
    control_start = control["start_date"] or data["start_date"]
    control_end = control["end_date"] or data["end_date"]
    quota = data.get("quota")
    official_status = (
        f'当前官方窗口：{html.escape(_short_time(quota["period_start_at"]))} 至 {html.escape(_short_time(quota["period_end_at"]))}（北京时间），已用 {quota["used_percent"]:g}%，剩余 {quota["remaining_percent"]:g}%。'
        + (" 快照已过期，硬上限暂时放行。" if quota["stale"] else "")
        if quota else "尚未收到官方 Usage 快照；选择后会等待 Bridge 自动同步。"
    )
    rows = []
    seen = set()
    for item in data["models"]:
        model_key = _model_key(item["model"])
        if model_key in seen:
            continue
        seen.add(model_key)
        rates = item["rates"] or (0, 0, 0)
        model = html.escape(item["model"], quote=True)
        rows.append(f"""<tr><td><strong>{model}</strong></td><td>
<form class="inline rates" method="post" action="/dashboard/rate"><input type="hidden" name="csrf" value="{csrf}">
<input type="hidden" name="days" value="{days}"><input type="hidden" name="model" value="{model}">
<label>输入<input type="number" name="input_rate" min="0" max="100000" step="0.001" value="{rates[0]}"></label>
<label>缓存<input type="number" name="cached_rate" min="0" max="100000" step="0.001" value="{rates[1]}"></label>
<label>输出<input type="number" name="output_rate" min="0" max="100000" step="0.001" value="{rates[2]}"></label><button>保存</button></form></td></tr>""")
    return f"""<section class="panel"><div class="panel-head"><div><h2>额度分配模式</h2><p>两种模式互斥：使用官方当前窗口自动分配，或者完全使用你填写的周期和预算。</p></div></div>
<form class="control-form" method="post" action="/dashboard/control"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="days" value="{days}">
<div class="mode-options"><label class="mode-card"><input type="radio" name="control_mode" value="official"{official_checked}><strong>官方额度自动六等分</strong><span>自动采用官方窗口起止时间；根据官方已用比例和各设备已记录 Credits 占比估算设备额度。</span></label>
<label class="mode-card"><input type="radio" name="control_mode" value="manual"{manual_checked}><strong>手动周期与预算</strong><span>使用你填写的日期和总 Credits，不依赖官方 Usage 快照。</span></label></div>
<div class="official-mode"><strong>官方模式状态</strong><p>{official_status}</p></div>
<div class="manual-fields"><label>开始日期<input type="date" name="start_date" value="{html.escape(str(control_start))}"></label>
<label>结束日期<input type="date" name="end_date" value="{html.escape(str(control_end))}"></label>
<label>周期总 Credits<input type="number" name="budget" min="0.000001" step="0.01" placeholder="例如 1200" value="{budget}"></label></div>
<label>均分设备数<input type="number" name="machine_slots" min="1" max="100" step="1" value="{control['machine_slots']}" required></label>
<label class="check"><input type="checkbox" name="hard_cap"{hard_checked}> 达到均分上限后自动禁用该机模型请求</label><button>保存控制设置</button></form>
<p class="safety-note">官方模式的单机占比是根据 Bridge 已记录用量估算的；快照过期或记录不足时自动放行。手动模式按明确 Credits 硬上限执行。中央服务不可达时客户端沿用最近策略。</p></section>
<section class="panel"><div class="panel-head"><div><h2>标准模型费率</h2><p>单位：每 100 万 Token 的 Credits。Fast 倍数由系统在标准费率之外自动应用。</p></div></div>
<div class="table-wrap"><table><thead><tr><th>模型</th><th>输入 / 缓存 / 输出</th></tr></thead><tbody>{''.join(rows) or '<tr><td colspan="2" class="empty">收到模型数据后可修改费率</td></tr>'}</tbody></table></div></section>"""


def _cards(total: dict[str, object]) -> str:
    return f"""<section class="cards"><article><span>设备</span><strong>{_number(total['machines'])}</strong></article>
<article><span>请求</span><strong>{_number(total['requests'])}</strong></article>
<article><span>原始 Token</span><strong>{_number(total['total_tokens'])}</strong></article>
<article><span>估算 Credits</span><strong>{_credits(total['estimated_credits'])}</strong></article></section>"""


def _page_url(page: str, days: int, machine_id: str) -> str:
    path = {
        "overview": "/dashboard", "daily": "/dashboard/daily", "machines": "/dashboard/machines",
        "models": "/dashboard/models", "settings": "/dashboard/settings", "machine": "/dashboard/machine",
    }[page]
    query = {"days": days}
    if page == "machine" and machine_id:
        query["id"] = machine_id
    return path + "?" + urlencode(query)


def _tier_label(value: str) -> str:
    return "Fast" if value.strip().lower() in {"fast", "priority"} else "标准"


def _number(value: object) -> str:
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.2f}"
    return f"{int(value):,}"


def _credits(value: object) -> str:
    return f"{float(value):,.4f}"


def _short_time(value: object) -> str:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(BEIJING).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(value)[:32]


_CSS = """
:root{color-scheme:light;font-family:Inter,'Microsoft YaHei',sans-serif;background:#f3f6fb;color:#14213d}*{box-sizing:border-box}
body{margin:0}h1,h2,p{margin:0}a{color:#245dc1}header{display:flex;justify-content:space-between;align-items:center;padding:22px max(4vw,24px);background:linear-gradient(120deg,#102a56,#173f79);color:white}header p{opacity:.72;margin-top:5px}.identity{display:flex;align-items:center;gap:14px}.identity span{padding:5px 10px;border:1px solid #ffffff42;border-radius:20px;font-size:12px}.identity a{color:white}
main{max-width:1440px;margin:auto;padding:22px}.toolbar{display:flex;justify-content:space-between;align-items:center;gap:14px;margin-bottom:18px}.primary{display:flex;gap:8px;overflow:auto}.primary a{padding:9px 15px;border-radius:10px;color:#52647b;text-decoration:none;background:white;border:1px solid #e2e8f2;white-space:nowrap}.primary a.active{background:#173f79;color:white;border-color:#173f79}.date-filter{display:flex;align-items:end;gap:8px}.date-filter label{font-size:11px;color:#718096}.date-filter input{display:block;padding:7px;margin-top:3px}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.cards article,.panel{background:white;border:1px solid #e0e7f1;border-radius:14px;box-shadow:0 5px 22px #19345d0b}.cards article{padding:18px}.cards span{display:block;color:#718096}.cards strong{display:block;font-size:27px;margin-top:7px}.panel{margin-top:18px;overflow:hidden}.split{display:grid;grid-template-columns:1.35fr 1fr;gap:18px}.split .panel{min-width:0}.panel-head{display:flex;justify-content:space-between;gap:18px;align-items:center;padding:20px}.panel-head p,.title-row p{color:#718096;margin-top:5px}.title-row{padding:8px 2px 2px}.title-row h2{font-size:26px}.back{margin:2px 0 14px}.back a{text-decoration:none}
.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;white-space:nowrap}th,td{text-align:left;padding:13px 15px;border-top:1px solid #edf1f7}th{font-size:12px;color:#718096;background:#fafcff}td small{display:block;color:#98a5b7;margin-top:3px}.name{font-weight:700;text-decoration:none}.mix{white-space:normal;min-width:180px;color:#52647b;font-size:13px}
.bar{display:inline-block;width:90px;height:7px;background:#e8eef8;border-radius:6px;margin-right:9px;vertical-align:middle}.bar i{display:block;height:100%;background:#2d6cdf;border-radius:6px}.over{color:#c53030}.under{color:#2b6cb0}.balanced{color:#218358}.notice{background:#fff8e6;border:1px solid #f4d48c;color:#7c5700;padding:11px 14px;border-radius:10px;margin-bottom:15px}
.dashboard-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(300px,1fr);gap:18px}.chart-panel,.total-panel{margin-top:0}.line-chart{padding:0 18px 18px}.line-chart svg{display:block;width:100%;height:auto;min-height:260px}.line-chart text{font-size:11px;fill:#718096}.gridline{stroke:#e6edf7;stroke-width:1}.legend{display:flex;flex-wrap:wrap;gap:10px 18px;justify-content:center}.legend span{font-size:12px;color:#52647b}.legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}.quota-strip{display:grid;grid-template-columns:auto auto 1fr;align-items:center;gap:28px;background:#eaf2ff;border:1px solid #c9dcff;border-radius:14px;padding:14px 18px;margin-bottom:18px}.quota-strip span{display:block;font-size:11px;color:#64748b}.quota-strip strong{display:block;margin-top:3px}.quota-strip p{text-align:right;color:#52647b;font-size:12px}.quota-strip.muted{display:block;color:#718096}.total-chart{padding:0 20px 18px}.total-row{display:grid;grid-template-columns:minmax(120px,1.25fr) minmax(70px,1fr) auto;align-items:center;gap:10px;padding:11px 0;border-top:1px solid #edf1f7}.total-row strong{display:block;font-size:13px}.total-row b{font-size:13px}.total-bar{height:8px;background:#e8eef8;border-radius:8px}.total-bar i{display:block;height:100%;background:linear-gradient(90deg,#2463eb,#5b8def);border-radius:8px}.advice{display:block;font-size:10px;margin-top:3px}.advice.limit{color:#c53030}.advice.reduce{color:#b7791f}.advice.available{color:#218358}.advice.neutral{color:#718096}.target-note{font-size:12px;color:#52647b;background:#f7f9fc;padding:10px;border-radius:8px;margin-top:8px}.control-form{display:grid;grid-template-columns:repeat(4,minmax(130px,1fr));gap:12px;padding:0 20px 18px;align-items:end}.control-form label{font-size:11px;color:#718096}.control-form input{display:block;width:100%;margin-top:4px}.mode-options{grid-column:1/5;display:grid;grid-template-columns:1fr 1fr;gap:12px}.mode-card{position:relative;padding:15px 15px 15px 44px;border:1px solid #dbe4f0;border-radius:12px;background:#f9fbfe;cursor:pointer}.mode-card input{position:absolute;left:15px;top:16px;width:auto;margin:0}.mode-card strong,.mode-card span{display:block}.mode-card strong{font-size:14px;color:#253858}.mode-card span{font-size:12px;line-height:1.55;margin-top:5px}.mode-card:has(input:checked){border-color:#2463eb;background:#eef4ff;box-shadow:0 0 0 1px #2463eb}.official-mode{grid-column:1/5;padding:11px 13px;border-radius:9px;background:#f1f6ff;color:#52647b}.official-mode strong{font-size:12px}.official-mode p{font-size:12px;margin-top:4px}.manual-fields{grid-column:1/5;display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.manual-fields label{display:block}.control-form .check{grid-column:1/4;display:flex;align-items:center;gap:8px;font-size:13px;color:#52647b}.control-form .check input{width:auto;margin:0}.safety-note{margin:0 20px 20px;color:#8a5b00;background:#fff8e6;padding:10px;border-radius:8px;font-size:12px}
.control-form:has(input[value="official"]:checked) .manual-fields{opacity:.42}.control-form:has(input[value="manual"]:checked) .official-mode{opacity:.42}
.total-bar{display:flex;overflow:hidden}.total-bar i{border-radius:0}.total-bar .used{background:linear-gradient(90deg,#2463eb,#5b8def)}.total-bar .remaining{background:#dce7f7}.total-bar.limit .used{background:linear-gradient(90deg,#e05252,#c53030)}.total-bar.reduce .used{background:linear-gradient(90deg,#eab84b,#d69e2e)}.quota-legend{display:flex;gap:16px;justify-content:flex-end;padding:0 0 8px;font-size:11px;color:#718096}.quota-legend i{display:inline-block;width:11px;height:7px;border-radius:4px;margin-right:5px}.quota-legend .used{background:#3974e8}.quota-legend .remaining{background:#dce7f7}
 button{border:0;border-radius:8px;background:#2463eb;color:white;padding:9px 14px;font-weight:600;cursor:pointer}input{border:1px solid #ccd6e5;border-radius:8px;padding:9px;background:white}.inline,.budget{display:flex;gap:7px}.rates label{font-size:11px;color:#718096}.rates input{display:block;width:78px;padding:6px}.budget input{width:250px}.empty{text-align:center;color:#718096;padding:32px}.foot{text-align:center;color:#718096;padding:26px;font-size:13px}
.login-body{min-height:100vh;display:grid;place-items:center;background:linear-gradient(135deg,#edf4ff,#f8fbff)}.login-card{width:min(390px,92vw);padding:30px;background:white;border-radius:18px;box-shadow:0 18px 60px #18375d22}.login-card .muted{color:#718096;margin:8px 0 22px}.login-card label{display:block;margin:14px 0;color:#52647b}.login-card input{display:block;width:100%;margin-top:6px}.login-card button{width:100%;margin-top:8px}.error{color:#c53030;background:#fff5f5;padding:10px;border-radius:8px}
@media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}.split,.dashboard-grid{grid-template-columns:1fr}.panel-head{align-items:flex-start;flex-direction:column}.budget{width:100%}.budget input{flex:1}main{padding:14px}.toolbar{align-items:flex-start;flex-direction:column}.date-filter{width:100%;overflow:auto}.quota-strip{grid-template-columns:1fr 1fr}.quota-strip p{grid-column:1/3;text-align:left}.control-form{grid-template-columns:repeat(2,1fr)}.mode-options,.official-mode,.manual-fields{grid-column:1/3}.control-form .check{grid-column:1/3}}
@media(max-width:520px){header{align-items:flex-start;flex-direction:column;padding:18px 24px}.identity{justify-content:flex-end;margin-top:12px;width:100%}.date-filter label{min-width:130px}.quota-strip{grid-template-columns:1fr}.quota-strip p{grid-column:auto}.control-form{grid-template-columns:1fr}.mode-options,.official-mode,.manual-fields,.control-form .check{grid-column:auto}.mode-options,.manual-fields{grid-template-columns:1fr}.line-chart{overflow:auto}.line-chart svg{min-width:620px}.total-row{grid-template-columns:1fr auto}.total-bar{grid-column:1/3}}
"""


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
    admin_username = credentials.get("dashboard_admin_username", credentials.get("dashboard_username", "admin"))
    admin_password = credentials.get("dashboard_admin_password", credentials.get("dashboard_password", ""))
    server = UsageServer(
        (args.listen, args.port), Handler, database=args.database,
        report_token=credentials["report_token"], admin_token=credentials["admin_token"],
        dashboard_admin_username=admin_username,
        dashboard_admin_password=admin_password,
        dashboard_viewer_username=credentials.get("dashboard_viewer_username", admin_username),
        dashboard_viewer_password=credentials.get("dashboard_viewer_password", admin_password),
        session_secret=credentials["session_secret"],
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
