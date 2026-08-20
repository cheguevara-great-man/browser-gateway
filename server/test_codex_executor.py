from __future__ import annotations

import base64
import http.client
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path


SERVER_DIRECTORY = Path(__file__).parent
sys.path.insert(0, str(SERVER_DIRECTORY))
import codex_credentials  # noqa: E402
import codex_executor  # noqa: E402


def _load_usage_module():
    spec = importlib.util.spec_from_file_location("usage_collector_for_executor_test", SERVER_DIRECTORY / "usage_collector.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _jwt() -> str:
    payload = base64.urlsafe_b64encode(b'{"exp":4000000000}').rstrip(b"=").decode("ascii")
    return f"header.{payload}.signature"


class ExecutorTests(unittest.TestCase):
    def test_extracts_sse_usage_and_request_metadata(self) -> None:
        raw = (
            b"event: response.completed\n"
            b'data: {"response":{"usage":{"input_tokens":12,"input_tokens_details":{"cached_tokens":5},'
            b'"output_tokens":8,"output_tokens_details":{"reasoning_tokens":3},"total_tokens":20}}}\n\n'
        )
        self.assertEqual(
            codex_executor._find_usage(raw),
            {
                "input_tokens": 12,
                "cached_input_tokens": 5,
                "output_tokens": 8,
                "reasoning_output_tokens": 3,
                "total_tokens": 20,
            },
        )
        self.assertEqual(
            codex_executor._request_metadata(
                b'{"model":"gpt-5.6-sol","reasoning":{"effort":"high"},"service_tier":"priority"}'
            ),
            ("gpt-5.6-sol", "high", "priority"),
        )

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.database = root / "usage.sqlite3"
        self.auth = root / "auth.json"
        self.usage = _load_usage_module()
        self.usage.initialize(self.database)
        code, _expires = self.usage.create_enrollment(self.database, "PC-1")
        enrollment = self.usage.redeem_enrollment(
            self.database,
            code,
            "",
            {
                "gateway": {"host": "203.0.113.10", "port": 443, "username": "device", "password": "secret"},
                "usageCollectorUrl": "https://203.0.113.10:9443/v1/usage/events",
                "dashboardUrl": "https://203.0.113.10:9443/dashboard",
            },
        )
        self.device_token = enrollment["deviceToken"]
        self.auth.write_text(
            json.dumps({"tokens": {"access_token": _jwt(), "refresh_token": "refresh", "account_id": "account-test"}}),
            encoding="utf-8",
        )
        self.original_relay = codex_executor.ExecutorRequestHandler._relay
        self.seen: list[tuple[str, str, bytes, str]] = []

        def fake_relay(handler, method, path, body, account, device, request_id):
            self.seen.append((method, path, body, account.account_id or ""))
            handler._json(200, {"ok": True}, request_id)

        codex_executor.ExecutorRequestHandler._relay = fake_relay
        self.server = codex_executor.ExecutorServer(
            ("127.0.0.1", 0),
            database=self.database,
            credential_store=codex_credentials.CredentialStore(self.auth, clock=lambda: 1),
            usage_module=self.usage,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        codex_executor.ExecutorRequestHandler._relay = self.original_relay
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.directory.cleanup()

    def request(self, method: str, path: str, *, token: str = "", body: bytes = b"") -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        headers = {"Content-Length": str(len(body))}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        headers_out = {name.lower(): value for name, value in response.getheaders()}
        connection.close()
        return response.status, headers_out, payload

    def test_path_policy_is_strict(self) -> None:
        self.assertTrue(codex_executor.allowed_request("GET", "/models"))
        self.assertTrue(codex_executor.allowed_request("POST", "/responses"))
        self.assertTrue(codex_executor.allowed_request("POST", "/responses/id/cancel"))
        self.assertFalse(codex_executor.allowed_request("POST", "/backend-api/account/delete"))
        self.assertFalse(codex_executor.allowed_request("GET", "/responses/a/b"))

    def test_registered_device_can_call_responses(self) -> None:
        status, _headers, payload = self.request(
            "POST", "/v1/codex/responses", token=self.device_token, body=b'{"stream":true}'
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload), {"ok": True})
        self.assertEqual(self.seen, [("POST", "/responses", b'{"stream":true}', "account-test")])

    def test_missing_device_token_is_rejected(self) -> None:
        status, _headers, payload = self.request("GET", "/v1/codex/models")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(payload)["error"]["code"], "invalid_device_token")

    def test_unknown_path_never_reaches_upstream(self) -> None:
        status, _headers, payload = self.request(
            "POST", "/v1/codex/anything", token=self.device_token, body=b"{}"
        )
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(payload)["error"]["code"], "route_not_found")
        self.assertEqual(self.seen, [])


if __name__ == "__main__":
    unittest.main()
