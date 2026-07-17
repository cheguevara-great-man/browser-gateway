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
        admin_token: str, dashboard_username: str, dashboard_password: str,
        session_secret: str,
    ):
        self.database = database
        self.report_token = report_token
        self.admin_token = admin_token
        self.dashboard_username = dashboard_username
        self.dashboard_password = dashboard_password
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
        if path.path == "/dashboard":
            session = self._session()
            if session is None:
                self._html(200, login_page(error=parse_qs(path.query).get("error") == ["1"]))
                return
            days = _query_days(path.query)
            self._html(200, dashboard_page(summary(self.server.database, days), session, days, self.server.session_secret))
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
        if not (
            hmac.compare_digest(username.encode("utf-8"), self.server.dashboard_username.encode("utf-8"))
            and hmac.compare_digest(password.encode("utf-8"), self.server.dashboard_password.encode("utf-8"))
        ):
            time.sleep(0.25)
            self._redirect("/dashboard?error=1")
            return
        token = create_session(self.server.session_secret)
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
        days = 30
        try:
            form = self._form(16384)
            if not hmac.compare_digest(
                form.get("csrf", [""])[0].encode("utf-8"),
                csrf_token(self.server.session_secret, session).encode("ascii"),
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

    def _session(self) -> str | None:
        for item in self.headers.get("Cookie", "").split(";"):
            name, separator, value = item.strip().partition("=")
            if separator and name == "bg_usage_session" and validate_session(self.server.session_secret, value):
                return value
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


def insert_events(path: Path, events: list[object]) -> int:
    rows = [validate_event(event) for event in events]
    accepted = 0
    received_at = datetime.now(timezone.utc).isoformat()
    with closing(connect(path)) as database, database:
        for row in rows:
            cursor = database.execute(
                """INSERT OR IGNORE INTO usage_events(
                    event_id,machine_id,machine_name,occurred_at,route,model,model_level,
                    input_tokens,cached_input_tokens,output_tokens,
                    reasoning_output_tokens,total_tokens,received_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (*row, received_at),
            )
            accepted += cursor.rowcount
    return accepted


def validate_event(value: object) -> tuple[object, ...]:
    required = {
        "event_id", "machine_id", "machine_name", "occurred_at", "route", "model", "model_level",
        "input_tokens", "cached_input_tokens", "output_tokens",
        "reasoning_output_tokens", "total_tokens",
    }
    legacy_required = required - {"model_level"}
    if not isinstance(value, dict) or frozenset(value) not in {
        frozenset(required), frozenset(legacy_required)
    }:
        raise ValueError("invalid fields")
    if "model_level" not in value:
        # Keep rolling upgrades safe: Bridge 2.7 events already queued on any of
        # the six machines remain valid after the central collector is upgraded.
        value = {**value, "model_level": "default"}
    strings = []
    for name, maximum in (
        ("event_id", 64), ("machine_id", 64), ("machine_name", 128),
        ("occurred_at", 64), ("route", 64), ("model", 128), ("model_level", 64),
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
            """SELECT machine_id,machine_name,model,model_level,COUNT(*),SUM(input_tokens),
                      SUM(cached_input_tokens),SUM(output_tokens),
                      SUM(reasoning_output_tokens),SUM(total_tokens),MAX(occurred_at)
                 FROM usage_events WHERE occurred_at >= ?
                 GROUP BY machine_id,machine_name,model,model_level""",
            (since,),
        ).fetchall()
        daily = database.execute(
            """SELECT substr(occurred_at,1,10),SUM(total_tokens)
                 FROM usage_events WHERE occurred_at >= ?
                 GROUP BY substr(occurred_at,1,10) ORDER BY 1""",
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
    model_map: dict[tuple[str, str], dict[str, object]] = {}
    for row in rows:
        machine_id, machine_name, model, model_level = row[:4]
        requests, input_tokens, cached_tokens, output_tokens, reasoning_tokens, total_tokens = row[4:10]
        rates = configured_rates.get(_model_key(model), DEFAULT_MODEL_RATES.get(_model_key(model)))
        estimated_credits = _estimate_credits(input_tokens, cached_tokens, output_tokens, rates)
        machine = machine_map.setdefault(
            (machine_id, machine_name),
            {
                "machine_id": machine_id, "machine_name": machine_name,
                "requests": 0, "input_tokens": 0, "cached_input_tokens": 0,
                "output_tokens": 0, "reasoning_output_tokens": 0,
                "total_tokens": 0, "estimated_credits": 0.0, "unrated_tokens": 0,
                "last_seen": row[10],
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
        machine["last_seen"] = max(machine["last_seen"], row[10])
        machine["models"].append(
            {
                "model": model, "model_level": model_level, "rates": rates,
                "requests": requests, "total_tokens": total_tokens,
                "estimated_credits": estimated_credits,
            }
        )
        model_item = model_map.setdefault(
            (model, model_level),
            {
                "model": model, "model_level": model_level, "rates": rates,
                "requests": 0, "input_tokens": 0, "cached_input_tokens": 0,
                "output_tokens": 0, "total_tokens": 0, "estimated_credits": 0.0,
                "unrated": rates is None,
            },
        )
        model_item["requests"] += requests
        model_item["input_tokens"] += input_tokens
        model_item["cached_input_tokens"] += cached_tokens
        model_item["output_tokens"] += output_tokens
        model_item["total_tokens"] += total_tokens
        if estimated_credits is not None:
            model_item["estimated_credits"] = round(model_item["estimated_credits"] + estimated_credits, 6)
    machines = sorted(machine_map.values(), key=lambda item: item["estimated_credits"], reverse=True)
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
    return {
        "days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "machines": machines,
        "models": models,
        "budget_credits": budget,
        "per_machine_target": target,
        "average_estimated_credits": average,
        "daily": [{"date": row[0], "total_tokens": row[1]} for row in daily],
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
) -> float | None:
    if rates is None:
        return None
    uncached = max(input_tokens - cached_tokens, 0)
    return round(
        (uncached * rates[0] + cached_tokens * rates[1] + output_tokens * rates[2]) / 1_000_000,
        6,
    )


def create_session(secret: bytes, now: int | None = None) -> str:
    issued = int(time.time()) if now is None else now
    nonce = base64.urlsafe_b64encode(hashlib.sha256(f"{issued}-{time.time_ns()}".encode()).digest()[:12]).decode().rstrip("=")
    payload = f"{issued}.{nonce}"
    signature = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def validate_session(secret: bytes, token: str, now: int | None = None) -> bool:
    try:
        issued_text, nonce, supplied = token.split(".", 2)
        issued = int(issued_text)
    except (ValueError, TypeError):
        return False
    current = int(time.time()) if now is None else now
    if not 0 <= current - issued <= 43200 or not 8 <= len(nonce) <= 64:
        return False
    expected = hmac.new(secret, f"{issued}.{nonce}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("ascii"))


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


def dashboard_page(data: dict[str, object], session: str, days: int, secret: bytes) -> str:
    csrf = csrf_token(secret, session)
    machines = data["machines"]
    models = data["models"]
    total = data["totals"]
    max_credits = max((item["estimated_credits"] for item in machines), default=1) or 1
    machine_rows = []
    for item in machines:
        width = min(100, round(item["estimated_credits"] / max_credits * 100, 1))
        deviation = item["deviation_percent"]
        deviation_class = "over" if deviation > 10 else "under" if deviation < -10 else "balanced"
        remaining = (
            _credits(item["budget_remaining"])
            if item["budget_remaining"] is not None
            else _credits(item["catch_up_to_highest"])
        )
        machine_rows.append(f"""<tr><td><strong>{html.escape(item['machine_name'])}</strong><small>{html.escape(item['machine_id'][:8])}</small></td>
<td>{_number(item['requests'])}</td><td>{_number(item['total_tokens'])}</td>
<td><div class="bar"><i style="width:{width}%"></i></div>{_credits(item['estimated_credits'])}</td>
<td class="{deviation_class}">{deviation:+.1f}%</td><td>{remaining}</td><td>{html.escape(_short_time(item['last_seen']))}</td></tr>""")
    model_rows = []
    for item in models:
        model = html.escape(item["model"], quote=True)
        level = html.escape(item["model_level"], quote=True)
        rates = item["rates"] or (0, 0, 0)
        credit_text = "未定价" if item["unrated"] else _credits(item["estimated_credits"])
        model_rows.append(f"""<tr><td>{model}</td><td>{level}</td><td>{_number(item['requests'])}</td>
<td>{_number(item['total_tokens'])}</td><td>{credit_text}</td><td>
<form class="inline rates" method="post" action="/dashboard/rate"><input type="hidden" name="csrf" value="{csrf}">
<input type="hidden" name="days" value="{days}"><input type="hidden" name="model" value="{model}">
<label>输入<input type="number" name="input_rate" min="0" max="100000" step="0.001" value="{rates[0]}"></label>
<label>缓存<input type="number" name="cached_rate" min="0" max="100000" step="0.001" value="{rates[1]}"></label>
<label>输出<input type="number" name="output_rate" min="0" max="100000" step="0.001" value="{rates[2]}"></label><button>保存</button></form></td></tr>""")
    budget = "" if data["budget_credits"] is None else str(data["budget_credits"])
    target_note = (
        f"已设置本周期总预算，每台目标 {_credits(data['per_machine_target'])} Credits"
        if data["per_machine_target"] is not None
        else "未设置总预算；建议可用值按当前消耗最高的机器计算"
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Token 用量中心</title><style>{_CSS}</style></head>
<body><header><div><h1>Token 用量中心</h1><p>{days} 天统计 · 更新于 {html.escape(_short_time(data['generated_at']))}</p></div><a href="/logout">退出</a></header>
<main><nav>{''.join(f'<a class="{"active" if days == choice else ""}" href="/dashboard?days={choice}">{choice} 天</a>' for choice in (7,30,90,180,366))}</nav>
<section class="cards"><article><span>机器</span><strong>{total['machines']}</strong></article><article><span>请求</span><strong>{_number(total['requests'])}</strong></article>
<article><span>原始 Token</span><strong>{_number(total['total_tokens'])}</strong></article><article><span>估算 Credits</span><strong>{_credits(total['estimated_credits'])}</strong></article></section>
<section class="panel"><div class="panel-head"><div><h2>机器额度平衡</h2><p>{target_note}</p></div>
<form class="budget" method="post" action="/dashboard/budget"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="days" value="{days}">
<input type="number" name="budget" min="0.000001" step="0.01" placeholder="本周期 Credits 预算（可留空）" value="{budget}"><button>设置预算</button></form></div>
<div class="table-wrap"><table><thead><tr><th>机器</th><th>请求</th><th>原始 Token</th><th>估算 Credits</th><th>偏离平均</th><th>建议可用 Credits</th><th>最后使用</th></tr></thead>
<tbody>{''.join(machine_rows) or '<tr><td colspan="7" class="empty">尚无用量数据</td></tr>'}</tbody></table></div></section>
<section class="panel"><div class="panel-head"><div><h2>模型、档位与 Credits 费率</h2><p>费率单位为每 100 万 Token 的 Credits；内置官方已知费率，未知模型可手工补充。</p></div></div>
<div class="table-wrap"><table><thead><tr><th>模型</th><th>档位</th><th>请求</th><th>原始 Token</th><th>估算 Credits</th><th>输入 / 缓存 / 输出费率</th></tr></thead>
<tbody>{''.join(model_rows) or '<tr><td colspan="6" class="empty">收到模型用量后将显示费率</td></tr>'}</tbody></table></div></section>
<p class="foot">Credits 根据模型返回的 usage 和费率估算；包含额度、滚动限制及重置时间仍以 OpenAI Usage 页面为准。未定价 Token：{_number(total['unrated_tokens'])}</p></main></body></html>"""


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
:root{color-scheme:light;font-family:Inter,'Microsoft YaHei',sans-serif;background:#f4f7fb;color:#14213d}*{box-sizing:border-box}
body{margin:0}header{display:flex;justify-content:space-between;align-items:center;padding:24px max(4vw,24px);background:#102a56;color:white}h1,h2,p{margin:0}header p{opacity:.7;margin-top:6px}header a{color:white}
main{max-width:1320px;margin:auto;padding:24px}nav{display:flex;gap:8px;margin-bottom:18px}nav a{padding:8px 14px;border-radius:20px;color:#46607f;text-decoration:none;background:white}nav a.active{background:#2463eb;color:white}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.cards article,.panel{background:white;border:1px solid #e2e8f2;border-radius:14px;box-shadow:0 5px 22px #19345d0d}.cards article{padding:18px}.cards span{display:block;color:#718096}.cards strong{display:block;font-size:26px;margin-top:8px}
.panel{margin-top:18px;overflow:hidden}.panel-head{display:flex;justify-content:space-between;gap:18px;align-items:center;padding:20px}.panel-head p{color:#718096;margin-top:5px}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;white-space:nowrap}th,td{text-align:left;padding:13px 16px;border-top:1px solid #edf1f7}th{font-size:12px;color:#718096;background:#fafcff}td small{display:block;color:#98a5b7;margin-top:3px}
.bar{display:inline-block;width:90px;height:7px;background:#e8eef8;border-radius:6px;margin-right:9px;vertical-align:middle}.bar i{display:block;height:100%;background:#2d6cdf;border-radius:6px}.over{color:#c53030}.under{color:#2b6cb0}.balanced{color:#218358}
button{border:0;border-radius:8px;background:#2463eb;color:white;padding:9px 14px;font-weight:600;cursor:pointer}input{border:1px solid #ccd6e5;border-radius:8px;padding:9px;background:white}.inline,.budget{display:flex;gap:7px}.rates label{font-size:11px;color:#718096}.rates input{display:block;width:75px;padding:6px}.budget input{width:240px}.empty{text-align:center;color:#718096;padding:32px}.foot{text-align:center;color:#718096;padding:24px;font-size:13px}
.login-body{min-height:100vh;display:grid;place-items:center;background:linear-gradient(135deg,#edf4ff,#f8fbff)}.login-card{width:min(390px,92vw);padding:30px;background:white;border-radius:18px;box-shadow:0 18px 60px #18375d22}.login-card .muted{color:#718096;margin:8px 0 22px}.login-card label{display:block;margin:14px 0;color:#52647b}.login-card input{display:block;width:100%;margin-top:6px}.login-card button{width:100%;margin-top:8px}.error{color:#c53030;background:#fff5f5;padding:10px;border-radius:8px}
@media(max-width:800px){.cards{grid-template-columns:repeat(2,1fr)}.panel-head{align-items:flex-start;flex-direction:column}.budget{width:100%}.budget input{flex:1}main{padding:14px}}
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
    server = UsageServer(
        (args.listen, args.port), Handler, database=args.database,
        report_token=credentials["report_token"], admin_token=credentials["admin_token"],
        dashboard_username=credentials["dashboard_username"],
        dashboard_password=credentials["dashboard_password"],
        session_secret=credentials["session_secret"],
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
