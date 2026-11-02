"""Configuration and safety flags for Codex Switcher.

v0.4 keeps every destructive action opt-in:
- CODEX_SWITCHER_ENABLE_SWITCH=1 is required for manual switching.
- CODEX_SWITCHER_ENABLE_AUTO_SWITCH=1 is required in addition for
  quota-driven auto-switching; the monitor never bypasses the switch path.
- Every switch creates a timestamped backup and verifies the new auth file.
- Login automation uses an isolated CODEX_HOME and never touches auth.json.
- Quota probing is read-only and never returns or logs tokens.
"""

from __future__ import annotations

from . import __version__

import os
import shutil
from pathlib import Path

APP_NAME = "CodexSwitcher"
VERSION = __version__

# Switching is implemented but explicitly opt-in.
SWITCHING_IMPLEMENTED = True
SWITCH_BLOCK_REASON = "switch_not_enabled"
SWITCH_BLOCK_MESSAGE = (
    "切号默认禁用。设置 CODEX_SWITCHER_ENABLE_SWITCH=1 并重启服务后，"
    "在 Dashboard 勾选确认再执行。"
)


def default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / "AppData" / "Local" / APP_NAME


def data_dir() -> Path:
    """Directory for the encrypted account store and local state."""
    path = Path(os.environ.get("CODEX_SWITCHER_DATA", default_data_dir()))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _settings_get(key: str) -> str | None:
    from . import settings as settings_store

    return settings_store.get(key)


def _cfg(key: str, *env_names: str, default: str = "") -> str:
    """settings.json > environment > default, as a string."""
    value = _settings_get(key)
    if value is not None and str(value).strip() != "":
        return str(value)
    return _env_first(*env_names, default=default)


def _cfg_bool(key: str, *env_names: str, default: bool = False) -> bool:
    value = _settings_get(key)
    if value is not None and str(value).strip() != "":
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    return _env_bool(default, *env_names)


def _cfg_int(
    key: str,
    *env_names: str,
    default: int = 0,
    minimum: int = 0,
    maximum: int = 2**31 - 1,
) -> int:
    value = _settings_get(key)
    if value is not None and str(value).strip() != "":
        try:
            number = int(float(str(value).strip()))
        except ValueError:
            number = default
    else:
        number = _env_int(default, *env_names)
    return max(minimum, min(maximum, number))


def switching_enabled() -> bool:
    """Live auth switching is implemented but opt-in.

    The flag is re-read on every call so the dashboard (settings.json) or an
    environment flip can change behaviour without restarting the service.
    """
    return _cfg_bool("enable_switch", "CODEX_SWITCHER_ENABLE_SWITCH")


def auth_file() -> Path:
    """Path to Codex CLI auth.json (switch target when explicitly enabled)."""
    override = os.environ.get("CODEX_SWITCHER_AUTH")
    if override:
        return Path(override)
    return Path.home() / ".codex" / "auth.json"


def backups_dir() -> Path:
    """Directory for timestamped auth.json backups created before each switch."""
    path = data_dir() / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def host() -> str:
    return _cfg("host", "CODEX_SWITCHER_HOST", default="127.0.0.1")


def port() -> int:
    return _cfg_int("port", "CODEX_SWITCHER_PORT", default=8787, minimum=1, maximum=65535)


def login_enabled() -> bool:
    """Login assistant is opt-in and disabled by default.

    Even when enabled, it uses an isolated CODEX_HOME and never replaces the
    real ~/.codex/auth.json.
    """
    return _cfg_bool("enable_login", "CODEX_SWITCHER_ENABLE_LOGIN")


def login_driver() -> str:
    return _cfg("login_driver", "CODEX_SWITCHER_LOGIN_DRIVER", default="manual")


def login_concurrency() -> int:
    """How many accounts may be logging in at the same time.

    Each Playwright login opens its own headed Chrome window, so the ceiling is
    low on purpose: more windows fight over the foreground and Cloudflare gets
    flaky. 1 = old serial behaviour.
    """
    return _cfg_int(
        "login_concurrency",
        "CODEX_SWITCHER_LOGIN_CONCURRENCY",
        default=2,
        minimum=1,
        maximum=4,
    )


def auto_login_on_import() -> bool:
    """Start logins for imported accounts that have no valid auth snapshot.

    Defaults to on only for the fully automated Playwright driver: manual
    device-code logins need a human per account, so batch import there just
    marks accounts as pending and the row-level button starts them.
    Override explicitly with CODEX_SWITCHER_AUTO_LOGIN_ON_IMPORT=0/1.
    """
    raw = _settings_get("auto_login_on_import")
    if raw is None or str(raw).strip() == "":
        raw = os.environ.get("CODEX_SWITCHER_AUTO_LOGIN_ON_IMPORT")
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    return login_enabled() and login_driver() == "playwright"


