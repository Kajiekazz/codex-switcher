"""SMS (接码) client for the phone-verification step of Codex OAuth.

Providers mirror chatgpt-autoregister / turb-gpt-free-register:

* ``grizzly``  – GrizzlySMS ``handler_api.php`` (GET)
* ``smsbower`` – SMSBower / HeroSMS ``handler_api.php`` (GET), service ``dr``
* ``l``        – local JSON admin API (``L_API_BASE`` + ``L_ADMIN_AUTH_CODE``)
* ``h``        – local JSON admin API (``H_API_BASE`` + ``H_ADMIN_AUTH_CODE``)

The module is inert unless ``CODEX_SWITCHER_SMS_PROVIDER`` (or ``SMS_PROVIDER``)
is set. Credentials stay in the environment; nothing is persisted here.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone

from . import config

# GrizzlySMS rejects cancels for the first ~2 minutes after take.
MIN_CANCEL_DELAY = 125

# Provider APIs are fronted by Cloudflare; the urllib default UA is blocked.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_ACQUIRED_LOCK = threading.Lock()
# activation_id -> {"at": epoch, "phone": digits, "service": str, "country": str}
_ACQUIRED: dict[str, dict] = {}
# Recent panel-facing events (newest last) and the last received code.
_EVENTS: deque[dict] = deque(maxlen=100)
_LAST_CODE: dict = {"code": "", "activation_id": "", "at": None}
_BALANCE_CACHE: dict = {"ts": 0.0, "payload": None}
_BALANCE_LOCK = threading.Lock()


class SmsError(Exception):
    """Base error for the SMS provider."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class SmsNoNumbers(SmsError):
    def __init__(self, message: str = "接码平台暂无可用号码"):
        super().__init__("no_numbers", message)


class SmsNoBalance(SmsError):
    def __init__(self, message: str = "接码平台余额不足"):
        super().__init__("no_balance", message)


class SmsTimeout(SmsError):
    def __init__(self, message: str = "等待短信验证码超时"):
        super().__init__("sms_timeout", message)


def provider() -> str:
    return config.sms_provider()


def enabled() -> bool:
    return config.sms_enabled()


def _log(message: str, level: str = "info") -> None:
    sys.stderr.write(f"[sms] {message}\n")
    _EVENTS.append(
        {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": level,
            "message": message,
        }
    )


