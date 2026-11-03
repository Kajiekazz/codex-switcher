"""First-run state and setup actions without changing the existing dashboard."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading

from . import config, settings
from .accounts import EMAIL_RE, mask_email
from .auth_state import read_auth_state_bytes
from .components import browser_components
from .startup import StartupRegistration


class Onboarding:
    def __init__(self, store, components=None, startup=None, auth_path=None):
        self.store = store
        self.components = components or browser_components()
        self.startup = startup or StartupRegistration()
        self.path = config.data_dir() / "onboarding.json"
        self.auth_path = Path(auth_path) if auth_path is not None else config.auth_file()
        self._lock = threading.RLock()

    def _saved(self):
        try:
            saved = json.loads(self.path.read_text(encoding="utf8"))
            return saved if isinstance(saved, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, saved):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(saved), encoding="utf8")
        os.replace(tmp, self.path)

    def _local(self):
        try:
            raw = self.auth_path.read_bytes()
            payload = json.loads(raw)
            tokens = payload.get("tokens") if isinstance(payload, dict) else None
            if not isinstance(tokens, dict) or not isinstance(tokens.get("access_token"), str) or not tokens["access_token"]:
                raise ValueError("no_chatgpt_credentials")
            state = read_auth_state_bytes(raw)
            if not state.get("logged_in") or not EMAIL_RE.fullmatch(state.get("email") or ""):
                raise ValueError("no_account_identity")
            if state.get("expired") and not tokens.get("refresh_token"):
                raise ValueError("expired_without_refresh")
            return raw, state
        except (OSError, ValueError, TypeError, AttributeError):
            return None, None

    def status(self) -> dict:
        with self._lock:
            saved = self._saved()
            accounts = self.store.list()
            ready = {s["email"].lower() for s in self.store.list_auth_snapshots() if not s.get("expired") and not s.get("disabled")}
            items = [{"email": a["email"], "email_masked": a["email_masked"],
                      "ready": a["email"].lower() in ready,
                      "has_password": a["has_password"], "has_totp": a["has_totp"]} for a in accounts]
            _, local = self._local()
            local_account = {"available": False}
            if local:
                existing = self.store.get(local["email"])
                local_account = {"available": True, "email": local["email"],
                                 "email_masked": mask_email(local["email"]), "plan": local.get("plan"),
                                 "fingerprint": local["file_sha256"], "expired": local.get("expired", False),
                                 "in_pool": existing is not None, "imported": local["email"].lower() in ready}
            exe = config.find_codex_exe()
            dismissed = saved.get("dismissed", bool(saved and saved.get("version") == 1))
            return {"ok": True, "show_guide": not saved.get("completed") and not dismissed and (bool(saved) or not ready),
                    "completed": saved.get("completed", False),
                    "step": saved.get("step", 0), "browser_mode": saved.get("browser_mode", "auto"),
                    "local_selected": saved.get("local_selected", True), "local_account": local_account,
                    "codex": {"available": bool(exe), "path": exe},
                    "accounts": {"total": len(accounts),
                                 "ready": sum(a["ready"] for a in items)}, "account_items": items,
                    "browser": self.components.status(), "startup": self.startup.status(),
                    "login_enabled": config.login_enabled(), "login_driver": config.login_driver()}

    def progress(self, *, step=None, browser_mode=None, local_selected=None):
        with self._lock:
            saved = self._saved()
            if step is not None:
                if type(step) is not int or not 0 <= step <= 3:
                    raise ValueError("引导步骤必须在 0 到 3 之间")
                saved["step"] = step
            if browser_mode is not None:
                if browser_mode not in {"auto", "manual", "local"}:
                    raise ValueError("请选择自动登录、手动授权或仅导入本地账号")
                saved["browser_mode"] = browser_mode
            if local_selected is not None:
                if type(local_selected) is not bool:
                    raise ValueError("local_selected 必须为布尔值")
                saved["local_selected"] = local_selected
            saved.update(version=2, dismissed=False)
            self._save(saved)
            return self.status()

    def import_local(self, fingerprint):
        with self._lock:
            raw, state = self._local()
            if raw is None:
                raise ValueError("未找到可导入的本地 ChatGPT 登录账号，请重新检测或手动添加")
            if not fingerprint or state["file_sha256"] != fingerprint:
                raise ValueError("本地登录账号已变化，请重新检测后再导入")
            existing = self.store.get(state["email"])
            email = existing["email"] if existing else state["email"]
            ready = {s["email"].lower() for s in self.store.list_auth_snapshots() if not s.get("expired")}
            if existing and email.lower() in ready:
                return {"email": email, "result": "unchanged"}
            if existing is None:
                self.store.upsert({"email": email})
            self.store.save_auth_snapshot(email, raw, {key: state.get(key) for key in ("email", "name", "plan", "account_id", "expires_at")})
            return {"email": email, "result": "updated" if existing else "inserted"}

    def finish(self, action):
        if action not in ("complete", "later"):
            raise ValueError("action 只能是 complete 或 later")
        with self._lock:
            state = self.status()
            if action == "complete" and (not state["codex"]["available"] or not state["accounts"]["ready"]):
                raise ValueError("请先检测 Codex 并完成至少一个账号授权，也可以选择稍后设置")
            saved = self._saved()
            saved.update(version=2, completed=action == "complete", dismissed=True)
            if action == "complete":
                saved["step"] = 3
            self._save(saved)
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
