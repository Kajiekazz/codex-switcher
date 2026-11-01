"""User-editable runtime settings (dashboard "设置" panel).

Precedence: ``settings.json`` > environment > built-in default.

The file lives next to the account store (``%LOCALAPPDATA%\\CodexSwitcher``)
and only stores keys the user actually changed, so the environment keeps
working as a fallback. Secrets (SMS API keys, L/H admin auth codes) are
DPAPI-encrypted before they hit the disk and never leave the process in
plaintext through the API.
"""

from __future__ import annotations

import base64
import json
import os
import threading
from pathlib import Path

from . import dpapi

VERSION = 1

# group -> (id, label)
GROUPS = [
    ("login", "登录"),
    ("sms", "接码（短信验证）"),
    ("switch", "切号"),
    ("goal", "目标续跑"),
    ("advanced", "高级"),
]

# key -> metadata. ``env`` is the legacy environment variable, ``default`` is
# used when neither settings.json nor the environment provides a value.
SPEC: dict[str, dict] = {
    "enable_login": {
        "group": "login",
        "label": "启用登录助手",
        "type": "bool",
        "env": "CODEX_SWITCHER_ENABLE_LOGIN",
        "default": False,
        "hint": "关闭后 /api/accounts/login 返回 423，账号行按钮不可用。",
    },
    "login_driver": {
        "group": "login",
        "label": "登录驱动",
        "type": "str",
        "env": "CODEX_SWITCHER_LOGIN_DRIVER",
        "default": "playwright",
        "choices": ["playwright", "manual"],
        "hint": "playwright 自动填邮箱/密码/TOTP/设备码/短信码；manual 需要你手动在浏览器完成。",
    },
    "auto_login_on_import": {
        "group": "login",
        "label": "导入后自动登录",
        "type": "bool",
        "env": "CODEX_SWITCHER_AUTO_LOGIN_ON_IMPORT",
        "default": "",
        "hint": "留空（未设置）时：playwright 驱动默认开启，manual 驱动默认关闭。",
    },
    "login_concurrency": {
        "group": "login",
        "label": "同时登录几个账号",
        "type": "int",
        "env": "CODEX_SWITCHER_LOGIN_CONCURRENCY",
        "default": 2,
        "min": 1,
        "max": 4,
        "hint": "每个登录会开一个独立 Chrome 窗口；2 个并行通常没问题，>2 容易抢前台导致人机校验失败。",
    },
    "sms_provider": {
        "group": "sms",
        "label": "接码平台",
        "type": "str",
        "env": "SMS_PROVIDER",
        "default": "",
        "choices": ["", "grizzly", "smsbower", "l", "h"],
        "hint": "留空 = 不接码；遇到手机验证会明确报错而不是卡住。",
    },
    "sms_service": {
        "group": "sms",
        "label": "服务代码",
        "type": "str",
        "env": "SMS_SERVICE",
        "default": "dr",
        "hint": "OpenAI / ChatGPT = dr。",
    },
    "sms_country": {
        "group": "sms",
        "label": "国家 ID",
        "type": "str",
        "env": "SMS_COUNTRY",
        "default": "10",
        "hint": "Grizzly/SMSBower 的国家 ID；L/H 由各自后台解释。",
    },
    "sms_max_price": {
        "group": "sms",
        "label": "单号最高价",
        "type": "str",
        "env": "SMS_MAX_PRICE",
        "default": "",
        "hint": "留空 = 不限价。",
    },
    "sms_code_wait": {
        "group": "sms",
        "label": "等短信超时（秒）",
        "type": "int",
        "env": "SMS_CODE_WAIT",
        "default": 120,
        "min": 30,
        "max": 600,
    },
    "sms_poll_interval": {
        "group": "sms",
        "label": "轮询间隔（秒）",
        "type": "int",
        "env": "SMS_POLL_INTERVAL",
        "default": 5,
        "min": 1,
        "max": 60,
    },
    "sms_max_retries": {
        "group": "sms",
        "label": "最多换号次数",
        "type": "int",
        "env": "SMS_MAX_RETRIES",
        "default": 10,
        "min": 1,
        "max": 50,
    },
    "sms_request_timeout": {
        "group": "sms",
        "label": "接口超时（秒）",
        "type": "int",
        "env": "SMS_REQUEST_TIMEOUT",
        "default": 30,
        "min": 5,
        "max": 120,
    },
    "sms_api_base": {
        "group": "sms",
        "label": "GrizzlySMS API 基址",
        "type": "str",
        "env": "SMS_API_BASE",
        "default": "https://api.grizzlysms.com/stubs/handler_api.php",
    },
    "sms_api_key": {
        "group": "sms",
        "label": "GrizzlySMS API Key",
        "type": "str",
        "env": "SMS_API_KEY",
        "default": "",
        "secret": True,
    },
    "smsbower_api_base": {
        "group": "sms",
        "label": "SMSBower API 基址",
        "type": "str",
        "env": "SMSBOWER_API_BASE",
        "default": "https://smsbower.page/stubs/handler_api.php",
    },
    "smsbower_api_key": {
        "group": "sms",
        "label": "SMSBower API Key",
        "type": "str",
        "env": "SMSBOWER_API_KEY",
        "default": "",
        "secret": True,
    },
    "smsbower_use_v2": {
        "group": "sms",
        "label": "SMSBower 使用 getNumberV2",
        "type": "bool",
        "env": "SMSBOWER_USE_V2",
        "default": False,
    },
    "smsbower_provider_ids": {
        "group": "sms",
        "label": "SMSBower 运营商 ID",
        "type": "str",
        "env": "SMSBOWER_PROVIDER_IDS",
        "default": "",
        "hint": "可选，逗号分隔。",
    },
    "l_api_base": {
        "group": "sms",
        "label": "L 取号服务地址",
        "type": "str",
        "env": "L_API_BASE",
        "default": "http://localhost:8788",
    },
    "l_admin_auth_code": {
        "group": "sms",
        "label": "L 后台授权码",
        "type": "str",
        "env": "L_ADMIN_AUTH_CODE",
        "default": "",
        "secret": True,
    },
    "l_phone_prefix": {
        "group": "sms",
        "label": "L 号码国家前缀",
        "type": "str",
        "env": "L_PHONE_PREFIX",
        "default": "",
    },
    "h_api_base": {
        "group": "sms",
        "label": "H 取号服务地址",
        "type": "str",
        "env": "H_API_BASE",
        "default": "http://localhost:8788",
    },
    "h_admin_auth_code": {
        "group": "sms",
        "label": "H 后台授权码",
        "type": "str",
        "env": "H_ADMIN_AUTH_CODE",
        "default": "",
        "secret": True,
    },
    "h_phone_prefix": {
        "group": "sms",
        "label": "H 号码国家前缀",
        "type": "str",
        "env": "H_PHONE_PREFIX",
        "default": "",
    },
    "h_phone_acquire_mode": {
        "group": "sms",
        "label": "H 取号方式",
        "type": "str",
        "env": "H_PHONE_ACQUIRE_MODE",
        "default": "reusable",
        "choices": ["reusable", "new"],
    },
    "enable_switch": {
        "group": "switch",
        "label": "启用切号",
        "type": "bool",
        "env": "CODEX_SWITCHER_ENABLE_SWITCH",
        "default": False,
        "hint": "关闭时所有切号请求都会被拒绝，且不会改动 ~/.codex/auth.json。",
    },
    "enable_auto_switch": {
        "group": "switch",
        "label": "启用自动切号",
        "type": "bool",
        "env": "CODEX_SWITCHER_ENABLE_AUTO_SWITCH",
        "default": False,
        "hint": "保存后立即启动/停止后台监控线程，不需要重启服务。",
    },
    "auto_switch_interval": {
        "group": "switch",
        "label": "检查间隔（秒）",
        "type": "int",
        "env": "CODEX_SWITCHER_AUTO_SWITCH_INTERVAL",
        "default": 90,
        "min": 15,
        "max": 3600,
    },
    "auto_switch_cooldown": {
        "group": "switch",
        "label": "切换冷却（秒）",
        "type": "int",
        "env": "CODEX_SWITCHER_AUTO_SWITCH_COOLDOWN",
        "default": 900,
        "min": 60,
        "max": 86400,
    },
    "auto_switch_max_per_hour": {
        "group": "switch",
        "label": "每小时最多切几次",
        "type": "int",
        "env": "CODEX_SWITCHER_AUTO_SWITCH_MAX_PER_HOUR",
        "default": 3,
        "min": 1,
        "max": 24,
    },
    "auto_switch_max_probes": {
        "group": "switch",
        "label": "每轮最多探测几个账号（0 = 全部）",
        "type": "int",
        "env": "CODEX_SWITCHER_AUTO_SWITCH_MAX_PROBES",
        "default": 0,
        "min": 0,
        "max": 500,
        "hint": "默认 0 表示整池探测；探测并行执行并复用账号池额度缓存。",
    },
    "auto_switch_failure_threshold": {
        "group": "switch",
        "label": "连续失败熔断阈值",
        "type": "int",
        "env": "CODEX_SWITCHER_AUTO_SWITCH_FAILURE_THRESHOLD",
        "default": 3,
        "min": 1,
        "max": 20,
    },
    "predictive_switch": {
        "group": "switch",
        "label": "预测式切号",
        "type": "bool",
        "env": "CODEX_SWITCHER_PREDICTIVE_SWITCH",
        "default": False,
        "hint": "额度快耗尽前提前切换，需要先启用自动切号。",
    },
    "predictive_threshold": {
        "group": "switch",
        "label": "预测阈值（秒）",
        "type": "int",
        "env": "CODEX_SWITCHER_PREDICTIVE_THRESHOLD",
        "default": 600,
        "min": 120,
        "max": 7200,
    },
    "restart_desktop": {
        "group": "switch",
        "label": "切号后重启桌面端",
        "type": "bool",
        "env": "CODEX_SWITCHER_RESTART_DESKTOP",
        "default": False,
    },
    "restart_desktop_timeout": {
        "group": "switch",
        "label": "桌面端重启超时（秒）",
        "type": "int",
        "env": "CODEX_SWITCHER_RESTART_DESKTOP_TIMEOUT",
        "default": 30,
        "min": 5,
        "max": 120,
    },
    "enable_auto_reset": {
        "group": "switch",
        "label": "额度耗尽时自动使用重置券",
        "type": "bool",
        "env": "CODEX_SWITCHER_ENABLE_AUTO_RESET",
        "default": False,
        "hint": "每张券只能使用一次且无法撤回。仅在换不到可用账号、周额度已达阈值、"
        "且 7 天窗口不会很快自然重置时才会消耗。",
    },
    "auto_reset_secondary_threshold": {
        "group": "switch",
        "label": "周额度触发阈值（%）",
        "type": "int",
        "env": "CODEX_SWITCHER_AUTO_RESET_SECONDARY_THRESHOLD",
        "default": 98,
        "min": 1,
        "max": 100,
    },
    "auto_reset_min_wait_hours": {
        "group": "switch",
        "label": "周窗口至少还要等（小时）才用券",
        "type": "int",
        "env": "CODEX_SWITCHER_AUTO_RESET_MIN_WAIT_HOURS",
        "default": 72,
        "min": 0,
        "max": 720,
        "hint": "7 天窗口在这个时间内会自然重置时，等它而不是烧券。默认 72 小时（3 天）。",
    },
    "auto_reset_max_per_week": {
        "group": "switch",
        "label": "每 7 天最多自动用几张券",
        "type": "int",
        "env": "CODEX_SWITCHER_AUTO_RESET_MAX_PER_WEEK",
        "default": 1,
        "min": 0,
        "max": 14,
    },
    "auto_reset_before_switch": {
        "group": "switch",
        "label": "先烧券再换号（不推荐）",
        "type": "bool",
        "env": "CODEX_SWITCHER_AUTO_RESET_BEFORE_SWITCH",
        "default": False,
        "hint": "默认先换号（免费），换不到可用账号时才消耗重置券。",
    },
    "enable_goal_resume": {
        "group": "goal",
        "label": "启用目标续跑",
        "type": "bool",
        "env": "CODEX_SWITCHER_ENABLE_GOAL_RESUME",
        "default": False,
        "restart": True,
        "hint": "服务级开关，改动后需要重启服务。",
    },
    "goal_resume_max_runtime": {
        "group": "goal",
        "label": "单次最长运行（秒）",
        "type": "int",
        "env": "CODEX_SWITCHER_GOAL_RESUME_MAX_RUNTIME",
        "default": 7200,
        "min": 60,
        "max": 86400,
    },
    "goal_resume_start_timeout": {
        "group": "goal",
        "label": "启动超时（秒）",
        "type": "int",
        "env": "CODEX_SWITCHER_GOAL_RESUME_START_TIMEOUT",
        "default": 12,
        "min": 3,
        "max": 120,
    },
    "goal_resume_poll": {
        "group": "goal",
        "label": "状态轮询（秒）",
        "type": "int",
        "env": "CODEX_SWITCHER_GOAL_RESUME_POLL",
        "default": 20,
        "min": 5,
        "max": 300,
    },
    "goal_resume_takeover": {
        "group": "goal",
        "label": "被桌面端占用时自动接管",
        "type": "bool",
        "env": "CODEX_SWITCHER_GOAL_RESUME_TAKEOVER",
        "default": True,
        "hint": (
            "桌面端打开着某个会话时会占住它的写入锁，续跑拿不到。开启后自动关闭桌面端、"
            "重试一次，任务结束后再把桌面端打开。终端里的 codex CLI 占用的会话仍会跳过。"
        ),
    },
    "goal_resume_takeover_wait": {
        "group": "goal",
        "label": "接管后等待锁释放（秒）",
        "type": "int",
        "env": "CODEX_SWITCHER_GOAL_RESUME_TAKEOVER_WAIT",
        "default": 20,
        "min": 3,
        "max": 120,
        "hint": "关闭桌面端后轮询 thread/resume 的时间上限。",
    },
    "auto_resume_after_switch": {
        "group": "goal",
        "label": "切号后自动续跑已勾选目标",
        "type": "bool",
        "env": "CODEX_SWITCHER_AUTO_RESUME_AFTER_SWITCH",
        "default": True,
        "hint": "先在会话行点「自动」勾选目标；之后每次切号成功，会自动唤醒这些目标（额度受限、已暂停、或回合被桌面重启打断都算）。",
    },
    "auto_resume_threads": {
        "group": "goal",
        "label": "自动续跑目标（内部）",
        "type": "str",
        "env": "CODEX_SWITCHER_AUTO_RESUME_THREADS",
        "default": "[]",
        "hidden": True,
    },
    "quota_cache_ttl": {
        "group": "advanced",
        "label": "额度缓存 TTL（秒）",
        "type": "int",
        "env": "CODEX_SWITCHER_QUOTA_CACHE_TTL",
        "default": 5,
        "min": 0,
        "max": 600,
        "hint": "当前账号额度卡的缓存时间。",
    },
    "pool_quota_ttl": {
        "group": "advanced",
        "label": "账号池额度缓存（秒）",
        "type": "int",
        "env": "CODEX_SWITCHER_POOL_QUOTA_TTL",
        "default": 60,
        "min": 5,
        "max": 3600,
        "hint": "账号池里每个账号额度多久刷新一次；点「刷新额度」会跳过缓存。",
    },
    "usage_url": {
        "group": "advanced",
        "label": "额度查询 URL",
        "type": "str",
        "env": "CODEX_SWITCHER_USAGE_URL",
        "default": "https://chatgpt.com/backend-api/wham/usage",
    },
    "host": {
        "group": "advanced",
        "label": "监听地址",
        "type": "str",
        "env": "CODEX_SWITCHER_HOST",
        "default": "127.0.0.1",
        "restart": True,
    },
    "port": {
        "group": "advanced",
        "label": "监听端口",
        "type": "int",
        "env": "CODEX_SWITCHER_PORT",
        "default": 8787,
        "min": 1,
        "max": 65535,
        "restart": True,
    },
}

