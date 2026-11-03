"""Device-code login assistant.

Safety design:
- `codex login --device-auth` is always run with an isolated `CODEX_HOME`
  under the CodexSwitcher data directory. It never writes the real
  ~/.codex/auth.json.
- The resulting auth.json is stored as an encrypted snapshot in the account
  store. It is not copied to ~/.codex/auth.json in v0.2.
- The assistant is disabled unless CODEX_SWITCHER_ENABLE_LOGIN=1.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from . import sms as sms_client
from .accounts import mask_email, totp_code, totp_remaining
from .auth_state import read_auth_state
from .store import AccountStore

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
DEVICE_CODE_RE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,6})\b")
DEVICE_URL_RE = re.compile(r"https://auth\.openai\.com/codex/device")

DEFAULT_DEVICE_URL = "https://auth.openai.com/codex/device"
ACTIVE_STATUSES = {"starting", "waiting_for_browser", "waiting_for_cli", "waiting_for_input"}

# Phone verification shows up on the Codex OAuth path only. Match the URL
# first (cheap and stable) and fall back to page copy for localized variants.
PHONE_URL_MARKERS = (
    "phone-verification",
    "phone_verification",
    "add-phone",
    "add_phone",
    "/phone",
)
PHONE_BODY_MARKERS = (
    "verify your phone",
    "verify your phone number",
    "enter your phone number",
    "add a phone number",
    "add your phone number",
    "phone number verification",
    "验证你的手机号",
    "验证您的手机号",
    "验证手机号",
    "输入手机号",
    "请输入手机号",
    "添加手机号",
    "添加手机号码",
    "手机号码验证",
    "电话号码验证",
)

# The ChatGPT login landing page offers "使用手机号继续" as a *login method*;
# treating it as phone verification made the driver try to take a number
# before even typing the email/password. URL markers still win because the
# real verification page lives on /add-phone.
PHONE_LOGIN_CHOICE_MARKERS = (
    "使用手机号继续",
    "continue with phone",
    "sign up with phone",
)
PHONE_ERROR_MARKERS = (
    "invalid phone",
    "not a valid phone",
    "this phone number",
    "already in use",
    "too many attempts",
    "无法使用该号码",
    "号码无效",
    "尝试次数过多",
    "incorrect code",
    "invalid code",
    "code expired",
    "验证码不正确",
    "验证码错误",
    "验证码已过期",
)

# OpenAI sometimes refuses to text a (virtual) number and silently falls back
# to WhatsApp: the code can never arrive, so treat it as a hard reject.
PHONE_SMS_UNAVAILABLE_MARKERS = (
    "无法向该电话号码发送短信",
    "无法向该号码发送短信",
    "已切换为 whatsapp",
    "switched to whatsapp",
    "unable to send a text message",
    "cannot send a text message",
    "can't send a text message",
    "couldn't send a text message",
)


def is_sms_unavailable(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in PHONE_SMS_UNAVAILABLE_MARKERS)


def code_page_uses_whatsapp(body: str) -> bool:
    """True when the code page says the code was sent over WhatsApp."""
    lowered = (body or "").lower()
    if "whatsapp" not in lowered:
        return False
    return any(
        marker in lowered
        for marker in ("发送到", "发送至", "sent to", "send to", "message to")
    )


def is_phone_prompt(url: str, body: str) -> bool:
    """True when the current page is the phone-verification step."""
    if any(marker in (url or "").lower() for marker in PHONE_URL_MARKERS):
        return True
    text = (body or "").lower()
    if any(marker in text for marker in PHONE_LOGIN_CHOICE_MARKERS):
        return False
    return any(marker in text for marker in PHONE_BODY_MARKERS)


def phone_e164(digits: str) -> str:
    value = "".join(ch for ch in str(digits or "") if ch.isdigit())
    return f"+{value}" if value else ""


def mask_phone(digits: str) -> str:
    value = "".join(ch for ch in str(digits or "") if ch.isdigit())
    if not value:
        return ""
    if len(value) <= 4:
        return f"+{value}"
    return f"+{value[:2]}****{value[-4:]}"


def trace_login(session, message: str) -> None:
    """Append a short diagnostic line next to the session's artifacts."""
    # Never persist query strings: auth URLs carry one-time OAuth codes.
    safe = re.sub(r"\?[^\s]*", "?<redacted>", str(message))
    try:
        path = session.codex_home / "driver.log"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%H:%M:%S')} {safe}\n")
    except Exception:
        pass