# ---------------------------------------------------------------------------
# SMS (接码) settings for the phone-verification step of Codex OAuth.
#
# The names match chatgpt-autoregister / turb-gpt-free-register so an existing
# .env can be reused. CODEX_SWITCHER_SMS_* wins when both are present.
# ---------------------------------------------------------------------------


def _env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip() != "":
            return value
    return default


def _env_int(default: int, *names: str) -> int:
    raw = _env_first(*names).strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _env_bool(default: bool, *names: str) -> bool:
    raw = _env_first(*names).strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def sms_provider() -> str:
    """grizzly | smsbower | l | h | '' (disabled)."""
    return _cfg(
        "sms_provider", "CODEX_SWITCHER_SMS_PROVIDER", "SMS_PROVIDER", default=""
    ).strip().lower()


def sms_enabled() -> bool:
    return bool(sms_provider())


def sms_service() -> str:
    return (
        _cfg("sms_service", "CODEX_SWITCHER_SMS_SERVICE", "SMS_SERVICE", default="dr")
        .strip()
        or "dr"
    )


def sms_country() -> str:
    return _cfg(
        "sms_country", "CODEX_SWITCHER_SMS_COUNTRY", "SMS_COUNTRY", default="10"
    ).strip()


def sms_max_price() -> str:
    return _cfg(
        "sms_max_price", "CODEX_SWITCHER_SMS_MAX_PRICE", "SMS_MAX_PRICE", default=""
    ).strip()


def sms_code_wait() -> int:
    return _cfg_int(
        "sms_code_wait",
        "CODEX_SWITCHER_SMS_CODE_WAIT",
        "SMS_CODE_WAIT",
        default=120,
        minimum=30,
        maximum=600,
    )


def sms_poll_interval() -> int:
    return _cfg_int(
        "sms_poll_interval",
        "CODEX_SWITCHER_SMS_POLL_INTERVAL",
        "SMS_POLL_INTERVAL",
        default=5,
        minimum=1,
        maximum=60,
    )


def sms_max_retries() -> int:
    return _cfg_int(
        "sms_max_retries",
        "CODEX_SWITCHER_SMS_MAX_RETRIES",
        "SMS_MAX_RETRIES",
        default=10,
        minimum=1,
        maximum=50,
    )


def sms_request_timeout() -> int:
    return _cfg_int(
        "sms_request_timeout",
        "CODEX_SWITCHER_SMS_REQUEST_TIMEOUT",
        "SMS_REQUEST_TIMEOUT",
        default=30,
        minimum=5,
        maximum=120,
    )


def sms_api_base() -> str:
    return _cfg(
        "sms_api_base",
        "CODEX_SWITCHER_SMS_API_BASE",
        "SMS_API_BASE",
        default="https://api.grizzlysms.com/stubs/handler_api.php",
    ).strip()


def sms_api_key() -> str:
    return _cfg(
        "sms_api_key", "CODEX_SWITCHER_SMS_API_KEY", "SMS_API_KEY"
    ).strip()


def smsbower_api_base() -> str:
    return _cfg(
        "smsbower_api_base",
        "CODEX_SWITCHER_SMSBOWER_API_BASE",
        "SMSBOWER_API_BASE",
        default="https://smsbower.page/stubs/handler_api.php",
    ).strip()


def smsbower_api_key() -> str:
    return _cfg(
        "smsbower_api_key", "CODEX_SWITCHER_SMSBOWER_API_KEY", "SMSBOWER_API_KEY"
    ).strip()


def smsbower_use_v2() -> bool:
    return _cfg_bool(
        "smsbower_use_v2", "CODEX_SWITCHER_SMSBOWER_USE_V2", "SMSBOWER_USE_V2"
    )


def smsbower_provider_ids() -> str:
    return _cfg(
        "smsbower_provider_ids",
        "CODEX_SWITCHER_SMSBOWER_PROVIDER_IDS",
        "SMSBOWER_PROVIDER_IDS",
    ).strip()


def l_api_base() -> str:
    return _cfg(
        "l_api_base", "CODEX_SWITCHER_L_API_BASE", "L_API_BASE",
        default="http://localhost:8788",
    ).strip()