_SECRET_KEYS = {key for key, meta in SPEC.items() if meta.get("secret")}

_LOCK = threading.RLock()
_RAW: dict[str, str] = {}
_LOADED = False
_LOADED_PATH: str | None = None


def settings_path() -> Path:
    from . import config

    return config.data_dir() / "settings.json"


def _read_file() -> dict[str, str]:
    path = settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    values = data.get("values") if isinstance(data, dict) else None
    if not isinstance(values, dict):
        return {}
    result: dict[str, str] = {}
    for key, value in values.items():
        if key not in SPEC or not isinstance(value, str):
            continue
        if key in _SECRET_KEYS and value.startswith("dpapi:"):
            try:
                raw = dpapi.unprotect(base64.b64decode(value[6:]))
                value = raw.decode("utf-8")
            except Exception:
                continue
        result[key] = value
    return result


def reload() -> None:
    global _RAW, _LOADED, _LOADED_PATH
    with _LOCK:
        try:
            path = str(settings_path())
        except Exception:
            path = ""
        _RAW = _read_file()
        _LOADED = True
        _LOADED_PATH = path


def _ensure_loaded() -> None:
    """(Re)load when the data directory changed (tests, CODEX_SWITCHER_DATA)."""
    global _LOADED_PATH
    try:
        current = str(settings_path())
    except Exception:
        current = ""
    if not _LOADED or current != _LOADED_PATH:
        reload()


