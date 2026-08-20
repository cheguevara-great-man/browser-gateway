"""Server-side storage and refresh for a single ChatGPT Codex account.

The file format intentionally matches Codex's ``auth.json`` token subset.  This
module never logs or returns token values; callers receive only the headers
needed for a fixed first-party request.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_TOKEN_URL = "https://auth.openai.com/oauth/token"
_MAX_DOCUMENT_BYTES = 1024 * 1024
_REFRESH_SKEW_SECONDS = 300


class CredentialError(RuntimeError):
    """A safe error that contains no credential material."""


@dataclass(frozen=True, slots=True)
class AccountHeaders:
    access_token: str
    account_id: str | None


class CredentialStore:
    """Loads, refreshes and atomically persists a server-local Codex login."""

    def __init__(
        self,
        path: Path,
        *,
        token_url: str = DEFAULT_TOKEN_URL,
        client_id: str = DEFAULT_CLIENT_ID,
        clock: callable = time.time,
    ) -> None:
        self._path = path
        self._token_url = token_url
        self._client_id = client_id
        self._clock = clock
        self._lock = threading.Lock()

    def headers(self) -> AccountHeaders:
        with self._lock:
            document = self._load()
            if self._should_refresh(document):
                document = self._refresh(document)
            return _headers_from_document(document)

    def refresh(self) -> AccountHeaders:
        with self._lock:
            return _headers_from_document(self._refresh(self._load()))

    def status(self) -> dict[str, object]:
        try:
            with self._lock:
                document = self._load()
                headers = _headers_from_document(document)
                return {
                    "configured": True,
                    "ready": not self._should_refresh(document),
                    "account_id_present": bool(headers.account_id),
                    "access_token_expires_at": _token_expiry(headers.access_token),
                }
        except CredentialError:
            return {"configured": False, "ready": False, "account_id_present": False}

    def _load(self) -> dict[str, Any]:
        try:
            if self._path.stat().st_size > _MAX_DOCUMENT_BYTES:
                raise CredentialError("credential file is too large")
            document = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CredentialError("server account credentials are unavailable") from exc
        if not isinstance(document, dict):
            raise CredentialError("server account credentials are invalid")
        _headers_from_document(document)
        return document

    def _should_refresh(self, document: dict[str, Any]) -> bool:
        headers = _headers_from_document(document)
        expiry = _token_expiry(headers.access_token)
        return expiry is not None and expiry - int(self._clock()) <= _REFRESH_SKEW_SECONDS

    def _refresh(self, document: dict[str, Any]) -> dict[str, Any]:
        tokens = document.get("tokens")
        assert isinstance(tokens, dict)
        refresh_token = tokens.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise CredentialError("server account needs sign-in")
        payload = urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self._client_id,
            }
        ).encode("ascii")
        request = Request(
            self._token_url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:  # nosec B310: fixed HTTPS URL
                raw = response.read(_MAX_DOCUMENT_BYTES + 1)
        except Exception as exc:
            raise CredentialError("server account refresh failed") from exc
        if len(raw) > _MAX_DOCUMENT_BYTES:
            raise CredentialError("server account refresh returned too much data")
        try:
            response_document = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise CredentialError("server account refresh returned invalid data") from exc
        if not isinstance(response_document, dict):
            raise CredentialError("server account refresh returned invalid data")
        next_document = json.loads(json.dumps(document))
        next_tokens = next_document["tokens"]
        for key in ("access_token", "refresh_token", "id_token"):
            value = response_document.get(key)
            if isinstance(value, str) and value:
                next_tokens[key] = value
        if not isinstance(next_tokens.get("access_token"), str) or not next_tokens["access_token"]:
            raise CredentialError("server account refresh omitted access token")
        if not isinstance(next_tokens.get("refresh_token"), str) or not next_tokens["refresh_token"]:
            raise CredentialError("server account refresh omitted refresh token")
        account_id = _account_id_from_id_token(next_tokens.get("id_token"))
        if account_id:
            next_tokens["account_id"] = account_id
        next_document["last_refresh"] = _utc_timestamp()
        self._save(next_document)
        return next_document

    def _save(self, document: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.", suffix=".next", dir=self._path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary_name, 0o640)
            os.replace(temporary_name, self._path)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _headers_from_document(document: dict[str, Any]) -> AccountHeaders:
    tokens = document.get("tokens")
    if not isinstance(tokens, dict):
        raise CredentialError("server account credentials are invalid")
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token or len(access_token) > _MAX_DOCUMENT_BYTES:
        raise CredentialError("server account credentials are invalid")
    account_id = tokens.get("account_id")
    if not isinstance(account_id, str) or not account_id:
        account_id = _account_id_from_id_token(tokens.get("id_token"))
    if account_id is not None and len(account_id) > 512:
        account_id = None
    return AccountHeaders(access_token=access_token, account_id=account_id)


def _token_expiry(token: str) -> int | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        value = decoded.get("exp") if isinstance(decoded, dict) else None
        return int(value) if isinstance(value, (int, float)) else None
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return None


def _account_id_from_id_token(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parts = value.split(".")
    if len(parts) < 2:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return None
    auth = claims.get("https://api.openai.com/auth") if isinstance(claims, dict) else None
    account_id = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    return account_id if isinstance(account_id, str) and account_id else None


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
