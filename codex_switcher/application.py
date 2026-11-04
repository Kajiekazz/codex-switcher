"""Installed application: one background service, native tray, default browser."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import time
import traceback
import urllib.request
from urllib.parse import urlsplit
import webbrowser

from . import config
from .startup import StartupRegistration


def dashboard_url():
    host = config.host()
    if host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{'[' + host + ']' if ':' in host else host}:{config.port()}"


def instance_info():
    try:
        info = json.loads((config.data_dir() / "app-instance.json").read_text(encoding="utf8"))
        parsed = urlsplit(info["url"])
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
                or not parsed.port or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")):
            return None
        return info
    except (OSError, ValueError, KeyError, TypeError):
        return None


class ApplicationRuntime:
    def __init__(self, *, startup_launch=False):
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.server = None
        self.url = dashboard_url()
        self.token = secrets.token_urlsafe(32)
        self.startup = StartupRegistration()
        self.startup_launch = startup_launch
        self.startup_recovery = None
        self.path = config.data_dir() / "app-instance.json"

    def attach(self, server):
        self.server = server
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pid": os.getpid(), "token": self.token, "url": self.url}), encoding="utf8")
        os.replace(tmp, self.path)
        self.ready.set()

    def stop(self):
        if self.stopped.is_set():
            return
        self.stopped.set()
        if self.server:
            threading.Thread(target=self.server.shutdown, name="service-shutdown", daemon=True).start()

    def start_background_tasks(self, server):
        if not self.startup_launch:
            server.auto_switch.start()
            return
        from .startup_recovery import StartupRecovery
        self.startup_recovery = StartupRecovery(
            server.desktop_client, server.goal_resume, server.quota_service,
            server.auto_switch, self.stopped,
        )
        threading.Thread(target=self.startup_recovery.run, name="startup-recovery", daemon=True).start()

    def close(self):
        self.stopped.set()
        try:
            if json.loads(self.path.read_text(encoding="utf8")).get("token") == self.token:
                self.path.unlink()
        except (OSError, ValueError):
            pass


def request_exit() -> bool:
    try:
        info = instance_info()
        if not info:
            return False
        request = urllib.request.Request(info["url"].rstrip("/") + "/api/app/exit", method="POST",
                                         data=b'{"confirm":true}',
                                         headers={"Content-Type": "application/json", "X-Cs-Instance": info["token"]})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response).get("ok") is True
    except (OSError, ValueError, KeyError):
        return False


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--startup", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--exit", action="store_true")
    # Headless lifecycle validation; normal installs always show a tray icon.
    parser.add_argument("--no-tray", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--diagnose", metavar="FILE", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    os.environ.setdefault("CODEX_SWITCHER_ENABLE_LOGIN", "1")
    os.environ.setdefault("CODEX_SWITCHER_LOGIN_DRIVER", "playwright")
    if getattr(sys, "frozen", False):
        from .components import browser_root
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_root())
    if args.exit:
        request_exit()
        return 0
    if args.diagnose:
        from .components import browser_components
        from .api import DASHBOARD_HTML
        Path(args.diagnose).write_text(json.dumps({"frozen": bool(getattr(sys, "frozen", False)),
                                                   "dashboard": "setupModal" in DASHBOARD_HTML,
                                                   "browser": browser_components().status(),
                                                   "startup": StartupRegistration().status()}), encoding="utf8")
        return 0
    from .windows_ui import SingleInstance, TrayIcon, message_box
    name = "Local\\CodexSwitcher-" + hashlib.sha256(str(config.data_dir().resolve()).lower().encode()).hexdigest()[:20]
    instance = SingleInstance(name)
    runtime = None
    log = None
    try:
        from .api import _existing_instance, run
        if not instance.acquired:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                info = instance_info()
                address = urlsplit(info["url"]) if info else None
                existing = _existing_instance(address.hostname if address else config.host(),
                                              address.port if address else config.port())
                if existing and (not info or existing.get("service_pid") == info.get("pid")):
                    if not args.background:
                        webbrowser.open(info["url"] if info else dashboard_url())
                    return 0
                time.sleep(.15)
            raise RuntimeError("后台程序仍在启动，请稍后重试")
        existing = _existing_instance(config.host(), config.port())
        if existing:
            if not args.background:
                webbrowser.open(dashboard_url())
            # Never create a second account-switching service over a source run.
            return 0
        logs = config.data_dir() / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / "app.log"
        if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
            os.replace(path, path.with_suffix(".previous.log"))
        log = path.open("a", encoding="utf8", buffering=1)
        sys.stdout = sys.stderr = log
        runtime = ApplicationRuntime(startup_launch=args.startup)
        runtime.startup.upgrade_command()

        def serve():
            try:
                run(app_runtime=runtime)
            except BaseException:
                traceback.print_exc()
            finally:
                runtime.close()

        service = threading.Thread(target=serve, name="switcher-service", daemon=True)
        service.start()
        deadline = time.monotonic() + 20
        while not runtime.ready.wait(.1):
            if runtime.stopped.is_set() or time.monotonic() >= deadline:
                raise RuntimeError("后台服务未启动，请查看 " + str(path))
        if not args.background:
            webbrowser.open(runtime.url)
        try:
            if args.no_tray:
                runtime.stopped.wait()
            else:
                icon = Path(__file__).with_name("app.ico")
                TrayIcon(lambda: webbrowser.open(runtime.url), runtime.startup,
                         runtime.stop, runtime.stopped, icon if icon.exists() else None).run()
        finally:
            runtime.stop()
            service.join(timeout=10)
        return 0
    except Exception as exc:
        if log:
            traceback.print_exc()
        message_box(str(exc))
        return 1
    finally:
        if runtime:
            runtime.close()
        instance.close()
