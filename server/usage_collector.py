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
            days = _query_days(path.query)
            page = {
                "/dashboard": "overview",
                "/dashboard/daily": "daily",
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
                summary(self.server.database, days), session[0], session[1], days,
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
        path = urlsplit(self.path).path
        if path == "/login":
            self._login()
            return
        if path in {"/dashboard/rate", "/dashboard/budget"}:
            self._dashboard_update(path)
            return
        if path != "/v1/usage/events":
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
            else:
                raw_budget = form.get("budget", [""])[0].strip()
                set_budget(self.server.database, days, float(raw_budget) if raw_budget else None)
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


def summary(path: Path, days: int) -> dict[str, object]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with closing(connect(path)) as database, database:
        rows = database.execute(
            """SELECT machine_id,machine_name,model,model_level,service_tier,COUNT(*),SUM(input_tokens),
                      SUM(cached_input_tokens),SUM(output_tokens),
                      SUM(reasoning_output_tokens),SUM(total_tokens),MAX(occurred_at)
                 FROM usage_events WHERE occurred_at >= ?
                 GROUP BY machine_id,machine_name,model,model_level,service_tier""",
            (since,),
        ).fetchall()
        daily_rows = database.execute(
            """SELECT date(occurred_at,'+8 hours'),machine_id,machine_name,model,model_level,
                      service_tier,COUNT(*),SUM(input_tokens),SUM(cached_input_tokens),
                      SUM(output_tokens),SUM(reasoning_output_tokens),SUM(total_tokens)
                 FROM usage_events WHERE occurred_at >= ?
                 GROUP BY date(occurred_at,'+8 hours'),machine_id,machine_name,model,model_level,service_tier
                 ORDER BY 1""",
            (since,),
        ).fetchall()
        configured_rates = {
            _model_key(row[0]): (float(row[1]), float(row[2]), float(row[3]))
            for row in database.execute(
                "SELECT model,input_rate,cached_input_rate,output_rate FROM model_rates"
            )
        }
        budget_row = database.execute(
            "SELECT setting_value FROM dashboard_settings WHERE setting_key = ?",
            (f"budget_{days}",),
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
    budget = float(budget_row[0]) if budget_row is not None else None
    target = round(budget / len(machines), 6) if budget is not None and machines else None
    for machine in machines:
        machine["deviation_percent"] = (
            round((machine["estimated_credits"] / average - 1) * 100, 1) if average else 0.0
        )
        machine["catch_up_to_highest"] = round(highest - machine["estimated_credits"], 6)
        machine["budget_remaining"] = (
            round(max(target - machine["estimated_credits"], 0), 6) if target is not None else None
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
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "machines": machines,
        "models": models,
        "budget_credits": budget,
        "per_machine_target": target,
        "average_estimated_credits": average,
        "daily": daily,
        "totals": {
            "machines": len(machines),
            "requests": sum(item["requests"] for item in machines),
            "input_tokens": sum(item["input_tokens"] for item in machines),
            "cached_input_tokens": sum(item["cached_input_tokens"] for item in machines),
            "output_tokens": sum(item["output_tokens"] for item in machines),
            "reasoning_output_tokens": sum(item["reasoning_output_tokens"] for item in machines),
            "total_tokens": sum(item["total_tokens"] for item in machines),
            "estimated_credits": round(sum(item["estimated_credits"] for item in machines), 6),
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
        "daily": lambda: _daily_page(data),
        "machines": lambda: _machines_page(data, days),
        "models": lambda: _models_page(data),
        "machine": lambda: _machine_page(data, machine_id),
        "settings": lambda: _settings_page(data, session, role, days, secret),
    }[page]()
    page_names = {
        "overview": "总览", "daily": "每日统计", "machines": "机器", "models": "模型",
        "machine": "机器详情", "settings": "设置",
    }
    primary = [
        ("overview", "/dashboard", "总览"),
        ("daily", "/dashboard/daily", "每日"),
        ("machines", "/dashboard/machines", "机器"),
        ("models", "/dashboard/models", "模型"),
        ("settings", "/dashboard/settings", "设置"),
    ]
    nav = "".join(
        f'<a class="{"active" if page == key or (page == "machine" and key == "machines") else ""}" '
        f'href="{url}?days={days}">{label}</a>' for key, url, label in primary
    )
    ranges = "".join(
        f'<a class="{"active" if days == choice else ""}" href="{_page_url(page, choice, machine_id)}">{choice} 天</a>'
        for choice in (7, 30, 90, 180, 366)
    )
    role_label = "管理员" if role == "admin" else "只读"
    total = data["totals"]
    warning = (
        f'<div class="notice">有 {_number(total["unrated_tokens"])} Token 尚未匹配费率，Credits 合计不包含这部分。</div>'
        if total["unrated_tokens"] else ""
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{page_names[page]} · Codex 用量中心</title><style>{_CSS}</style></head>
<body><header><div><h1>Codex 用量中心</h1><p>{days} 天 · 更新于 {html.escape(_short_time(data['generated_at']))}</p></div>
<div class="identity"><span>{role_label}</span><a href="/logout">退出</a></div></header>
<main><nav class="primary">{nav}</nav><nav class="ranges">{ranges}</nav>{warning}{content}
<p class="foot">Credits 由模型响应中的 usage、官方标准费率和 Fast 倍率估算；账号剩余额度与重置时间仍以 OpenAI Usage 页面为准。</p></main></body></html>"""


def _overview_page(data: dict[str, object], days: int) -> str:
    total, machines, models, daily = data["totals"], data["machines"], data["models"], data["daily"]
    cards = _cards(total)
    max_credits = max((item["estimated_credits"] for item in machines), default=1) or 1
    rows = []
    for item in machines:
        width = min(100, item["estimated_credits"] / max_credits * 100)
        deviation = item["deviation_percent"]
        css = "over" if deviation > 10 else "under" if deviation < -10 else "balanced"
        link = "/dashboard/machine?" + urlencode({"id": item["machine_id"], "days": days})
        rows.append(f"""<tr><td><a class="name" href="{link}">{html.escape(item['machine_name'])}</a><small>{html.escape(item['machine_id'][:8])}</small></td>
<td>{_number(item['requests'])}</td><td><div class="bar"><i style="width:{width:.1f}%"></i></div>{_credits(item['estimated_credits'])}</td>
<td class="{css}">{deviation:+.1f}%</td><td>{_credits(item['catch_up_to_highest'])}</td><td>{html.escape(_short_time(item['last_seen']))}</td></tr>""")
    recent = daily[-14:]
    chart_max = max((item["estimated_credits"] for item in recent), default=1) or 1
    bars = "".join(
        f'<div class="daybar"><span>{_credits(item["estimated_credits"])}</span><i style="height:{max(3,item["estimated_credits"]/chart_max*100):.1f}%"></i><small>{html.escape(item["date"][5:])}</small></div>'
        for item in recent
    ) or '<p class="empty">尚无每日数据</p>'
    top_models = "".join(
        f'<li><span>{html.escape(item["model"])} · {html.escape(item["model_level"])} · {html.escape(_tier_label(item["service_tier"]))}</span><strong>{_credits(item["estimated_credits"])} C</strong></li>'
        for item in models[:6]
    ) or '<li class="empty">尚无模型数据</li>'
    target = (
        f'预算目标：每台 {_credits(data["per_machine_target"])} Credits'
        if data["per_machine_target"] is not None else '按当前最高消耗计算追平建议'
    )
    return f"""{cards}<section class="split"><article class="panel"><div class="panel-head"><div><h2>最近每日消耗</h2><p>最近 14 个有记录的日期</p></div></div><div class="chart">{bars}</div></article>
<article class="panel"><div class="panel-head"><div><h2>主要模型组合</h2><p>模型、推理档位与速度档位</p></div></div><ul class="rank">{top_models}</ul></article></section>
<section class="panel"><div class="panel-head"><div><h2>机器额度平衡</h2><p>{target}</p></div></div>
<div class="table-wrap"><table><thead><tr><th>机器</th><th>请求</th><th>估算 Credits</th><th>偏离平均</th><th>追平还可用</th><th>最后使用</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan="6" class="empty">尚无用量数据</td></tr>'}</tbody></table></div></section>"""


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
        link = "/dashboard/machine?" + urlencode({"id": item["machine_id"], "days": days})
        top_names = list(dict.fromkeys(model["model"] for model in item["models"]))[:3]
        top = "、".join(html.escape(model) for model in top_names)
        rows.append(f"""<tr><td><a class="name" href="{link}">{html.escape(item['machine_name'])}</a><small>{html.escape(item['machine_id'])}</small></td>
<td>{_number(item['requests'])}</td><td>{_number(item['input_tokens'])}</td><td>{_number(item['cached_input_tokens'])}</td><td>{item['cache_hit_rate']:.1f}%</td>
<td>{_number(item['output_tokens'])}</td><td>{_credits(item['estimated_credits'])}</td><td class="mix">{top}</td></tr>""")
    return f"""{_cards(data['totals'])}<section class="panel"><div class="panel-head"><div><h2>机器统计</h2><p>点击机器名称查看每日记录和模型组合。</p></div></div>
<div class="table-wrap"><table><thead><tr><th>机器</th><th>请求</th><th>输入</th><th>缓存输入</th><th>缓存率</th><th>输出</th><th>Credits</th><th>主要模型</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan="8" class="empty">尚无机器数据</td></tr>'}</tbody></table></div></section>"""


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
    return f"""{_cards(data['totals'])}<section class="panel"><div class="panel-head"><div><h2>模型与档位</h2><p>推理档位影响实际输出量；Fast 档位按官方倍数提高标准 Credits。</p></div></div>
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
    return f"""<div class="back"><a href="/dashboard/machines?days={data['days']}">← 返回机器列表</a></div><div class="title-row"><div><h2>{html.escape(machine['machine_name'])}</h2><p>{html.escape(machine['machine_id'])} · 最后使用 {html.escape(_short_time(machine['last_seen']))}</p></div></div>
{_cards(local_totals)}<section class="split"><article class="panel"><div class="panel-head"><div><h2>模型组合</h2><p>缓存率 {machine['cache_hit_rate']:.1f}%</p></div></div><div class="table-wrap"><table><thead><tr><th>模型</th><th>推理</th><th>速度</th><th>请求</th><th>输入</th><th>缓存</th><th>输出</th><th>Credits</th></tr></thead><tbody>{model_rows}</tbody></table></div></article>
<article class="panel"><div class="panel-head"><div><h2>每日历史</h2><p>仅显示有使用记录的日期</p></div></div><div class="table-wrap"><table><thead><tr><th>日期</th><th>请求</th><th>Token</th><th>Credits</th></tr></thead><tbody>{''.join(daily_rows)}</tbody></table></div></article></section>"""


def _settings_page(data: dict[str, object], session: str, role: str, days: int, secret: bytes) -> str:
    if role != "admin":
        return '<section class="panel"><div class="panel-head"><div><h2>只读账号</h2><p>你可以查看全部统计，但修改预算和费率需要管理员账号。</p></div></div></section>'
    csrf = csrf_token(secret, session)
    budget = "" if data["budget_credits"] is None else str(data["budget_credits"])
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
    return f"""<section class="panel"><div class="panel-head"><div><h2>周期预算</h2><p>预算只用于六台机器之间的公平建议，不会修改 OpenAI 账号额度。</p></div>
<form class="budget" method="post" action="/dashboard/budget"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="days" value="{days}">
<input type="number" name="budget" min="0.000001" step="0.01" placeholder="本周期 Credits 预算（可留空）" value="{budget}"><button>保存预算</button></form></div></section>
<section class="panel"><div class="panel-head"><div><h2>标准模型费率</h2><p>单位：每 100 万 Token 的 Credits。Fast 倍数由系统在标准费率之外自动应用。</p></div></div>
<div class="table-wrap"><table><thead><tr><th>模型</th><th>输入 / 缓存 / 输出</th></tr></thead><tbody>{''.join(rows) or '<tr><td colspan="2" class="empty">收到模型数据后可修改费率</td></tr>'}</tbody></table></div></section>"""


def _cards(total: dict[str, object]) -> str:
    return f"""<section class="cards"><article><span>机器</span><strong>{_number(total['machines'])}</strong></article>
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
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(value)[:32]


_CSS = """
:root{color-scheme:light;font-family:Inter,'Microsoft YaHei',sans-serif;background:#f3f6fb;color:#14213d}*{box-sizing:border-box}
body{margin:0}h1,h2,p{margin:0}a{color:#245dc1}header{display:flex;justify-content:space-between;align-items:center;padding:22px max(4vw,24px);background:linear-gradient(120deg,#102a56,#173f79);color:white}header p{opacity:.72;margin-top:5px}.identity{display:flex;align-items:center;gap:14px}.identity span{padding:5px 10px;border:1px solid #ffffff42;border-radius:20px;font-size:12px}.identity a{color:white}
main{max-width:1440px;margin:auto;padding:22px}.primary,.ranges{display:flex;gap:8px;overflow:auto}.primary{margin-bottom:12px}.ranges{margin-bottom:18px}.primary a,.ranges a{padding:9px 15px;border-radius:10px;color:#52647b;text-decoration:none;background:white;border:1px solid #e2e8f2;white-space:nowrap}.primary a.active{background:#173f79;color:white;border-color:#173f79}.ranges a{padding:6px 12px;border-radius:18px;font-size:13px}.ranges a.active{background:#2d6cdf;color:white;border-color:#2d6cdf}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.cards article,.panel{background:white;border:1px solid #e0e7f1;border-radius:14px;box-shadow:0 5px 22px #19345d0b}.cards article{padding:18px}.cards span{display:block;color:#718096}.cards strong{display:block;font-size:27px;margin-top:7px}.panel{margin-top:18px;overflow:hidden}.split{display:grid;grid-template-columns:1.35fr 1fr;gap:18px}.split .panel{min-width:0}.panel-head{display:flex;justify-content:space-between;gap:18px;align-items:center;padding:20px}.panel-head p,.title-row p{color:#718096;margin-top:5px}.title-row{padding:8px 2px 2px}.title-row h2{font-size:26px}.back{margin:2px 0 14px}.back a{text-decoration:none}
.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;white-space:nowrap}th,td{text-align:left;padding:13px 15px;border-top:1px solid #edf1f7}th{font-size:12px;color:#718096;background:#fafcff}td small{display:block;color:#98a5b7;margin-top:3px}.name{font-weight:700;text-decoration:none}.mix{white-space:normal;min-width:180px;color:#52647b;font-size:13px}
.bar{display:inline-block;width:90px;height:7px;background:#e8eef8;border-radius:6px;margin-right:9px;vertical-align:middle}.bar i{display:block;height:100%;background:#2d6cdf;border-radius:6px}.over{color:#c53030}.under{color:#2b6cb0}.balanced{color:#218358}.notice{background:#fff8e6;border:1px solid #f4d48c;color:#7c5700;padding:11px 14px;border-radius:10px;margin-bottom:15px}
.chart{height:230px;display:flex;align-items:flex-end;gap:8px;padding:20px;overflow:auto}.daybar{height:190px;min-width:38px;display:flex;flex-direction:column;align-items:center;justify-content:flex-end}.daybar span{font-size:10px;color:#718096}.daybar i{width:24px;background:linear-gradient(#4b82ed,#245dc1);border-radius:6px 6px 2px 2px;margin:5px 0}.daybar small{font-size:10px;color:#718096}.rank{list-style:none;margin:0;padding:0 20px 18px}.rank li{display:flex;justify-content:space-between;gap:15px;padding:12px 0;border-top:1px solid #edf1f7}.rank span{color:#52647b}
button{border:0;border-radius:8px;background:#2463eb;color:white;padding:9px 14px;font-weight:600;cursor:pointer}input{border:1px solid #ccd6e5;border-radius:8px;padding:9px;background:white}.inline,.budget{display:flex;gap:7px}.rates label{font-size:11px;color:#718096}.rates input{display:block;width:78px;padding:6px}.budget input{width:250px}.empty{text-align:center;color:#718096;padding:32px}.foot{text-align:center;color:#718096;padding:26px;font-size:13px}
.login-body{min-height:100vh;display:grid;place-items:center;background:linear-gradient(135deg,#edf4ff,#f8fbff)}.login-card{width:min(390px,92vw);padding:30px;background:white;border-radius:18px;box-shadow:0 18px 60px #18375d22}.login-card .muted{color:#718096;margin:8px 0 22px}.login-card label{display:block;margin:14px 0;color:#52647b}.login-card input{display:block;width:100%;margin-top:6px}.login-card button{width:100%;margin-top:8px}.error{color:#c53030;background:#fff5f5;padding:10px;border-radius:8px}
@media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}.split{grid-template-columns:1fr}.panel-head{align-items:flex-start;flex-direction:column}.budget{width:100%}.budget input{flex:1}main{padding:14px}.chart{height:205px}.daybar{height:165px}}
@media(max-width:520px){header{align-items:flex-start;flex-direction:column;padding:18px 24px}.identity{justify-content:flex-end;margin-top:12px;width:100%}}
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