def reset_cache() -> None:
    """Forget the cached values (tests change the data dir between cases)."""
    global _RAW, _LOADED, _LOADED_PATH
    with _LOCK:
        _RAW = {}
        _LOADED = False
        _LOADED_PATH = None


def get(key: str) -> str | None:
    """Value from settings.json only (None when unset)."""
    _ensure_loaded()
    with _LOCK:
        return _RAW.get(key)


def effective(key: str) -> str:
    """settings > env > default, as a string."""
    meta = SPEC.get(key)
    if meta is None:
        return ""
    value = get(key)
    if value is not None and str(value).strip() != "":
        return str(value)
    env_name = meta.get("env")
    if env_name:
        env_value = os.environ.get(env_name)
        if env_value is not None and env_value.strip() != "":
            return env_value
    default = meta.get("default", "")
    return "" if default is None else str(default)


def source(key: str) -> str:
    value = get(key)
    if value is not None and str(value).strip() != "":
        return "settings"
    meta = SPEC.get(key) or {}
    env_name = meta.get("env")
    if env_name and (os.environ.get(env_name) or "").strip():
        return "env"
    return "default"


def _mask(value: str) -> str:
    text = str(value or "")
    if len(text) <= 4:
        return "****"
    return "****" + text[-4:]


def _coerce(key: str, raw) -> str:
    meta = SPEC[key]
    kind = meta.get("type", "str")
    if kind == "bool":
        if isinstance(raw, bool):
            return "1" if raw else "0"
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return "1"
        if text in ("0", "false", "no", "off", ""):
            return "0"
        raise ValueError(f"{key} 需要布尔值")
    if kind == "int":
        try:
            number = int(float(str(raw).strip()))
        except (TypeError, ValueError):
            raise ValueError(f"{key} 需要整数")
        minimum = int(meta.get("min", -2**31))
        maximum = int(meta.get("max", 2**31 - 1))
        if number < minimum or number > maximum:
            raise ValueError(f"{key} 超出范围 {minimum}-{maximum}")
        return str(number)
    text = str(raw if raw is not None else "").strip()
    if len(text) > 1000:
        raise ValueError(f"{key} 太长")
    choices = meta.get("choices")
    if choices is not None and text not in choices:
        raise ValueError(f"{key} 只能是 {', '.join(choices) or '(空)'}")
    return text


