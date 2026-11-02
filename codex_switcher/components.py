"""Download only the pinned Playwright browser when automatic login needs it."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

from . import config


class ComponentError(RuntimeError):
    pass


def browser_root() -> Path:
    return config.data_dir() / "components" / "browsers"


def browser_executable() -> Path | None:
    try:
        import playwright
        manifest = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
        browser = next(x for x in json.loads(manifest.read_text(encoding="utf8"))["browsers"]
                       if x["name"] == "chromium")
        folder = browser_root() / ("chromium-" + browser["revision"])
        for name in ("chrome-win64/chrome.exe", "chrome-win/chrome.exe",
                     "chrome-linux64/chrome", "chrome-linux/chrome"):
            path = folder / name
            if path.is_file() and (folder / "INSTALLATION_COMPLETE").is_file():
                return path
    except (ImportError, OSError, ValueError, KeyError, StopIteration):
        pass
    return None


def managed_browser_required() -> bool:
    return bool(getattr(sys, "frozen", False) or browser_executable())


def browser_launch_options() -> dict:
    if managed_browser_required():
        executable = browser_executable()
        if not executable:
            raise ComponentError("登录浏览器尚未准备，请在使用引导中下载后重试")
        return {"executable_path": str(executable)}
    # Preserve the source distribution's existing system-Chrome workflow.
    return {"channel": "chrome"}


class BrowserComponents:
    def __init__(self, process_factory=subprocess.Popen, executable=browser_executable, timeout=900):
        self._lock = threading.RLock()
        self._done = threading.Event()
        self._cancel = threading.Event()
        self._proc = None
        self._state = "missing"
        self._message = "首次自动登录时下载专用浏览器；手动授权无需下载"
        self._progress = None
        self._factory, self._executable, self.timeout = process_factory, executable, timeout

    def status(self) -> dict:
        with self._lock:
            ready = self._executable() is not None
            return {"state": "ready" if ready and self._state != "installing" else self._state,
                    "ready": ready and self._state != "installing",
                    "progress": self._progress,
                    "message": "登录浏览器已准备" if ready and self._state != "installing" else self._message}

    def start(self, *, retry=True) -> dict:
        with self._lock:
            if self.status()["ready"] or self._state == "installing":
                return self.status()
            if not retry and self._state == "failed":
                return self.status()
            self._state, self._message, self._progress = "installing", "正在连接下载源…", None
            self._done.clear()
            self._cancel.clear()
            threading.Thread(target=self._install, name="browser-download", daemon=True).start()
            return self.status()

    def _install(self):
        timer = None
        try:
            from playwright._impl._driver import compute_driver_executable, get_driver_env
            node, cli = compute_driver_executable()
            env = {**get_driver_env(), "PLAYWRIGHT_BROWSERS_PATH": str(browser_root()),
                   "CI": "1", "PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT": "60000"}
            browser_root().mkdir(parents=True, exist_ok=True)
            proc = self._factory([node, cli, "install", "chromium", "--no-shell"], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf8", errors="replace",
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with self._lock:
                self._proc = proc
                if self._cancel.is_set():
                    proc.terminate()
            timer = threading.Timer(self.timeout, self.cancel)
            timer.daemon = True
            timer.start()
            lines = []
            for raw in proc.stdout:
                line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw).strip()
                if not line:
                    continue
                lines = (lines + [line])[-5:]
                with self._lock:
                    self._message = line[:250]
                    percent = re.search(r"(\d{1,3})%", line)
                    self._progress = min(100, int(percent[1])) if percent else None
            code = proc.wait()
            if self._cancel.is_set():
                raise ComponentError("下载已停止或超时，可点击重试；已下载的其他组件会复用")
            if code or not self._executable():
                raise ComponentError("下载未完成，请检查网络后重试。" + " ".join(lines)[-450:])
            with self._lock:
                self._state, self._message, self._progress = "ready", "登录浏览器已准备", 100
        except Exception as exc:
            with self._lock:
                self._state, self._message = "failed", str(exc)[:600]
        finally:
            if timer:
                timer.cancel()
            with self._lock:
                self._proc = None
            self._done.set()

    def ensure_ready(self, cancel_event=None, on_progress=None):
        state = self.start(retry=False)
        while not state["ready"]:
            if cancel_event and cancel_event.is_set():
                # One cancelled account must not cancel another account's download.
                raise ComponentError("登录已取消")
            if state["state"] != "installing":
                raise ComponentError(state["message"])
            if on_progress:
                on_progress(state["message"])
            self._done.wait(.5)
            state = self.status()

    def cancel(self):
        with self._lock:
            self._cancel.set()
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()


_instances: dict[str, BrowserComponents] = {}
_instances_lock = threading.Lock()


def browser_components() -> BrowserComponents:
    key = str(config.data_dir().resolve())
    with _instances_lock:
        return _instances.setdefault(key, BrowserComponents())
