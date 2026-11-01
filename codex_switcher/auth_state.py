"""Read-only inspection of Codex ~/.codex/auth.json.

This module never writes the auth file and never returns access/refresh tokens.
It only decodes the non-secret display claims from the id_token/access_token.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * ((4 - len(segment) % 4) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def decode_jwt_payload(token: str | None) -> dict:
    if not token or token.count(".") < 2:
        return {}
    try:
        payload = token.split(".")[1]
        return json.loads(_b64url_decode(payload).decode("utf-8"))
    except Exception:
        return {}


def sha256_file(path: Path) -> str | None:
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return None
    return hashlib.sha256(data).hexdigest()


def _state_from_data(data: dict, *, sha: str | None, path: str | None) -> dict:
    tokens = data.get("tokens") or {}
    id_claims = decode_jwt_payload(tokens.get("id_token"))
    access_claims = decode_jwt_payload(tokens.get("access_token"))

    auth_claims = id_claims.get("https://api.openai.com/auth") or {}
    if not auth_claims:
        auth_claims = access_claims.get("https://api.openai.com/auth") or {}

    exp = id_claims.get("exp") or access_claims.get("exp")
    now = int(time.time())
    expired = bool(exp and exp < now)

    return {
        "logged_in": True,
        "auth_mode": data.get("auth_mode"),
        "email": id_claims.get("email") or access_claims.get("email"),
        "name": id_claims.get("name"),
        "plan": auth_claims.get("chatgpt_plan_type"),
        "account_id": auth_claims.get("chatgpt_account_id")
        or tokens.get("account_id"),
        "user_id": auth_claims.get("chatgpt_user_id"),
        "expires_at": exp,
        "expired": expired,
        "last_refresh": data.get("last_refresh"),
        "file_sha256": sha,
        "path": path,
    }


def read_auth_state_bytes(data: bytes) -> dict:
    """Decode auth.json bytes without writing them to disk.

    Used by the switch engine to validate an encrypted snapshot before it
    replaces the live auth file.
    """
    sha = hashlib.sha256(data).hexdigest()
    try:
        payload = json.loads(data.decode("utf-8"))
    except Exception as exc:
        return {
            "logged_in": False,
            "reason": "auth_file_invalid",
            "error": str(exc),
            "file_sha256": sha,
            "path": None,
        }
    if not isinstance(payload, dict):
        return {
            "logged_in": False,
            "reason": "auth_file_invalid",
            "error": "auth.json root must be an object",
            "file_sha256": sha,
            "path": None,
        }
    return _state_from_data(payload, sha=sha, path=None)


def read_auth_state(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {
            "logged_in": False,
            "reason": "auth_file_missing",
            "path": str(path),
            "file_sha256": None,
        }
    try:
        data = path.read_bytes()
        payload = json.loads(data.decode("utf-8"))
    except Exception as exc:
        return {
            "logged_in": False,
            "reason": "auth_file_invalid",
            "error": str(exc),
            "path": str(path),
            "file_sha256": sha256_file(path),
        }
    if not isinstance(payload, dict):
        return {
            "logged_in": False,
            "reason": "auth_file_invalid",
            "error": "auth.json root must be an object",
            "path": str(path),
            "file_sha256": sha256_file(path),
        }
    return _state_from_data(
        payload,
        sha=hashlib.sha256(data).hexdigest(),
        path=str(path),
    )
