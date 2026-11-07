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

from .identity import profile_id_for


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * ((4 - len(segment) % 4) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def decode_jwt_payload(token: str | None) -> dict:
    if not isinstance(token, str) or token.count(".") < 2:
        return {}
    try:
        payload = token.split(".")[1]
        decoded = json.loads(_b64url_decode(payload).decode("utf-8"))
        return decoded if isinstance(decoded, dict) else {}
    except Exception:
        return {}


def sha256_file(path: Path) -> str | None:
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return None
    return hashlib.sha256(data).hexdigest()


def _state_from_data(data: dict, *, sha: str | None, path: str | None) -> dict:
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    id_claims = decode_jwt_payload(tokens.get("id_token"))
    access_claims = decode_jwt_payload(tokens.get("access_token"))
    id_auth = id_claims.get("https://api.openai.com/auth")
    access_auth = access_claims.get("https://api.openai.com/auth")
    id_auth = id_auth if isinstance(id_auth, dict) else {}
    access_auth = access_auth if isinstance(access_auth, dict) else {}

    def values(*items):
        return [item.strip() for item in items if isinstance(item, str) and item.strip()]

    accounts = values(tokens.get("account_id"), data.get("account_id"),
                      id_auth.get("chatgpt_account_id"), access_auth.get("chatgpt_account_id"))
    users = values(id_auth.get("chatgpt_user_id"), access_auth.get("chatgpt_user_id"))
    emails = values(id_claims.get("email"), access_claims.get("email"))
    identity_error = None
    if len(set(accounts)) > 1:
        identity_error = "workspace_identity_conflict"
    elif len(set(users)) > 1:
        identity_error = "user_identity_conflict"
    elif len({email.casefold() for email in emails}) > 1:
        identity_error = "email_identity_conflict"
    exp = id_claims.get("exp") or access_claims.get("exp")
    try:
        exp = int(exp) if exp is not None else None
    except (ValueError, TypeError):
        exp = None
    workspace = accounts[0] if accounts else None
    state = {
        "logged_in": not bool(identity_error),
        "auth_mode": data.get("auth_mode"),
        "email": emails[0] if emails else None,
        "name": id_claims.get("name"),
        "plan": id_auth.get("chatgpt_plan_type") or access_auth.get("chatgpt_plan_type"),
        "account_id": workspace,
        "workspace_id": workspace,
        "workspace_name": id_auth.get("chatgpt_account_name") or access_auth.get("chatgpt_account_name")
                          or id_auth.get("workspace_name") or access_auth.get("workspace_name"),
        "user_id": users[0] if users else None,
        "identity_error": identity_error,
        "expires_at": exp,
        "expired": bool(exp and exp < int(time.time())),
        "last_refresh": data.get("last_refresh"),
        "file_sha256": sha,
        "path": path,
    }
    if identity_error:
        state["reason"] = "auth_identity_conflict"
    state["profile_id"] = profile_id_for(state) or None
    return state


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
