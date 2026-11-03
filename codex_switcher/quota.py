"""Read-only quota probe for the current Codex account.

The live auth token is used only in the Authorization header of the official
ChatGPT usage endpoint. It is never returned, logged, cached, or written to
disk. The public payload is reduced to display fields (plan, percentage and
reset times) so the dashboard and userscript can show quota state safely.

The endpoint is configurable through ``CODEX_SWITCHER_USAGE_URL`` for tests
and for future API changes. Network failures are reported as data, not raised,
so a quota outage never breaks account switching or the dashboard.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import config
from .auth_state import read_auth_state_bytes
from .quota_state import number, usage_state

DEFAULT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
DEFAULT_RESET_CREDITS_URL = (
    "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
)
USER_AGENT = "codex-switcher/0.4 quota-probe"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _error(code: str, message: str, **extra) -> dict:
    payload = {
        "ok": False,
        "error": code,
        "message": message,
        "fetched_at": _now_iso(),
    }
    payload.update(extra)
    return payload


def _window(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    return {
        "used_percent": number(raw.get("used_percent"), maximum=100),
        "window_seconds": number(raw.get("limit_window_seconds"), minimum=1),
        "reset_at": number(raw.get("reset_at"), minimum=1),
        "reset_after_seconds": number(raw.get("reset_after_seconds")),
    }


def _extract_token(auth_bytes: bytes) -> tuple[str | None, str | None]:
    try:
        data = json.loads(auth_bytes.decode("utf-8"))
    except Exception:
        return None, None
    if not isinstance(data, dict):
        return None, None
    tokens = data.get("tokens") or {}
    if not isinstance(tokens, dict):
        tokens = {}
    token = tokens.get("access_token")
    account_id = tokens.get("account_id") or data.get("account_id")
    if not isinstance(token, str):
        token = None
    if not isinstance(account_id, str):
        account_id = None
    return token, account_id


def sanitize_usage(
    payload: dict,
    *,
    email: str | None = None,
    account_id: str | None = None,
    source: str = "live",
) -> dict:
    """Reduce the upstream response to the public, non-secret display schema."""
    rate_limit = payload.get("rate_limit") if isinstance(payload, dict) else None
    if not isinstance(rate_limit, dict):
        rate_limit = {}
    limit_reached = rate_limit.get("limit_reached") if type(rate_limit.get("limit_reached")) is bool else None
    allowed = rate_limit.get("allowed") if type(rate_limit.get("allowed")) is bool else None

    credits = payload.get("credits") if isinstance(payload, dict) else None
    if not isinstance(credits, dict):
        credits = {}
    upsell = payload.get("rate_limit_upsell") if isinstance(payload, dict) else None
    if not isinstance(upsell, dict):
        upsell = {}

    reached_type = payload.get("rate_limit_reached_type") if isinstance(payload, dict) else None
    if isinstance(reached_type, dict):
        reached_type = reached_type.get("type")

    raw_reset = payload.get("rate_limit_reset_credits") if isinstance(payload, dict) else None
    reset_credits = {
        "known": isinstance(raw_reset, dict),
        "available_count": 0,
        "applicable_available_count": 0,
    }
    if isinstance(raw_reset, dict):
        for key in ("available_count", "applicable_available_count"):
            try:
                reset_credits[key] = int(raw_reset.get(key) or 0)
            except (TypeError, ValueError):
                reset_credits[key] = 0

    result = {
        "ok": True,
        "source": source,
        "email": email or (payload.get("email") if isinstance(payload, dict) else None),
        "plan": payload.get("plan_type") if isinstance(payload, dict) else None,
        "account_id": account_id or (payload.get("account_id") if isinstance(payload, dict) else None),
        "allowed": allowed,
        "limit_reached": limit_reached,
        "primary_window": _window(rate_limit.get("primary_window")),
        "secondary_window": _window(rate_limit.get("secondary_window")),
        "window_states": {key: ("not_applicable" if key in rate_limit and rate_limit[key] is None else
                          "known" if isinstance(rate_limit.get(key), dict) and number(rate_limit[key].get("used_percent"), maximum=100) is not None else "unknown")
                          for key in ("primary_window", "secondary_window")},
        "credits": {
            "has_credits": bool(credits.get("has_credits")),
            "unlimited": bool(credits.get("unlimited")),
        },
        "reset_credits": reset_credits,
        "upsell": {
            "title": upsell.get("title"),
            "description": upsell.get("description"),
            "reset_at": upsell.get("reset_at"),
        }
        if upsell
        else None,
        "rate_limit_reached_type": reached_type,
        "fetched_at": _now_iso(),
    }
    result["availability"] = usage_state(result)
    return result


def fetch_quota(
    auth_bytes: bytes,
    *,
    timeout: float = 15.0,
    url: str | None = None,
) -> dict:
    """Call the usage endpoint with the auth token and return a safe summary."""
    token, account_id = _extract_token(auth_bytes)
    if not token:
        return _error(
            "no_chatgpt_token",
            "auth.json 里没有可用的 ChatGPT access token。",
        )

    endpoint = url or config.usage_url() or DEFAULT_USAGE_URL
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if account_id:
        headers["chatgpt-account-id"] = account_id
    request = urllib.request.Request(endpoint, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status_code = response.status
            body = response.read()
    except urllib.error.HTTPError as exc:
        code = exc.code
        if code in (401, 403):
            return _error(
                "auth_error",
                "额度接口拒绝了当前 token（可能需要重新登录）。",
                status_code=code,
            )
        if code == 429:
            return _error(
                "upstream_rate_limited",
                "额度接口暂时限流，请稍后重试。",
                status_code=code,
            )
        return _error(
            "http_error",
            f"额度接口返回 HTTP {code}。",
            status_code=code,
        )
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, TimeoutError):
            return _error("timeout", "额度接口请求超时。")
        return _error("network_error", "无法连接额度接口。", detail=str(reason)[:200])
    except TimeoutError:
        return _error("timeout", "额度接口请求超时。")
    except Exception as exc:  # noqa: BLE001 - a quota outage must not crash the service
        return _error("request_failed", "额度接口请求失败。", detail=str(exc)[:200])

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return _error(
            "invalid_response",
            "额度接口返回了无法解析的响应。",
            status_code=status_code,
        )
    if not isinstance(payload, dict):
        return _error("invalid_response", "额度接口响应不是 JSON 对象。", status_code=status_code)

    return sanitize_usage(
        payload,
        account_id=account_id,
        source="live",
    )


def _headers(token: str, account_id: str | None, *, json_body: bool = False) -> dict:
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if account_id:
        headers["chatgpt-account-id"] = account_id
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _http_json(
    endpoint: str,
    token: str,
    account_id: str | None,
    *,
    method: str = "GET",
    body: dict | None = None,
    timeout: float = 15.0,
) -> tuple[int, dict | None, dict | None]:
    """Call an endpoint and return ``(status, payload, error)``; never raises."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers=_headers(token, account_id, json_body=body is not None),
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status_code = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status_code = exc.code
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001 - the body is only used for diagnostics
            raw = b""
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, TimeoutError):
            return 0, None, _error("timeout", "额度接口请求超时。")
        return 0, None, _error("network_error", "无法连接额度接口。", detail=str(reason)[:200])
    except TimeoutError:
        return 0, None, _error("timeout", "额度接口请求超时。")
    except Exception as exc:  # noqa: BLE001 - a quota outage must not crash the service
        return 0, None, _error("request_failed", "额度接口请求失败。", detail=str(exc)[:200])

    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        return status_code, None, _error(
            "invalid_response",
            "额度接口返回了无法解析的响应。",
            status_code=status_code,
        )
    return status_code, payload, None