def l_admin_auth_code() -> str:
    return _cfg(
        "l_admin_auth_code", "CODEX_SWITCHER_L_ADMIN_AUTH_CODE", "L_ADMIN_AUTH_CODE"
    ).strip()


def l_phone_prefix() -> str:
    return _cfg(
        "l_phone_prefix", "CODEX_SWITCHER_L_PHONE_PREFIX", "L_PHONE_PREFIX"
    ).strip()


def h_api_base() -> str:
    return _cfg(
        "h_api_base", "CODEX_SWITCHER_H_API_BASE", "H_API_BASE",
        default="http://localhost:8788",
    ).strip()


def h_admin_auth_code() -> str:
    return _cfg(
        "h_admin_auth_code", "CODEX_SWITCHER_H_ADMIN_AUTH_CODE", "H_ADMIN_AUTH_CODE"
    ).strip()


def h_phone_prefix() -> str:
    return _cfg(
        "h_phone_prefix", "CODEX_SWITCHER_H_PHONE_PREFIX", "H_PHONE_PREFIX"
    ).strip()


def h_phone_acquire_mode() -> str:
    raw = _cfg(
        "h_phone_acquire_mode",
        "CODEX_SWITCHER_H_PHONE_ACQUIRE_MODE",
        "H_PHONE_ACQUIRE_MODE",
        default="reusable",
    ).strip().lower()
    return "new" if raw in ("new", "fresh", "always_new") else "reusable"


def auto_switch_enabled() -> bool:
    """Quota-driven auto-switch is opt-in and disabled by default.

    It also requires switching_enabled(); the monitor never bypasses the
    normal backup/atomic-write/verify/rollback path.
    """
    return _cfg_bool("enable_auto_switch", "CODEX_SWITCHER_ENABLE_AUTO_SWITCH")


def auto_reset_enabled() -> bool:
    """Consume a "Full reset (Weekly + 5 hr)" credit when the pool is dry.

    Off by default: every reset credit is single-use and cannot be refunded,
    so the feature is strictly opt-in.
    """
    return _cfg_bool("enable_auto_reset", "CODEX_SWITCHER_ENABLE_AUTO_RESET")


def auto_reset_secondary_threshold() -> int:
    """Weekly (7d) usage percentage that makes a reset worth spending."""
    return _cfg_int(
        "auto_reset_secondary_threshold",
        "CODEX_SWITCHER_AUTO_RESET_SECONDARY_THRESHOLD",
        default=98,
        minimum=1,
        maximum=100,
    )


def auto_reset_min_wait_hours() -> int:
    """Skip the reset when the weekly window refills itself sooner than this."""
    return _cfg_int(
        "auto_reset_min_wait_hours",
        "CODEX_SWITCHER_AUTO_RESET_MIN_WAIT_HOURS",
        default=72,
        minimum=0,
        maximum=720,
    )


def auto_reset_max_per_week() -> int:
    """Upper bound on automatic resets in any rolling 7 day window."""
    return _cfg_int(
        "auto_reset_max_per_week",
        "CODEX_SWITCHER_AUTO_RESET_MAX_PER_WEEK",
        default=1,
        minimum=0,
        maximum=14,
    )


def auto_reset_before_switch() -> bool:
    """Try the reset before looking for another account (not recommended)."""
    return _cfg_bool(
        "auto_reset_before_switch", "CODEX_SWITCHER_AUTO_RESET_BEFORE_SWITCH"
    )


def reset_credits_url() -> str:
    return _cfg(
        "reset_credits_url",
        "CODEX_SWITCHER_RESET_CREDITS_URL",
        default="",
    )


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def auto_switch_interval_seconds() -> int:
    return _cfg_int(
        "auto_switch_interval",
        "CODEX_SWITCHER_AUTO_SWITCH_INTERVAL",
        default=90,
        minimum=15,
        maximum=3600,
    )


def auto_switch_cooldown_seconds() -> int:
    return _cfg_int(
        "auto_switch_cooldown",
        "CODEX_SWITCHER_AUTO_SWITCH_COOLDOWN",
        default=900,
        minimum=60,
        maximum=86400,
    )


def auto_switch_max_per_hour() -> int:
    return _cfg_int(
        "auto_switch_max_per_hour",
        "CODEX_SWITCHER_AUTO_SWITCH_MAX_PER_HOUR",
        default=3,
        minimum=1,
        maximum=24,
    )


def auto_switch_max_probes() -> int:
    """How many pool accounts a single auto-switch tick may probe.

    ``0`` (the default) means "probe the whole pool"; probes run in parallel
    and share the pool quota cache, so the cap is only needed for very large
    pools where a tick would otherwise run for a long time.
    """
    return _cfg_int(
        "auto_switch_max_probes",
        "CODEX_SWITCHER_AUTO_SWITCH_MAX_PROBES",
        default=0,
        minimum=0,
        maximum=500,
    )


