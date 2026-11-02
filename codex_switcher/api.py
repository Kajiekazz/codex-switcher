"""Local HTTP API + dashboard for Codex Switcher.

Safety guarantees for v0.4:
- GET endpoints only read ~/.codex/auth.json; /api/quota makes a read-only
  upstream call with the live token and returns only percentages/reset times.
- POST /api/accounts/import stores accounts encrypted (DPAPI).
- POST /api/switch is always available, requires
  confirm=true, backs up the live auth.json, writes atomically, verifies,
  and rolls back on failure.
- Auto-switch is opt-in (CODEX_SWITCHER_ENABLE_AUTO_SWITCH=1),
  probes candidate quota and uses
  cooldown/hourly-cap/circuit-breaker guards.
- Login assistant is disabled unless CODEX_SWITCHER_ENABLE_LOGIN=1.
- Login assistant runs `codex login --device-auth` with an isolated
  CODEX_HOME and stores only an encrypted auth.json snapshot.
- No endpoint returns passwords, TOTP seeds, or tokens.
"""

from __future__ import annotations

import json
import hmac
import os
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from . import __version__, config, extension_install, settings, sms, ui_prefs
from .accounts import (
    BASE32_RE,
    EMAIL_RE,
    normalize_tags,
    normalize_totp_secret,
    parse_accounts,
    parse_priority,
    public_account,
)
from .auth_state import read_auth_state
from .auto_switch import AutoSwitchMonitor
from .appserver import AppServerError
from .desktop import DesktopClient
from .goal_resume import GoalResumeError, GoalResumeService
from .login import LoginError, LoginManager
from .onboarding import Onboarding
from .quota import QuotaService
from .store import AccountStore
from .switcher import SwitchError, SwitchManager
from .trend import predict_quota

MAX_BODY_BYTES = 1024 * 1024  # 1 MiB