def _credit_list(payload: dict) -> list[dict]:
    credits = payload.get("credits") if isinstance(payload, dict) else None
    if not isinstance(credits, list):
        return []
    result = []
    for item in credits:
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "id": item.get("id"),
                "status": item.get("status"),
                "reset_type": item.get("reset_type"),
                "title": item.get("title"),
                "expires_at": item.get("expires_at"),
                "redeemed_at": item.get("redeemed_at"),
            }
        )
    return result


def fetch_reset_credits(
    auth_bytes: bytes,
    *,
    timeout: float = 15.0,
    url: str | None = None,
) -> dict:
    """List the account's rate limit reset credits (read-only)."""
    token, account_id = _extract_token(auth_bytes)
    if not token:
        return _error("no_chatgpt_token", "auth.json 里没有可用的 ChatGPT access token。")

    endpoint = url or config.reset_credits_url() or DEFAULT_RESET_CREDITS_URL
    status_code, payload, error = _http_json(
        endpoint, token, account_id, timeout=timeout
    )
    if error is not None:
        return error
    assert payload is not None
    if status_code >= 400:
        return _error(
            "auth_error" if status_code in (401, 403) else "http_error",
            f"重置券接口返回 HTTP {status_code}。",
            status_code=status_code,
        )
    credits = _credit_list(payload)
    available = [item for item in credits if item.get("status") == "available"]
    try:
        count = int(payload.get("available_count"))
    except (TypeError, ValueError):
        count = len(available)
    expires = sorted(
        item["expires_at"] for item in available if item.get("expires_at")
    )
    return {
        "ok": True,
        "available_count": max(count, len(available)),
        "credits": credits,
        "next_expires_at": expires[0] if expires else None,
        "history_enabled": bool(payload.get("history_enabled")),
        "status_code": status_code,
        "fetched_at": _now_iso(),
    }


