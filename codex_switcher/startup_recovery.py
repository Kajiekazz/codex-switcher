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

    def _plan(self):
        provider = getattr(self.goals, "startup_recovery_plan", None)
        plan = provider() if callable(provider) else None
        if isinstance(plan, dict):
            return plan
        return {"enabled": True, "thread_ids": self.goals.startup_recovery_ids(), "skipped": []}

    def _finish_empty_plan(self, plan):
        labels = {
            "paused_in_switcher": "Switcher 内已暂停", "archived": "已归档",
            "already_queued": "已有排队消息", "already_running": "已经运行",
            "complete": "目标已完成", "blocked": "目标受阻", "budget_limited": "目标预算已用尽",
            "single_turn_finished": "单次任务已结束", "paused_state_unconfirmed": "暂停状态尚未确认",
            "no_unfinished_goal": "没有未完成目标",
        }
        skipped = plan.get("skipped") or []
        counts = {}
        for item in skipped:
            reason = item.get("reason")
            counts[reason] = counts.get(reason, 0) + 1
        message = "自动恢复未开启" if not plan.get("enabled", True) else (
            "开机恢复已跳过：" + "；".join(f"{labels.get(reason, reason)} {count} 个"
                                            for reason, count in counts.items())
            if skipped else "没有标记为自动恢复的未完成会话")
        self._set("done", message, thread_ids=[], skipped=skipped)

    def _observe_dispatch(self, outcome, skipped):
        """Job creation is not delivery: retain the eventual failure reason."""
        job = outcome.get("job") or {}
        job_id = job.get("id")
        if not job_id:
            self._set("done", "会话已开始运行，无需重复投递", thread_ids=[], skipped=skipped)
            return
        ids = outcome.get("thread_ids", [])
        while not self.stopped.is_set():
            values = {"thread_ids": ids, "job_id": job_id, "skipped": skipped}
            try:
                current = self.goals.status().get("job") or {}
                if current.get("id") != job_id:
                    current = next((item for item in self.goals.history() if item.get("id") == job_id), {})
            except Exception as exc:
                # The job already exists. An observation failure must never
                # return to on_startup() and create another queue submission.
                self._set("waiting_dispatch", "正在重新读取开机投递状态", error=str(exc)[:300], **values)
                if self.wait(2):
                    break
                continue
            if current.get("state") in {"failed", "interrupted", "cancelled", "partial"}:
                self._set("failed", "开机恢复投递未完成，详情见续跑队列",
                          error=current.get("error"), dispatch_state=current.get("state"), **values)
                return
            if current.get("state") == "done":
                results = current.get("results") or []
                submitted = [item["thread_id"] for item in results if item.get("queued_id")]
                changed = [{"thread_id": item.get("thread_id"),
                            "reason": "already_running" if item.get("already_running") else item.get("error")}
                           for item in results if not item.get("queued_id")]
                message = "开机恢复投递已完成，执行状态见续跑队列" if submitted else (
                    "会话已开始运行，无需重复投递" if results and all(item.get("already_running") for item in results)
                    else "会话状态已变化，已跳过本次开机投递")
                self._set("done", message, **{**values, "thread_ids": submitted, "skipped": skipped + changed})
                return
            self._set("waiting_dispatch", "正在投递开机恢复，执行状态见续跑队列", **values)
            if self.wait(2):
                break
        self._set("cancelled", "开机恢复已停止", job_id=job_id, thread_ids=ids)

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
                plan = self._plan()
                ids = plan["thread_ids"]
                if not ids:
                    self._finish_empty_plan(plan)
                    return
                state = usage_state(self.quota.current(force=True))
                if state != "available":
                    self._set("waiting_quota", "等待可用账号额度后恢复任务", thread_ids=ids)
                else:
                    if self.stopped.is_set():
                        break
                    outcome = self.goals.on_startup()
                    if outcome.get("ok") and outcome.get("skipped") != "job_running":
                        if outcome.get("skipped"):
                            self._finish_empty_plan(self._plan())
                        else:
                            self._observe_dispatch(outcome, outcome.get("skipped_goals", plan.get("skipped", [])))
                        return
                    self._set("waiting_dispatch", "等待当前续跑投递完成后恢复任务", thread_ids=ids)
            except Exception:
                self._set("waiting_retry", "开机恢复暂未就绪，稍后重试")
            if self.wait(30):
                break
        self._set("cancelled", "开机恢复已停止")
