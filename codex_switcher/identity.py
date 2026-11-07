"""Non-secret identity keys shared by storage, switching and quota lookup."""

from __future__ import annotations

import hashlib
import json


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def profile_id_for(state: dict | None) -> str:
    """Identify a member within a workspace, independently of token rotation."""
    state = state or {}
    if state.get("identity_error"):
        return ""
    user = _text(state.get("user_id"))
    email = _text(state.get("email")).casefold()
    workspace = _text(state.get("workspace_id") or state.get("account_id"))
    if not user and not email:
        return ""
    key = ["user" if user else "email", user or email, workspace]
    digest = hashlib.sha256(json.dumps(key, separators=(",", ":")).encode("utf-8")).hexdigest()
    return "profile_" + digest[:32]


def profile_ref(state: dict | None) -> str:
    """Use a concrete profile; old callers without identity metadata keep email."""
    state = state or {}
    if state.get("identity_error"):
        return ""
    return _text(state.get("profile_id")) or _text(state.get("email"))


def same_identity(left: dict | None, right: dict | None) -> bool:
    """Never treat two known workspaces or members as the same login identity."""
    left, right = left or {}, right or {}
    if left.get("identity_error") or right.get("identity_error"):
        return False
    left_workspace = _text(left.get("workspace_id") or left.get("account_id"))
    right_workspace = _text(right.get("workspace_id") or right.get("account_id"))
    if left_workspace != right_workspace:
        return False
    left_user, right_user = _text(left.get("user_id")), _text(right.get("user_id"))
    if left_user and right_user:
        return left_user == right_user
    left_email, right_email = _text(left.get("email")).casefold(), _text(right.get("email")).casefold()
    left_id, right_id = _text(left.get("profile_id")), _text(right.get("profile_id"))
    if left_id and right_id and left_id != right_id and not left_workspace:
        return False
    if left_email and right_email:
        return left_email == right_email
    return bool(left_id and left_id == right_id)