def consume_reset_credit(
    auth_bytes: bytes,
    credit_id: str,
    redeem_request_id: str,
    *,
    timeout: float = 30.0,
    url: str | None = None,
) -> dict:
    """Redeem exactly one reset credit.

    The call is irreversible, so ``redeem_request_id`` is a caller-supplied
    UUID that makes retries idempotent: replaying the same id returns the
    original outcome (``already_redeemed``) instead of burning a second credit.
    """
    token, account_id = _extract_token(auth_bytes)
    if not token:
        return _error("no_chatgpt_token", "auth.json 里没有可用的 ChatGPT access token。")
    if not credit_id:
        return _error("credit_required", "缺少 credit_id。")
    if not redeem_request_id:
        return _error("request_id_required", "缺少 redeem_request_id。")

    base = url or config.reset_credits_url() or DEFAULT_RESET_CREDITS_URL
    endpoint = base.rstrip("/") + "/consume"
    status_code, payload, error = _http_json(
        endpoint,
        token,
        account_id,
        method="POST",
        body={"credit_id": credit_id, "redeem_request_id": redeem_request_id},
        timeout=timeout,
    )
    if error is not None:
        return error
    assert payload is not None
    code = payload.get("code")
    if code != "reset":
        known = {"nothing_to_reset", "no_credit", "already_redeemed"}
        if code in known:
            error_code = code
        elif status_code >= 400:
            error_code = "auth_error" if status_code in (401, 403) else "http_error"
        else:
            error_code = "consume_failed"
        return _error(
            error_code,
            {
                "nothing_to_reset": "当前用量还不需要重置。",
                "no_credit": "账号没有可用的重置券。",
                "already_redeemed": "这张重置券已经被用掉了。",
                "auth_error": "重置接口拒绝了当前 token（可能需要重新登录）。",
                "http_error": f"重置接口返回 HTTP {status_code}。",
            }.get(error_code, "重置请求被上游拒绝。"),
            upstream_code=code,
            status_code=status_code,
        )
    credit = payload.get("credit") if isinstance(payload.get("credit"), dict) else {}
    return {
        "ok": True,
        "code": "reset",
        "credit_id": credit.get("id") or credit_id,
        "redeemed_at": credit.get("redeemed_at"),
        "windows_reset": payload.get("windows_reset"),
        "status_code": status_code,
        "fetched_at": _now_iso(),
    }


