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
from collections import OrderedDict
from concurrent.futures import CancelledError, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import config
from .auth_state import read_auth_state_bytes
from .identity import profile_ref, same_identity
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
    state = read_auth_state_bytes(auth_bytes)
    if state.get("identity_error"):
        return None, None
    account_id = state.get("account_id")
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
    """Shared identity cache with bounded refreshes and fresh decision reads.

    Display callers may opt into background refresh. Switching/coupon callers
    keep the synchronous default and never receive an expired success value.
    Current-account and pool views share a fetch only for identical credentials.
    """

    def __init__(
        self,
        store,
        auth_path: Path,
        *,
        ttl_seconds: float | None = None,
        fetch_func: Callable[[bytes], dict] | None = None,
        auth_reader: Callable[[Path], bytes | None] | None = None,
        max_workers: int = 4,
        max_pending: int = 16,
        wait_timeout: float = 75.0,
        clock=None,
    ):
        self.store = store
        self.auth_path = Path(auth_path)
        self._ttl_override = float(ttl_seconds) if ttl_seconds is not None else None
        self._fetch_func = fetch_func or fetch_quota
        self._auth_reader = auth_reader or self._read_live
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._cache: dict[str, dict] = {}
        self._workers = max(1, int(max_workers))
        self._max_pending = max(self._workers, int(max_pending))
        self._executor = None
        self._futures = set()
        self._deferred = OrderedDict()
        self._sync_waiters = 0
        self._wait_timeout = max(.01, float(wait_timeout))
        self._closed = False

    @property
    def ttl_seconds(self):
        # Settings changes are advertised as hot, so do not freeze the default
        # when the service is constructed. Explicit test/caller overrides stay.
        return self._ttl_override if self._ttl_override is not None else float(config.quota_cache_ttl_seconds())

    def close(self):
        with self._condition:
            self._closed = True
            self._deferred.clear()
            executor = self._executor
            self._condition.notify_all()
        if executor is not None:
            # Cancel queued work and finish the bounded, already running HTTP
            # reads before the server closes its account-store connection.
            executor.shutdown(wait=True, cancel_futures=True)

    @staticmethod
    def _read_live(path: Path) -> bytes | None:
        try:
            return Path(path).read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            return None

    def _refresh(self, key, entry, auth_bytes, state, source):
        payload = self._fetch_safe(auth_bytes, source=source, email=state.get("email"))
        return self._store_result(key, entry, payload)

    def _refresh_deferred(self, key, entry, state, source, target):
        # A waiting display refresh retains only an identity and credential
        # hash, not auth bytes. Re-read credentials when a worker becomes free.
        try:
            raw = self.auth_bytes_for(target) if target else self._auth_reader(self.auth_path)
            current = read_auth_state_bytes(raw) if raw else {}
            if not raw or _sha256(raw) != entry["sha"] or profile_ref(current).lower() != key:
                result = _error("quota_invalidated", "额度或登录凭据已变更，请重新刷新。")
                return self._store_result(key, entry, result)
        except Exception:  # noqa: BLE001 - removed/ambiguous profiles are data
            return self._store_result(key, entry, _error("quota_invalidated", "账号或登录凭据已变更，请重新刷新。"))
        return self._refresh(key, entry, raw, state, source)

    def _store_result(self, key, entry, payload):
        now = self._clock()
        with self._condition:
            accepted = not self._closed and self._cache.get(key) is entry
            if accepted:
                entry.update(payload=dict(payload), stored=now)
                if payload.get("ok"):
                    entry.update(good=dict(payload), good_stored=now)
        # Persist a throttled sample for the dashboard trend chart. Sampling is
        # best-effort: a store error must never break a quota probe.
        try:
            if accepted and payload.get("ok") and hasattr(self.store, "record_quota_sample"):
                self.store.record_quota_sample(
                    key or payload.get("email") or "", payload
                )
        except Exception:  # noqa: BLE001 - analytics must stay optional
            pass
        return payload

    def _finished(self, entry, future):
        with self._condition:
            if entry.get("future") is future:
                entry.update(future=None, requested=False)
            self._futures.discard(future)
            self._drain_deferred()
            self._condition.notify_all()

    def _drain_deferred(self):
        # FIFO completion-driven admission avoids starving the end of a large
        # account pool when early entries expire before its next display poll.
        # Leave the next freed slot to a synchronous switching/coupon reader.
        while not self._closed and not self._sync_waiters and self._deferred and len(self._futures) < self._max_pending:
            key, (entry, state, source, target) = self._deferred.popitem(last=False)
            if self._cache.get(key) is entry and entry.get("requested") and entry.get("future") is None:
                self._schedule(key, entry, None, state, source, target=target)

    def _schedule(self, key, entry, auth_bytes, state, source, *, target=None, synchronous=False):
        if self._closed or entry.get("future") is not None or len(self._futures) >= self._max_pending:
            return
        if not synchronous and auth_bytes is not None and (self._sync_waiters or self._deferred):
            return
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="quota-refresh")
        if auth_bytes is None:
            future = self._executor.submit(self._refresh_deferred, key, entry, state, source, target)
        else:
            future = self._executor.submit(self._refresh, key, entry, auth_bytes, state, source)
        self._deferred.pop(key, None)
        entry["future"] = future
        self._futures.add(future)
        future.add_done_callback(lambda done: self._finished(entry, done))

    def _display(self, entry, state, source, max_age):
        payload = entry.get("good") or entry.get("payload")
        stored = entry.get("good_stored") if entry.get("good") else entry.get("stored")
        age = max(0.0, self._clock() - stored) if stored is not None else None
        if payload is None:
            result = {"ok": False, "error": "quota_pending", "message": "正在刷新额度", "fetched_at": None,
                      "profile_id": state.get("profile_id"), "workspace_id": state.get("account_id"), "email": state.get("email")}
        else:
            result = dict(payload)
        pending = bool(entry.get("requested") or entry.get("future"))
        latest = entry.get("payload") or {}
        failed = bool(latest and not latest.get("ok"))
        result.update(source=source, pending=pending, refreshing=entry.get("future") is not None,
                      stale=bool(age is None or age > max_age or failed or pending),
                      cache_age_seconds=round(age, 3) if age is not None else None)
        if failed and entry.get("good"):
            result["refresh_error"] = {field: latest.get(field) for field in ("error", "message", "status_code", "fetched_at")}
        return result

    def _get(self, auth_bytes, state, *, source, force, ttl=None, background=False, target=None):
        key, sha = profile_ref(state).lower(), _sha256(auth_bytes)
        max_age = self.ttl_seconds if ttl is None else max(0.0, float(ttl))
        # TTL=0 still means fresh synchronous reads. Display polling needs a
        # brief observation window in which a completed refresh can be received.
        display_age = max(1.0, max_age) if background else max_age
        deadline = time.monotonic() + self._wait_timeout
        with self._condition:
            if self._closed:
                return _error("service_closed", "额度服务已停止。")
            entry = self._cache.get(key)
            if entry is None or entry.get("sha") != sha:
                entry = {"sha": sha, "email": state.get("email"), "requested": False, "future": None}
                self._cache[key] = entry
                self._deferred.pop(key, None)
            payload = entry.get("payload")
            age = self._clock() - entry.get("stored", self._clock())
            fresh = payload is not None and age <= display_age
            if force or not fresh:
                entry["requested"] = True
            if not background and fresh and not entry.get("requested") and entry.get("future") is None:
                return {**payload, "source": source}
            if background:
                if entry.get("requested") and entry.get("future") is None:
                    self._schedule(key, entry, auth_bytes, state, source, target=target)
                    if entry.get("future") is None:
                        self._deferred.setdefault(key, (entry, state, source, target))
                        self._drain_deferred()
            else:
                self._sync_waiters += 1
                try:
                    while entry.get("requested") and entry.get("future") is None:
                        self._schedule(key, entry, auth_bytes, state, source, target=target, synchronous=True)
                        if entry.get("future") is not None:
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return _error("refresh_busy", "额度刷新繁忙，请稍后重试。")
                        self._condition.wait(timeout=min(.5, remaining))
                        if self._closed:
                            return _error("service_closed", "额度服务已停止。")
                        if self._cache.get(key) is not entry:
                            return _error("quota_invalidated", "额度或登录凭据已变更，请重新刷新。")
                finally:
                    self._sync_waiters -= 1
                    self._drain_deferred()
            if background:
                return self._display(entry, state, source, display_age)
            future = entry.get("future")
            if future is None:
                return {**(entry.get("payload") or _error("quota_unavailable", "额度暂不可用。")), "source": source}
        try:
            result = future.result(timeout=max(0, deadline - time.monotonic()))
        except CancelledError:
            return _error("service_closed", "额度服务已停止。")
        except TimeoutError:
            return _error("refresh_timeout", "等待额度刷新超时，请稍后重试。")
        with self._condition:
            if self._cache.get(key) is not entry:
                return _error("quota_invalidated", "额度或登录凭据已变更，请重新刷新。")
        return {**result, "source": source}

    def invalidate(self, email: str | None = None) -> None:
        with self._condition:
            if email is None:
                self._cache.clear()
                self._deferred.clear()
                self._condition.notify_all()
                return
            lowered = email.lower()
            for key in [key for key, entry in self._cache.items()
                        if key == lowered or str(entry.get("email") or "").lower() == lowered]:
                self._cache.pop(key, None)
                self._deferred.pop(key, None)
            self._condition.notify_all()

    def _fetch_safe(self, auth_bytes: bytes, *, source: str, email: str | None) -> dict:
        state = read_auth_state_bytes(auth_bytes)
        if state.get("identity_error"):
            return _error("identity_conflict", "登录凭据的工作区信息不一致，请重新授权。")
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
        payload["profile_id"] = state.get("profile_id")
        payload["workspace_id"] = state.get("account_id")
        if email and not payload.get("email"):
            payload["email"] = email
        payload.setdefault("fetched_at", _now_iso())
        return payload

    def current(self, force: bool = False, *, background: bool = False) -> dict:
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
        return self._get(auth_bytes, state, source="live", force=force, background=background)

    def for_email(
        self,
        email: str,
        force: bool = False,
        ttl: float | None = None,
        scope: str = "live",
        background: bool = False,
    ) -> dict:
        email = (email or "").strip()
        if not email:
            return _error("email_required", "email 不能为空。")

        try:
            snapshot = self.auth_bytes_for(email)
        except ValueError as exc:
            return _error(getattr(exc, "code", "identity_conflict"), str(exc))
        if snapshot is None:
            return _error("snapshot_not_found", f"账号池里没有 {email} 的 auth 快照。")
        state = read_auth_state_bytes(snapshot)
        live = self._auth_reader(self.auth_path)
        source = "live" if live == snapshot else "snapshot"
        # scope remains accepted for old callers; freshness is a per-read TTL,
        # while identical identity/credentials now share cache and in-flight work.
        return self._get(snapshot, state, source=source, force=force, ttl=ttl, background=background, target=email)

    # ---------------------------------------------------------- reset credits
    def auth_bytes_for(self, email: str | None = None) -> bytes | None:
        """Resolve a profile (or unambiguous legacy email) before using live auth."""
        target = (email or "").strip()
        resolve = getattr(self.store, "resolve_auth_profile", None)
        profile = resolve(target) if target and callable(resolve) else None
        if profile and profile.get("identity_error"):
            raise ValueError("工作区快照身份不一致，请重新登录后操作")
        auth_bytes = self._auth_reader(self.auth_path)
        if auth_bytes:
            state = read_auth_state_bytes(auth_bytes)
            current_email = state.get("email") or ""
            matches = same_identity(state, profile) if profile else (
                not target or profile_ref(state) == target or current_email.lower() == target.lower())
            if state.get("logged_in") and not state.get("identity_error") and matches:
                return auth_bytes
        if target:
            raw = self.store.get_auth_snapshot(target)
            if raw:
                state = read_auth_state_bytes(raw)
                if state.get("identity_error") or profile and (
                    not same_identity(state, profile) or
                    str(state.get("email") or "").casefold() != str(profile.get("email") or "").casefold()
                ):
                    raise ValueError("工作区快照身份不一致，请重新登录后操作")
            return raw
        return None

    def reset_credits(self, email: str | None = None) -> dict:
        """How many "Full reset" credits ``email`` (or the live account) has."""
        try:
            auth_bytes = self.auth_bytes_for(email)
        except ValueError as exc:
            return _error(getattr(exc, "code", "identity_conflict"), str(exc))
        if not auth_bytes:
            return _error("no_auth_file", "没有可用的 auth 数据。")
        try:
            payload = fetch_reset_credits(auth_bytes)
        except Exception as exc:  # noqa: BLE001 - never break the dashboard
            return _error("request_failed", "重置券查询失败。", detail=str(exc)[:200])
        state = read_auth_state_bytes(auth_bytes)
        payload.setdefault("email", state.get("email"))
        payload["profile_id"] = state.get("profile_id")
        return payload

    def consume_reset(
        self,
        *,
        email: str | None = None,
        credit_id: str | None = None,
        redeem_request_id: str | None = None,
    ) -> dict:
        """Redeem one reset credit for ``email`` (default: the live account)."""
        try:
            auth_bytes = self.auth_bytes_for(email)
        except ValueError as exc:
            return _error(getattr(exc, "code", "identity_conflict"), str(exc))
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
        state = read_auth_state_bytes(auth_bytes)
        result["email"] = state.get("email")
        result["profile_id"] = state.get("profile_id")
        result["redeem_request_id"] = redeem_request_id
        if result.get("ok"):
            # Both usage windows just changed; never serve the stale snapshot.
            self.invalidate()
        return result
