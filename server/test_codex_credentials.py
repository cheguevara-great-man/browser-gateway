from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent))
import codex_credentials as credentials  # noqa: E402


def _jwt(payload: dict[str, object]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).rstrip(b"=").decode("ascii")
    return f"header.{encoded}.signature"


class CredentialStoreTests(unittest.TestCase):
    def test_loads_headers_and_account_from_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            path.write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {
                            "access_token": _jwt({"exp": 4_000_000_000}),
                            "refresh_token": "refresh-test",
                            "account_id": "account-test",
                        },
                    }
                ),
                encoding="utf-8",
            )
            store = credentials.CredentialStore(path, clock=lambda: 1)
            headers = store.headers()
            self.assertEqual(headers.account_id, "account-test")
            self.assertTrue(headers.access_token.startswith("header."))
            self.assertTrue(store.status()["ready"])

    def test_refresh_is_atomic_and_preserves_account_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            path.write_text(
                json.dumps(
                    {
                        "tokens": {
                            "access_token": _jwt({"exp": 1}),
                            "refresh_token": "refresh-old",
                            "account_id": "account-test",
                        }
                    }
                ),
                encoding="utf-8",
            )

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self, _size: int) -> bytes:
                    return json.dumps(
                        {"access_token": _jwt({"exp": 4_000_000_000}), "refresh_token": "refresh-new"}
                    ).encode("utf-8")

            original = credentials.urlopen
            credentials.urlopen = lambda *_args, **_kwargs: Response()  # type: ignore[assignment]
            try:
                store = credentials.CredentialStore(path, clock=lambda: 2)
                headers = store.headers()
            finally:
                credentials.urlopen = original  # type: ignore[assignment]
            self.assertEqual(headers.account_id, "account-test")
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["tokens"]["refresh_token"], "refresh-new")


if __name__ == "__main__":
    unittest.main()