def _write_file(values: dict[str, str]) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, str] = {}
    for key, value in values.items():
        if key in _SECRET_KEYS and value:
            token = base64.b64encode(
                dpapi.protect(value.encode("utf-8"))
            ).decode("ascii")
            payload[key] = f"dpapi:{token}"
        else:
            payload[key] = value
    text = json.dumps(
        {"version": VERSION, "values": payload},
        ensure_ascii=False,
        indent=2,
    )
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save(values: dict) -> list[str]:
    """Validate and persist a batch of settings. Returns the changed keys."""
    global _RAW
    if not isinstance(values, dict):
        raise ValueError("values 必须是对象")
    unknown = [key for key in values if key not in SPEC]
    if unknown:
        raise ValueError(f"未知配置项: {', '.join(unknown)}")
    _ensure_loaded()
    staged = dict(_RAW)
    changed: list[str] = []
    for key, raw in values.items():
        coerced = _coerce(key, raw)
        if staged.get(key) != coerced:
            staged[key] = coerced
            changed.append(key)
    if not changed:
        return []
    _write_file(staged)
    with _LOCK:
        _RAW = staged
    return changed


def clear(keys: list[str]) -> list[str]:
    """Remove keys from settings.json so env/defaults apply again."""
    global _RAW
    if not isinstance(keys, list):
        raise ValueError("clear 必须是数组")
    unknown = [key for key in keys if key not in SPEC]
    if unknown:
        raise ValueError(f"未知配置项: {', '.join(unknown)}")
    _ensure_loaded()
    staged = dict(_RAW)
    removed = [key for key in keys if staged.pop(key, None) is not None]
    if not removed:
        return []
    _write_file(staged)
    with _LOCK:
        _RAW = staged
    return removed


def public() -> dict:
    """API view: grouped items with effective values, secrets masked."""
    _ensure_loaded()
    items = []
    for key, meta in SPEC.items():
        if meta.get("hidden"):
            continue
        is_secret = bool(meta.get("secret"))
        value = effective(key)
        items.append(
            {
                "key": key,
                "label": meta.get("label", key),
                "type": meta.get("type", "str"),
                "group": meta.get("group", "advanced"),
                "value": _mask(value) if is_secret else value,
                "is_set": key in _RAW,
                "secret": is_secret,
                "has_value": bool(value),
                "source": source(key),
                "choices": meta.get("choices"),
                "hint": meta.get("hint", ""),
                "restart": bool(meta.get("restart")),
                "min": meta.get("min"),
                "max": meta.get("max"),
            }
        )
    return {
        "groups": [{"id": gid, "label": label} for gid, label in GROUPS],
        "items": items,
        "path": str(settings_path()),
    }


def restart_required(changed: list[str]) -> list[str]:
    return [key for key in changed if SPEC.get(key, {}).get("restart")]
