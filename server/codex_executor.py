"""Private, device-authenticated server executor for ChatGPT Codex requests.

This is deliberately a fixed reverse proxy, not an OpenAI-compatible public
gateway.  A registered Browser Gateway device may invoke only the Codex
Responses paths listed below; account authentication is always replaced by the
server-held ChatGPT login.
"""

from __future__ import annotations

import argparse
import http.client
import importlib.util
import json
import logging
import ssl
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

from codex_credentials import AccountHeaders, CredentialError, CredentialStore


_LOG = logging.getLogger("browser_gateway.codex_executor")
_HOP_BY_HOP = frozenset(
    {
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailer", "transfer-encoding", "upgrade",
    }
)
_INBOUND_BLOCKED_HEADERS = _HOP_BY_HOP | frozenset(
    {"authorization", "cookie", "host", "content-length", "chatgpt-account-id"}
)
_MAX_BODY_BYTES = 32 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024
_USAGE_CAPTURE_BYTES = 2 * 1024 * 1024
_PROTOCOL_VERSION = 1


class ExecutorError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class AllowedRequest:
    method: str
    relative_path: str


def allowed_request(method: str, relative_path: str) -> bool:
    """Return whether a device may invoke this Lite endpoint."""

    if method == "GET" and relative_path == "/models":
        return True
    if method == "POST" and relative_path in {"/responses", "/responses/compact"}:
        return True
    segments = relative_path.split("/")
    return (
        len(segments) == 3
        and segments[0] == ""
        and segments[1] == "responses"
        and bool(segments[2])
        and method in {"GET", "DELETE"}
    ) or (
        len(segments) == 4
        and segments[0] == ""
        and segments[1] == "responses"
        and bool(segments[2])
        and segments[3] == "cancel"
        and method == "POST"
    )


class ExecutorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        address: tuple[str, int],
        *,
        database: Path,
        credential_store: CredentialStore,
        upstream_base_url: str = "https://chatgpt.com/backend-api/codex",
        usage_module: object,
    ) -> None:
        self.database = database
        self.credential_store = credential_store
        self.upstream = _parse_upstream(upstream_base_url)
        self.usage_module = usage_module
        self.ssl_context = ssl.create_default_context()
        self.started_at = time.time()
        super().__init__(address, ExecutorRequestHandler)


class ExecutorRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BrowserGatewayCodex/0.1"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("HEAD")

    def _handle(self, method: str) -> None:
        request_id = uuid.uuid4().hex
        try:
            if self.path == "/v1/executor/health":
                device = self._device()
                self._json(
                    200,
                    {
                        "status": "ok",
                        "protocol_version": _PROTOCOL_VERSION,
                        "account": self._server().credential_store.status(),
                        "machine_id": device["machine_id"],
                        "machine_name": device["machine_name"],
                    },
                    request_id,
                    head_only=method == "HEAD",
                )
                return
            relative_path, query = self._relative_path()
            if method == "HEAD":
                raise ExecutorError(405, "method_not_allowed", "HEAD is not supported for this endpoint")
            if not allowed_request(method, relative_path):
                raise ExecutorError(404, "route_not_found", "The requested Codex endpoint is not available")
            device = self._device()
            policy = self._policy(str(device["machine_id"]))
            if bool(policy.get("blocked")):
                raise ExecutorError(429, "device_quota_exceeded", "This device has reached its configured quota")
            body = self._body()
            account = self._server().credential_store.headers()
            self._relay(method, relative_path + query, body, account, device, request_id)
            self._touch_device(str(device["machine_id"]))
        except ExecutorError as error:
            self._discard_rejected_body()
            self._json(error.status, {"error": {"code": error.code, "message": error.message}}, request_id)
        except CredentialError:
            self._discard_rejected_body()
            self._json(
                503,
                {"error": {"code": "server_account_unavailable", "message": "Server account needs sign-in"}},
                request_id,
            )
        except (http.client.HTTPException, OSError, ssl.SSLError):
            self._discard_rejected_body()
            self._json(
                502,
                {"error": {"code": "upstream_connection_failed", "message": "Server could not reach Codex upstream"}},
                request_id,
            )
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            _LOG.exception("unexpected_executor_error request_id=%s", request_id)
            self._discard_rejected_body()
            self._json(
                500,
                {"error": {"code": "internal_error", "message": "Server executor failed"}},
                request_id,
            )

    def _relative_path(self) -> tuple[str, str]:
        parsed = urlsplit(self.path)
        prefix = "/v1/codex"
        if not parsed.path.startswith(prefix + "/"):
            raise ExecutorError(404, "route_not_found", "The requested endpoint is not available")
        relative = parsed.path[len(prefix) :]
        if not relative.startswith("/") or "//" in relative or "/../" in relative or relative.endswith("/.."):
            raise ExecutorError(404, "route_not_found", "The requested endpoint is not available")
        return relative, f"?{parsed.query}" if parsed.query else ""

    def _device(self) -> dict[str, object]:
        value = self.headers.get("Authorization", "")
        if not value.startswith("Bearer "):
            raise ExecutorError(401, "invalid_device_token", "A registered device token is required")
        device = self._server().usage_module.authenticate_device(
            self._server().database, value[7:]
        )
        if device is None:
            raise ExecutorError(401, "invalid_device_token", "A registered device token is required")
        return device

    def _policy(self, machine_id: str) -> dict[str, object]:
        try:
            return self._server().usage_module.machine_policy(self._server().database, machine_id)
        except Exception:
            _LOG.warning("policy_unavailable machine_id=%s", machine_id)
            return {"blocked": False, "reason": "policy_unavailable"}

    def _touch_device(self, machine_id: str) -> None:
        try:
            self._server().usage_module.touch_device(self._server().database, machine_id)
        except Exception:
            _LOG.warning("device_touch_failed machine_id=%s", machine_id)

    def _body(self) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            raise ExecutorError(400, "chunked_request_not_supported", "Chunked request bodies are not supported")
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ExecutorError(400, "invalid_content_length", "Content-Length is invalid") from exc
        if length < 0 or length > _MAX_BODY_BYTES:
            raise ExecutorError(413, "request_too_large", "Request body exceeds the server limit")
        if length == 0:
            return b""
        value = self.rfile.read(length)
        if len(value) != length:
            raise ExecutorError(400, "incomplete_request_body", "Request body ended unexpectedly")
        return value

    def _relay(
        self,
        method: str,
        relative_path: str,
        body: bytes,
        account: AccountHeaders,
        device: dict[str, object],
        request_id: str,
    ) -> None:
        server = self._server()
        upstream_path = server.upstream.path.rstrip("/") + relative_path
        headers = _upstream_headers(self.headers.items(), account, request_id)
        connection = http.client.HTTPSConnection(
            server.upstream.hostname,
            server.upstream.port or 443,
            timeout=30,
            context=server.ssl_context,
        )
        started = time.monotonic()
        try:
            connection.request(method, upstream_path, body=body or None, headers=headers)
            response = connection.getresponse()
            head_ms = round((time.monotonic() - started) * 1000)
            # The only credentials presented upstream are server-held.  Do not
            # expose an upstream 401 as a misleading client API-key error.
            if response.status in {401, 403}:
                response.read(16 * 1024)
                raise CredentialError("server account was rejected by the upstream")
            self.send_response(response.status, response.reason)
            for name, value in response.getheaders():
                lowered = name.lower()
                if lowered in _HOP_BY_HOP or lowered in {"content-length", "content-encoding"}:
                    continue
                self.send_header(name, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Bridge-Request-Id", request_id)
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            first_body_ms: int | None = None
            usage_capture = _UsageCapture()
            while True:
                chunk = response.read(_CHUNK_BYTES)
                if not chunk:
                    break
                if first_body_ms is None:
                    first_body_ms = round((time.monotonic() - started) * 1000)
                usage_capture.write(chunk)
                self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            self._record_usage(device, body, usage_capture.value(), request_id)
            _LOG.info(
                "request_complete request_id=%s method=%s path=%s status=%s head_ms=%s first_body_ms=%s total_ms=%s",
                request_id,
                method,
                _redacted_path(relative_path),
                response.status,
                head_ms,
                first_body_ms,
                round((time.monotonic() - started) * 1000),
            )
        finally:
            connection.close()

    def _record_usage(
        self, device: dict[str, object], request_body: bytes, response_body: bytes, request_id: str
    ) -> None:
        usage = _find_usage(response_body)
        if usage is None:
            return
        model, model_level, service_tier = _request_metadata(request_body)
        event = {
            "event_id": f"server-{request_id}",
            "machine_id": str(device["machine_id"]),
            "machine_name": str(device["machine_name"]),
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "route": "server-codex",
            "model": model,
            "model_level": model_level,
            "service_tier": service_tier,
            **usage,
        }
        try:
            self._server().usage_module.insert_events(self._server().database, [event])
        except Exception:
            _LOG.warning("usage_record_failed request_id=%s", request_id)

    def _discard_rejected_body(self) -> None:
        # The handler may reject before reading a body.  Closing avoids treating
        # unread bytes as a second HTTP request on a persistent connection.
        self.close_connection = True

    def _json(self, status: int, value: dict[str, object], request_id: str, *, head_only: bool = False) -> None:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Bridge-Request-Id", request_id)
        self.end_headers()
        if not head_only:
            self.wfile.write(encoded)

    def _server(self) -> ExecutorServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _upstream_headers(
    inbound: Iterable[tuple[str, str]], account: AccountHeaders, request_id: str
) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in inbound:
        lowered = name.lower()
        if lowered in _INBOUND_BLOCKED_HEADERS or lowered.startswith("proxy-"):
            continue
        if lowered in {"accept", "content-type", "openai-beta", "user-agent", "x-stainless-lang", "x-stainless-package-version"}:
            headers[name] = value
    headers["Authorization"] = f"Bearer {account.access_token}"
    if account.account_id:
        headers["ChatGPT-Account-ID"] = account.account_id
    headers["X-Bridge-Request-Id"] = request_id
    headers.setdefault("Accept", "application/json")
    return headers


def _parse_upstream(value: str):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "chatgpt.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/backend-api/codex"
    ):
        raise ValueError("upstream must be https://chatgpt.com/backend-api/codex")
    return parsed