def auto_switch_failure_threshold() -> int:
    return _cfg_int(
        "auto_switch_failure_threshold",
        "CODEX_SWITCHER_AUTO_SWITCH_FAILURE_THRESHOLD",
        default=3,
        minimum=1,
        maximum=20,
    )


def predictive_switch_enabled() -> bool:
    """Opt-in: switch *before* the 5h/7d window is fully exhausted.

    The strict default (only switch at ``limit_reached=true``) stays in place;
    this flag additionally allows a switch when the sampled burn rate predicts
    exhaustion inside ``predictive_threshold_seconds``.
    """
    return _cfg_bool("predictive_switch", "CODEX_SWITCHER_PREDICTIVE_SWITCH")


def predictive_threshold_seconds() -> int:
    return _cfg_int(
        "predictive_threshold",
        "CODEX_SWITCHER_PREDICTIVE_THRESHOLD",
        default=600,
        minimum=120,
        maximum=7200,
    )


def desktop_restart_enabled() -> bool:
    """Restart the ChatGPT desktop app after a successful switch (opt-in).

    The desktop client caches the account at startup, so replacing auth.json
    has no effect until it restarts. Manual switches can force this per request;
    auto-switch uses this flag as its default.
    """
    return _cfg_bool("restart_desktop", "CODEX_SWITCHER_RESTART_DESKTOP")


def desktop_restart_timeout_seconds() -> int:
    return _cfg_int(
        "restart_desktop_timeout",
        "CODEX_SWITCHER_RESTART_DESKTOP_TIMEOUT",
        default=30,
        minimum=5,
        maximum=120,
    )


def quota_cache_ttl_seconds() -> int:
    return _cfg_int(
        "quota_cache_ttl",
        "CODEX_SWITCHER_QUOTA_CACHE_TTL",
        default=5,
        minimum=0,
        maximum=600,
    )


def pool_quota_ttl_seconds() -> int:
    """How long per-account (pool) quota probes stay cached.

    The dashboard polls the pool regularly, so a short TTL would hit the
    upstream usage endpoint once per account per poll and risk 429s.
    """
    return _cfg_int(
        "pool_quota_ttl",
        "CODEX_SWITCHER_POOL_QUOTA_TTL",
        default=60,
        minimum=5,
        maximum=3600,
    )


def goal_resume_enabled() -> bool:
    """Goal auto-resume is opt-in and disabled by default.

    Even when enabled, a resume requires explicit thread selection and a
    confirm=true request; dry-run never mutates goal state.
    """
    return _cfg_bool("enable_goal_resume", "CODEX_SWITCHER_ENABLE_GOAL_RESUME")


def goal_resume_start_timeout_seconds() -> int:
    """How long to wait for the desktop app to take the queued message.

    This is a *confirmation* wait, not a turn wait: the turn itself runs in the
    desktop app and can take as long as it wants. If the app is not running, the
    message simply stays queued — the job still succeeds.
    """
    return _cfg_int(
        "goal_resume_start_timeout",
        "CODEX_SWITCHER_GOAL_RESUME_START_TIMEOUT",
        default=12,
        minimum=3,
        maximum=120,
    )


def goal_resume_wake_enabled() -> bool:
    """Whether a resume may wake the desktop app with a ``codex://`` deep link.

    A queued message is only delivered once the thread is loaded in the desktop
    app. The app ships a purpose-built deep link for exactly that
    (``codex://threads/wake/<thread>``: it checks ``thread/queue/list`` and
    resumes the thread when something is pending), so a resume can complete
    without the user clicking anything. It never *launches* the app: when no
    desktop process is running the wake is skipped and the message stays queued.
    """
    return _cfg_bool(
        "goal_resume_wake_desktop",
        "CODEX_SWITCHER_GOAL_RESUME_WAKE",
        default=True,
    )


def goal_resume_watchdog_enabled() -> bool:
    """Whether a queued resume keeps being watched after the job ends.

    The job only waits a dozen seconds for the desktop app; the app can take
    minutes (another thread is mid-turn) or lose the wake when it was restarting
    for a switch. The watchdog re-sends the same deep link on a backoff and
    marks the message delivered when the queue row disappears. Read-only: it
    never writes the queue database.
    """
    return _cfg_bool(
        "goal_resume_watchdog",
        "CODEX_SWITCHER_GOAL_RESUME_WATCHDOG",
        default=True,
    )