def _get(url: str, params: dict) -> str:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v != ""})
    sep = "&" if "?" in url else "?"
    request = urllib.request.Request(
        f"{url}{sep}{query}",
        # GrizzlySMS sits behind Cloudflare: the default Python-urllib UA gets
        # a 403 challenge page, a browser UA gets the plain-text API answer.
        headers={"Accept": "text/plain", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(
            request, timeout=config.sms_request_timeout()
        ) as response:
            return response.read().decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise SmsError("http_error", f"接码 HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SmsError("network_error", f"接码网络错误: {exc.reason}") from exc


def _post_json(url: str, payload: dict, token: str) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(
            request, timeout=config.sms_request_timeout()
        ) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        raise SmsError("http_error", f"接码 HTTP {exc.code}: {raw[:200]}") from exc
    except urllib.error.URLError as exc:
        raise SmsError("network_error", f"接码网络错误: {exc.reason}") from exc

    try:
        data = json.loads(raw)
    except ValueError:
        raise SmsError("bad_response", f"接码响应不是 JSON: {raw[:200]}")
    if not isinstance(data, dict):
        raise SmsError("bad_response", f"接码响应格式异常: {raw[:200]}")

    error = str(data.get("error") or "")
    if error:
        combined = f"{error} {data.get('raw') or ''}".strip()
        if "NO_BALANCE" in combined or "余额不足" in combined:
            raise SmsNoBalance(f"接码余额不足: {combined}")
        if "NO_NUMBERS" in combined or "暂无号码" in combined:
            raise SmsNoNumbers(f"接码暂无号码: {combined}")
        raise SmsError("provider_error", f"接码请求失败: {combined[:200]}")
    return data


def _digits(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _with_prefix(phone: str, prefix: str) -> str:
    digits = _digits(phone)
    prefix_digits = _digits(prefix)
    if prefix_digits and digits and not digits.startswith(prefix_digits):
        return f"{prefix_digits}{digits}"
    return digits


def _remember(
    activation_id: str,
    phone: str = "",
    service: str = "",
    country: str = "",
) -> None:
    with _ACQUIRED_LOCK:
        _ACQUIRED[activation_id] = {
            "at": time.time(),
            "phone": _digits(phone),
            "service": str(service or ""),
            "country": str(country or ""),
            "provider": provider(),
        }


def _forget(activation_id: str) -> None:
    with _ACQUIRED_LOCK:
        _ACQUIRED.pop(activation_id, None)


def _record_code(activation_id: str, code: str) -> None:
    code = str(code or "").strip()
    if not code:
        return
    _LAST_CODE.update(
        {
            "code": code,
            "activation_id": activation_id,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    _log(f"收到验证码 id={activation_id} code={code}")


# --------------------------------------------------------------------- grizzly


def _grizzly(params: dict) -> str:
    api_key = config.sms_api_key()
    if not api_key:
        raise SmsError("not_configured", "缺少 SMS_API_KEY（GrizzlySMS）")
    text = _get(config.sms_api_base(), {"api_key": api_key, **params})
    if text == "BAD_KEY":
        raise SmsError("bad_key", "接码 API key 无效（BAD_KEY）")
    if text == "NO_BALANCE":
        raise SmsNoBalance("接码余额不足（NO_BALANCE）")
    if text == "NO_NUMBERS":
        raise SmsNoNumbers("接码暂无可用号码（NO_NUMBERS）")
    if text in ("BAD_ACTION", "BAD_SERVICE", "BAD_STATUS"):
        raise SmsError("bad_request", f"接码请求参数错误：{text}")
    if text == "NO_ACTIVATION":
        raise SmsError("no_activation", "接码激活 ID 不存在（NO_ACTIVATION）")
    if text.startswith("The service is prohibited"):
        raise SmsError("prohibited", f"该服务被平台禁售：{text}")
    return text


# -------------------------------------------------------------------- smsbower


def _smsbower(params: dict) -> str:
    api_key = config.smsbower_api_key()
    if not api_key:
        raise SmsError("not_configured", "缺少 SMSBOWER_API_KEY")
    text = _get(config.smsbower_api_base(), {"api_key": api_key, **params})
    if text == "BAD_KEY":
        raise SmsError("bad_key", "SMSBower API key 无效（BAD_KEY）")
    if text in ("BAD_SERVICE", "WRONG_SERVICE"):
        raise SmsError("bad_request", f"SMSBower 服务代码无效（{text}）")
    if text == "BAD_STATUS":
        raise SmsError("bad_request", "SMSBower 状态参数错误（BAD_STATUS）")
    if text == "NO_ACTIVATION":
        raise SmsError("no_activation", "SMSBower 激活 ID 不存在")
    if text in ("NO_BALANCE", "NO_MONEY"):
        raise SmsNoBalance(f"SMSBower 余额不足（{text}）")
    if text == "NO_NUMBERS":
        raise SmsNoNumbers("SMSBower 暂无可用号码（NO_NUMBERS）")
    if text.startswith("The service is prohibited"):
        raise SmsError("prohibited", f"SMSBower 该服务被禁售：{text}")
    return text


def _smsbower_service(service: str) -> str:
    code = str(service or config.sms_service()).strip()
    if code.lower() in ("openai", "chatgpt"):
        return "dr"
    return code


# ------------------------------------------------------------------------- l/h


def _admin_post(base: str, path: str, token: str, payload: dict) -> dict:
    if not base:
        raise SmsError("not_configured", "接码 API 基址未配置")
    if not token:
        raise SmsError("not_configured", "接码后台授权码未配置")
    url = base.rstrip("/") + "/" + path.lstrip("/")
    return _post_json(url, payload, token)


# ---------------------------------------------------------------- public API


def acquire_number(
    service: str | None = None, country: str | None = None
) -> tuple[str, str]:
    """Take one number. Returns ``(activation_id, phone_digits)``."""
    name = provider()
    if not name:
        raise SmsError(
            "not_configured",
            "接码未启用：设置 CODEX_SWITCHER_SMS_PROVIDER=grizzly|smsbower|l|h",
        )
    service = service or config.sms_service()
    country = country or config.sms_country()

    if name == "grizzly":
        params = {"action": "getNumber", "service": service, "country": country}
        if config.sms_max_price():
            params["maxPrice"] = config.sms_max_price()
        text = _grizzly(params)
        if not text.startswith("ACCESS_NUMBER:"):
            raise SmsError("bad_response", f"getNumber 非预期响应：{text[:120]}")
        parts = text.split(":")
        if len(parts) < 3:
            raise SmsError("bad_response", f"getNumber 响应格式异常：{text[:120]}")
        activation_id, phone = parts[1].strip(), _digits(parts[2])
        _remember(activation_id, phone, service, country)
        _log(f"取号成功 provider=grizzly id={activation_id} phone=+{phone}")
        return activation_id, phone

    if name == "smsbower":
        use_v2 = config.smsbower_use_v2()
        params = {
            "action": "getNumberV2" if use_v2 else "getNumber",
            "service": _smsbower_service(service),
            "country": country,
        }
        if config.sms_max_price():
            params["maxPrice"] = config.sms_max_price()
        text = _smsbower(params)
        if use_v2:
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            if isinstance(data, dict):
                activation_id = str(
                    data.get("activationId") or data.get("id") or ""
                ).strip()
                phone = _digits(
                    str(data.get("phoneNumber") or data.get("phone") or "")
                )
                if activation_id and phone:
                    _remember(activation_id, phone, service, country)
                    _log(f"取号成功 provider=smsbower id={activation_id}")
                    return activation_id, phone
            raise SmsError("bad_response", f"getNumberV2 响应异常：{text[:120]}")
        if not text.startswith("ACCESS_NUMBER:"):
            raise SmsError("bad_response", f"getNumber 非预期响应：{text[:120]}")
        parts = text.split(":", 2)
        if len(parts) < 3:
            raise SmsError("bad_response", f"getNumber 响应格式异常：{text[:120]}")
        activation_id, phone = parts[1].strip(), _digits(parts[2])
        _remember(activation_id, phone, service, country)
        _log(f"取号成功 provider=smsbower id={activation_id}")
        return activation_id, phone

    if name == "l":
        payload = {"service": service, "country": country}
        if config.sms_max_price():
            payload["maxPrice"] = config.sms_max_price()
        data = _admin_post(
            config.l_api_base(),
            "/api/admin/l/take-phone",
            config.l_admin_auth_code(),
            payload,
        )
        item = data.get("item") or {}
        activation_id = str(item.get("id") or "").strip()
        phone = _with_prefix(
            str(item.get("phone") or ""), config.l_phone_prefix()
        )
        if not activation_id or not phone:
            raise SmsError("bad_response", f"take-phone 响应缺少 id/phone：{str(data)[:160]}")
        _remember(activation_id, phone, service, country)
        _log(f"取号成功 provider=l id={activation_id} phone=+{phone}")
        return activation_id, phone

    if name == "h":
        payload = {"projectId": service, "country": country}
        path = (
            "/api/admin/h/take-phone"
            if config.h_phone_acquire_mode() == "new"
            else "/api/admin/h/take-reusable-phone"
        )
        data = _admin_post(
            config.h_api_base(), path, config.h_admin_auth_code(), payload
        )
        item = data.get("item") or {}
        activation_id = str(item.get("id") or "").strip()
        phone = _with_prefix(
            str(item.get("phone") or ""), config.h_phone_prefix()
        )
        if not activation_id or not phone:
            raise SmsError("bad_response", f"take-phone 响应缺少 id/phone：{str(data)[:160]}")
        _remember(activation_id, phone, service, country)
        _log(f"取号成功 provider=h id={activation_id} phone=+{phone}")
        return activation_id, phone

    raise SmsError("bad_provider", f"未知接码平台：{name}")


def wait_for_code(
    activation_id: str,
    timeout: int | None = None,
    poll_interval: int | None = None,
    stop_event=None,
) -> str:
    """Poll the provider until a verification code arrives.

    ``stop_event`` lets the login flow abort the wait (user cancelled) without
    leaving a paid number open — the caller still owns the activation and is
    responsible for cancelling it.
    """
    name = provider()
    total = int(timeout or config.sms_code_wait())
    interval = int(poll_interval or config.sms_poll_interval())
    deadline = time.time() + total
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            raise SmsError("cancelled", "登录已取消，停止等待短信")
        if name == "l":
            data = _admin_post(
                config.l_api_base(),
                "/api/admin/l/fetch-code",
                config.l_admin_auth_code(),
                {"id": activation_id},
            )
            code = str(data.get("code") or "").strip()
            if code:
                _record_code(activation_id, code)
                return code
        elif name == "h":
            data = _admin_post(
                config.h_api_base(),
                "/api/admin/h/fetch-code",
                config.h_admin_auth_code(),
                {"id": activation_id},
            )
            code = str(data.get("code") or "").strip()
            if code:
                _record_code(activation_id, code)
                return code
        elif name == "smsbower":
            text = _smsbower({"action": "getStatus", "id": activation_id})
            if text.startswith("STATUS_OK:"):
                code = text.split(":", 1)[1].strip().strip("'")
                _record_code(activation_id, code)
                return code
            if text == "STATUS_CANCEL":
                raise SmsError("cancelled", "SMSBower 激活已被取消")
        elif name == "grizzly":
            text = _grizzly({"action": "getStatus", "id": activation_id})
            if text.startswith("STATUS_OK:"):
                code = text.split(":", 1)[1].strip()
                _record_code(activation_id, code)
                return code
            if text == "STATUS_CANCEL":
                raise SmsError("cancelled", "激活已被取消")
        else:
            raise SmsError("bad_provider", f"未知接码平台：{name}")
        time.sleep(interval)
    raise SmsTimeout(f"等待短信超时（>{total}s，id={activation_id}）")


def set_status(activation_id: str, status: int) -> None:
    name = provider()
    if name in ("l", "h"):
        return
    if name == "smsbower":
        if int(status) == 1:
            return
        _smsbower(
            {"action": "setStatus", "status": str(status), "id": activation_id}
        )
        return
    if name == "grizzly":
        _grizzly(
            {"action": "setStatus", "status": str(status), "id": activation_id}
        )


def complete(activation_id: str) -> None:
    """Mark the activation as successfully used (best effort)."""
    try:
        set_status(activation_id, 6)
    except Exception as exc:  # noqa: BLE001 - never fail the login for this
        _log(f"标记完成失败（忽略）：{exc}")
    finally:
        _forget(activation_id)


def _cancel_now(activation_id: str) -> None:
    try:
        if provider() in ("l", "h"):
            if provider() == "l":
                _admin_post(
                    config.l_api_base(),
                    "/api/admin/l/release",
                    config.l_admin_auth_code(),
                    {"id": activation_id},
                )
            else:
                _admin_post(
                    config.h_api_base(),
                    "/api/admin/h/release",
                    config.h_admin_auth_code(),
                    {"id": activation_id},
                )
        else:
            set_status(activation_id, 8)
        _log(f"已释放号码 id={activation_id}")
    except Exception as exc:  # noqa: BLE001 - release is best effort
        _log(f"释放号码失败（忽略）：{exc}")
    finally:
        _forget(activation_id)


def cancel(activation_id: str, background: bool = True) -> None:
    """Release a number so it isn't billed twice.

    GrizzlySMS forbids cancelling within ~2 minutes of taking the number, so
    the wait happens in a daemon thread by default.
    """
    with _ACQUIRED_LOCK:
        entry = _ACQUIRED.get(activation_id)
    acquired_at = entry.get("at") if entry else None
    if provider() != "grizzly" or acquired_at is None:
        _cancel_now(activation_id)
        return
    elapsed = time.time() - acquired_at
    if elapsed >= MIN_CANCEL_DELAY:
        _cancel_now(activation_id)
        return
    if not background:
        time.sleep(MIN_CANCEL_DELAY - elapsed)
        _cancel_now(activation_id)
        return
    wait = MIN_CANCEL_DELAY - elapsed
    thread = threading.Thread(
        target=lambda: (time.sleep(wait), _cancel_now(activation_id)),
        name=f"sms-cancel-{activation_id}",
        daemon=True,
    )
    thread.start()


# ------------------------------------------------------------ panel helpers


def balance(force: bool = False, cache_seconds: float = 30.0) -> dict:
    """Provider balance for the dashboard panel (read-only, cached)."""
    name = provider()
    if not name:
        return {
            "ok": False,
            "error": "not_configured",
            "message": "接码未启用：先选择接码平台并填写 API key。",
        }
    with _BALANCE_LOCK:
        cached = _BALANCE_CACHE.get("payload")
        cached_at = float(_BALANCE_CACHE.get("ts") or 0.0)
        if (
            not force
            and cached is not None
            and (time.time() - cached_at) < cache_seconds
        ):
            return dict(cached)
    try:
        if name == "grizzly":
            text = _grizzly({"action": "getBalance"})
        elif name == "smsbower":
            text = _smsbower({"action": "getBalance"})
        else:
            payload = {
                "ok": False,
                "error": "unsupported",
                "message": "本地接码台（l/h）不提供余额查询。",
                "provider": name,
            }
            with _BALANCE_LOCK:
                _BALANCE_CACHE.update({"ts": time.time(), "payload": dict(payload)})
            return payload
        if not text.startswith("ACCESS_BALANCE:"):
            raise SmsError("bad_response", f"getBalance 非预期响应：{text[:120]}")
        raw = text.split(":", 1)[1].strip()
        payload = {
            "ok": True,
            "provider": name,
            "balance": float(raw),
            "raw": raw,
            "fetched_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
        }
    except SmsError as exc:
        payload = {"ok": False, "provider": name, "error": exc.code, "message": exc.message}
    except (TypeError, ValueError) as exc:
        payload = {
            "ok": False,
            "provider": name,
            "error": "bad_response",
            "message": f"余额解析失败：{exc}",
        }
    with _BALANCE_LOCK:
        _BALANCE_CACHE.update({"ts": time.time(), "payload": dict(payload)})
    return payload


def activations() -> list[dict]:
    """Numbers this process still holds (waiting for a code or a refund)."""
    now = time.time()
    with _ACQUIRED_LOCK:
        items = [dict(value, activation_id=key) for key, value in _ACQUIRED.items()]
    for item in items:
        item["age_seconds"] = max(0, int(now - float(item.get("at") or now)))
        item.pop("at", None)
    items.sort(key=lambda item: item["age_seconds"])
    return items


def last_code() -> dict:
    return dict(_LAST_CODE)


def recent_events(limit: int = 20) -> list[dict]:
    with _ACQUIRED_LOCK:
        items = list(_EVENTS)
    return items[-max(1, int(limit)):]