def _redacted_path(value: str) -> str:
    return value.split("?", 1)[0]


class _UsageCapture:
    """Bounded response capture used solely to recover the final usage event."""

    def __init__(self) -> None:
        self._value = bytearray()

    def write(self, chunk: bytes) -> None:
        remaining = _USAGE_CAPTURE_BYTES - len(self._value)
        if remaining > 0:
            self._value.extend(chunk[:remaining])

    def value(self) -> bytes:
        return bytes(self._value)


def _request_metadata(body: bytes) -> tuple[str, str, str]:
    try:
        value = json.loads(body)
    except (UnicodeError, json.JSONDecodeError):
        return "unknown", "default", "default"
    if not isinstance(value, dict):
        return "unknown", "default", "default"
    model = value.get("model")
    model_name = model[:128] if isinstance(model, str) and model else "unknown"
    reasoning = value.get("reasoning")
    effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
    model_level = effort[:64] if isinstance(effort, str) and effort else "default"
    service_tier = value.get("service_tier")
    tier = service_tier[:64] if isinstance(service_tier, str) and service_tier else "default"
    return model_name, model_level, tier


def _find_usage(raw: bytes) -> dict[str, int] | None:
    """Extract a standard Responses usage object from JSON or completed SSE events."""

    candidates: list[object] = []
    try:
        candidates.append(json.loads(raw))
    except (UnicodeError, json.JSONDecodeError):
        pass
    for line in raw.splitlines():
        if not line.startswith(b"data:"):
            continue
        try:
            candidates.append(json.loads(line[5:].strip()))
        except (UnicodeError, json.JSONDecodeError):
            continue
    for candidate in reversed(candidates):
        usage = _usage_object(candidate)
        if usage is not None:
            return usage
    return None


def _usage_object(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    response = value.get("response")
    source = response if isinstance(response, dict) else value
    usage = source.get("usage") if isinstance(source, dict) else None
    if not isinstance(usage, dict):
        return None

    def number(*names: str) -> int:
        for name in names:
            item = usage.get(name)
            if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
                return item
        return 0

    input_tokens = number("input_tokens", "prompt_tokens")
    cached_input_tokens = number("cached_input_tokens")
    output_tokens = number("output_tokens", "completion_tokens")
    reasoning_output_tokens = number("reasoning_output_tokens")
    total_tokens = number("total_tokens")
    if total_tokens <= 0:
        total_tokens = input_tokens + output_tokens
    if total_tokens <= 0 or total_tokens < input_tokens + output_tokens:
        return None
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "total_tokens": total_tokens,
    }


def _load_usage_module(path: Path):
    spec = importlib.util.spec_from_file_location("browser_gateway_usage", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load usage collector")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Browser Gateway Codex server executor")
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19444)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--usage-module", type=Path, required=True)
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    usage_module = _load_usage_module(args.usage_module)
    server = ExecutorServer(
        (args.listen, args.port),
        database=args.database,
        credential_store=CredentialStore(args.credentials),
        usage_module=usage_module,
    )
    if (args.tls_cert is None) != (args.tls_key is None):
        parser.error("--tls-cert and --tls-key must be supplied together")
    if args.tls_cert is not None and args.tls_key is not None:
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
        tls_context.load_cert_chain(args.tls_cert, args.tls_key)
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