class _ThemeBus:
    """Fan-out for ``GET /api/theme/events`` (server-sent events).

    The dashboard writes the theme, the extension side panel follows it. That
    used to be a 2 s poll, so the panel always looked "one beat behind" the
    click; an ``EventSource`` makes it a single round trip on localhost.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue] = set()

    def subscribe(self) -> queue.Queue:
        client: queue.Queue = queue.Queue(maxsize=8)
        with self._lock:
            self._subscribers.add(client)
        return client

    def unsubscribe(self, client: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(client)

    def publish(self, payload: dict) -> int:
        with self._lock:
            subscribers = list(self._subscribers)
        for client in subscribers:
            try:
                # Slow/dead clients are dropped by the stream handler itself;
                # blocking a theme write on a stalled socket is never worth it.
                client.put_nowait(payload)
            except queue.Full:
                pass
        return len(subscribers)

    def count(self) -> int:
        with self._lock:
            return len(self._subscribers)


THEME_BUS = _ThemeBus()


def _cors_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Access-Control-Max-Age", "600")


def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    _cors_headers(handler)
    handler.end_headers()
    handler.wfile.write(body)


def _send_html(handler: BaseHTTPRequestHandler, status: int, html: str) -> None:
    body = html.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    _cors_headers(handler)
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return {}
    if length > MAX_BODY_BYTES:
        raise ValueError("request body too large")
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object")
    return data


def _status_payload(
    store: AccountStore,
    auth_path: Path,
    switch_manager: SwitchManager,
    auto_switch: AutoSwitchMonitor | None = None,
) -> dict:
    auth = read_auth_state(auth_path)
    accounts = store.list()
    status_counts = store.status_counts()
    payload = {
        "ok": True,
        "version": __version__,
        "server_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "switching": switch_manager.status(),
        "auth": auth,
        "accounts": {
            "total": len(accounts),
            "by_status": status_counts,
        },
    }
    if auto_switch is not None:
        payload["auto_switch"] = auto_switch.status()
    return payload


def _login_payload(login_manager: LoginManager) -> dict:
    return {
        "ok": True,
        "login_enabled": config.login_enabled(),
        "auto_login_on_import": config.auto_login_on_import(),
        "sms_enabled": config.sms_enabled(),
        "sms_provider": config.sms_provider(),
        "driver": login_manager.driver_name,
        "codex_exe": config.find_codex_exe(),
        "concurrency": login_manager.concurrency(),
        "sessions": login_manager.list(),
        "queue": login_manager.queue_info(),
    }


def _login_overlay(login_manager: LoginManager) -> dict[str, dict]:
    """Map lowercased email to its live login state for the account list."""
    overlay: dict[str, dict] = {}
    latest = {}
    for session in login_manager.list():
        latest.setdefault(session["email"].lower(), session)
    for key, session in latest.items():
        if session["status"] in {"failed", "cancelled"}:
            overlay[key] = {"state": session["status"], "session_id": session["id"],
                            "message": session["message"], "error": session["error"], "position": 0}
    for email, session in login_manager.active_map().items():
        overlay[email] = {
            "state": session["status"],
            "session_id": session["id"],
            "device_code": session["device_code"],
            "message": session["message"],
            "position": 0,
        }
    for item in login_manager.queue_info():
        key = item["email"].lower()
        if key not in overlay:
            overlay[key] = {
                "state": "queued",
                "session_id": item.get("session_id"),
                "device_code": None,
                "message": f"排队第 {item['position']} 位",
                "position": item["position"],
            }
    return overlay


def _read_dashboard() -> str:
    """Read the dashboard from disk so UI edits do not need a restart."""
    try:
        html = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")
    except OSError:
        html = DASHBOARD_HTML
    # Stamp the saved theme into the document itself. Applying it from JS after
    # the first paint is what made every dark-theme reload flash white.
    try:
        theme = ui_prefs.theme()
    except Exception:  # noqa: BLE001 - never break the dashboard over a pref
        theme = ui_prefs.DEFAULT_THEME
    return html.replace(
        '<html lang="zh-CN" data-theme="">',
        f'<html lang="zh-CN" data-theme="{theme}">',
        1,
    )


def make_handler(
    store: AccountStore,
    auth_path: Path,
    login_manager: LoginManager,
    switch_manager: SwitchManager,
    quota_service: QuotaService | None = None,
    auto_switch: AutoSwitchMonitor | None = None,
    desktop_client: DesktopClient | None = None,
    goal_resume: GoalResumeService | None = None,
    onboarding=None,
    app_runtime=None,
):
    quota_service = quota_service or QuotaService(store, auth_path)
    desktop_client = desktop_client or DesktopClient()
    goal_resume = goal_resume or GoalResumeService(desktop_client=desktop_client)
    onboarding = onboarding or Onboarding(store)

    class Handler(BaseHTTPRequestHandler):
        server_version = f"CodexSwitcher/{__version__}"
        sys_version = ""

        def log_message(self, fmt, *args):  # noqa: A003
            sys.stderr.write(
                "%s - %s\n" % (self.log_date_time_string(), fmt % args)
            )

        def do_OPTIONS(self):  # noqa: N802
            self.send_response(204)
            _cors_headers(self)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _stream_theme(self) -> None:
            """``GET /api/theme/events`` — long-lived SSE stream of theme state.

            ``Connection: close`` + no ``Content-Length`` makes the body
            close-delimited, which is exactly what ``EventSource`` wants: events
            are framed by a blank line and arrive as soon as they are flushed.
            A 15 s comment keeps idle connections from being dropped by anything
            in between (and lets the client notice a dead service quickly).
            """
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            _cors_headers(self)
            self.end_headers()
            client = THEME_BUS.subscribe()
            try:
                # Reconnect delay hint + the current state, so a panel that
                # opens later is correct without waiting for a change.
                self.wfile.write(b"retry: 1000\n\n")
                self._sse_send(ui_prefs.state())
                while True:
                    try:
                        payload = client.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    self._sse_send(payload)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                THEME_BUS.unsubscribe(client)

        def _sse_send(self, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False)
            self.wfile.write(f"data: {body}\n\n".encode("utf-8"))
            self.wfile.flush()

        def do_GET(self):  # noqa: N802
            raw_path = self.path
            path = raw_path.split("?", 1)[0]
            query = parse_qs(raw_path.split("?", 1)[1]) if "?" in raw_path else {}
            try:
                if path == "/api/theme/events":
                    self._stream_theme()
                    return
                if path == "/":
                    _send_html(self, 200, _read_dashboard())
                    return
                if path == "/userscript/codex-switcher.user.js":
                    # Retired surface, kept as an archive URL: the panel now
                    # ships as the extension side panel (extension/sidepanel.*).
                    root = Path(__file__).resolve().parent.parent
                    script_path = root / "legacy" / "codex-switcher.user.js"
                    if not script_path.exists():
                        script_path = root / "userscript" / "codex-switcher.user.js"
                    if not script_path.exists():
                        _send_json(
                            self,
                            404,
                            {
                                "ok": False,
                                "error": "userscript_missing",
                                "path": str(script_path),
                            },
                        )
                        return
                    body = script_path.read_bytes()
                    self.send_response(200)
                    self.send_header(
                        "Content-Type", "application/javascript; charset=utf-8"
                    )
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    _cors_headers(self)
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path == "/api/extension/status":
                    _send_json(self, 200, extension_install.status())
                    return
                if path == "/api/setup/status":
                    _send_json(self, 200, onboarding.status())
                    return
                if path == "/api/app/startup":
                    _send_json(self, 200, {"ok": True, **onboarding.startup.status()})
                    return
                if path == "/api/health":
                    auto_status = auto_switch.status() if auto_switch else None
                    _send_json(
                        self,
                        200,
                        {
                            "ok": True,
                            "version": __version__,
                            "desktop_launcher": app_runtime is not None,
                            "service_pid": os.getpid(),
                            "switching_implemented": config.SWITCHING_IMPLEMENTED,
                            "switching_enabled": switch_manager.enabled(),
                            "switch_block_reason": (
                                None
                                if switch_manager.enabled()
                                else config.SWITCH_BLOCK_REASON
                            ),
                            "auto_switch_enabled": config.auto_switch_enabled(),
                            "auto_switch_running": bool(
                                auto_status and auto_status.get("running")
                            ),
                            "goal_resume_enabled": goal_resume.enabled(),
                            "goal_resume_running": goal_resume.running(),
                            "session_api_version": 4,
                    "theme_subscribers": THEME_BUS.count(),
                            "login_enabled": config.login_enabled(),
                            "login_driver": config.login_driver(),
                            "auto_login_on_import": config.auto_login_on_import(),
                            "sms_enabled": config.sms_enabled(),
                            "sms_provider": config.sms_provider(),
                            "quota_url": config.usage_url(),
                        },
                    )
                    return
                if path == "/api/status":
                    _send_json(
                        self,
                        200,
                        _status_payload(
                            store,
                            auth_path,
                            switch_manager,
                            auto_switch=auto_switch,
                        ),
                    )
                    return
                if path == "/api/quota":
                    email = (query.get("email", [""])[0] or "").strip()
                    force = (query.get("force", ["0"])[0] == "1")
                    if email:
                        quota = quota_service.for_email(email, force=force)
                    else:
                        quota = quota_service.current(force=force)
                    status = 404 if quota.get("error") == "snapshot_not_found" else 200
                    _send_json(
                        self,
                        status,
                        {"ok": status == 200, "quota": quota},
                    )
                    return
                if path == "/api/accounts/quotas":
                    # Per-account quota for the pool view. Each account is
                    # probed with its own encrypted snapshot token, in
                    # parallel, and cached for ``pool_quota_ttl`` seconds so
                    # the 5s dashboard poll does not hammer the upstream.
                    force = (query.get("force", ["0"])[0] == "1")
                    ttl = float(config.pool_quota_ttl_seconds())
                    accounts = store.list()
                    emails = [
                        account["email"]
                        for account in accounts
                        if account.get("has_auth_snapshot") and account.get("email")
                    ]
                    quotas: dict[str, dict] = {}

                    def _probe(address: str) -> tuple[str, dict]:
                        try:
                            return address, quota_service.for_email(
                                address, force=force, ttl=ttl, scope="pool"
                            )
                        except Exception as exc:  # noqa: BLE001
                            return address, {
                                "ok": False,
                                "error": "request_failed",
                                "message": str(exc)[:200],
                            }

                    if emails:
                        workers = min(6, len(emails))
                        with ThreadPoolExecutor(max_workers=workers) as pool:
                            for address, payload in pool.map(_probe, emails):
                                quotas[address] = payload
                    skipped = [
                        account["email"]
                        for account in accounts
                        if account.get("email")
                        and not account.get("has_auth_snapshot")
                    ]
                    _send_json(
                        self,
                        200,
                        {
                            "ok": True,
                            "ttl": ttl,
                            "quotas": quotas,
                            "skipped": skipped,
                            "fetched_at": datetime.now(timezone.utc).isoformat(
                                timespec="seconds"
                            ),
                        },
                    )
                    return
                if path == "/api/quota/history":
                    email = (query.get("email", [""])[0] or "").strip()
                    try:
                        hours = int(query.get("hours", ["24"])[0])
                    except (TypeError, ValueError):
                        hours = 24
                    hours = max(1, min(168, hours))
                    if not email:
                        auth = read_auth_state(auth_path)
                        if auth.get("logged_in"):
                            email = auth.get("email") or ""
                    if not email:
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "email_required",
                                "message": "没有可查询的账号邮箱",
                            },
                        )
                        return
                    samples = store.quota_history(email, hours=hours)
                    _send_json(
                        self,
                        200,
                        {
                            "ok": True,
                            "email": email,
                            "hours": hours,
                            "samples": samples,
                            "prediction": predict_quota(None, samples),
                        },
                    )
                    return
                if path == "/api/auto-switch/status":
                    if auto_switch is not None:
                        auto_status = auto_switch.status()
                    else:
                        auto_status = {
                            "enabled": config.auto_switch_enabled(),
                            "running": False,
                            "last_result": "not_started",
                            "last_quota": None,
                            "switches_last_hour": 0,
                            "max_switches_per_hour": config.auto_switch_max_per_hour(),
                        }
                    _send_json(
                        self,
                        200,
                        {"ok": True, "auto_switch": auto_status},
                    )
                    return
                if path == "/api/snapshots":
                    _send_json(
                        self,
                        200,
                        {
                            "ok": True,
                            "switching": switch_manager.status(),
                            "current": switch_manager.current(),
                            "snapshots": switch_manager.list_targets(),
                        },
                    )
                    return
                if path == "/api/accounts":
                    accounts = store.list()
                    overlay = _login_overlay(login_manager)
                    for account in accounts:
                        account["login"] = overlay.get(account["email"].lower())
                    _send_json(
                        self,
                        200,
                        {"ok": True, "accounts": accounts, "login_concurrency": login_manager.concurrency()},
                    )
                    return
                if path == "/api/desktop/status":
                    _send_json(
                        self,
                        200,
                        {"ok": True, "desktop": desktop_client.status()},
                    )
                    return
                if path == "/api/desktop/processes":
                    # Who could be holding a thread writer: desktop app, our
                    # own app-server job, or a codex CLI in a terminal.
                    _send_json(
                        self,
                        200,
                        {"ok": True, **desktop_client.processes()},
                    )
                    return
                if path == "/api/goals":
                    payload = goal_resume.list(
                        include_subagents=(
                            query.get("subagents", ["0"])[0] == "1"
                        )
                    )
                    payload["session_api_version"] = 4
                    _send_json(self, 200, {"ok": True, **payload})
                    return
                if path == "/api/sms/status":
                    _send_json(
                        self,
                        200,
                        {
                            "ok": True,
                            "enabled": sms.enabled(),
                            "provider": sms.provider(),
                            "service": config.sms_service(),
                            "country": config.sms_country(),
                            "max_price": config.sms_max_price(),
                            "code_wait_seconds": config.sms_code_wait(),
                            "poll_interval_seconds": config.sms_poll_interval(),
                            "balance": sms.balance(
                                force=(query.get("balance", ["0"])[0] == "1")
                            ),
                            "activations": sms.activations(),
                            "last_code": sms.last_code(),
                            "events": sms.recent_events(20),
                        },
                    )
                    return
                if path == "/api/config":
                    _send_json(self, 200, {"ok": True, **config.public_config()})
                    return
                if path == "/api/settings":
                    _send_json(self, 200, {"ok": True, **settings.public()})
                    return
                if path == "/api/theme":
                    _send_json(self, 200, {"ok": True, **ui_prefs.state()})
                    return
                if path == "/api/login/status":
                    _send_json(self, 200, _login_payload(login_manager))
                    return
                if path.startswith("/api/login/session/"):
                    session_id = path.rsplit("/", 1)[-1]
                    session = login_manager.get(session_id)
                    if session is None:
                        _send_json(
                            self,
                            404,
                            {
                                "ok": False,
                                "error": "session_not_found",
                                "session_id": session_id,
                            },
                        )
                        return
                    _send_json(
                        self, 200, {"ok": True, "session": session.to_public()}
                    )
                    return
                _send_json(
                    self,
                    404,
                    {"ok": False, "error": "not_found", "path": path},
                )
            except Exception as exc:
                sys.stderr.write(f"[GET {path}] {exc}\n")
                _send_json(
                    self,
                    500,
                    {"ok": False, "error": "internal_error", "message": str(exc)},
                )

        def do_POST(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            try:
                if path in {"/api/setup/finish", "/api/setup/codex", "/api/setup/browser", "/api/app/startup", "/api/app/exit"}:
                    origin = self.headers.get("Origin")
                    allowed = {f"http://{host}:{self.server.server_port}" for host in ("127.0.0.1", "localhost", "[::1]")}
                    if origin and origin not in allowed:
                        _send_json(self, 403, {"ok": False, "error": "local_origin_required"})
                        return
                    body = _read_json(self)
                    if body.get("confirm") is not True:
                        raise ValueError("操作需要 confirm=true")
                    if path == "/api/app/exit":
                        token = self.headers.get("X-Cs-Instance", "")
                        if app_runtime is None or not hmac.compare_digest(token, app_runtime.token):
                            _send_json(self, 403, {"ok": False, "error": "instance_token_required"})
                            return
                        _send_json(self, 200, {"ok": True})
                        app_runtime.stop()
                        return
                    if path == "/api/setup/finish":
                        payload = onboarding.finish(body.get("action"))
                    elif path == "/api/setup/codex":
                        payload = onboarding.select_codex(body.get("path"))
                    elif path == "/api/setup/browser":
                        payload = {"ok": True, "browser": onboarding.components.start()}
                    else:
                        payload = {"ok": True, **onboarding.startup.set_enabled(body.get("enabled"))}
                    _send_json(self, 200, payload)
                    return
                if path == "/api/theme":
                    body = _read_json(self)
                    try:
                        saved = ui_prefs.set_theme(body.get("theme") or "")
                    except ValueError as exc:
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "invalid_theme",
                                "message": str(exc),
                            },
                        )
                        return
                    _send_json(self, 200, {"ok": True, **ui_prefs.state(), "saved": saved})
                    # Push to the extension side panel(s) instead of making
                    # them poll: one round trip instead of up to 2 s.
                    THEME_BUS.publish(ui_prefs.state())
                    return
                if path == "/api/quota/reset":
                    body = _read_json(self)
                    if body.get("confirm") is not True:
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "confirmation_required",
                                "message": "消耗重置券需要 confirm=true（不会二次确认，请自行确认）。",
                            },
                        )
                        return
                    email = (body.get("email") or "").strip()
                    result = quota_service.consume_reset(
                        email=email or None,
                        credit_id=(body.get("credit_id") or None),
                    )
                    fresh = None
                    if result.get("ok"):
                        try:
                            fresh = (
                                quota_service.for_email(email, force=True)
                                if email
                                else quota_service.current(force=True)
                            )
                        except Exception:  # noqa: BLE001 - the reset already worked
                            fresh = None
                    _send_json(
                        self,
                        200 if result.get("ok") else 400,
                        {"ok": bool(result.get("ok")), "result": result, "quota": fresh},
                    )
                    return
                if path == "/api/settings":
                    body = _read_json(self)
                    if body.get("confirm") is not True:
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "confirmation_required",
                                "message": "保存配置需要 confirm=true",
                            },
                        )
                        return
                    try:
                        changed = settings.save(body.get("values") or {})
                        cleared = settings.clear(body.get("clear") or [])
                    except ValueError as exc:
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "invalid_settings",
                                "message": str(exc),
                            },
                        )
                        return
                    touched = set(changed) | set(cleared)
                    if auto_switch is not None and "enable_auto_switch" in touched:
                        # The monitor thread reads its flags live, so applying
                        # the change here avoids a service restart.
                        try:
                            if config.auto_switch_enabled():
                                auto_switch.start()
                            else:
                                auto_switch.stop()
                        except Exception as exc:  # noqa: BLE001
                            sys.stderr.write(
                                f"[settings] auto-switch apply failed: {exc}\n"
                            )
                    payload = settings.public()
                    _send_json(
                        self,
                        200,
                        {
                            "ok": True,
                            "changed": changed,
                            "cleared": cleared,
                            "restart_required": settings.restart_required(
                                changed + cleared
                            ),
                            **payload,
                        },
                    )
                    return

                if path == "/api/switching/toggle":
                    body = _read_json(self)
                    if body.get("confirm") is not True:
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "confirmation_required",
                                "message": "切换开关需要 confirm=true",
                            },
                        )
                        return
                    key = str(body.get("key") or "").strip()
                    if key != "enable_auto_switch":
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "bad_key",
                                "message": "手动切号始终可用，key 仅支持 enable_auto_switch",
                            },
                        )
                        return
                    value = bool(body.get("value"))
                    settings.save({key: "1" if value else "0"})
                    if auto_switch is not None:
                        try:
                            if config.auto_switch_enabled():
                                auto_switch.start()
                            else:
                                auto_switch.stop()
                        except Exception as exc:  # noqa: BLE001
                            sys.stderr.write(
                                f"[toggle] auto-switch apply failed: {exc}\n"
                            )
                    _send_json(
                        self,
                        200,
                        {
                            "ok": True,
                            "key": key,
                            "value": value,
                            "switching": switch_manager.status(),
                            "auto_switch": auto_switch.status()
                            if auto_switch is not None
                            else None,
                            "settings": settings.public(),
                        },
                    )
                    return

                if path == "/api/accounts/login":
                    body = _read_json(self)
                    email = (body.get("email") or "").strip()
                    if not email:
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "email_required",
                                "message": "email 不能为空",
                            },
                        )
                        return
                    try:
                        info = login_manager.enqueue(email)
                    except LoginError as exc:
                        status = (
                            423
                            if exc.code == "login_disabled"
                            else 404
                            if exc.code == "account_not_found"
                            else 400
                        )
                        _send_json(
                            self,
                            status,
                            {
                                "ok": False,
                                "error": exc.code,
                                "message": exc.message,
                            },
                        )
                        return
                    _send_json(self, 200, {"ok": True, "login": info})
                    return

                if path == "/api/accounts/import":
                    body = _read_json(self)
                    text = body.get("text")
                    if not isinstance(text, str) or not text.strip():
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "empty_text",
                                "message": "text 不能为空",
                            },
                        )
                        return
                    accounts, errors = parse_accounts(text)
                    inserted = 0
                    updated = 0
                    stored = []
                    for account in accounts:
                        result = store.upsert(account)
                        if result == "inserted":
                            inserted += 1
                        else:
                            updated += 1
                        stored.append(public_account(account))
                    login_queued = []
                    login_tasks = []
                    login_errors = []
                    login_auto = config.auto_login_on_import()
                    if login_auto:
                        snapshot_state = {
                            snap["email"].lower(): bool(snap["expired"])
                            for snap in store.list_auth_snapshots()
                        }
                        for account in accounts:
                            email = account["email"]
                            if not account.get("password"):
                                continue
                            key = email.lower()
                            if key in snapshot_state and not snapshot_state[key]:
                                continue
                            try:
                                info = login_manager.enqueue(email, source="import")
                            except LoginError as exc:
                                login_errors.append(
                                    {"email": email, "error": exc.code}
                                )
                                continue
                            login_tasks.append(info)
                            login_queued.append(email)
                    _send_json(
                        self,
                        200,
                        {
                            "ok": True,
                            "inserted": inserted,
                            "updated": updated,
                            "errors": errors,
                            "accounts": stored,
                            "login_auto": login_auto,
                            "login_concurrency": login_manager.concurrency(),
                            "login_queued": login_queued,
                            "login_tasks": login_tasks,
                            "login_errors": login_errors,
                        },
                    )
                    return

                if path == "/api/accounts/add":
                    body = _read_json(self)
                    email = (body.get("email") or "").strip()
                    if not EMAIL_RE.match(email):
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "invalid_email",
                                "message": "邮箱格式不正确",
                            },
                        )
                        return
                    password = body.get("password")
                    if password is None:
                        password = ""
                    if not isinstance(password, str):
                        raise ValueError("password 必须是字符串")
                    totp = normalize_totp_secret(body.get("totp") or "")
                    if totp and not BASE32_RE.fullmatch(totp):
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "invalid_totp",
                                "message": "2FA 必须是 base32 或 otpauth:// 链接",
                            },
                        )
                        return
                    note = str(body.get("note") or "").strip()[:200]
                    result = store.upsert(
                        {
                            "email": email,
                            "password": password,
                            "totp": totp,
                            "totp_format": "base32" if totp else "none",
                            "tags": normalize_tags(body.get("tags")),
                            "note": note,
                            "priority": parse_priority(body.get("priority"), 100),
                        }
                    )
                    if body.get("disabled") is True:
                        store.update_account(email, disabled=True)
                    login = None
                    if config.auto_login_on_import() and password:
                        try:
                            login = login_manager.enqueue(email, source="account_add")
                        except LoginError as exc:
                            login = {
                                "state": "error",
                                "error": exc.code,
                                "message": exc.message,
                            }
                    _send_json(
                        self,
                        200,
                        {
                            "ok": True,
                            "result": result,
                            "account": store.get(email),
                            "login": login,
                        },
                    )
                    return

                if path == "/api/accounts/update":
                    body = _read_json(self)
                    email = (body.get("email") or "").strip()
                    if not email:
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "email_required",
                                "message": "email 不能为空",
                            },
                        )
                        return
                    if store.get(email) is None:
                        _send_json(
                            self,
                            404,
                            {
                                "ok": False,
                                "error": "account_not_found",
                                "message": "账号池里没有这个账号",
                            },
                        )
                        return

                    kwargs: dict = {}
                    password = body.get("password")
                    if password:
                        if not isinstance(password, str):
                            raise ValueError("password 必须是字符串")
                        kwargs["password"] = password
                    totp_raw = body.get("totp")
                    if totp_raw:
                        totp = normalize_totp_secret(totp_raw)
                        if not totp or not BASE32_RE.fullmatch(totp):
                            _send_json(
                                self,
                                400,
                                {
                                    "ok": False,
                                    "error": "invalid_totp",
                                    "message": "2FA 必须是 base32 或 otpauth:// 链接",
                                },
                            )
                            return
                        kwargs["totp"] = totp
                    if body.get("clear_totp") is True:
                        kwargs["clear_totp"] = True
                    if "tags" in body:
                        kwargs["tags"] = normalize_tags(body.get("tags"))
                    if "note" in body:
                        kwargs["note"] = str(body.get("note") or "").strip()[:200]
                    if "priority" in body:
                        kwargs["priority"] = parse_priority(body.get("priority"), 100)
                    if "disabled" in body:
                        kwargs["disabled"] = bool(body.get("disabled"))

                    account = store.update_account(email, **kwargs)
                    login = None
                    if config.auto_login_on_import() and password:
                        try:
                            login = login_manager.enqueue(email, source="account_update")
                        except LoginError as exc:
                            login = {
                                "state": "error",
                                "error": exc.code,
                                "message": exc.message,
                            }
                    _send_json(
                        self, 200, {"ok": True, "account": account, "login": login}
                    )
                    return

                if path == "/api/accounts/reorder":
                    body = _read_json(self)
                    emails = [
                        str(item).strip()
                        for item in (body.get("emails") or [])
                        if str(item).strip()
                    ]
                    if not emails:
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "emails_required",
                                "message": "emails 不能为空",
                            },
                        )
                        return
                    lowered = [email.lower() for email in emails]
                    if len(set(lowered)) != len(lowered):
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "duplicate_email",
                                "message": "emails 里有重复账号",
                            },
                        )
                        return
                    accounts = {item["email"].lower(): item for item in store.list()}
                    unknown = [email for email in emails if email.lower() not in accounts]
                    if unknown:
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "unknown_email",
                                "message": f"账号池里没有：{unknown[0]}",
                            },
                        )
                        return
                    # Top of the list wins: 100, 99, 98 ... clamped at 1.
                    for index, email in enumerate(emails):
                        store.update_account(
                            email, priority=max(1, 100 - index)
                        )
                    _send_json(
                        self,
                        200,
                        {"ok": True, "accounts": store.list()},
                    )
                    return

                if path == "/api/accounts/delete":
                    body = _read_json(self)
                    email = (body.get("email") or "").strip()
                    if not email:
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "email_required",
                                "message": "email 不能为空",
                            },
                        )
                        return
                    if body.get("confirm") is not True:
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "confirmation_required",
                                "message": "删除账号需要 confirm=true",
                            },
                        )
                        return
                    if store.get(email) is None:
                        _send_json(
                            self,
                            404,
                            {
                                "ok": False,
                                "error": "account_not_found",
                                "message": "账号池里没有这个账号",
                            },
                        )
                        return
                    auth = read_auth_state(auth_path)
                    is_current = bool(
                        auth.get("logged_in")
                        and (auth.get("email") or "").lower() == email.lower()
                    )
                    deleted = store.delete(email)
                    payload = {"ok": True, "deleted": deleted, "email": email}
                    if is_current:
                        payload["warning"] = (
                            "该账号仍是当前 live auth.json；只删除了池内记录和快照，"
                            "~/.codex/auth.json 未被修改。"
                        )
                    _send_json(self, 200, payload)
                    return

                if path == "/api/sms/test":
                    body = _read_json(self)
                    if body.get("confirm") is not True:
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "confirmation_required",
                                "message": "取号会占用平台额度，需要 confirm=true",
                            },
                        )
                        return
                    try:
                        activation_id, phone = sms.acquire_number()
                    except sms.SmsError as exc:
                        _send_json(
                            self,
                            400,
                            {"ok": False, "error": exc.code, "message": exc.message},
                        )
                        return
                    # Try to hand it back right away so a test costs nothing.
                    sms.cancel(activation_id)
                    _send_json(
                        self,
                        200,
                        {
                            "ok": True,
                            "activation_id": activation_id,
                            "phone": phone,
                            "cancel_scheduled": True,
                            "message": "号码已获取，正在释放退款"
                            + (
                                "（Grizzly 需等约 2 分钟）"
                                if sms.provider() == "grizzly"
                                else ""
                            ),
                        },
                    )
                    return

                if path == "/api/extension/install":
                    body = _read_json(self)
                    result = extension_install.install(
                        copy=body.get("copy", True) is not False,
                        target=str(body.get("target") or "path"),
                    )
                    _send_json(self, 200 if result.get("ok") else 400, result)
                    return

                if path == "/api/sms/cancel":
                    body = _read_json(self)
                    if body.get("confirm") is not True:
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "confirmation_required",
                                "message": "释放号码需要 confirm=true",
                            },
                        )
                        return
                    activation_id = str(body.get("activation_id") or "").strip()
                    targets = (
                        [activation_id]
                        if activation_id
                        else [item["activation_id"] for item in sms.activations()]
                    )
                    if not targets:
                        _send_json(
                            self,
                            200,
                            {
                                "ok": True,
                                "cancelled": [],
                                "message": "没有需要释放的号码",
                            },
                        )
                        return
                    for item in targets:
                        sms.cancel(item)
                    _send_json(
                        self,
                        200,
                        {
                            "ok": True,
                            "cancelled": targets,
                            "message": "已安排释放退款",
                        },
                    )
                    return

                if path == "/api/desktop/restart":
                    body = _read_json(self)
                    if body.get("confirm") is not True:
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "confirmation_required",
                                "message": "重启桌面端会关闭 ChatGPT 窗口，需要 confirm=true",
                            },
                        )
                        return
                    desktop = desktop_client.restart(force=True)
                    _send_json(
                        self,
                        200,
                        {"ok": bool(desktop.get("ok")), "desktop": desktop},
                    )
                    return

                if path == "/api/goals/resume":
                    body = _read_json(self)
                    ids = body.get("thread_ids")
                    if not isinstance(ids, list):
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "thread_ids_required",
                                "message": "thread_ids 必须是数组",
                            },
                        )
                        return
                    for option in ("dry_run", "confirm", "restart_desktop", "takeover_desktop", "continue_once"):
                        if option in body and not isinstance(body[option], bool):
                            _send_json(
                                self, 400,
                                {
                                    "ok": False,
                                    "error": "invalid_option",
                                    "message": f"{option} 必须是 JSON 布尔值",
                                },
                            )
                            return
                    dry_run = body.get("dry_run") is not False
                    try:
                        job = goal_resume.start(
                            ids,
                            dry_run=dry_run,
                            confirm=body.get("confirm") is True,
                            restart_desktop=body.get("restart_desktop") is True,
                            takeover_desktop=body.get("takeover_desktop"),
                            continue_once=body.get("continue_once") is True,
                        )
                    except GoalResumeError as exc:
                        status = {
                            "goal_resume_disabled": 403,
                            "confirmation_required": 400,
                            "thread_required": 400,
                            "too_many_threads": 400,
                            "job_running": 409,
                        }.get(exc.code, 400)
                        _send_json(
                            self,
                            status,
                            {
                                "ok": False,
                                "error": exc.code,
                                "message": exc.message,
                            },
                        )
                        return
                    _send_json(self, 200, {"ok": True, "job": job})
                    return

                if path == "/api/goals/remove":
                    body = _read_json(self)
                    if body.get("confirm") is not True:
                        _send_json(self, 400, {"ok": False, "error": "confirmation_required"})
                        return
                    if not isinstance(body.get("thread_ids"), list):
                        _send_json(self, 400, {"ok": False, "error": "thread_ids_required"})
                        return
                    try:
                        outcome = goal_resume.remove(body["thread_ids"])
                    except (GoalResumeError, AppServerError) as exc:
                        _send_json(self, 400, {"ok": False, "error": getattr(exc, "code", "remove_failed"), "message": str(exc)})
                        return
                    _send_json(self, 200 if outcome.get("ok") else 207, outcome)
                    return

                if path == "/api/goals/pause":
                    body = _read_json(self)
                    if body.get("confirm") is not True:
                        _send_json(self, 400, {"ok": False, "error": "confirmation_required"})
                        return
                    if not isinstance(body.get("thread_ids"), list):
                        _send_json(self, 400, {"ok": False, "error": "thread_ids_required"})
                        return
                    try:
                        outcome = goal_resume.pause(body["thread_ids"])
                    except (GoalResumeError, AppServerError) as exc:
                        _send_json(self, 400, {"ok": False, "error": getattr(exc, "code", "pause_failed"), "message": str(exc)})
                        return
                    _send_json(self, 200 if outcome.get("ok") else 207, outcome)
                    return

                if path == "/api/goals/cancel-queued":
                    body = _read_json(self)
                    if body.get("confirm") is not True:
                        _send_json(self, 400, {"ok": False, "error": "confirmation_required"})
                        return
                    if not isinstance(body.get("thread_ids"), list):
                        _send_json(self, 400, {"ok": False, "error": "thread_ids_required"})
                        return
                    try:
                        outcome = goal_resume.cancel_queued(body["thread_ids"])
                    except (GoalResumeError, AppServerError) as exc:
                        _send_json(self, 400, {"ok": False, "error": getattr(exc, "code", "queue_delete_failed"), "message": str(exc)})
                        return
                    _send_json(self, 200, outcome)
                    return

                if path == "/api/goals/cancel":
                    body = _read_json(self)
                    if body.get("confirm") is not True:
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "confirmation_required",
                                "message": "取消续跑需要 confirm=true",
                            },
                        )
                        return
                    thread_ids = body.get("thread_ids")
                    if isinstance(thread_ids, list) and thread_ids:
                        outcome = goal_resume.stop(thread_ids)
                        _send_json(self, 200, outcome)
                        return
                    cancelled = goal_resume.cancel()
                    _send_json(
                        self,
                        200,
                        {"ok": True, "cancelled": cancelled},
                    )
                    return

                if path == "/api/goals/auto-resume":
                    body = _read_json(self)
                    thread_id = (body.get("thread_id") or "").strip()
                    if not thread_id:
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "thread_required",
                                "message": "thread_id 不能为空",
                            },
                        )
                        return
                    payload = goal_resume.set_auto_resume_thread(
                        thread_id, body.get("enabled") is True
                    )
                    _send_json(self, 200, payload)
                    return

                if path == "/api/goals/create":
                    body = _read_json(self)
                    if body.get("confirm") is not True:
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "confirmation_required",
                                "message": "创建目标会写入 Codex 的 goals 库，需要 confirm=true",
                            },
                        )
                        return
                    ids = body.get("thread_ids")
                    if not isinstance(ids, list):
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "thread_ids_required",
                                "message": "thread_ids 必须是数组",
                            },
                        )
                        return
                    try:
                        payload = goal_resume.create_goals(
                            ids,
                            str(body.get("objective") or ""),
                            status=str(body.get("status") or "paused"),
                        )
                    except GoalResumeError as exc:
                        _send_json(
                            self,
                            {
                                "goal_resume_disabled": 403,
                                "job_running": 409,
                            }.get(exc.code, 400),
                            {
                                "ok": False,
                                "error": exc.code,
                                "message": exc.message,
                            },
                        )
                        return
                    _send_json(self, 200 if payload.get("ok") else 207, payload)
                    return

                if path == "/api/switch":
                    body = _read_json(self)
                    email = (body.get("email") or "").strip()
                    confirm = body.get("confirm") is True
                    restart_raw = body.get("restart_desktop")
                    restart_flag = (
                        None if restart_raw is None else bool(restart_raw)
                    )
                    try:
                        result = switch_manager.switch_to(
                            email,
                            confirm=confirm,
                            restart_desktop=restart_flag,
                        )
                    except SwitchError as exc:
                        status = {
                            "switching_disabled": 423,
                            "confirmation_required": 400,
                            "email_required": 400,
                            "snapshot_not_found": 404,
                            "already_active": 409,
                            "target_snapshot_invalid": 422,
                            "snapshot_email_mismatch": 422,
                            "current_snapshot_failed": 500,
                            "backup_failed": 500,
                            "write_failed": 500,
                            "verify_failed": 500,
                        }.get(exc.code, 400)
                        payload = {
                            "ok": False,
                            "error": exc.code,
                            "message": exc.message,
                            "switching": switch_manager.status(),
                        }
                        if exc.detail:
                            payload["detail"] = exc.detail
                        _send_json(self, status, payload)
                        return
                    _send_json(
                        self,
                        200,
                        {
                            "ok": True,
                            "switching": switch_manager.status(),
                            "switch": result.to_public(),
                        },
                    )
                    return

                if path == "/api/login/start":
                    body = _read_json(self)
                    email = (body.get("email") or "").strip()
                    if not email:
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "email_required",
                                "message": "email 不能为空",
                            },
                        )
                        return
                    try:
                        session = login_manager.start(email)
                    except LoginError as exc:
                        status = 423 if exc.code == "login_disabled" else 400
                        payload = {
                            "ok": False,
                            "error": exc.code,
                            "message": exc.message,
                            "login_enabled": config.login_enabled(),
                        }
                        if exc.detail:
                            payload["detail"] = exc.detail
                        _send_json(self, status, payload)
                        return
                    _send_json(
                        self,
                        200,
                        {
                            "ok": True,
                            "login_enabled": config.login_enabled(),
                            "driver": session.driver,
                            "session": session.to_public(),
                        },
                    )
                    return

                if path == "/api/login/cancel":
                    body = _read_json(self)
                    session_id = (body.get("session_id") or "").strip()
                    if not session_id:
                        _send_json(
                            self,
                            400,
                            {
                                "ok": False,
                                "error": "session_id_required",
                                "message": "session_id 不能为空",
                            },
                        )
                        return
                    cancelled = login_manager.cancel(session_id)
                    _send_json(
                        self,
                        200 if cancelled else 404,
                        {
                            "ok": cancelled,
                            "cancelled": cancelled,
                            "session_id": session_id,
                            "error": None if cancelled else "session_not_found",
                        },
                    )
                    return

                _send_json(
                    self,
                    404,
                    {"ok": False, "error": "not_found", "path": path},
                )
            except ValueError as exc:
                _send_json(
                    self,
                    400,
                    {"ok": False, "error": "bad_request", "message": str(exc)},
                )
            except Exception as exc:
                sys.stderr.write(f"[POST {path}] {exc}\n")
                _send_json(
                    self,
                    500,
                    {"ok": False, "error": "internal_error", "message": str(exc)},
                )

    return Handler


def create_server(
    store: AccountStore,
    auth_path: Path,
    host: str,
    port: int,
    login_manager: LoginManager | None = None,
    switch_manager: SwitchManager | None = None,
    quota_service: QuotaService | None = None,
    auto_switch: AutoSwitchMonitor | None = None,
    desktop_client: DesktopClient | None = None,
    goal_resume: GoalResumeService | None = None,
    onboarding=None,
    app_runtime=None,
) -> ThreadingHTTPServer:
    login_manager = login_manager or LoginManager(store)
    desktop_client = desktop_client or DesktopClient()
    switch_manager = switch_manager or SwitchManager(
        store, auth_path, desktop_client=desktop_client
    )
    quota_service = quota_service or QuotaService(store, auth_path)
    auto_switch = auto_switch or AutoSwitchMonitor(
        store, switch_manager, quota_service
    )
    goal_resume = goal_resume or GoalResumeService(desktop_client=desktop_client)
    if getattr(switch_manager, "on_switched", None) is None:
        switch_manager.on_switched = goal_resume.on_account_switched
    if getattr(switch_manager, "before_switch", None) is None:
        switch_manager.before_switch = goal_resume.capture_before_switch
    handler = make_handler(
        store,
        auth_path,
        login_manager,
        switch_manager,
        quota_service=quota_service,
        auto_switch=auto_switch,
        desktop_client=desktop_client,
        goal_resume=goal_resume,
        onboarding=onboarding,
        app_runtime=app_runtime,
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.login_manager = login_manager  # type: ignore[attr-defined]
    server.switch_manager = switch_manager  # type: ignore[attr-defined]
    server.quota_service = quota_service  # type: ignore[attr-defined]
    server.auto_switch = auto_switch  # type: ignore[attr-defined]
    server.desktop_client = desktop_client  # type: ignore[attr-defined]
    server.goal_resume = goal_resume  # type: ignore[attr-defined]
    return server


def _existing_instance(host: str, port: int) -> dict | None:
    """Return the health payload of a service already on ``host:port``.

    ``ThreadingHTTPServer`` sets ``SO_REUSEADDR``, which on Windows lets a
    second process bind the same port instead of failing. Two live services
    then fight over ``auth.json`` and drive the same goal threads with
    different credentials — the "会话被占用 / 桌面端卡住" failure mode — so the
    second instance refuses to start.
    """
    import urllib.request

    probe_host = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    try:
        with urllib.request.urlopen(
            f"http://{probe_host}:{port}/api/health", timeout=2.0
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - no listener / not ours
        return None
    if isinstance(payload, dict) and payload.get("ok"):
        return payload
    return None


def run(app_runtime=None) -> None:
    store = AccountStore(config.data_dir() / "accounts.sqlite3")
    host = config.host()
    port = config.port()
    existing = _existing_instance(host, port)
    if existing is not None:
        store.close()
        print("")
        print(
            f"Codex Switcher 已经在 {host}:{port} 上运行"
            f"（版本 {existing.get('version')}），本次启动已退出。"
        )
        print(
            "两个实例会抢同一个 auth.json、同一个线程写锁，导致「会话被占用」。"
        )
        print("如需重启：先运行 .\\stop.ps1，再重新启动。")
        raise SystemExit(1)
    login_manager = LoginManager(store)
    desktop_client = DesktopClient()
    switch_manager = SwitchManager(
        store, config.auth_file(), desktop_client=desktop_client
    )
    quota_service = QuotaService(store, config.auth_file())
    auto_switch = AutoSwitchMonitor(store, switch_manager, quota_service)
    goal_resume = GoalResumeService(desktop_client=desktop_client)
    server = create_server(
        store,
        config.auth_file(),
        host,
        port,
        login_manager=login_manager,
        switch_manager=switch_manager,
        quota_service=quota_service,
        auto_switch=auto_switch,
        desktop_client=desktop_client,
        goal_resume=goal_resume,
        app_runtime=app_runtime,
    )
    url = f"http://{host}:{port}"
    switch_state = switch_manager.status()
    print("")
    print(f"Codex Switcher v{__version__}")
    print(f"Local service : {url}")
    print(f"Dashboard     : {url}/")
    print(f"Auth file     : {config.auth_file()}  (switch target)")
    print(f"Data dir      : {config.data_dir()}")
    print(
        "Switching     : "
        + ("ENABLED" if switch_state["enabled"] else "DISABLED (opt-in)")
        + (
            ""
            if switch_state["enabled"]
            else f" ({switch_state['block_reason']}) - auth.json is not modified"
        )
    )
    print(f"Backups       : {switch_manager.backup_dir}")
    print(
        "Login helper  : "
        + ("ENABLED" if config.login_enabled() else "DISABLED (opt-in)")
        + f" / driver={login_manager.driver_name}"
        + " / isolated CODEX_HOME, no auth.json swap"
    )
    print(
        "Auto switch   : "
        + (
            "ENABLED (quota + cooldown + circuit breaker)"
            if config.auto_switch_enabled()
            else "DISABLED (opt-in)"
        )
        + f" / interval={config.auto_switch_interval_seconds()}s"
    )
    print(f"Quota probe   : {config.usage_url()} (read-only)")
    print(
        "Desktop restart: "
        + (
            "ENABLED after switch (ChatGPT process tree)"
            if config.desktop_restart_enabled()
            else "DISABLED (opt-in; dashboard can force per switch)"
        )
    )
    print(
        "Goal resume   : "
        + (
            "ENABLED (queues 「继续」 into the thread; the Codex desktop app runs the turn)"
            if config.goal_resume_enabled()
            else "DISABLED (opt-in)"
        )
    )
    print("")
    print("Press Ctrl+C to stop.")
    print("")
    auto_switch.start()
    if app_runtime is not None:
        app_runtime.attach(server)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        auto_switch.stop()
        if app_runtime is not None:
            from .components import browser_components
            browser_components().cancel()
            for session in login_manager.list():
                login_manager.cancel(session["id"])
        server.server_close()
        store.close()


DASHBOARD_HTML = (Path(__file__).with_name("dashboard.html")).read_text(encoding="utf-8")
