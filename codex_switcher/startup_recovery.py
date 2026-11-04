"""Windows startup: open Codex, then restore opted-in unfinished chats."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading

from . import config
from .quota_state import usage_state


class StartupRecovery:
    def __init__(self, desktop, goals, quota, monitor, stopped, *, path=None, wait=None):
        self.desktop, self.goals, self.quota, self.monitor = desktop, goals, quota, monitor
        self.stopped = stopped
        self.wait = wait or stopped.wait
        self.path = Path(path) if path is not None else config.data_dir() / "startup-recovery.json"
        self._lock = threading.Lock()
        self._state = {"state": "pending", "message": "正在准备开机恢复"}

    def status(self):
        with self._lock:
            return dict(self._state)

    def _set(self, state, message, **values):
        with self._lock:
            self._state = {"state": state, "message": message, **values,
                           "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._state, ensure_ascii=False), encoding="utf-8")
                tmp.replace(self.path)
            except OSError:
                pass

    def run(self):
        ready = False
        try:
            # Serialize the initial desktop activation before starting the
            # account-switch monitor, which may need to restart the same app.
            for attempt in range(3):
                if self.stopped.is_set():
                    return
                self._set("starting_desktop", "正在启动 Codex", attempt=attempt + 1)
                state = self.desktop.status()
                if state.get("ok") and state.get("running"):
                    ready = True
                elif state.get("ok"):
                    ready = bool(self.desktop.start(before=state).get("ok"))
                if ready or self.wait(15 * 2 ** attempt):
                    break
        except Exception:
            self._set("failed", "Codex 启动失败，请手动打开后续跑")
        finally:
            if not self.stopped.is_set():
                self.monitor.start()
        if self.stopped.is_set():
            self._set("cancelled", "开机恢复已停止")
            return
        if not ready:
            self._set("failed", "Codex 未完成启动，请手动打开后续跑")
            return

        while not self.stopped.is_set():
            try:
                ids = self.goals.startup_recovery_ids()
                if not ids:
                    self._set("done", "没有等待开机恢复的会话", thread_ids=[])
                    return
                state = usage_state(self.quota.current(force=True))
                if state != "available":
                    self._set("waiting_quota", "等待可用账号额度后恢复任务", thread_ids=ids)
                else:
                    if self.stopped.is_set():
                        break
                    outcome = self.goals.on_startup()
                    if outcome.get("ok") and outcome.get("skipped") != "job_running":
                        self._set("done", "开机恢复已提交，执行状态见续跑队列",
                                  thread_ids=outcome.get("thread_ids", []),
                                  job_id=(outcome.get("job") or {}).get("id"))
                        return
                    self._set("waiting_dispatch", "等待当前续跑投递完成后恢复任务", thread_ids=ids)
            except Exception:
                self._set("waiting_retry", "开机恢复暂未就绪，稍后重试")
            if self.wait(30):
                break
        self._set("cancelled", "开机恢复已停止")