class QuotaService:
    """Small per-email cache around the quota endpoint.

    key includes the auth file hash: if Codex refreshes a token, the next call
    fetches fresh usage instead of returning the old window.
    """

    def __init__(
        self,
        store,
        auth_path: Path,
        *,
        ttl_seconds: float | None = None,
        fetch_func: Callable[[bytes], dict] | None = None,
        auth_reader: Callable[[Path], bytes | None] | None = None,
    ):
        self.store = store
        self.auth_path = Path(auth_path)
        self.ttl_seconds = (
            float(ttl_seconds)
            if ttl_seconds is not None
            else float(config.quota_cache_ttl_seconds())
        )
        self._fetch_func = fetch_func or fetch_quota
        self._auth_reader = auth_reader or self._read_live
        self._lock = threading.Lock()
        self._cache: dict[str, dict] = {}

    @staticmethod
    def _read_live(path: Path) -> bytes | None:
        try:
            return Path(path).read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            return None

    def _cached(
        self,
        email: str,
        auth_sha: str,
        force: bool,
        scope: str = "live",
        ttl: float | None = None,
    ) -> dict | None:
        if force:
            return None
        key = (scope, (email or "").lower())
        max_age = self.ttl_seconds if ttl is None else float(ttl)
        with self._lock:
            item = self._cache.get(key)
            if not item:
                return None
            if item.get("sha") != auth_sha:
                return None
            if item.get("stored", 0) + max_age < time.time():
                return None
            return dict(item["payload"])

    def _remember(
        self, email: str, auth_sha: str, payload: dict, scope: str = "live"
    ) -> dict:
        key = (scope, (email or "").lower())
        now = time.time()
        with self._lock:
            self._cache[key] = {
                "sha": auth_sha,
                "stored": now,
                "expires": now + self.ttl_seconds,
                "payload": dict(payload),
            }
        # Persist a throttled sample for the dashboard trend chart. Sampling is
        # best-effort: a store error must never break a quota probe.
        try:
            if payload.get("ok") and hasattr(self.store, "record_quota_sample"):
                self.store.record_quota_sample(
                    email or payload.get("email") or "", payload
                )
        except Exception:  # noqa: BLE001 - analytics must stay optional
            pass
        return payload

    def invalidate(self, email: str | None = None) -> None:
        with self._lock:
            if email is None:
                self._cache.clear()
                return
            lowered = email.lower()
            for key in [item for item in self._cache if item[1] == lowered]:
                self._cache.pop(key, None)

    def _fetch_safe(self, auth_bytes: bytes, *, source: str, email: str | None) -> dict:
        try:
            payload = self._fetch_func(auth_bytes)
        except Exception as exc:  # noqa: BLE001
            return _error("request_failed", "额度检查失败。", detail=str(exc)[:200])
        if not isinstance(payload, dict):
            return _error("invalid_response", "额度检查返回了无效数据。")
        payload = dict(payload)
        payload.setdefault("ok", False)
        # ``source`` describes where the auth bytes came from (live auth.json
        # vs. an encrypted snapshot), not which HTTP client fetched them.
        payload["source"] = source
        if email and not payload.get("email"):
            payload["email"] = email
        payload.setdefault("fetched_at", _now_iso())
        return payload

    def current(self, force: bool = False) -> dict:
        auth_bytes = self._auth_reader(self.auth_path)
        if not auth_bytes:
            return _error("no_auth_file", "当前没有可读取的 auth.json。")
        state = read_auth_state_bytes(auth_bytes)
        if not state.get("logged_in"):
            return _error(
                "not_logged_in",
                "当前 auth.json 不是有效登录状态。",
                detail=state.get("reason") or state.get("error"),
            )
        email = state.get("email") or ""
        sha = _sha256(auth_bytes)
        cached = self._cached(email, sha, force)
        if cached is not None:
            return cached
        payload = self._fetch_safe(auth_bytes, source="live", email=email)
        return self._remember(email, sha, payload)

    def for_email(
        self,
        email: str,
        force: bool = False,
        ttl: float | None = None,
        scope: str = "live",
    ) -> dict:
        email = (email or "").strip()
        if not email:
            return _error("email_required", "email 不能为空。")

        auth_bytes = self._auth_reader(self.auth_path)
        if auth_bytes:
            state = read_auth_state_bytes(auth_bytes)
            current_email = state.get("email") or ""
            if state.get("logged_in") and current_email.lower() == email.lower():
                if scope == "live":
                    return self.current(force=force)
                # Pool view keeps its own longer-lived entry even for the
                # account that happens to be active, so the dashboard does not
                # re-hit the upstream endpoint on every poll.
                sha = _sha256(auth_bytes)
                cached = self._cached(email, sha, force, scope=scope, ttl=ttl)
                if cached is not None:
                    return cached
                payload = self._fetch_safe(auth_bytes, source="live", email=email)
                return self._remember(email, sha, payload, scope=scope)

        snapshot = self.store.get_auth_snapshot(email)
        if snapshot is None:
            return _error("snapshot_not_found", f"账号池里没有 {email} 的 auth 快照。")

        sha = _sha256(snapshot)
        cached = self._cached(email, sha, force, scope=scope, ttl=ttl)
        if cached is not None:
            return cached
        payload = self._fetch_safe(snapshot, source="snapshot", email=email)
        return self._remember(email, sha, payload, scope=scope)

    # ---------------------------------------------------------- reset credits
    def auth_bytes_for(self, email: str | None = None) -> bytes | None:
        """Auth bytes for ``email``: live file when it matches, else snapshot."""
        target = (email or "").strip()
        auth_bytes = self._auth_reader(self.auth_path)
        if auth_bytes:
            state = read_auth_state_bytes(auth_bytes)
            current_email = state.get("email") or ""
            if state.get("logged_in") and (
                not target or current_email.lower() == target.lower()
            ):
                return auth_bytes
        if target:
            try:
                return self.store.get_auth_snapshot(target)
            except Exception:  # noqa: BLE001 - missing store must not raise
                return None
        return auth_bytes

    def reset_credits(self, email: str | None = None) -> dict:
        """How many "Full reset" credits ``email`` (or the live account) has."""
        auth_bytes = self.auth_bytes_for(email)
        if not auth_bytes:
            return _error("no_auth_file", "没有可用的 auth 数据。")
        try:
            payload = fetch_reset_credits(auth_bytes)
        except Exception as exc:  # noqa: BLE001 - never break the dashboard
            return _error("request_failed", "重置券查询失败。", detail=str(exc)[:200])
        if email and not payload.get("email"):
            payload["email"] = email
        return payload

    def consume_reset(
        self,
        *,
        email: str | None = None,
        credit_id: str | None = None,
        redeem_request_id: str | None = None,
    ) -> dict:
        """Redeem one reset credit for ``email`` (default: the live account)."""
        auth_bytes = self.auth_bytes_for(email)
        if not auth_bytes:
            return _error("no_auth_file", "没有可用的 auth 数据。")

        if not credit_id:
            try:
                listing = fetch_reset_credits(auth_bytes)
            except Exception as exc:  # noqa: BLE001
                return _error(
                    "request_failed", "重置券查询失败。", detail=str(exc)[:200]
                )
            if not listing.get("ok"):
                return listing
            available = [
                item
                for item in (listing.get("credits") or [])
                if item.get("status") == "available" and item.get("id")
            ]
            if not available:
                return _error("no_credit", "账号没有可用的重置券。")
            # Spend the credit that expires first so nothing rots unused.
            available.sort(key=lambda item: item.get("expires_at") or "~")
            credit_id = available[0]["id"]

        if not redeem_request_id:
            redeem_request_id = str(uuid.uuid4())

        result = consume_reset_credit(auth_bytes, credit_id, redeem_request_id)
        result["email"] = (email or "").strip() or None
        result["redeem_request_id"] = redeem_request_id
        if result.get("ok"):
            # Both usage windows just changed; never serve the stale snapshot.
            self.invalidate()
        return result