def goal_resume_watch_interval_seconds() -> int:
    """How often the watchdog checks the queue row and retries a wake."""
    return _cfg_int(
        "goal_resume_watch_interval",
        "CODEX_SWITCHER_GOAL_RESUME_WATCH_INTERVAL",
        default=15,
        minimum=5,
        maximum=600,
    )


def goal_resume_watch_ttl_seconds() -> int:
    """How long a queued message is retried before it is reported as expired."""
    return _cfg_int(
        "goal_resume_watch_ttl",
        "CODEX_SWITCHER_GOAL_RESUME_WATCH_TTL",
        default=6 * 3600,
        minimum=60,
        maximum=7 * 24 * 3600,
    )


def queue_db_path() -> Path:
    """Codex's conversation queue (read-only for us)."""
    override = os.environ.get("CODEX_SWITCHER_QUEUE_DB")
    if override:
        return Path(override)
    return Path.home() / ".codex" / "queue_1.sqlite"


def goals_db_path() -> Path:
    override = os.environ.get("CODEX_SWITCHER_GOALS_DB")
    if override:
        return Path(override)
    return Path.home() / ".codex" / "goals_1.sqlite"


def state_db_path() -> Path:
    override = os.environ.get("CODEX_SWITCHER_STATE_DB")
    if override:
        return Path(override)
    return Path.home() / ".codex" / "state_5.sqlite"


def usage_url() -> str:
    return _cfg(
        "usage_url",
        "CODEX_SWITCHER_USAGE_URL",
        default="https://chatgpt.com/backend-api/wham/usage",
    )


def login_sessions_dir() -> Path:
    path = data_dir() / "login-sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def browser_profile_dir() -> Path:
    path = data_dir() / "browser-profile"
    path.mkdir(parents=True, exist_ok=True)
    return path


def find_codex_exe() -> str | None:
    """Locate the Codex CLI executable.

    Priority:
      1. CODEX_SWITCHER_CODEX_EXE
      2. codex on PATH
      3. newest %LOCALAPPDATA%/OpenAI/Codex/bin/*/codex.exe
    """
    override = os.environ.get("CODEX_SWITCHER_CODEX_EXE")
    if override and Path(override).exists():
        return str(Path(override))
    # Prefer the real executable over npm's codex.CMD wrapper: subprocess
    # cannot launch a .CMD directly without cmd.exe.
    local = os.environ.get("LOCALAPPDATA")
    if local:
        base = Path(local) / "OpenAI" / "Codex" / "bin"
        if base.exists():
            candidates = sorted(
                base.glob("*/codex.exe"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if candidates:
                return str(candidates[0])
    found = shutil.which("codex")
    if found:
        return found
    return None


def public_config() -> dict:
    return {
        "version": VERSION,
        "host": host(),
        "port": port(),
        "data_dir": str(data_dir()),
        "auth_file": str(auth_file()),
        "switching_implemented": SWITCHING_IMPLEMENTED,
        "switching_enabled": switching_enabled(),
        "switch_block_reason": SWITCH_BLOCK_REASON,
        "switch_block_message": SWITCH_BLOCK_MESSAGE,
        "backups_dir": str(backups_dir()),
        "login_enabled": login_enabled(),
        "login_driver": login_driver(),
        "auto_login_on_import": auto_login_on_import(),
        "sms_enabled": sms_enabled(),
        "sms_provider": sms_provider(),
        "auto_switch_enabled": auto_switch_enabled(),
        "auto_switch_interval_seconds": auto_switch_interval_seconds(),
        "auto_switch_cooldown_seconds": auto_switch_cooldown_seconds(),
        "auto_switch_max_per_hour": auto_switch_max_per_hour(),
        "auto_switch_failure_threshold": auto_switch_failure_threshold(),
        "predictive_switch_enabled": predictive_switch_enabled(),
        "predictive_threshold_seconds": predictive_threshold_seconds(),
        "desktop_restart_enabled": desktop_restart_enabled(),
        "desktop_restart_timeout_seconds": desktop_restart_timeout_seconds(),
        "quota_cache_ttl_seconds": quota_cache_ttl_seconds(),
        "goal_resume_enabled": goal_resume_enabled(),
        "goal_resume_start_timeout_seconds": goal_resume_start_timeout_seconds(),
        "usage_url": usage_url(),
        "codex_exe": find_codex_exe(),
        "login_sessions_dir": str(login_sessions_dir()),
        "browser_profile_dir": str(browser_profile_dir()),
    }