class LoginError(Exception):
    def __init__(self, code: str, message: str, detail: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


def parse_device_code_output(text: str) -> tuple[str, str | None]:
    clean = ANSI_RE.sub("", text or "")
    url_match = DEVICE_URL_RE.search(clean)
    code_match = DEVICE_CODE_RE.search(clean)
    return (
        url_match.group(0) if url_match else DEFAULT_DEVICE_URL,
        code_match.group(1) if code_match else None,
    )


class ProcessOutput:
    """Thread-safe line collector that also persists the CLI log."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self.done = threading.Event()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    def add(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)
            if self.path is not None:
                try:
                    with self.path.open("a", encoding="utf-8") as fh:
                        fh.write(line)
                except Exception:
                    pass

    def text(self) -> str:
        with self._lock:
            return "".join(self._lines)

    def tail(self, limit: int = 1500) -> str:
        text = self.text()
        return text[-limit:]


def start_device_auth(
    codex_cmd: list[str],
    codex_home: Path,
    output_path: Path | None = None,
    timeout: float = 45.0,
    cancel_event: threading.Event | None = None,
) -> tuple[subprocess.Popen, str, str, ProcessOutput]:
    """Start `codex login --device-auth` and parse the device code.

    The process is left running so the browser can approve it.
    """
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    env.setdefault("RUST_BACKTRACE", "0")
    creationflags = (
        subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    )
    out = ProcessOutput(output_path)
    proc = subprocess.Popen(
        codex_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
        creationflags=creationflags,
    )
    assert proc.stdout is not None
    line_queue: queue.Queue[str | None] = queue.Queue()

    def reader() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                out.add(line)
                line_queue.put(line)
        finally:
            out.done.set()
            line_queue.put(None)

    threading.Thread(target=reader, daemon=True, name="codex-login-reader").start()

    deadline = time.time() + timeout
    url = DEFAULT_DEVICE_URL
    code: str | None = None
    while time.time() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            break
        try:
            line = line_queue.get(timeout=0.5)
        except queue.Empty:
            if proc.poll() is not None:
                break
            continue
        if line is None:
            break
        found_url, found_code = parse_device_code_output(out.text())
        url = found_url
        if found_code:
            code = found_code
            break

    if not code:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:
            pass
        if cancel_event is not None and cancel_event.is_set():
            raise LoginError("cancelled", "登录已取消")
        raise LoginError(
            "device_code_not_found",
            "没有从 CLI 输出里解析到设备码",
            out.tail(),
        )
    return proc, code, url, out


def wait_for_auth(
    proc: subprocess.Popen,
    auth_path: Path,
    out: ProcessOutput,
    timeout: float = 14 * 60,
    cancel_event: threading.Event | None = None,
) -> bytes:
    """Wait for the CLI to write auth.json and return its bytes."""
    deadline = time.time() + timeout
    last_size = -1
    stable = 0
    while time.time() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            raise LoginError("cancelled", "登录已取消")
        if auth_path.exists():
            try:
                size = auth_path.stat().st_size
                if size > 0 and size == last_size:
                    stable += 1
                else:
                    stable = 0
                last_size = size
                if stable >= 2:
                    data = auth_path.read_bytes()
                    if data.strip():
                        return data
            except Exception:
                pass
        exit_code = proc.poll()
        if exit_code is not None:
            if auth_path.exists():
                data = auth_path.read_bytes()
                if data.strip():
                    return data
            raise LoginError(
                "cli_failed",
                f"CLI 退出码 {exit_code}，没有生成 auth.json",
                out.tail(),
            )
        time.sleep(0.4)
    raise LoginError("timeout", "等待 CLI 写入 auth.json 超时", out.tail())


@dataclass
class LoginSession:
    id: str
    email: str
    codex_home: Path
    status: str = "starting"
    message: str = ""
    device_code: str | None = None
    verification_url: str = DEFAULT_DEVICE_URL
    driver: str = "manual"
    error: str | None = None
    source: str = "manual"
    phase: str = "queued"
    attempt: int = 1
    initial_login: bool = False
    purpose: str = "login"
    needs_input: bool = False
    attempt_errors: list[dict] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    auth_path: Path = field(init=False)
    process: subprocess.Popen | None = field(default=None, repr=False)
    output: ProcessOutput | None = field(default=None, repr=False)
    sms_activation_id: str | None = None
    sms_number: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    done_event: threading.Event = field(default_factory=threading.Event, repr=False)
    manual_event: threading.Event = field(default_factory=threading.Event, repr=False)
    on_change: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.auth_path = self.codex_home / "auth.json"

    def touch(self) -> None:
        self.updated_at = time.time()

    def waiting_for_input(self, message: str) -> None:
        self.status = "waiting_for_input"
        self.needs_input = True
        self.phase = "manual_verification"
        self.message = message
        self.touch()
        if self.on_change:
            self.on_change(self)

    def input_finished(self) -> None:
        self.needs_input = False
        self.status = "waiting_for_browser"
        self.touch()
        if self.on_change:
            self.on_change(self)

    def to_public(self) -> dict:
        active = self.status in ACTIVE_STATUSES
        payload = {
            "id": self.id,
            "email": self.email,
            "email_masked": mask_email(self.email),
            "status": self.status,
            "message": self.message,
            "device_code": self.device_code if active else None,
            "verification_url": self.verification_url,
            "driver": self.driver,
            "error": self.error,
            "source": self.source,
            "phase": self.phase,
            "attempt": self.attempt,
            "initial_login": self.initial_login,
            "purpose": self.purpose,
            "needs_input": self.needs_input,
            "attempt_errors": list(self.attempt_errors),
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "codex_home": str(self.codex_home),
        }
        if self.sms_number or self.sms_activation_id:
            payload["sms_number"] = mask_phone(self.sms_number or "")
            payload["sms_active"] = bool(self.sms_activation_id)
        if self.status == "failed" and self.output is not None:
            payload["log_tail"] = self.output.tail()
        return payload


class ManualDriver:
    """Leave the browser step to the user; CLI still runs in isolated home."""

    name = "manual"

    def complete_login(
        self,
        *,
        email: str,
        password: str,
        totp: str,
        device_url: str,
        device_code: str,
        session: LoginSession,
    ) -> None:
        session.waiting_for_input(
            f"请在浏览器打开 {device_url}，选择 {mask_email(email)}，"
            f"输入设备码 {device_code}。完成后 CLI 会自动写入临时 auth.json。"
        )
        try:
            import webbrowser
            webbrowser.open(device_url, new=2)
        except Exception:
            pass


class PlaywrightDriver:
    """Headed system Chrome automation for the login + device-code flow.

    This is opt-in via CODEX_SWITCHER_LOGIN_DRIVER=playwright. It uses a
    separate Chrome profile per account and never touches the user's browser
    profile or the real ~/.codex/auth.json.
    """

    name = "playwright"

    def __init__(self, profile_dir: Path, timeout_ms: int = 30_000):
        self.profile_dir = Path(profile_dir)
        self.timeout_ms = timeout_ms
        self._pw = None
        self._context = None
        self._page = None

    def complete_login(
        self,
        *,
        email: str,
        password: str,
        totp: str,
        device_url: str,
        device_code: str,
        session: LoginSession,
    ) -> None:
        try:
            self._start()
            page = self._page
            assert page is not None
            self._ensure_visible(page)
            session.phase = "signing_in"
            self._signin_or_manual(page, email, password, totp, session)
            session.phase = "authorizing_device"
            try:
                self._approve_device_code(page, email, device_url, device_code, session)
                self._settle_after_approval(page, session)
            except Exception as exc:
                if isinstance(exc, LoginError) and exc.code in {"cancelled", "browser_closed", "manual_verification_timeout"}:
                    raise
                self._wait_manual_authorization(page, session, "自动设备授权未完成")
        finally:
            self._close()

    def _signin_or_manual(self, page, email, password, totp, session):
        try:
            self._ensure_target_account(page, email, password, totp, session)
        except Exception as exc:
            if isinstance(exc, LoginError) and exc.code in {"cancelled", "browser_closed", "manual_verification_timeout"}:
                raise
            reason = exc.message if isinstance(exc, LoginError) else "自动登录未完成，请在浏览器继续"
            self._wait_manual_signin(page, email, session, reason)

    def _manual_check(self, page, session, deadline):
        if session.cancel_event.is_set():
            raise LoginError("cancelled", "操作已取消")
        if page.is_closed():
            raise LoginError("browser_closed", "验证窗口已关闭，可以重新打开任务继续")
        if time.monotonic() >= deadline:
            raise LoginError("manual_verification_timeout", "等待手动验证超时，账号资料已保留，可以重试")

    def _wait_manual_signin(self, page, email, session, reason):
        session.waiting_for_input(reason + "。请在当前浏览器完成验证，完成后会自动继续。")
        self._ensure_visible(page)
        deadline = time.monotonic() + 14 * 60
        while True:
            self._manual_check(page, session, deadline)
            actual = self._get_chatgpt_email(page)
            if actual and actual.casefold() == email.casefold():
                session.input_finished()
                return
            page.wait_for_timeout(1_000)

    def _wait_manual_authorization(self, page, session, reason):
        session.waiting_for_input(reason + "。请在当前浏览器完成设备授权，登录码可在 Switcher 中查看。")
        self._ensure_visible(page)
        deadline = time.monotonic() + 14 * 60
        while True:
            self._manual_check(page, session, deadline)
            if session.auth_path.exists():
                session.input_finished()
                return
            if session.process is not None and session.process.poll() is not None:
                raise LoginError("cli_failed", "设备授权已结束，请重新登录")
            page.wait_for_timeout(1_000)

    def _wait_manual_totp(self, page, email, session, store, reason, candidate=None):
        from .totp_setup import factor_enabled, read_binding_secret
        session.waiting_for_input(reason + "。请在浏览器继续设置，也可在 Switcher 填写已有密钥；不想配置时可跳过。")
        self._ensure_visible(page)
        deadline = time.monotonic() + 14 * 60
        while True:
            self._manual_check(page, session, deadline)
            actual = self._get_chatgpt_email(page)
            if actual and actual.casefold() == email.casefold():
                if not candidate:
                    try:
                        if page.locator('input[name="verification-code"]').is_visible():
                            candidate = read_binding_secret(page, email, allow_toggle=False)
                            store.stage_totp(email, candidate)
                    except Exception:
                        pass
                if factor_enabled(page):
                    if candidate and not (store.get_secret(email) or {}).get("totp"):
                        store.confirm_totp(email)
                    if (store.get_secret(email) or {}).get("totp"):
                        session.input_finished()
                        return "TOTP 已配置，密钥已加密保存"
                    session.message = "验证器已开启。请在 Switcher 填写绑定密钥以便自动登录，也可暂时跳过。"
                    session.touch()
            page.wait_for_timeout(1_000)

    def configure_totp(self, *, email, password, totp, session, store):
        from .totp_setup import BINDING_URL, SECURITY_URL, TotpSetupError, factor_enabled, factor_state, read_binding_secret
        candidate = None
        try:
            if session.cancel_event.is_set():
                raise LoginError("cancelled", "设置已取消")
            self._start()
            page = self._page
            assert page is not None
            self._signin_or_manual(page, email, password, totp, session)
            session.phase = "configuring_totp"
            session.message = "正在打开安全设置"
            session.touch()
            self._goto(page, SECURITY_URL, session)
            page.get_by_text(re.compile(r"^(身份验证器应用|Authenticator app)$", re.I)).first.wait_for(timeout=15_000)
            actual = self._get_chatgpt_email(page)
            if not actual or actual.casefold() != email.casefold():
                raise LoginError("account_mismatch", "设置页面所属账号未确认，请在浏览器核对账号")
            state = factor_state(page)
            if state == "enabled":
                return self._wait_manual_totp(page, email, session, store, "该账号已开启验证器，无法再次读取原密钥")
            if state != "disabled":
                return self._wait_manual_totp(page, email, session, store, "无法确认验证器状态，请在浏览器中手动设置")
            if session.cancel_event.is_set():
                raise LoginError("cancelled", "设置已取消")
            self._goto(page, BINDING_URL, session)
            actual = self._get_chatgpt_email(page)
            if not actual or actual.casefold() != email.casefold():
                raise LoginError("account_mismatch", "设置页面所属账号未确认，请在浏览器核对账号")
            if session.manual_event.is_set():
                return self._wait_manual_totp(page, email, session, store, "已切换为手动设置")
            candidate = read_binding_secret(page, email)
            if session.cancel_event.is_set():
                raise LoginError("cancelled", "设置已取消")
            # The seed must survive a service interruption after the server
            # accepts the code. Unconfirmed seeds never appear as configured.
            store.stage_totp(email, candidate)
            session.phase = "configuring_totp"
            session.message = "正在验证绑定码并确认开启结果"
            session.touch()
            remaining = totp_remaining()
            if remaining < 6 and session.cancel_event.wait(remaining + 1):
                raise LoginError("cancelled", "设置已取消")
            if session.cancel_event.is_set():
                raise LoginError("cancelled", "设置已取消")
            if session.manual_event.is_set():
                return self._wait_manual_totp(page, email, session, store, "已切换为手动设置", candidate)
            dialog = page.get_by_role("dialog").filter(has=page.locator('input[name="verification-code"]')).first
            dialog.locator('input[name="verification-code"]').fill(totp_code(candidate))
            if session.cancel_event.is_set():
                raise LoginError("cancelled", "设置已取消")
            dialog.get_by_role("button", name=re.compile(r"^(验证|Verify)$", re.I)).click()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if session.cancel_event.is_set():
                    raise LoginError("cancelled", "设置已取消")
                if session.manual_event.is_set():
                    break
                if not dialog.is_visible() and factor_enabled(page):
                    actual = self._get_chatgpt_email(page)
                    if actual and actual.casefold() == email.casefold():
                        store.confirm_totp(email)
                        return "TOTP 已开启，密钥已加密保存"
                page.wait_for_timeout(500)
            raise TotpSetupError("尚未确认绑定成功，请在浏览器检查并继续")
        except LoginError as exc:
            if exc.code in {"cancelled", "browser_closed", "manual_verification_timeout"} or self._page is None:
                raise
            return self._wait_manual_totp(self._page, email, session, store, exc.message, candidate)
        except Exception as exc:
            if self._page is None:
                raise LoginError("browser_start_failed", "无法打开设置窗口，请改用手动设置") from None
            reason = str(exc) if isinstance(exc, TotpSetupError) else "自动设置未完成"
            return self._wait_manual_totp(self._page, email, session, store, reason, candidate)
        finally:
            self._close()

    #: A fresh Chrome profile sometimes stalls on its first navigation when the
    #: system proxy (Clash/VPN) is switching nodes: ``page.goto`` then burns the
    #: whole timeout and the tab stays white. Retry instead of failing login.
    NAV_ATTEMPTS = 3
    NAV_TIMEOUT_MS = 45_000

    def _goto(self, page, url: str, session: LoginSession | None = None):
        """Navigate with retries and a message that names the real cause."""
        last_error: Exception | None = None
        for attempt in range(1, self.NAV_ATTEMPTS + 1):
            if session is not None and session.cancel_event.is_set():
                raise LoginError("cancelled", "操作已取消")
            try:
                page.goto(url, wait_until="commit", timeout=self.NAV_TIMEOUT_MS)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=20_000)
                except Exception:
                    pass
                if session is not None:
                    trace_login(session, f"nav ok attempt={attempt} url={page.url}")
                return page
            except Exception as exc:  # noqa: BLE001 - retried below
                last_error = exc
                first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
                if session is not None:
                    trace_login(
                        session,
                        f"nav retry attempt={attempt} url={url} err={first_line}",
                    )
                try:
                    page.wait_for_timeout(1500 * attempt)
                except Exception:
                    pass
        raise LoginError(
            "navigation_failed",
            f"打不开 {url}（尝试 {self.NAV_ATTEMPTS} 次都超时）：浏览器根本没连上页面，"
            "通常不是账号问题，而是代理/VPN 节点抖动或断流。"
            "确认 Clash/VPN 可用（系统代理端口能连通）后重试即可。",
        ) from last_error

    def _start(self) -> None:
        from playwright.sync_api import sync_playwright  # lazy import
        from .components import browser_launch_options

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            **browser_launch_options(),
            headless=False,
            args=[
                "--no-first-run",
                "--no-default-browser-check",
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
            ],
            ignore_default_args=["--enable-automation"],
            viewport=None,
        )
        self._page = (
            self._context.pages[0]
            if self._context.pages
            else self._context.new_page()
        )
        self._page.set_default_timeout(self.timeout_ms)

    def _close(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._context = None
        self._pw = None
        self._page = None

    def _settle_after_approval(self, page, session: LoginSession) -> None:
        """Keep the approval page alive briefly so the POST can land.

        The device page can render the success text before the browser has
        finished the approval request. Closing the context immediately can
        abort it, leaving the CLI polling until timeout.
        """
        try:
            page.wait_for_load_state("networkidle", timeout=6_000)
        except Exception:
            pass
        page.wait_for_timeout(2_500)
        try:
            page.screenshot(path=str(session.codex_home / "approved.png"))
        except Exception:
            pass

    def _screenshot(self, session: LoginSession) -> None:
        if self._page is None:
            return
        try:
            path = session.codex_home / "failure.png"
            self._page.screenshot(path=str(path), full_page=True)
        except Exception:
            pass

    def _ensure_visible(self, page) -> None:
        for _ in range(8):
            try:
                page.bring_to_front()
            except Exception:
                pass
            page.wait_for_timeout(350)
            try:
                if page.evaluate("document.visibilityState") == "visible":
                    return
            except Exception:
                pass
        raise LoginError(
            "page_hidden",
            "自动化 Chrome 窗口处于隐藏/遮挡状态，Sentinel 无法生成 token。"
            "请把 Codex Switcher 打开的 Chrome 窗口切到前台后重试。",
        )

    def _get_chatgpt_email(self, page) -> str | None:
        try:
            resp = page.context.request.get(
                "https://chatgpt.com/api/auth/session", timeout=10_000
            )
            if not resp.ok:
                return None
            data = resp.json()
            return (data.get("user") or {}).get("email")
        except Exception:
            return None

    def _body_text(self, page) -> str:
        try:
            return page.evaluate("document.body ? document.body.innerText : ''")
        except Exception:
            return ""

    def _has_text(self, page, text: str, timeout: int = 10_000) -> bool:
        try:
            page.wait_for_function(
                "(t) => document.body && document.body.innerText.includes(t)",
                arg=text,
                timeout=timeout,
            )
            return True
        except Exception:
            return False

    def _js_click_text(self, page, text: str) -> bool:
        result = page.evaluate(
            """(text) => {
                const nodes = [...document.querySelectorAll('button, [role="button"], a')];
                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const enabled = (el) => !el.disabled && el.getAttribute('aria-disabled') !== 'true';
                let el = nodes.find(n => (n.textContent || '').trim() === text && visible(n) && enabled(n));
                if (!el) {
                    el = nodes.find(n => (n.textContent || '').includes(text) && visible(n) && enabled(n));
                }
                if (!el) return {ok: false, count: nodes.length};
                el.click();
                return {ok: true, tag: el.tagName, text: (el.textContent || '').trim()};
            }""",
            text,
        )
        return bool(result and result.get("ok"))

    def _click_any_text(self, page, texts: list[str]) -> bool:
        for text in texts:
            if self._js_click_text(page, text):
                return True
        return False

    def _click_continue(self, page) -> bool:
        return self._click_any_text(page, ["继续", "下一步", "Continue", "Next"])

    def _is_visible(self, locator) -> bool:
        try:
            return locator.count() > 0 and locator.first.is_visible()
        except Exception:
            return False

    def _wait_for_error_or(self, page, success_markers: list[str], timeout: int = 20_000) -> str:
        """Wait for an error page or any success marker. Returns 'error'/'ok'."""
        deadline = time.time() + timeout / 1000
        while time.time() < deadline:
            body = self._body_text(page)
            if "糟糕，出错了" in body:
                return "error"
            if any(marker in body for marker in success_markers):
                return "ok"
            page.wait_for_timeout(300)
        return "timeout"

    # ------------------------------------------------------------------ SMS
    def _phone_error_text(self, page) -> str:
        body = self._body_text(page)
        lowered = body.lower()
        for marker in PHONE_ERROR_MARKERS:
            if marker in lowered:
                for line in body.splitlines():
                    if marker in line.lower():
                        return line.strip()[:200]
                return marker
        return ""

    def _phone_code_locator(self, page):
        try:
            return page.locator(
                'input[autocomplete="one-time-code"], '
                'input[inputmode="numeric"], input[name="code"]'
            )
        except Exception:
            return None

    def _has_visible_phone_code_input(self, page) -> bool:
        locator = self._phone_code_locator(page)
        if locator is None:
            return False
        try:
            for index in range(min(locator.count(), 3)):
                if locator.nth(index).is_visible():
                    return True
        except Exception:
            return False
        return False

    def _phone_input_locator(self, page):
        try:
            return page.locator(
                'input[type="tel"], input[name="phone"], '
                'input[autocomplete="tel"], input[inputmode="tel"]'
            )
        except Exception:
            return None

    def _phone_input_visible(self, page) -> bool:
        try:
            locator = self._phone_input_locator(page)
            if locator is None:
                return False
            count = locator.count()
            return any(
                locator.nth(index).is_visible() for index in range(min(count, 3))
            )
        except Exception:
            return False

    def _wait_phone_input(self, page, timeout: float = 10.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._phone_input_visible(page):
                return True
            try:
                page.wait_for_timeout(400)
            except Exception:
                time.sleep(0.4)
        return False

    def _select_sms_channel(self, page) -> None:
        """Prefer the SMS channel when the page offers several options."""
        try:
            page.evaluate(
                """() => {
                    const words = ['短信', 'sms', 'text message', 'text'];
                    const nodes = [...document.querySelectorAll(
                        'label, [role="radio"], button, [role="button"], li'
                    )];
                    const visible = (el) => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    };
                    const hit = nodes.find((el) => {
                        const text = (el.textContent || '').trim().toLowerCase();
                        return text && visible(el) && words.some((w) => text.includes(w));
                    });
                    if (!hit) return false;
                    hit.click();
                    return true;
                }"""
            )
        except Exception:
            pass

    def _fill_phone(self, page, digits: str) -> bool:
        value = phone_e164(digits)
        if not value:
            return False
        try:
            locator = self._phone_input_locator(page)
            if locator is None:
                return False
            field = locator.first
            field.wait_for(state="visible", timeout=12_000)
            try:
                field.click()
                field.fill(value)
            except Exception:
                pass
            # React-controlled inputs sometimes ignore fill(); force the value
            # through the native setter and fire the change events manually.
            return bool(
                page.evaluate(
                    """(value) => {
                        const el = document.querySelector(
                            'input[type="tel"], input[name="phone"], '
                            + 'input[autocomplete="tel"], input[inputmode="tel"]'
                        );
                        if (!el) return false;
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value'
                        ).set;
                        setter.call(el, value);
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    }""",
                    value,
                )
            )
        except Exception:
            return False

    def _wait_phone_code_page(self, page, timeout: int = 30) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._has_visible_phone_code_input(page):
                # OpenAI sometimes renders the code page but sends it over
                # WhatsApp: the SMS provider will never see it, so fail fast
                # instead of burning the full timeout on a wasted number.
                if self._code_page_via_whatsapp(page):
                    return False
                return True
            if self._phone_error_text(page) or is_sms_unavailable(
                self._body_text(page)
            ):
                return False
            page.wait_for_timeout(700)
        return False

    def _code_page_via_whatsapp(self, page) -> bool:
        return code_page_uses_whatsapp(self._body_text(page))

    def _fill_phone_code(self, page, code: str) -> bool:
        code = str(code or "").strip()
        if not code:
            return False
        try:
            single = page.locator('input[maxlength="1"]')
            visible_single = [
                single.nth(i)
                for i in range(min(single.count(), 8))
                if single.nth(i).is_visible()
            ]
            if len(visible_single) >= len(code):
                for index, char in enumerate(code):
                    visible_single[index].fill(char)
                return True
            locator = self._phone_code_locator(page)
            if locator is None:
                return False
            field = locator.first
            field.wait_for(state="visible", timeout=12_000)
            field.fill(code)
            return True
        except Exception:
            return False

    def _finish_phone_code(self, page, session: LoginSession) -> bool:
        activation = session.sms_activation_id
        if not activation:
            return True
        session.message = f"等待短信验证码：{mask_phone(session.sms_number or '')}"
        session.touch()
        try:
            code = sms_client.wait_for_code(
                activation, stop_event=session.cancel_event
            )
        except sms_client.SmsTimeout as exc:
            sms_client.cancel(activation)
            session.sms_activation_id = None
            session.sms_number = None
            raise LoginError("sms_timeout", exc.message) from exc
        except sms_client.SmsError as exc:
            sms_client.cancel(activation)
            session.sms_activation_id = None
            session.sms_number = None
            if exc.code == "cancelled":
                raise LoginError("cancelled", "登录已取消") from exc
            raise LoginError("sms_failed", exc.message) from exc

        session.message = "已收到短信验证码，正在提交"
        session.touch()
        if not self._fill_phone_code(page, code):
            sms_client.cancel(activation)
            session.sms_activation_id = None
            session.sms_number = None
            raise LoginError("ui_changed", "找不到短信验证码输入框")
        clicked = self._click_continue(page)
        page.wait_for_timeout(2500)
        still_on_code = self._has_visible_phone_code_input(page)
        error = self._phone_error_text(page) if still_on_code else ""
        if error or (not clicked and still_on_code):
            sms_client.cancel(activation)
            session.sms_activation_id = None
            session.sms_number = None
            raise LoginError(
                "sms_code_rejected",
                f"短信验证码未被接受：{error or '找不到「继续」按钮'}",
            )

        sms_client.complete(activation)
        session.sms_activation_id = None
        session.sms_number = None
        session.message = "手机验证已通过"
        session.touch()
        page.wait_for_timeout(1500)
        return True

    def _handle_phone_verification(self, page, session: LoginSession) -> bool:
        """Handle the phone-verification step if it is on screen.

        Returns True when the page was a phone step (handled or already in
        progress), False when there is nothing to do.
        """
        if not is_phone_prompt(page.url, self._body_text(page)):
            return False
        if self._has_visible_phone_code_input(page):
            if session.sms_activation_id:
                return self._finish_phone_code(page, session)
            raise LoginError(
                "sms_code_unexpected",
                "页面要求输入短信验证码，但当前没有进行中的接码记录。",
            )
        if not sms_client.enabled():
            raise LoginError(
                "sms_not_configured",
                "该账号登录需要手机短信验证，但接码未启用。设置 "
                "CODEX_SWITCHER_SMS_PROVIDER=grizzly|smsbower|l|h 及对应密钥后重启服务。",
            )

        self._select_sms_channel(page)
        # Never buy a number until the phone form is actually on screen: the
        # add-phone URL can briefly render the account/login chooser first.
        if not self._wait_phone_input(page, timeout=4):
            if self._click_any_text(
                page, ["使用手机号继续", "Continue with phone", "Sign up with phone"]
            ):
                self._wait_phone_input(page, timeout=10)
            if not self._phone_input_visible(page):
                raise LoginError(
                    "ui_changed",
                    "手机号验证页没有出现输入框（未取号，避免浪费号码）",
                )
        attempts = config.sms_max_retries()
        last_error = "未知错误"
        rejected = 0
        for attempt in range(1, attempts + 1):
            activation = None
            try:
                if attempt > 1 and not self._wait_phone_input(page, timeout=8):
                    last_error = "提交失败后无法回到手机号输入页"
                    break
                activation, phone = sms_client.acquire_number()
                session.sms_activation_id = activation
                session.phase = "waiting_for_sms"
                session.sms_number = phone
                session.message = (
                    f"接码中（第 {attempt}/{attempts} 次）：{mask_phone(phone)}"
                )
                session.touch()

                if not self._fill_phone(page, phone):
                    last_error = "找不到手机号输入框"
                    sms_client.cancel(activation)
                    session.sms_activation_id = None
                    session.sms_number = None
                    continue
                if not self._click_continue(page):
                    last_error = "手机号页找不到「继续」按钮"
                    sms_client.cancel(activation)
                    session.sms_activation_id = None
                    session.sms_number = None
                    continue
                if not self._wait_phone_code_page(page, timeout=30):
                    if self._code_page_via_whatsapp(page):
                        last_error = (
                            "验证码改由 WhatsApp 发送，接码平台收不到"
                            "（该号码被 OpenAI 判定为虚拟号）"
                        )
                    else:
                        last_error = self._phone_error_text(page) or (
                            "提交手机号后没有进入短信验证码页"
                        )
                    sms_client.cancel(activation)
                    session.sms_activation_id = None
                    session.sms_number = None
                    if is_sms_unavailable(last_error) or "WhatsApp" in last_error:
                        rejected += 1
                        # OpenAI is refusing to text this country's numbers and
                        # falls back to WhatsApp; buying more wastes money.
                        if rejected >= 2:
                            raise LoginError(
                                "sms_number_rejected",
                                "平台连续拒绝向该号码发送短信（自动切到 WhatsApp）。"
                                "请更换接码国家/平台后再试。",
                            )
                    continue
                return self._finish_phone_code(page, session)
            except LoginError:
                raise
            except sms_client.SmsNoNumbers as exc:
                raise LoginError("sms_no_numbers", exc.message) from exc
            except sms_client.SmsNoBalance as exc:
                raise LoginError("sms_no_balance", exc.message) from exc
            except Exception as exc:  # noqa: BLE001 - retry with a new number
                last_error = str(exc) or exc.__class__.__name__
                if activation:
                    sms_client.cancel(activation)
                    session.sms_activation_id = None
                    session.sms_number = None
                if session.cancel_event.is_set():
                    raise LoginError("cancelled", "登录已取消") from exc
                continue

        raise LoginError(
            "sms_failed",
            f"接码失败（已尝试 {attempts} 个号码）：{last_error}",
        )

    def _retry_error_page(self, page) -> bool:
        body = self._body_text(page)
        if "糟糕，出错了" not in body:
            return False
        self._ensure_visible(page)
        if not self._js_click_text(page, "重试"):
            return False
        page.wait_for_timeout(1500)
        return True

    def _ensure_target_account(
        self, page, email: str, password: str, totp: str, session: LoginSession
    ) -> None:
        self._goto(page, "https://chatgpt.com/auth/login", session)
        page.wait_for_timeout(1200)
        self._ensure_visible(page)

        current = self._get_chatgpt_email(page)
        if current and current.lower() == email.lower():
            return

        # A per-account profile should not normally hold another account, but
        # clear cookies if it does so the target account can log in cleanly.
        if current and current.lower() != email.lower():
            try:
                page.context.clear_cookies()
            except Exception:
                pass
            self._goto(page, "https://chatgpt.com/auth/login", session)
            page.wait_for_timeout(1200)

        last_body = ""
        disabled_email = 0
        for _ in range(24):
            if session.cancel_event.is_set():
                raise LoginError("cancelled", "登录已取消")
            if session.manual_event.is_set():
                self._wait_manual_signin(page, email, session, "已切换为手动验证")
                return
            self._ensure_visible(page)

            if self._retry_error_page(page):
                page.wait_for_timeout(1200)
                continue

            if self._handle_phone_verification(page, session):
                continue

            body = self._body_text(page)
            # Headings can wrap across lines (e.g. "查看你的身份验证器\n应用"),
            # so match on a whitespace-stripped copy for step detection.
            body_norm = re.sub(r"\s+", "", body)

            # Already logged in?
            if "今天想聊点什么" in body or "新聊天" in body:
                page.wait_for_timeout(1200)
                current = self._get_chatgpt_email(page)
                if current and current.lower() == email.lower():
                    trace_login(session, f"signin_done url={page.url}")
                    return
                # The session endpoint may lag behind the UI; retry.
                page.wait_for_timeout(1200)
                continue

            # Account chooser shown before the email form.
            if "选择账户" in body or "欢迎回来" in body:
                if not self._click_any_text(
                    page,
                    ["使用其他帐户", "使用其他账户", "Use another account"],
                ):
                    clicked = page.evaluate(
                        """(email) => {
                            const buttons = [...document.querySelectorAll('button, [role="button"]')];
                            const el = buttons.find(b => (b.textContent || '').toLowerCase().includes(email.toLowerCase()));
                            if (!el) return false;
                            el.click();
                            return true;
                        }""",
                        email,
                    )
                    if not clicked:
                        page.wait_for_timeout(800)
                        continue
                page.wait_for_timeout(1800)
                continue

            # TOTP step.
            if (
                "查看你的身份验证器应用" in body_norm
                or "身份验证器" in body_norm
                or "authenticator" in body.lower()
            ):
                if not totp:
                    self._wait_manual_signin(page, email, session, "该账号需要验证器验证码，尚未提供 TOTP 密钥")
                    return
                remaining = totp_remaining()
                if remaining < 6:
                    page.wait_for_timeout((remaining + 1) * 1000)
                code = totp_code(totp)
                try:
                    otp_input = page.locator(
                        'input[autocomplete="one-time-code"], '
                        'input[inputmode="numeric"], input[name="code"], '
                        'input[placeholder*="验证码"]'
                    ).first
                    otp_input.wait_for(state="visible", timeout=15_000)
                    try:
                        otp_input.fill(code)
                    except Exception:
                        # React can disable/detach the OTP input the moment the
                        # code is accepted and start the callback navigation.
                        # Let the outer loop re-detect the next state instead
                        # of turning a successful login into a failure.
                        page.wait_for_timeout(1200)
                        continue
                    if not self._click_continue(page):
                        # The form may already have auto-submitted; the outer
                        # loop will detect the logged-in page or retry.
                        page.wait_for_timeout(1200)
                        continue
                except LoginError:
                    raise
                except Exception:
                    page.wait_for_timeout(1200)
                    continue
                page.wait_for_timeout(2200)
                continue

            if any(marker in body_norm.lower() for marker in ("查看你的邮箱", "查看您的邮箱", "检查你的邮箱", "checkyouremail", "verifyyouremail")) or (
                "一次性验证码" in body_norm and "邮箱" in body_norm
            ):
                self._wait_manual_signin(page, email, session, "请在浏览器输入邮箱验证码")
                return

            # Password step.
            password_input = page.locator('input[type="password"]')
            if "输入密码" in body_norm or self._is_visible(password_input):
                if not password:
                    raise LoginError("password_missing", "账号池里没有保存密码")
                try:
                    field = password_input.first
                    field.wait_for(state="visible", timeout=15_000)
                    # The previous step's password input can remain briefly as
                    # a detached node while the next page renders. Do not fill
                    # a disabled/detached field; let the loop re-detect the
                    # actual step (password, TOTP, or error).
                    if not field.is_enabled():
                        page.wait_for_timeout(900)
                        continue
                    field.fill(password)
                    if not self._click_continue(page):
                        raise LoginError(
                            "ui_changed", "找不到密码步骤的「继续」按钮"
                        )
                except LoginError:
                    raise
                except Exception:
                    # A DOM transition (password -> TOTP) can detach the field
                    # mid-fill. Retry from the top instead of failing the login.
                    page.wait_for_timeout(1200)
                    continue
                page.wait_for_timeout(2200)
                continue

            # Email step.
            email_input = page.locator(
                'input[type="email"], input[name="email"]'
            )
            if self._is_visible(email_input):
                try:
                    field = email_input.first
                    # The field renders disabled until the page hydrates, and it
                    # disappears entirely when OpenAI already knows the address
                    # and jumps straight to /log-in/password. Neither is an
                    # error: re-detect the step on the next iteration.
                    if not field.is_enabled():
                        disabled_email += 1
                        if disabled_email >= 10:
                            raise LoginError(
                                "email_disabled",
                                "邮箱输入框一直是禁用状态（通常是人机校验/Cloudflare 没通过，"
                                "或页面停在半截）。请在前台 Chrome 窗口里重试一次。",
                            )
                        page.wait_for_timeout(700)
                        continue
                    disabled_email = 0
                    field.fill(email)
                    if not self._click_continue(page):
                        raise LoginError(
                            "ui_changed", "找不到邮箱步骤的「继续」按钮"
                        )
                except LoginError:
                    raise
                except Exception:
                    # Detached mid-fill（通常是自动跳到了密码步骤）属于进度，
                    # 让循环重新识别当前步骤，不要把登录判死。
                    page.wait_for_timeout(1200)
                    continue
                page.wait_for_timeout(2200)
                continue

            # Unknown intermediate state (Cloudflare, render, redirect).
            last_body = body
            page.wait_for_timeout(1200)

        snippet = (self._body_text(page) or last_body)[-800:]
        raise LoginError(
            "login_flow_timeout",
            "登录流程没有到达已登录状态，可能是页面改版、Cloudflare 或网络问题。",
            snippet,
        )

    def _fill_device_code(self, page, code: str) -> bool:
        """Fill the 9 device-code boxes, verifying each one stuck.

        React re-renders these inputs as the code is entered; a plain
        ``fill()`` loop can lose a character and leave 继续 disabled. Retry the
        empty/mismatched boxes a few times and verify the concatenated value.
        """
        chars = str(code).replace("-", "").strip()
        if not chars:
            return False
        inputs = page.locator('input[maxlength="1"]')
        try:
            count = inputs.count()
        except Exception:
            return False
        if count == 0:
            return False
        for _ in range(3):
            for index, char in enumerate(chars[:count]):
                box = inputs.nth(index)
                try:
                    if box.input_value() == char:
                        continue
                    box.click()
                    box.fill(char)
                except Exception:
                    continue
            page.wait_for_timeout(450)
            try:
                values = "".join(inputs.nth(i).input_value() for i in range(count))
            except Exception:
                values = ""
            if values.startswith(chars):
                return True
        return False

    def _device_flow_done(self, page, submitted: bool = True) -> bool:
        """Strict success check for the device approval page.

        The OAuth callback URL (``/deviceauth/callback?code=...``) is the SPA
        route that *hosts* the code-entry form, so a URL check alone returns
        success while the form is still empty (and the CLI keeps waiting).
        The form and its 9 inputs always win; only after the code has been
        submitted do the success banners count.
        """
        body = self._body_text(page)
        if "使用你的设备代码" in body or "Enter the code" in body:
            return False
        inputs = page.locator('input[maxlength="1"]')
        try:
            for index in range(min(inputs.count(), 3)):
                if inputs.nth(index).is_visible():
                    return False
        except Exception:
            pass
        if not submitted:
            return False
        if any(
            marker in body
            for marker in (
                "已登录 Codex",
                "Signed in to Codex",
                "设备已授权",
                "Device authorized",
                "You may now close this window",
                "You can close this window",
            )
        ):
            return True
        # Fallback: the flow left the auth host for an app page without the
        # code form (some builds redirect straight to chatgpt.com).
        url = page.url.lower()
        return "chatgpt.com" in url and "auth.openai.com" not in url

    def _enable_device_code_login(self, page) -> bool:
        """Enable 设置 → 安全 → 「设备代码登录」 for accounts that block the flow.

        New accounts default to the toggle being off; the consent page then
        refuses to authorize the CLI. The browser is already signed in, so we
        can flip it and re-enter the device flow instead of asking the user.
        """
        script = """() => {
            const switches = [...document.querySelectorAll('[role="switch"]')];
            const detail = (el) => {
                const id = el.getAttribute('aria-labelledby') || '';
                const labelEl = id ? document.getElementById(id) : null;
                let text = labelEl ? (labelEl.textContent || '') : '';
                if (!/设备代码|device code/i.test(text)) {
                    let node = el.parentElement;
                    for (let i = 0; i < 5 && node; i += 1) {
                        text += ' ' + (node.innerText || '');
                        node = node.parentElement;
                    }
                }
                return text;
            };
            const target = switches.find(
                (el) => /设备代码|device code/i.test(detail(el))
            );
            if (!target) return {found: false};
            const checked = target.getAttribute('aria-checked') === 'true';
            return {found: true, checked};
        }"""
        try:
            self._goto(page, "https://chatgpt.com/settings/security")
            page.wait_for_timeout(4000)
            state = {}
            for _ in range(10):
                state = page.evaluate(script) or {}
                if state.get("found"):
                    break
                page.wait_for_timeout(1200)
            if not state.get("found"):
                return False
            if state.get("checked"):
                return True
            page.evaluate(
                """() => {
                    const switches = [...document.querySelectorAll('[role="switch"]')];
                    const target = switches.find((el) => {
                        const id = el.getAttribute('aria-labelledby') || '';
                        const labelEl = id ? document.getElementById(id) : null;
                        let text = labelEl ? (labelEl.textContent || '') : '';
                        if (!/设备代码|device code/i.test(text)) {
                            let node = el.parentElement;
                            for (let i = 0; i < 5 && node; i += 1) {
                                text += ' ' + (node.innerText || '');
                                node = node.parentElement;
                            }
                        }
                        return /设备代码|device code/i.test(text);
                    });
                    if (target) target.click();
                }"""
            )
            page.wait_for_timeout(2500)
            for _ in range(6):
                after = page.evaluate(script) or {}
                if after.get("found") and after.get("checked"):
                    return True
                page.wait_for_timeout(900)
            return False
        except LoginError:
            raise
        except Exception:
            return False

    def _approve_device_code(
        self,
        page,
        email: str,
        device_url: str,
        device_code: str,
        session: LoginSession,
    ) -> None:
        self._goto(page, device_url, session)
        page.wait_for_timeout(1500)
        self._ensure_visible(page)
        trace_login(session, f"device_start url={page.url}")
        if session.manual_event.is_set():
            self._wait_manual_authorization(page, session, "已切换为手动授权")
            return

        error_retries = 0
        submitted = False
        setting_fixed = False
        for iteration in range(1, 15):
            if session.cancel_event.is_set():
                raise LoginError("cancelled", "登录已取消")
            if session.manual_event.is_set():
                self._wait_manual_authorization(page, session, "已切换为手动授权")
                return
            self._ensure_visible(page)

            if self._retry_error_page(page):
                error_retries += 1
                if error_retries > 3:
                    raise LoginError(
                        "sentinel_timeout",
                        "设备码授权页反复出现「糟糕，出错了」，通常是 Sentinel token 超时。"
                        "请确认自动化 Chrome 窗口保持前台可见后重试。",
                    )
                page.wait_for_timeout(1500)
                continue

            if self._handle_phone_verification(page, session):
                continue

            body = self._body_text(page)
            trace_login(
                session,
                f"iter={iteration} submitted={submitted} url={page.url} "
                f"body={body[:160]!r}",
            )
            if self._device_flow_done(page, submitted):
                trace_login(session, "device_done url=%s" % page.url)
                page.wait_for_timeout(1200)
                return

            if "请在 ChatGPT 安全设置中" in body or (
                "启用设备代码登录" in body and not submitted
            ):
                if not setting_fixed and self._enable_device_code_login(page):
                    setting_fixed = True
                    trace_login(session, "device_code_setting_enabled")
                    self._goto(page, device_url, session)
                    page.wait_for_timeout(1800)
                    continue
                raise LoginError(
                    "device_code_login_disabled",
                    "该账号没有启用「为 Codex、Excel、PowerPoint 和 Word 启用设备代码登录」。"
                    "请先在 chatgpt.com/settings/security 打开该开关，再重试。",
                )

            if "选择账户" in body or "欢迎回来" in body:
                clicked = page.evaluate(
                    """(email) => {
                        const buttons = [...document.querySelectorAll('button, [role="button"]')];
                        const el = buttons.find(b => (b.textContent || '').toLowerCase().includes(email.toLowerCase()));
                        if (!el) return false;
                        el.click();
                        return true;
                    }""",
                    email,
                )
                if not clicked:
                    clicked = page.evaluate(
                        """() => {
                            const buttons = [...document.querySelectorAll('button, [role="button"]')];
                            const el = buttons.find(b => (b.textContent || '').includes('@'));
                            if (!el) return false;
                            el.click();
                            return true;
                        }"""
                    )
                if clicked:
                    page.wait_for_timeout(3000)
                    continue

            if "/sign-in-with-chatgpt/codex/consent" in page.url:
                if self._click_continue(page):
                    page.wait_for_timeout(3500)
                    continue
                raise LoginError("ui_changed", "同意页找不到「继续」按钮")

            inputs = page.locator('input[maxlength="1"]')
            if "使用你的设备代码" in body or inputs.count() >= 9:
                if not self._fill_device_code(page, device_code):
                    trace_login(session, "device_code_fill_failed")
                    page.wait_for_timeout(1200)
                    continue
                page.wait_for_timeout(400)
                if not self._click_continue(page):
                    trace_login(session, "device_code_continue_not_clickable")
                    page.wait_for_timeout(1500)
                    continue
                submitted = True
                trace_login(session, "device_code_submitted")
                page.wait_for_timeout(3500)
                continue

            page.wait_for_timeout(1500)

        body = self._body_text(page)
        if self._device_flow_done(page, submitted):
            return
        raise LoginError(
            "device_code_not_accepted",
            "设备码授权没有完成。请检查设备码是否过期，或手动在浏览器完成。",
            body[-1000:],
        )


class LoginManager:
    def __init__(
        self,
        store: AccountStore,
        codex_cmd: list[str] | None = None,
        driver_name: str | None = None,
        sessions_dir: Path | None = None,
        profile_root: Path | None = None,
        driver_factory=None,
        history_path: Path | None = None,
        concurrency: int | None = None,
    ):
        self.store = store
        self.codex_cmd = codex_cmd
        # ``None`` means "ask config on every use", so a driver change from the
        # dashboard takes effect without restarting the service.
        self._driver_name = driver_name
        self.sessions_dir = sessions_dir or config.login_sessions_dir()
        self.profile_root = profile_root or config.browser_profile_dir()
        self.driver_factory = driver_factory
        # ``None`` means "ask config on every use" (hot-apply from the UI).
        self._concurrency = concurrency
        # Login history survives a service restart: without it the dashboard
        # looked like "no logins ever happened" after every restart.
        self.history_path = Path(history_path) if history_path else (
            self.sessions_dir.parent / "login-history.json"
        )
        self._sessions: dict[str, LoginSession] = {}
        self._lock = threading.RLock()
        # Login queue: each Playwright login opens its own headed Chrome, so
        # several may run at once but the ceiling stays low (default 2).
        self._active: dict[str, str] = {}
        self._queue: list[str] = []
        self._pending: dict[str, str] = {}
        self._reserved = 0
        self._queue_cv = threading.Condition(self._lock)
        self._workers: list[threading.Thread] = []
        # Scrub any plaintext auth.json left by an interrupted older run.
        try:
            for stale in self.sessions_dir.glob("*/auth.json"):
                try:
                    stale.unlink()
                except Exception:
                    pass
        except Exception:
            pass
        self._load_history()

    def concurrency(self) -> int:
        value = self._concurrency
        if value is None:
            try:
                value = config.login_concurrency()
            except Exception:
                value = 1
        try:
            return max(1, min(4, int(value)))
        except (TypeError, ValueError):
            return 1

    # ------------------------------------------------------------- history
    HISTORY_LIMIT = 50

    def _load_history(self) -> None:
        try:
            raw = self.history_path.read_text(encoding="utf-8")
            entries = json.loads(raw)
        except (OSError, ValueError):
            return
        if not isinstance(entries, list):
            return
        loaded: dict[str, LoginSession] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            session_id = str(entry.get("id") or "")
            email = str(entry.get("email") or "")
            if not session_id or not email:
                continue
            status = str(entry.get("status") or "failed")
            message = str(entry.get("message") or "")
            error = entry.get("error")
            if status in ACTIVE_STATUSES | {"queued"}:
                # The service died while this login was in flight; the codex
                # process and Chrome window went with it.
                status = "failed"
                error = "interrupted"
                message = "服务重启，这次登录被中断（需要重新点一次登录）。"
            session = LoginSession(
                id=session_id,
                email=email,
                codex_home=Path(
                    entry.get("codex_home") or (self.sessions_dir / session_id)
                ),
                status=status,
                message=message,
                driver=str(entry.get("driver") or "playwright"),
                error=error,
                source=str(entry.get("source") or "unknown"),
                phase=str(entry.get("phase") or "unknown"),
                attempt=int(entry.get("attempt") or 1),
                initial_login=bool(entry.get("initial_login")),
                purpose=str(entry.get("purpose") or "login"),
                attempt_errors=list(entry.get("attempt_errors") or []),
                started_at=float(entry.get("started_at") or 0.0),
                updated_at=float(entry.get("updated_at") or 0.0),
            )
            session.done_event.set()
            loaded[session_id] = session
        with self._lock:
            for session_id, session in loaded.items():
                self._sessions.setdefault(session_id, session)

    def _persist(self) -> None:
        with self._lock:
            ordered = sorted(
                self._sessions.values(), key=lambda item: item.started_at, reverse=True
            )
            # History is capped on disk; drop the same overflow from memory so a
            # long-running service does not accumulate dead sessions forever.
            for stale in ordered[self.HISTORY_LIMIT :]:
                if stale.status not in ACTIVE_STATUSES | {"queued"}:
                    self._sessions.pop(stale.id, None)
            records = []
            retained = [s for s in ordered if s.status in ACTIVE_STATUSES | {"queued"}]
            retained += [s for s in ordered if s not in retained][:self.HISTORY_LIMIT]
            for session in retained:
                record = session.to_public()
                # Never write the one-time device code or CLI output to disk.
                record.pop("device_code", None)
                record.pop("log_tail", None)
                records.append(record)
            try:
                self.history_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.history_path.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8"
                )
                os.replace(tmp, self.history_path)
            except OSError:
                pass

    def _note(self, session: LoginSession) -> None:
        session.touch()
        self._persist()

    def _session_changed(self, session):
        with self._queue_cv:
            self._note(session)
            self._queue_cv.notify_all()

    @property
    def driver_name(self) -> str:
        """Effective login driver (dashboard > env > default)."""
        return self._driver_name or config.login_driver()

    @driver_name.setter
    def driver_name(self, value: str | None) -> None:
        self._driver_name = value

    def _command(self) -> list[str]:
        if self.codex_cmd:
            return self.codex_cmd
        exe = config.find_codex_exe()
        if not exe:
            raise LoginError(
                "codex_not_found",
                "找不到 codex.exe。请在设置中的使用引导重新检测或选择位置。",
            )
        if exe.lower().endswith((".cmd", ".bat")):
            return ["cmd.exe", "/c", exe, "login", "--device-auth"]
        return [exe, "login", "--device-auth"]

    def start(self, email: str, *, source: str = "manual", queued_session_id: str | None = None, purpose="login") -> LoginSession:
        if not config.login_enabled():
            raise LoginError(
                "login_disabled",
                "登录助手默认禁用。设置 CODEX_SWITCHER_ENABLE_LOGIN=1 后重启服务再试。",
            )
        secret = self.store.get_secret(email)
        if secret is None:
            raise LoginError("account_not_found", f"账号池里没有 {email}")
        queued = self._sessions.get(queued_session_id) if queued_session_id else None
        purpose = queued.purpose if queued else purpose
        requested_driver = queued.driver if queued else ("playwright" if purpose == "totp_setup" else self.driver_name)
        if purpose not in {"login", "totp_setup"}:
            raise LoginError("invalid_purpose", "不支持的账号操作")
        if not secret.get("password") and purpose == "login" and requested_driver != "manual":
            raise LoginError("password_missing", f"{email} 没有保存密码")

        key = email.lower()
        with self._queue_cv:
            if queued_session_id:
                queued_session = self._sessions.get(queued_session_id)
                if queued_session and (queued_session.cancel_event.is_set() or self._pending.get(key) != queued_session_id):
                    return queued_session
            existing_id = self._active.get(key)
            existing = self._sessions.get(existing_id) if existing_id else None
            if existing is not None and existing.status in ACTIVE_STATUSES:
                return existing

            pending_id = self._pending.pop(key, None)
            session = self._sessions.get(pending_id) if pending_id else None
            if session is None:
                session_id = uuid.uuid4().hex[:12]
                session = LoginSession(
                    id=session_id, email=email, codex_home=self.sessions_dir / session_id,
                    driver=requested_driver, source=source, purpose=purpose,
                    initial_login=not self.store.has_auth_snapshot(email),
                )
                self._sessions[session.id] = session
            self._queue = [value for value in self._queue if value.lower() != key]
            self._active[key] = session.id
            session.on_change = self._session_changed
            session.status = "starting"
            session.phase = "generating_device_code"
            session.message = "正在生成设备登录码"
            self._note(session)
            self._persist()
        # Register before starting the CLI. Cold starts and their failures must
        # remain visible, and duplicate clicks must attach to this same task.
        try:
            from .components import ComponentError, browser_components, managed_browser_required
            if self.driver_factory is None and session.driver == "playwright" and managed_browser_required() and not (session.purpose == "totp_setup" and secret.get("totp")):
                def progress(message):
                    if session.message != "准备登录浏览器：" + message:
                        session.phase = "preparing_browser"
                        session.message = "准备登录浏览器：" + message
                        self._note(session)
                try:
                    browser_components().ensure_ready(session.cancel_event, progress)
                except ComponentError as exc:
                    raise LoginError("browser_component_failed", str(exc)) from exc
                session.phase = "generating_device_code"
                session.message = "正在生成设备登录码"
                self._note(session)
            if session.purpose == "totp_setup":
                session.phase = "configuring_totp"
                session.message = "正在打开该账号的安全设置"
                self._note(session)
                threading.Thread(target=self._run_totp_setup, args=(session, secret), daemon=True,
                                 name=f"totp-setup-{session.id}").start()
                return session
            session.codex_home.mkdir(parents=True, exist_ok=True)
            for startup_attempt in range(1, 3):
                session.attempt = startup_attempt
                try:
                    proc, code, url, out = start_device_auth(
                        self._command(), session.codex_home,
                        output_path=session.codex_home / "cli.log",
                        cancel_event=session.cancel_event,
                    )
                    break
                except LoginError as exc:
                    detail = str(exc.detail or "").lower()
                    transient = exc.code == "device_code_not_found" and any(value in detail for value in (
                        "timed out", "timeout", "connection reset", "connection refused", "tls", "502", "503", "504",
                    )) and not any(value in detail for value in ("429", "403", "401", "rate limit"))
                    if startup_attempt == 2 or not transient:
                        raise
                    session.attempt_errors.append({"phase": session.phase, "error": exc.code})
                    session.message = "生成设备码时网络暂时失败，正在重试（1/1）"
                    self._note(session)
                    if session.cancel_event.wait(2):
                        raise LoginError("cancelled", "登录已取消")
        except Exception as exc:
            session.status = "cancelled" if session.cancel_event.is_set() else "failed"
            session.error = getattr(exc, "code", "cli_start_failed")
            session.message = getattr(exc, "message", str(exc))
            self._note(session)
            with self._queue_cv:
                self._active.pop(key, None)
                self._queue_cv.notify_all()
            session.done_event.set()
            return session
        session.process = proc
        session.output = out
        session.device_code = code
        session.verification_url = url
        session.status = "waiting_for_browser"
        session.message = "设备码已生成，等待浏览器授权"
        self._note(session)

        self._persist()
        threading.Thread(
            target=self._run,
            args=(session, secret),
            daemon=True,
            name=f"login-{session.id}",
        ).start()
        return session

    def enqueue(self, email: str, *, source: str = "manual", purpose="login") -> dict:
        """Queue an account for login (auto-login / per-row button).

        Returns ``{"state": "running"|"queued", "position": n, "session": ...}``.
        Up to ``concurrency()`` accounts log in at once (each with its own
        headed Chrome window); the rest wait in order.
        """
        if not config.login_enabled():
            raise LoginError(
                "login_disabled",
                "登录助手默认禁用。设置 CODEX_SWITCHER_ENABLE_LOGIN=1 后重启服务再试。",
            )
        secret = self.store.get_secret(email)
        if secret is None:
            raise LoginError("account_not_found", f"账号池里没有 {email}")
        if purpose not in {"login", "totp_setup"}:
            raise LoginError("invalid_purpose", "不支持的账号操作")
        if not secret.get("password") and purpose == "login" and self.driver_name != "manual":
            raise LoginError("password_missing", f"{email} 没有保存密码")

        key = email.lower()
        with self._queue_cv:
            active_id = self._active.get(key)
            if active_id:
                session = self._sessions.get(active_id)
                if session is not None:
                    return {
                        "state": "running",
                        "position": 0,
                        "email": email,
                        "session": session.to_public(),
                    }
            for index, queued in enumerate(self._queue):
                if queued.lower() == key:
                    return {
                        "state": "queued",
                        "position": index + 1,
                        "email": email,
                        "session": self._sessions[self._pending[key]].to_public(),
                    }
            pending_id = self._pending.get(key)
            if pending_id:
                return {"state": "queued", "position": 1, "email": email,
                        "session": self._sessions[pending_id].to_public()}
            session_id = uuid.uuid4().hex[:12]
            session = LoginSession(
                id=session_id, email=email, codex_home=self.sessions_dir / session_id,
                status="queued", source=source, driver="playwright" if purpose == "totp_setup" else self.driver_name, purpose=purpose,
                message="等待登录", initial_login=not self.store.has_auth_snapshot(email),
            )
            self._sessions[session_id] = session
            session.on_change = self._session_changed
            self._pending[key] = session_id
            self._queue.append(email)
            self._persist()
            self._ensure_worker_locked()
            return {
                "state": "queued",
                "position": len(self._queue),
                "email": email,
                "session": session.to_public(),
            }

    def queue_info(self) -> list[dict]:
        with self._lock:
            return [
                {"email": email, "position": index + 1,
                 "session_id": self._pending.get(email.lower())}
                for index, email in enumerate(self._queue)
            ]

    def active_map(self) -> dict[str, dict]:
        with self._lock:
            result: dict[str, dict] = {}
            for key, session_id in self._active.items():
                session = self._sessions.get(session_id)
                if session is not None:
                    result[key] = session.to_public()
            return result

    def _ensure_worker_locked(self) -> None:
        """Top the worker pool up to ``concurrency()`` (caller holds the lock)."""
        self._workers = [worker for worker in self._workers if worker.is_alive()]
        limit = self.concurrency()
        while len(self._workers) < limit and self._queue:
            worker = threading.Thread(
                target=self._worker_loop,
                name=f"login-queue-{len(self._workers) + 1}",
                daemon=True,
            )
            self._workers.append(worker)
            worker.start()

    def _worker_loop(self) -> None:
        while True:
            with self._queue_cv:
                while self._queue:
                    pending = self._sessions.get(self._pending.get(self._queue[0].lower()))
                    cold_active = any(
                        not self._sessions[sid].needs_input and self._sessions[sid].initial_login and self._sessions[sid].source in {"import", "account_add"}
                        for sid in self._active.values()
                    )
                    cold_pending = pending and pending.initial_login and pending.source in {"import", "account_add"}
                    busy = sum(not self._sessions[sid].needs_input for sid in self._active.values()) + self._reserved
                    # Waiting windows retain their account/profile ownership,
                    # but do not occupy every automatic-login slot. Bound the
                    # extra windows so a large import cannot flood the desktop.
                    room = len(self._active) + self._reserved < self.concurrency() + 4
                    if room and busy < self.concurrency() and not cold_active and not (cold_pending and busy):
                        break
                    self._queue_cv.wait(0.5)
                if not self._queue:
                    return
                email = self._queue.pop(0)
                queued_session_id = self._pending.get(email.lower())
                self._reserved += 1
                self._queue_cv.notify_all()
            session = None
            try:
                session = self.start(email, queued_session_id=queued_session_id)
            except Exception as exc:  # noqa: BLE001 - keep the queue alive
                with self._queue_cv:
                    failed = self._sessions.get(queued_session_id)
                    if failed is not None and failed.status != "cancelled":
                        failed.status = "failed"
                        failed.error = getattr(exc, "code", "login_start_failed")
                        failed.message = getattr(exc, "message", str(exc))
                        self._note(failed)
                        failed.done_event.set()
                    if self._pending.get(email.lower()) == queued_session_id:
                        self._pending.pop(email.lower(), None)
                sys.stderr.write(f"[login-queue] {email}: {exc}\n")
            finally:
                with self._queue_cv:
                    self._reserved = max(0, self._reserved - 1)
                    self._queue_cv.notify_all()
            if session is not None:
                while not session.done_event.wait(0.2):
                    if session.needs_input:
                        break

    def _driver_for(self, session: LoginSession):
        if self.driver_factory is not None:
            return self.driver_factory(session)
        if session.driver == "playwright":
            digest = hashlib.sha256(session.email.encode("utf-8")).hexdigest()[:16]
            return PlaywrightDriver(self.profile_root / digest)
        return ManualDriver()

    def _run_totp_setup(self, session, secret):
        try:
            if session.cancel_event.is_set():
                raise LoginError("cancelled", "设置已取消")
            if secret.get("totp"):
                message = "该账号已有 TOTP 密钥，未重复绑定"
            else:
                driver = self._driver_for(session)
                message = driver.configure_totp(email=session.email, password=secret.get("password", ""),
                                                totp="", session=session, store=self.store)
            session.status = "success"
            session.phase = "ready"
            session.message = message
            session.error = None
        except Exception as exc:
            session.status = "cancelled" if session.cancel_event.is_set() else "failed"
            session.error = "cancelled" if session.cancel_event.is_set() else getattr(exc, "code", "totp_setup_failed")
            session.message = "已停止本次设置；若已提交绑定码，请到官方设置确认结果。账号资料已保留。" if session.cancel_event.is_set() else getattr(exc, "message", "TOTP 设置未完成，可改用手动设置")
        finally:
            session.needs_input = False
            self._note(session)
            with self._queue_cv:
                self._active.pop(session.email.lower(), None)
                self._queue_cv.notify_all()
            session.done_event.set()

    def _run(self, session: LoginSession, secret: dict) -> None:
        try:
            driver = self._driver_for(session)
            for attempt in range(1, 3):
                try:
                    driver.complete_login(
                        email=session.email, password=secret.get("password", ""),
                        totp=secret.get("totp", ""), device_url=session.verification_url,
                        device_code=session.device_code or "", session=session,
                    )
                    break
                except LoginError as exc:
                    # SMS, password and verification failures are not retried
                    # as a fresh login (which could buy another phone number).
                    if attempt == 2 or exc.code not in {"navigation_failed", "sentinel_timeout"}:
                        raise
                    session.attempt_errors.append({"phase": session.phase, "error": exc.code})
                    session.attempt += 1
                    session.message = "网络或页面加载暂时失败，正在重试（1/1）"
                    session.phase = "retrying"
                    self._note(session)
                    if session.cancel_event.wait(2):
                        raise LoginError("cancelled", "登录已取消")
                    driver = self._driver_for(session)
            if getattr(driver, "name", "") == "manual":
                session.waiting_for_input("等待你在默认浏览器完成授权，完成后会自动保存登录凭据")
            else:
                session.status = "waiting_for_cli"
                session.phase = "saving_credentials"
                session.message = "浏览器授权完成，等待 CLI 写入临时 auth.json"
            self._note(session)
            auth_bytes = wait_for_auth(
                session.process,  # type: ignore[arg-type]
                session.auth_path,
                session.output,  # type: ignore[arg-type]
                timeout=14 * 60,
                cancel_event=session.cancel_event,
            )
            state = read_auth_state(session.auth_path)
            if not state.get("logged_in"):
                raise LoginError("invalid_auth", "CLI 生成的 auth.json 无法解析")
            if state.get("email") and state["email"].lower() != session.email.lower():
                raise LoginError(
                    "account_mismatch",
                    f"auth.json 属于 {state['email']}，不是 {session.email}",
                )
            self.store.save_auth_snapshot(
                session.email,
                auth_bytes,
                {
                    "email": state.get("email"),
                    "name": state.get("name"),
                    "plan": state.get("plan"),
                    "account_id": state.get("account_id"),
                    "expires_at": state.get("expires_at"),
                },
            )
            session.status = "success"
            session.phase = "ready"
            session.error = None
            session.message = (
                "登录成功，auth.json 快照已加密保存。"
                "v0.2 不会替换当前 ~/.codex/auth.json。"
            )
        except LoginError as exc:
            if session.cancel_event.is_set() or session.status == "cancelled":
                session.status = "cancelled"
                session.error = "cancelled"
                session.message = "登录已取消"
            else:
                session.status = "failed"
                session.error = exc.code
                session.message = exc.message
        except Exception as exc:
            if session.cancel_event.is_set() or session.status == "cancelled":
                session.status = "cancelled"
                session.error = "cancelled"
                session.message = "登录已取消"
            else:
                session.status = "failed"
                session.error = "unexpected"
                session.message = str(exc)
        finally:
            # Never leave a paid 接码 activation open after the session ends.
            session.needs_input = False
            if session.sms_activation_id:
                try:
                    sms_client.cancel(session.sms_activation_id)
                except Exception:
                    pass
                session.sms_activation_id = None
                session.sms_number = None
            self._note(session)
            proc = session.process
            if proc is not None:
                if proc.poll() is None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                try:
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                try:
                    if proc.stdout is not None:
                        proc.stdout.close()
                except Exception:
                    pass
            # Do not leave a plaintext auth.json in the temp CODEX_HOME.
            try:
                if session.auth_path.exists():
                    session.auth_path.unlink()
            except Exception:
                pass
            with self._queue_cv:
                self._active.pop(session.email.lower(), None)
                self._queue_cv.notify_all()
            session.done_event.set()

    def get(self, session_id: str) -> LoginSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def list(self) -> list[dict]:
        with self._lock:
            sessions = list(self._sessions.values())
        sessions.sort(key=lambda s: s.started_at, reverse=True)
        return [s.to_public() for s in sessions]

    def cancel(self, session_id: str) -> bool:
        with self._queue_cv:
            session = self._sessions.get(session_id)
            if session is None or session.status in {"success", "failed", "cancelled"}:
                return False
            key = session.email.lower()
            if session.status == "queued":
                self._pending.pop(key, None)
                self._queue = [email for email in self._queue if email.lower() != key]
                session.done_event.set()
                self._queue_cv.notify_all()
        if session is None:
            return False
        session.cancel_event.set()
        if session.process is not None and session.process.poll() is None:
            try:
                session.process.terminate()
            except Exception:
                pass
        session.status = "cancelled"
        session.error = "cancelled"
        session.message = "登录已取消"
        session.phase = "cancelled"
        self._note(session)
        return True

    def manual(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.status not in ACTIVE_STATUSES:
                return False
            session.manual_event.set()
            session.message = "已请求手动接管，请在该账号的浏览器窗口中继续"
            self._note(session)
            return True
