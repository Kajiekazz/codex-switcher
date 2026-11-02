"""First-run state and setup actions without changing the existing dashboard."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading

from . import config, settings
from .components import browser_components
from .startup import StartupRegistration


class Onboarding:
    def __init__(self, store, components=None, startup=None):
        self.store = store
        self.components = components or browser_components()
        self.startup = startup or StartupRegistration()
        self.path = config.data_dir() / "onboarding.json"
        self._lock = threading.RLock()

    def status(self) -> dict:
        with self._lock:
            try:
                saved = json.loads(self.path.read_text(encoding="utf8"))
            except (OSError, ValueError):
                saved = {}
            accounts = self.store.list()
            exe = config.find_codex_exe()
            return {"ok": True, "show_guide": not saved and not accounts,
                    "completed": saved.get("completed", False),
                    "codex": {"available": bool(exe), "path": exe},
                    "accounts": {"total": len(accounts),
                                 "ready": sum(bool(a.get("has_auth_snapshot")) for a in accounts)},
                    "browser": self.components.status(), "startup": self.startup.status(),
                    "login_enabled": config.login_enabled(), "login_driver": config.login_driver()}

    def finish(self, action):
        if action not in ("complete", "later"):
            raise ValueError("action 只能是 complete 或 later")
        with self._lock:
            state = self.status()
            if action == "complete" and (not state["codex"]["available"] or not state["accounts"]["ready"]):
                raise ValueError("请先检测 Codex 并完成至少一个账号授权，也可以选择稍后设置")
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": 1, "completed": action == "complete"}), encoding="utf8")
            os.replace(tmp, self.path)
            return self.status()

    def select_codex(self, path=None):
        if path is None:
            from .windows_ui import select_executable
            path = select_executable()
            if not path:
                return {"ok": True, "cancelled": True, **self.status()}
        if not isinstance(path, str) or not path.strip():
            raise ValueError("请选择 codex.exe")
        candidate = Path(path).expanduser().resolve()
        if not candidate.is_file() or candidate.name.lower() != "codex.exe":
            raise ValueError("请选择有效的 codex.exe 文件")
        settings.save({"codex_exe": str(candidate)})
        return self.status()
