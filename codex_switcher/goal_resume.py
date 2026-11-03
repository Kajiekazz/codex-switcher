"""Goal resume dispatcher: hand selected goals back to the Codex desktop app.

A resume is a *dispatch*, not a background run:

1. Prepare a paused goal for one-turn continuation, or an active goal for
   continuous work. Derived mutations use ``origin=automatic`` so they do not
   record a user instruction through another process's thread writer lock.
2. ``thread/queue/add`` with a ``继续`` message — also lock-free. The row lands
   in ``~/.codex/queue_1.sqlite`` and the desktop's own app-server picks it up
   through the ``queued_thread_revisions`` trigger and starts the turn there.
3. ``codex://threads/wake/<thread>`` — the app only drains the queue of threads
   it has loaded, so we hand the running app its own deep link. Its main process
   lists ``thread/queue/list`` and calls ``resumeThread`` when something is
   pending. Queue execution also depends on the desktop's conversation runtime.
   No process is started, closed, or restarted.

When consumed, the turn runs inside the user's Codex window with normal UI
progress. Queue disappearance alone cannot confirm consumption: editing or
deleting the message removes the same row. This service never has to steal the
writer lock or close the app. See
``work/codex-goal-resume-re/findings.md`` for the reverse-engineering evidence.

Safety model:
- manual continuation is always available; automatic recovery is opt-in
- explicit thread selection from the dashboard
- ``dry_run`` is the default and never mutates goal state
- real dispatches require ``confirm=true``; one-turn continuation pauses
  automatic goal advancement before sending exactly one message
- one job at a time, serial threads
- state changes go through ``thread/goal/set`` / ``thread/queue/add`` in the
  app-server; no SQLite database is written directly
- the wake deep link is only sent to an app that is already running
  (``CODEX_SWITCHER_GOAL_RESUME_WAKE=0`` turns it off)
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config, settings
from .appserver import AppServerError, AppServerSession
from .desktop import DesktopClient
from .desktop_ipc import DesktopControl, DesktopIpcError
from .execution import snapshot_threads
from .resume_execution import ResumeExecutionTracker, rollout_cursor
from .resume_queue import build_queue
from .goals import list_goals, list_sessions
from .queue_watch import (
    QueueWatchdog,
    first_resume_row,
    resume_rows,
    thread_archived,
)

#: Goal states worth re-arming. ``usageLimited`` is the obvious one, but a goal
#: also needs a nudge when it is ``paused`` (stop) or still ``active`` while no
#: turn is actually running — which is exactly what a switch does: the desktop
#: app is restarted, the running turn dies, and the goal stays ``active``
#: forever. Without this, "自动续跑" only ever fires once per goal.
ARMABLE_STATUSES = {"usageLimited", "paused", "active"}

#: The same set in the goals-SQLite vocabulary (``goals.py`` DB_TO_RPC_STATUS).
ARMABLE_DB_STATUSES = {"usage_limited", "paused", "active"}

HISTORY_LIMIT = 200

#: The message we drop into the thread queue. The desktop app turns it into a
#: normal user turn, so it must read like something the user would say.
RESUME_MESSAGE = "继续"

# Bound automatic goals to the work the user already requested in this chat.
# A queue message wakes the desktop; this objective drives later goal turns.
RESUME_GOAL_OBJECTIVE = (
    "完成本会话中用户已明确要求但尚未完成的任务，遵守原有范围和约束。"
    "以实际结果和必要验证确认完成，全部完成后结束目标；"
    "需要用户决定或缺少必要信息时说明原因并等待，不自行扩展任务。"
)

#: Poll interval while waiting for the desktop app to take the queued message.
QUEUE_POLL_SECONDS = 0.5

def _flag(key: str) -> bool:
    return (settings.effective(key) or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def auto_resume_ids() -> list[str]:
    """Thread ids the user marked "自动" (resume after every switch)."""
    try:
        raw = json.loads(settings.effective("auto_resume_threads") or "[]")
    except ValueError:
        return []
    if not isinstance(raw, list):
        return []
    ids: list[str] = []
    for value in raw:
        if isinstance(value, str):
            tid = value.strip()
            if tid and tid not in ids:
                ids.append(tid)
    return ids


def set_auto_resume(thread_id: str, enabled: bool) -> list[str]:
    thread_id = (thread_id or "").strip()
    if not thread_id:
        raise GoalResumeError("thread_required", "thread_id 不能为空")
    ids = auto_resume_ids()
    if enabled:
        if thread_id not in ids:
            ids.append(thread_id)
    else:
        ids = [value for value in ids if value != thread_id]
    settings.save({"auto_resume_threads": json.dumps(ids)})
    return ids


class GoalResumeError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _compact_wake(wake) -> dict | None:
    """The fields worth persisting from a wake result (no pid lists)."""
    if not isinstance(wake, dict):
        return None
    keys = ("ok", "skipped", "reason", "error", "url", "duration_ms")
    compact = {key: wake[key] for key in keys if wake.get(key) is not None}
    return compact or None


class GoalResumeService:
    def __init__(
        self,
        session_factory=AppServerSession,
        goals_provider=list_goals,
        history_path: Path | str | None = None,
        sessions_provider=list_sessions,
        desktop_client: DesktopClient | None = None,
        sleep=None,
        clock=None,
        queue_watch: QueueWatchdog | None = None,
        execution_provider=snapshot_threads,
        receipt_tracker=None,
        desktop_control=None,
        model_guard=None,
    ):
        self._lock = threading.RLock()
        self._control_lock = threading.RLock()
        self._job: dict | None = None
        self._thread: threading.Thread | None = None
        self._session: AppServerSession | None = None
        self._session_factory = session_factory
        self._goals_provider = goals_provider
        self._sessions_provider = sessions_provider
        self._execution_provider = execution_provider
        self._desktop = desktop_client if desktop_client is not None else DesktopClient()
        self._desktop_control = desktop_control or DesktopControl()
        self.model_guard = model_guard
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._history_path = (
            Path(history_path)
            if history_path is not None
            else config.data_dir() / "goal-resume-history.jsonl"
        )
        self._history: list[dict] = self._load_history()
        self._mode_path = self._history_path.with_name("resume-modes.json")
        try:
            modes = json.loads(self._mode_path.read_text(encoding="utf-8"))
            self._modes = {tid: mode for tid, mode in modes.items() if mode in {"once", "continuous"}}
        except (OSError, ValueError, AttributeError):
            self._modes = {}
        self._pause_path = self._history_path.with_name("resume-pauses.json")
        try:
            saved_pauses = json.loads(self._pause_path.read_text(encoding="utf-8"))
            self._pauses = {tid: row for tid, row in saved_pauses.items() if isinstance(row, dict)}
        except (OSError, ValueError, TypeError, AttributeError):
            self._pauses = {}
        self._interrupted = self._mark_interrupted()
        #: outcome of the most recent post-switch auto resume, surfaced in
        #: /api/goals so "换号后什么都没发生" is explainable.
        self._last_auto: dict | None = None
        #: Keeps a queued message owned after the job ends: the desktop app may
        #: take minutes or lose a wake during its restart for a switch.
        #: Queue disappearance only confirms removal, not execution: editing or
        #: deleting a queued message removes the same row.
        self._queue_watch: QueueWatchdog | None = queue_watch
        if queue_watch is None and config.goal_resume_watchdog_enabled():
            self._queue_watch = QueueWatchdog(
                wake=self._wake_for_watch,
                queue_db=config.queue_db_path(),
                state_path=config.data_dir() / "goal-resume-queue.json",
                ttl_seconds=config.goal_resume_watch_ttl_seconds(),
                interval=config.goal_resume_watch_interval_seconds(),
                on_change=self._apply_queue_entry,
                probe=self._desktop_probe,
                archived=lambda thread_id: thread_archived(
                    config.state_db_path(), thread_id
                ),
                log=lambda level, message: print(
                    f"[queue-watch] {message}", flush=True
                ),
            )
        try:
            self._adopt_orphans()
        except Exception as exc:  # noqa: BLE001 - startup must not fail
            print(f"[queue-watch] 接管历史队列失败: {exc}", flush=True)
        self._receipt_tracker = receipt_tracker or ResumeExecutionTracker(
            on_change=self._apply_execution_receipt,
        )
        # Backfill the rows already visible in the UI, including ones that left
        # the queue before the service was updated. Never submit another turn.
        for tid, result in self._last_resume_by_thread().items():
            if result.get("ok") and result.get("queued_id") and not result.get("cancelled"):
                self._receipt_tracker.track(tid, result, requested_at=result.get("queued_at") or result.get("requested_at"))

    # --------------------------------------------------------------- history
    def _load_history(self) -> list[dict]:
        try:
            raw = self._history_path.read_text(encoding="utf-8")
        except OSError:
            return []
        items: list[dict] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                items.append(item)
        return items[-HISTORY_LIMIT:]

    def _record_history(self, job: dict) -> None:
        summary = {
            "id": job.get("id"),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "dry_run": bool(job.get("dry_run")),
            "continue_once": bool(job.get("continue_once")),
            "continuation_modes": dict(job.get("continuation_modes") or {}),
            "skipped_running": list(job.get("skipped_running") or []),
            "started_via": job.get("started_via"),
            "state": job.get("state"),
            "thread_ids": list(job.get("thread_ids") or []),
            "error": job.get("error"),
            "desktop_taken_over": bool(job.get("desktop_taken_over")),
            "takeover_threads": list(job.get("takeover_threads") or []),
            "desktop_relaunch": job.get("desktop_relaunch"),
            "desktop_restart": job.get("desktop_restart"),
            "results": [
                {
                    "thread_id": item.get("thread_id"),
                    "continue_once": item.get("continue_once", bool(job.get("continue_once"))),
                    "already_running": bool(item.get("already_running")),
                    "message": item.get("message"),
                    "model": item.get("model"),
                    "account_signature": item.get("account_signature"),
                    "model_access": item.get("model_access"),
                    "ok": bool(item.get("ok")),
                    "status_before": item.get("status_before"),
                    "status_after": item.get("status_after"),
                    "goal_created": bool(item.get("goal_created")),
                    "goal_creation_error": item.get("goal_creation_error"),
                    "turns": int(item.get("turns") or 0),
                    "error": item.get("error"),
                    "started_via": item.get("started_via"),
                    "cancelled": bool(item.get("cancelled")),
                    "skipped_by_user": bool(item.get("skipped_by_user")),
                    "rollback_failed": bool(item.get("rollback_failed")),
                    # The compatible "delivered" field means no longer queued,
                    # not that a turn started. Persisting the id
                    # lets the watchdog and a restarted service keep tracking it.
                    "queued_id": item.get("queued_id"),
                    "client_message_id": item.get("client_message_id"),
                    "queued_at": item.get("queued_at"),
                    "execution": item.get("execution"),
                    "owned_queue": bool(item.get("owned_queue")),
                    "pending": bool(item.get("pending")),
                    "delivered": item.get("delivered"),
                    "delivered_at": item.get("delivered_at"),
                    "queue_expired": bool(item.get("queue_expired")),
                    "queue_abandoned": bool(item.get("queue_abandoned")),
                    "wake": _compact_wake(item.get("wake")),
                }
                for item in (job.get("results") or [])
            ],
        }
        with self._lock:
            # Upsert by job id: a job is first written as "running" when it
            # starts and rewritten when it finishes, so a crash/restart leaves
            # exactly one record instead of a phantom duplicate.
            index = next(
                (
                    position
                    for position, entry in enumerate(self._history)
                    if entry.get("id") == summary["id"]
                ),
                None,
            )
            if index is None:
                self._history.append(summary)
            else:
                self._history[index] = summary
            self._history = self._history[-HISTORY_LIMIT:]
            snapshot = list(self._history)
        self._write_history(snapshot)

    def _write_history(self, snapshot: list[dict]) -> None:
        with self._lock:
            snapshot = list(self._history)
            try:
                self._history_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._history_path.with_suffix(".jsonl.tmp")
                tmp.write_text(
                    "\n".join(json.dumps(x, ensure_ascii=False) for x in snapshot)
                    + "\n",
                    encoding="utf-8",
                )
                os.replace(tmp, self._history_path)
            except OSError:
                pass

    def _mark_interrupted(self) -> int:
        """A ``running`` record survived a restart: the job died with the service.

        Keeping it as ``running`` forever would make the dashboard poll a task
        that no longer exists, so rewrite it as ``interrupted`` — the restart
        becomes visible instead of silent. A job that had closed the desktop app
        must also give it back: its ``finally`` never ran, so nothing else would
        reopen the window.
        """
        reopen_desktop = False
        with self._lock:
            changed = 0
            for entry in self._history:
                if entry.get("state") != "running":
                    continue
                entry["state"] = "interrupted"
                entry["error"] = entry.get("error") or "service_restarted"
                entry["finished_at"] = entry.get("finished_at") or _now_iso()
                if entry.get("desktop_taken_over"):
                    reopen_desktop = True
                changed += 1
            snapshot = list(self._history) if changed else None
        if snapshot is not None:
            self._write_history(snapshot)
        if reopen_desktop:
            try:
                self._desktop.start()
            except Exception:  # noqa: BLE001 - startup must not fail
                pass
        return changed

    def history(self) -> list[dict]:
        """Recorded jobs, newest first (persisted across restarts)."""
        with self._lock:
            items = list(self._history)
        return list(reversed(items))

    def _last_resume_by_thread(self) -> dict[str, dict]:
        latest: dict[str, dict] = {}
        with self._lock:
            history = list(self._history)
        for entry in reversed(history):
            if entry.get("dry_run"):
                continue
            for result in entry.get("results") or []:
                tid = result.get("thread_id")
                if not tid or tid in latest or result.get("already_running"):
                    continue
                latest[tid] = {
                    "job_id": entry.get("id"),
                    "requested_at": entry.get("started_at"),
                    "finished_at": entry.get("finished_at"),
                    "dry_run": bool(entry.get("dry_run")),
                    "state": entry.get("state"),
                    "continue_once": result.get("continue_once", entry.get("continue_once", True)),
                    "message": result.get("message"),
                    "model": result.get("model"),
                    "account_signature": result.get("account_signature"),
                    "model_access": result.get("model_access"),
                    "ok": result.get("ok"),
                    "status_before": result.get("status_before"),
                    "status_after": result.get("status_after"),
                    "goal_created": bool(result.get("goal_created")),
                    "goal_creation_error": result.get("goal_creation_error"),
                    "turns": result.get("turns"),
                    "error": result.get("error"),
                    "started_via": result.get("started_via") or entry.get("started_via"),
                    "cancelled": bool(result.get("cancelled")),
                    "skipped_by_user": bool(result.get("skipped_by_user")),
                    "rollback_failed": bool(result.get("rollback_failed")),
                    "queued_id": result.get("queued_id"),
                    "client_message_id": result.get("client_message_id"),
                    "queued_at": result.get("queued_at"),
                    "execution": result.get("execution"),
                    "owned_queue": bool(result.get("owned_queue")),
                    "pending": bool(result.get("pending")),
                    "delivered": result.get("delivered"),
                    "delivered_at": result.get("delivered_at"),
                    "queue_expired": bool(result.get("queue_expired")),
                    "queue_abandoned": bool(result.get("queue_abandoned")),
                    "wake": result.get("wake"),
                    "watch": (
                        self._queue_watch.for_thread(tid)
                        if self._queue_watch is not None
                        else None
                    ),
                }
        for result in latest.values():
            # A previous watchdog entry for the same chat says nothing about a
            # newly submitted queue item.
            if result.get("watch") and result["watch"].get("queued_id") != result.get("queued_id"):
                result["watch"] = None
            tracker = getattr(self, "_receipt_tracker", None)
            receipt = tracker.for_queue(result.get("queued_id")) if tracker else None
            if receipt:
                result["execution"] = receipt
        return latest

    def _pending_by_thread(self) -> dict[str, dict]:
        """Queue ownership survives a preview or a subsequent failed request."""
        pending = {}
        seen = set()
        with self._lock:
            for entry in reversed(self._history):
                if entry.get("dry_run"):
                    continue
                for result in entry.get("results", []):
                    tid = result.get("thread_id")
                    queued_id = result.get("queued_id")
                    if not result.get("owned_queue") or not queued_id or queued_id in seen:
                        continue
                    seen.add(queued_id)
                    if (tid and tid not in pending and result.get("owned_queue")
                            and result.get("queued_id") and result.get("pending")
                            and not result.get("cancelled")):
                        pending[tid] = dict(result)
        return pending

    def history_path(self) -> str:
        return str(self._history_path)

    #: A row adopted after a restart must have been created within this window
    #: around the recorded dispatch; anything else is someone else's queue.
    ORPHAN_WINDOW_SECONDS = 15 * 60

    def _adopt_orphans(self) -> int:
        """Re-attach rows queued before ``queued_id`` was persisted.

        Older history entries only recorded ``ok`` — the queue row id was lost,
        so a message queued right before a restart (or by an older build) would
        sit in ``queue_1.sqlite`` forever: the exact "切号后一直没人取走" case.
        Match our own ``继续`` rows inside the dispatch window and hand them to
        the watchdog. Only the newest few entries are considered, and a row is
        adopted once.
        """
        watchdog = self._queue_watch
        if watchdog is None:
            return 0
        candidates: list[tuple[dict, dict, str]] = []
        with self._lock:
            for entry in reversed(self._history):
                if entry.get("dry_run") or not entry.get("finished_at"):
                    continue
                for result in entry.get("results") or []:
                    if not result.get("ok") or result.get("queued_id"):
                        continue
                    if result.get("delivered") or result.get("pending") is False:
                        continue
                    if result.get("cancelled") or result.get("skipped_by_user"):
                        continue
                    thread_id = result.get("thread_id")
                    if isinstance(thread_id, str) and thread_id:
                        candidates.append((entry, result, thread_id))
                if len(candidates) >= 5:
                    break
        adopted = 0
        used: set[str] = set()
        for entry, result, thread_id in candidates:
            if thread_archived(config.state_db_path(), thread_id):
                # Archived conversations never resume; adopting the row would
                # only resurrect a 「等桌面端取走」 that can never happen.
                continue
            try:
                finished = datetime.fromisoformat(
                    str(entry.get("finished_at"))
                ).timestamp()
            except (TypeError, ValueError):
                continue
            rows = resume_rows(
                config.queue_db_path(),
                thread_id,
                message=RESUME_MESSAGE,
                since_ms=int(finished - self.ORPHAN_WINDOW_SECONDS) * 1000,
                until_ms=int(finished + self.ORPHAN_WINDOW_SECONDS) * 1000,
            )
            queued_id = next((row for row in rows if row not in used), None)
            if not queued_id:
                continue
            used.add(queued_id)
            tracked = watchdog.track(
                thread_id, queued_id, job_id=entry.get("id"), observed=True
            )
            result.update(
                queued_id=queued_id,
                pending=bool(tracked.get("pending")),
                delivered=bool(tracked.get("delivered")),
                delivered_at=tracked.get("delivered_at"),
            )
            adopted += 1
            print(
                f"[queue-watch] 接管历史遗留的排队消息 "
                f"{thread_id[:8]} · {queued_id}",
                flush=True,
            )
        if adopted:
            with self._lock:
                snapshot = list(self._history)
            self._write_history(snapshot)
        return adopted

    # ------------------------------------------------------------------ state
    def enabled(self) -> bool:
        return config.goal_resume_enabled()

    def running(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict:
        self._receipt_tracker.reconcile()
        with self._lock:
            job = dict(self._job) if self._job else None
        if job is not None:
            job["events"] = list(job.get("events", []))
            job["results"] = [dict(item) for item in job.get("results", [])]
        return {
            "enabled": self.enabled(),
            "running": self.running(),
            "start_timeout_seconds": config.goal_resume_start_timeout_seconds(),
            "job": job,
            "history": self.history()[:20],
            "queue_watch": (
                self._queue_watch.snapshot()
                if self._queue_watch is not None
                else {"enabled": False, "pending": 0, "entries": []}
            ),
            "execution_watch": {"active": self._receipt_tracker.active()},
        }

    def list(self, include_subagents: bool = False) -> dict:
        try:
            goals = list(self._goals_provider())
        except Exception as exc:  # noqa: BLE001 - display must not 500
            goals = []
            error = str(exc)
        else:
            error = None
        payload = self.status()
        payload["goals"] = goals
        try:
            try:
                sessions = self._sessions_provider(
                    include_subagents=include_subagents
                )
            except TypeError:
                sessions = self._sessions_provider()
        except Exception as exc:  # noqa: BLE001 - display must not 500
            sessions = None
            sessions_error = str(exc)
        else:
            sessions_error = None
        payload["sessions"] = sessions
        payload["sessions_error"] = sessions_error
        payload["last_resume"] = self._last_resume_by_thread()
        payload["pending_resume"] = self._pending_by_thread()
        items = {}
        if isinstance(sessions, dict):
            for item in sessions.get("pinned", []) + sessions.get("recent", []):
                items[item["thread_id"]] = item
            for project in sessions.get("projects", []):
                for item in project.get("items", []):
                    items[item["thread_id"]] = item
        # Bound cold-start I/O. Marked/recently dispatched chats are always
        # observed; other chats get evidence when explicitly dispatched.
        observed = list(dict.fromkeys(auto_resume_ids() + list(payload["last_resume"])
                                     + list(items)[:20]))
        execution = self._execution_provider(observed)
        for tid, item in items.items():
            item["continuable"] = not item.get("internal")
            item["execution"] = execution.get(tid, {"state": "unknown"})
        payload["execution"] = execution
        with self._lock:
            pauses = {tid: dict(row) for tid, row in self._pauses.items()}
        payload["resume_queue"] = build_queue(
            history=self.history(), latest=payload["last_resume"], pending=payload["pending_resume"],
            job=payload["job"], goals=goals, items=items, execution=execution, pauses=pauses,
        )
        payload["paused_threads"] = pauses
        payload["auto_resume"] = self.auto_resume_status()
        payload["history_path"] = self.history_path()
        payload["error"] = error
        return payload

    # ---------------------------------------------------------- auto resume
    def auto_resume_status(self) -> dict:
        return {
            "enabled": _flag("auto_resume_after_switch"),
            "thread_ids": auto_resume_ids(),
            "last": self._last_auto,
        }

    def set_auto_resume_thread(self, thread_id: str, enabled: bool) -> dict:
        ids = set_auto_resume(thread_id, enabled)
        return {"ok": True, "thread_ids": ids}

    def capture_before_switch(self) -> dict:
        """Capture marked chats before auth/desktop changes, never arm them."""
        wanted = auto_resume_ids()
        goals = {row["thread_id"]: row for row in self._goals_provider()}
        execution = self._execution_provider(wanted)
        eligible, skipped, running_turns = {}, [], {}
        latest = self._last_resume_by_thread()
        for tid in wanted:
            status = (goals.get(tid) or {}).get("status")
            current = execution.get(tid) or {}
            once = self.continuation_mode(tid) == "once"
            last = latest.get(tid) or {}
            receipt = last.get("execution") or {}
            unfinished_once = once and bool(last) and not last.get("cancelled") and receipt.get("state") != "completed"
            if self.is_paused(tid):
                reason = "paused_by_user"
            elif current.get("archived") or thread_archived(config.state_db_path(), tid):
                reason = "archived"
            elif status in {"complete", "blocked", "budget_limited"} or (status == "paused" and not unfinished_once):
                reason = status
            elif status == "usage_limited" or current.get("usage_limited"):
                eligible[tid] = "usage_limited"
                continue
            elif current.get("state") == "running":
                eligible[tid] = "running_before_switch"
                running_turns[tid] = current.get("turn_id")
                continue
            else:
                reason = "idle_or_unconfirmed"
            skipped.append({"thread_id": tid, "status": status, "reason": reason})
        return {"eligible": eligible, "running_turns": running_turns, "skipped": skipped,
                "continuation_modes": {tid: self.continuation_mode(tid) for tid in eligible}, "at": _now_iso()}

    def continuation_mode(self, thread_id):
        with self._lock:
            if thread_id in self._modes:
                return self._modes[thread_id]
        last = self._last_resume_by_thread().get(thread_id)
        return "once" if last and last.get("continue_once") else "continuous"

    def _save_modes(self):
        self._mode_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._mode_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._modes), encoding="utf-8")
        os.replace(tmp, self._mode_path)

    def required_models(self):
        if not self.enabled() or not _flag("auto_resume_after_switch"):
            return []
        from .model_access import thread_models
        context = self.capture_before_switch()
        return sorted(set(thread_models(list(context.get("eligible") or {})).values()))

    def on_account_switched(self, switch_result=None) -> dict:
        """Resume the goals the user pre-marked as "自动" after a switch.

        This is the only path that starts a real run without a per-click
        ``confirm``: the user already opted in per goal (the 「自动」 toggle),
        which is stricter than the card's one-off confirmation.
        """
        outcome = self._resume_after_switch(switch_result)
        self._last_auto = dict(outcome)
        self._last_auto["at"] = _now_iso()
        return outcome

    def on_quota_recovered(self, email: str) -> dict:
        """Recover opted-in quota-blocked chats without switching accounts."""
        if not self.enabled():
            return {"ok": True, "skipped": "goal_resume_disabled"}
        if not _flag("auto_resume_after_switch"):
            return {"ok": True, "skipped": "auto_resume_disabled"}
        if not auto_resume_ids():
            return {"ok": True, "skipped": "no_marked_goal"}
        if self.running():
            return {"ok": True, "skipped": "job_running"}
        context = self.capture_before_switch()
        states = self._execution_provider(list(context["eligible"]))
        context["eligible"] = {
            tid: reason for tid, reason in context["eligible"].items()
            if reason == "usage_limited" and (states.get(tid) or {}).get("state") != "running"
        }
        from types import SimpleNamespace
        outcome = self._resume_after_switch(
            SimpleNamespace(resume_context=context, target_email=email, desktop_restart=None),
            started_via="quota_recovery:" + email,
            cancel_stale=False,
        )
        self._last_auto = {**outcome, "at": _now_iso(), "trigger": "quota_recovered"}
        return outcome

    def _resume_after_switch(self, switch_result=None, *, started_via=None, cancel_stale=True) -> dict:
        cancelled_stale = False
        if self.running():
            if not cancel_stale:
                return {"ok": True, "skipped": "job_running"}
            # A job from the previous account cannot survive the switch: its
            # app-server was started with the old auth (and keeps sending it),
            # it holds the thread writer locks the fresh desktop app needs, and
            # it burns the old quota. Cancel it, then resume with the new one.
            if not self._cancel_stale_job():
                return {
                    "ok": False,
                    "skipped": "job_running",
                    "message": "旧续跑任务仍在运行且未能取消，已跳过本次自动续跑。",
                    "cancelled_stale_job": False,
                }
            cancelled_stale = True
        if not self.enabled():
            return {
                "ok": True,
                "skipped": "goal_resume_disabled",
                "cancelled_stale_job": cancelled_stale,
            }
        if not _flag("auto_resume_after_switch"):
            return {
                "ok": True,
                "skipped": "auto_resume_disabled",
                "cancelled_stale_job": cancelled_stale,
            }
        wanted = auto_resume_ids()
        if not wanted:
            return {
                "ok": True,
                "skipped": "no_marked_goal",
                "cancelled_stale_job": cancelled_stale,
            }
        try:
            context = getattr(switch_result, "resume_context", None)
            if not isinstance(context, dict):
                context = self.capture_before_switch()
        except Exception as exc:  # noqa: BLE001 - switch must not fail
            return {
                "ok": False, "skipped": "goals_unavailable", "error": str(exc),
                "cancelled_stale_job": cancelled_stale,
            }
        pending: list[str] = []
        if context.get("error"):
            return {"ok": False, "skipped": "snapshot_failed", "error": context["error"]}
        skipped_goals: list[dict] = list(context.get("skipped") or [])
        restart = getattr(switch_result, "desktop_restart", None)
        current_states = self._execution_provider(list((context.get("eligible") or {}).keys()))
        for tid in wanted:
            reason = (context.get("eligible") or {}).get(tid)
            if self.is_paused(tid):
                skipped_goals.append({"thread_id": tid, "reason": "paused_by_user"})
                continue
            current = current_states.get(tid) or {}
            previous_turn = (context.get("running_turns") or {}).get(tid)
            if reason == "running_before_switch" and (
                current.get("state") == "ended" or (
                    current.get("state") == "running" and previous_turn
                    and current.get("turn_id") != previous_turn
                )
            ):
                skipped_goals.append({"thread_id": tid, "reason": "finished_or_already_recovered"})
                continue
            if reason == "usage_limited" or (
                reason == "running_before_switch" and isinstance(restart, dict)
                and restart.get("ok") and not restart.get("skipped")
            ):
                pending.append(tid)
            elif reason:
                skipped_goals.append(
                    {"thread_id": tid, "reason": "not_interrupted_by_switch"}
                )
        if not pending:
            return {
                "ok": True,
                "skipped": "no_armable_goal",
                "thread_ids": [],
                "skipped_goals": skipped_goals,
                "cancelled_stale_job": cancelled_stale,
            }
        target = getattr(switch_result, "target_email", None) or ""
        try:
            job = self.start(
                pending,
                dry_run=False,
                confirm=True,
                started_via=started_via or (("switch:" + target) if target else "switch"),
                continuation_modes={tid: (context.get("continuation_modes") or {}).get(tid, self.continuation_mode(tid)) for tid in pending},
            )
        except Exception as exc:  # noqa: BLE001 - switch already succeeded
            return {
                "ok": False, "error": str(exc), "thread_ids": pending,
                "cancelled_stale_job": cancelled_stale,
            }
        return {
            "ok": True,
            "job": job,
            "thread_ids": pending,
            "skipped_goals": skipped_goals,
            "cancelled_stale_job": cancelled_stale,
        }

    # ------------------------------------------------------------------ start
    def create_goals(
        self,
        thread_ids: list[str],
        objective: str,
        status: str = "paused",
    ) -> dict:
        """Create a Codex goal on threads that do not have one yet.

        Measured on the local app-server (``0.6.x``): ``thread/goal/set`` with
        an ``objective`` **creates** the goal and defaults ``status`` to
        ``active``; without an objective it only updates an existing goal
        (``no goal exists`` otherwise). So creation is opt-in per thread:

        * threads that already have a goal are skipped — an existing objective
          is never overwritten by a bulk create;
        * ``status="paused"`` (default) creates a goal that will not start
          running until it is activated, which keeps bulk creation cheap;
        * nothing here starts a turn; creating a goal is not running one.
        """
        if not self.enabled():
            raise GoalResumeError(
                "goal_resume_disabled", "目标续跑未启用，不能创建目标。"
            )
        status = str(status or "paused").strip().lower()
        if status not in ("paused", "active"):
            raise GoalResumeError(
                "invalid_status", "status 只能是 paused 或 active"
            )
        objective = (objective or "").strip() or "直到完成任务"
        if len(objective) > 400:
            raise GoalResumeError("objective_too_long", "目标描述最多 400 字")
        ids: list[str] = []
        for value in thread_ids or []:
            tid = str(value or "").strip()
            if tid and tid not in ids:
                ids.append(tid)
        if not ids:
            raise GoalResumeError("thread_required", "至少选择一个会话")
        if len(ids) > 50:
            raise GoalResumeError("too_many_threads", "一次最多创建 50 个目标")
        with self._lock:
            if self.running():
                raise GoalResumeError(
                    "job_running", "续跑任务正在运行，等它结束后再创建目标"
                )

        created: list[dict] = []
        skipped: list[dict] = []
        errors: list[dict] = []
        session = None
        try:
            session = self._session_factory()
            session.initialize()
            for tid in ids:
                try:
                    existing = (
                        session.request(
                            "thread/goal/get", {"threadId": tid}, timeout=30
                        )
                        or {}
                    ).get("goal")
                    if existing:
                        skipped.append(
                            {
                                "thread_id": tid,
                                "reason": "already_has_goal",
                                "goal_status": existing.get("status"),
                            }
                        )
                        continue
                    response = session.request(
                        "thread/goal/set",
                        {
                            "threadId": tid,
                            "objective": objective,
                            "status": status,
                        },
                        timeout=30,
                    )
                    goal = (response or {}).get("goal") or {}
                    created.append(
                        {
                            "thread_id": tid,
                            "goal_status": goal.get("status") or status,
                            "objective": goal.get("objective") or objective,
                        }
                    )
                except AppServerError as exc:
                    errors.append({"thread_id": tid, "error": str(exc)[:300]})
        except AppServerError as exc:
            raise GoalResumeError("appserver_error", str(exc)) from exc
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:  # noqa: BLE001
                    pass
        return {
            "ok": not errors,
            "objective": objective,
            "status": status,
            "created": created,
            "skipped": skipped,
            "errors": errors,
        }

    def start(
        self,
        thread_ids: list[str],
        *,
        dry_run: bool = True,
        confirm: bool = False,
        started_via: str | None = None,
        takeover_desktop: bool | None = None,
        restart_desktop: bool = False,
        continue_once: bool = False,
        continuation_modes: dict | None = None,
        skip_running: bool = False,
    ) -> dict:
        if not self.enabled():
            raise GoalResumeError(
                "goal_resume_disabled",
                "目标续跑默认禁用。设置 CODEX_SWITCHER_ENABLE_GOAL_RESUME=1 并重启服务。",
            )
        ids: list[str] = []
        for value in thread_ids or []:
            if not isinstance(value, str):
                continue
            tid = value.strip()
            if tid and tid not in ids:
                ids.append(tid)
        if not ids:
            raise GoalResumeError("thread_required", "至少选择一个目标会话")
        if len(ids) > 10:
            raise GoalResumeError("too_many_threads", "一次最多续跑 10 个目标")
        if not dry_run and confirm is not True:
            raise GoalResumeError(
                "confirmation_required", "真实续跑需要 confirm=true"
            )
        if takeover_desktop is not None and not isinstance(takeover_desktop, bool):
            raise GoalResumeError("invalid_options", "takeover_desktop 必须是布尔值或 null")
        if not isinstance(restart_desktop, bool):
            raise GoalResumeError("invalid_options", "restart_desktop 必须是布尔值")
        if not isinstance(continue_once, bool):
            raise GoalResumeError("invalid_options", "continue_once 必须是布尔值")
        original_ids = set(ids)
        skipped_running = []
        if skip_running and not dry_run:
            states = self._execution_provider(ids)
            skipped_running = [tid for tid in ids if (states.get(tid) or {}).get("state") == "running"]
            ids = [tid for tid in ids if tid not in skipped_running]
            if not ids:
                return {"id": None, "state": "already_running", "noop": True,
                        "thread_ids": [], "skipped_running": skipped_running,
                        "message": "会话正在运行，未重复投递"}
        if continuation_modes is not None and (not isinstance(continuation_modes, dict) or
                any(tid not in original_ids or mode not in {"once", "continuous"} for tid, mode in continuation_modes.items())):
            raise GoalResumeError("invalid_options", "会话续跑模式无效")
        modes = {tid: (continuation_modes or {}).get(tid, "once" if continue_once else "continuous") for tid in ids}
        with self._control_lock, self._lock:
            if self._thread and self._thread.is_alive():
                raise GoalResumeError("job_running", "已有续跑任务在执行")
            if not dry_run:
                self._modes.update(modes)
                self._save_modes()
            if not dry_run and not (started_via or "").startswith("switch"):
                for tid in ids:
                    self._pauses.pop(tid, None)
                self._save_pauses()
            job = {
                "id": uuid.uuid4().hex[:12],
                "started_at": _now_iso(),
                "finished_at": None,
                "last_activity_at": _now_iso(),
                "turn_started_at": None,
                "dry_run": bool(dry_run),
                "continue_once": continue_once,
                "continuation_modes": modes,
                "skip_running": skip_running,
                "skipped_running": skipped_running,
                "state": "running",
                "thread_ids": ids,
                "current_thread": None,
                "started_via": started_via,
                "events": [],
                "results": [],
                "error": None,
                "cancel_requested": False,
                "skipped": [],
                "cancel_queue": [],
                #: kept in the persisted shape for older dashboard builds; a
                #: queue dispatch never closes the desktop window.
                "takeover_threads": [],
                "desktop_taken_over": False,
                "desktop_relaunch": None,
                "takeover_desktop": None,
                "restart_desktop": bool(restart_desktop and not dry_run),
                "desktop_restart": None,
            }
            self._job = job
            if takeover_desktop and not dry_run:
                self._log(
                    job,
                    "warn",
                    "takeover_desktop 已不再使用：续跑改为把「继续」排进会话队列，"
                    "由 Codex 桌面端执行，不会关闭窗口",
                )
            # Persist "running" immediately so a service restart in the middle
            # of a job is visible in the history (see _mark_interrupted).
            self._record_history(job)
            self._thread = threading.Thread(
                target=self._run_job,
                args=(job, ids, bool(dry_run), started_via),
                name="goal-resume",
                daemon=True,
            )
            self._thread.start()
            return {
                "id": job["id"],
                "dry_run": job["dry_run"],
                "state": job["state"],
                "thread_ids": list(ids),
                "skipped_running": skipped_running,
                "started_at": job["started_at"],
            }

    def cancel(self) -> bool:
        with self._lock:
            job = self._job
            if not job or job.get("state") != "running":
                return False
            job["cancel_requested"] = True
            session = self._session
        if session is not None:
            session.close()
        return True

    def is_paused(self, thread_id: str) -> bool:
        with self._lock:
            return thread_id in self._pauses

    def _save_pauses(self):
        """Persist explicit pause intent before any cancellation or RPC."""
        try:
            self._pause_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._pause_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._pauses, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._pause_path)
        except OSError as exc:
            raise GoalResumeError("pause_save_failed", "无法保存暂停状态") from exc

    def pause(self, thread_ids: list[str]) -> dict:
        with self._control_lock:
            return self._pause(thread_ids)

    def _pause(self, thread_ids: list[str]) -> dict:
        """Pause future work and the goal; never pretend to interrupt a foreign turn.

        Desktop turns belong to the desktop's app-server. A separate client's
        turn/interrupt cannot control them. Return the required manual stop
        explicitly, while persisting pause intent even when a goal RPC fails.
        """
        ids = list(dict.fromkeys(tid.strip() for tid in thread_ids if isinstance(tid, str) and tid.strip()))
        if not ids:
            raise GoalResumeError("thread_required", "请选择要暂停的会话")
        if len(ids) > 50:
            raise GoalResumeError("too_many_threads", "一次最多暂停 50 个会话")
        states = self._execution_provider(ids)
        with self._lock:
            for tid in ids:
                self._pauses[tid] = {"at": _now_iso(), "needs_manual_stop": states.get(tid, {}).get("state") == "running"}
            self._save_pauses()
        try:
            cancellation = self.cancel_queued(ids)
        except AppServerError as exc:
            cancellation = {"results": [{"thread_id": tid, "state": "failed", "error": str(exc)[:300]} for tid in ids]}
        cancelled = {row["thread_id"]: row for row in cancellation["results"]}
        latest = self._last_resume_by_thread()
        session, results = None, []
        try:
            for tid in ids:
                row = {"thread_id": tid, "goal_paused": False, "queue": cancelled.get(tid)}
                try:
                    if session is None:
                        session = self._session_factory()
                        session.initialize()
                    goal = (session.request("thread/goal/get", {"threadId": tid}, timeout=15) or {}).get("goal")
                    if goal and goal.get("status") not in {"complete", "blocked", "budgetLimited"}:
                        updated = session.request("thread/goal/set", {"threadId": tid, "status": "paused"}, timeout=15)
                        row["goal_paused"] = (updated or {}).get("goal", {}).get("status") == "paused"
                        if not row["goal_paused"]:
                            row["error"] = "goal_pause_unconfirmed"
                except AppServerError as exc:
                    row["error"] = str(exc)[:300]
                with self._lock:
                    self._pauses[tid].update({key: value for key, value in row.items() if key in {"goal_paused", "error"}})
                    if (cancelled.get(tid) or {}).get("state") == "failed":
                        self._pauses[tid]["error"] = "queue_cancel_failed"
                        row["error"] = row.get("error") or "queue_cancel_failed"
                    uncertain = (latest.get(tid, {}).get("execution") or {}).get("state") in {"running", "started", "confirming", "unconfirmed"}
                    removed = (cancelled.get(tid) or {}).get("state") == "already_removed"
                    if uncertain or removed:
                        self._pauses[tid]["needs_manual_stop"] = True
                    self._save_pauses()
                    row["needs_manual_stop"] = self._pauses[tid]["needs_manual_stop"]
                row["state"] = "needs_manual_stop" if row["needs_manual_stop"] else "pause_failed" if row.get("error") else "paused"
                results.append(row)
        finally:
            if session is not None:
                session.close()
        return {"ok": not any(row.get("error") for row in results), "results": results}

    def remove(self, thread_ids: list[str]) -> dict:
        """Cancel, pause and force-interrupt managed chats before hiding them."""
        ids = list(dict.fromkeys(tid.strip() for tid in thread_ids if isinstance(tid, str) and tid.strip()))
        if not ids:
            raise GoalResumeError("thread_required", "请选择要删除的续跑任务")
        if len(ids) > 50:
            raise GoalResumeError("too_many_threads", "一次最多删除 50 个续跑任务")
        with self._control_lock:
            known = {tid for entry in self.history() if not entry.get("dry_run") for tid in entry.get("thread_ids", [])}
            if any(tid not in known for tid in ids):
                raise GoalResumeError("not_managed", "只能删除 Switcher 的续跑记录")
            paused = self._pause(ids)
            latest = self._last_resume_by_thread()
            results = []
            for row in paused["results"]:
                tid = row["thread_id"]
                outcome = {**row, "state": "failed", "removed": False, "stopped": False}
                try:
                    state = (self._execution_provider([tid]) or {}).get(tid) or {}
                    if state.get("state") == "running" and state.get("turn_id"):
                        outcome.update(self._desktop_control.interrupt(tid, state["turn_id"]))
                        outcome["needs_manual_stop"] = False
                    elif state.get("state") in {"ended", "interrupted"}:
                        outcome.update(stopped=True, needs_manual_stop=False, evidence="rollout")
                    elif row.get("needs_manual_stop"):
                        raise DesktopIpcError("无法确认当前回合，未移除记录；请刷新后重试")
                    queue_state = (row.get("queue") or {}).get("state")
                    if queue_state == "failed":
                        raise DesktopIpcError("排队消息未撤回，未移除记录")
                    archived_missing = state.get("archived") and "thread not found" in str(row.get("error") or "").lower()
                    if row.get("error") and not archived_missing:
                        raise DesktopIpcError(row["error"])
                    previous = latest.get(tid) or {}
                    if previous.get("pending") and not previous.get("owned_queue"):
                        raise DesktopIpcError("仍有不属于 Switcher 的排队消息，请在 Codex 中核对")
                    if self._pending_by_thread().get(tid):
                        raise DesktopIpcError("排队消息仍在取消中，请稍后重试删除")
                    if queue_state == "cancelling" and self.running():
                        raise DesktopIpcError("投递仍在取消中，请稍后重试删除")
                    job_id = next((entry["id"] for entry in self.history() if not entry.get("dry_run") and tid in entry.get("thread_ids", [])), None)
                    with self._lock:
                        self._pauses[tid].pop("error", None)
                        self._pauses[tid].update(removed_job_id=job_id, needs_manual_stop=False,
                                                 stopped_turn_id=outcome.get("turn_id"), stop_evidence=outcome.get("evidence"))
                        self._save_pauses()
                    outcome.update(removed=True, state="removed")
                    outcome.pop("error", None)
                except DesktopIpcError as exc:
                    outcome["error"] = str(exc)[:300]
                    with self._lock:
                        self._pauses[tid]["error"] = outcome["error"]
                        self._save_pauses()
                results.append(outcome)
            return {"ok": all(row["removed"] for row in results), "results": results}

    def _delete_queued(self, session, thread_id: str, queued_id: str) -> bool:
        response = session.request("thread/queue/delete", {
            "threadId": thread_id, "queuedSubmissionId": queued_id,
        }, timeout=15)
        deleted = bool((response or {}).get("deleted"))
        if deleted and self._queue_watch is not None:
            self._queue_watch.cancel(queued_id)
        if deleted:
            self._receipt_tracker.cancel(queued_id)
        return deleted

    def cancel_queued(self, thread_ids: list[str]) -> dict:
        """Withdraw only messages this service created, including in-flight adds.

        Queue deletion can lose a race with the desktop. A false ``deleted``
        never means a running turn was stopped.
        """
        targets = list(dict.fromkeys(tid.strip() for tid in thread_ids if isinstance(tid, str) and tid.strip()))
        if not targets:
            raise GoalResumeError("thread_required", "请选择要取消排队的会话")
        if len(targets) > 50:
            raise GoalResumeError("too_many_threads", "一次最多处理 50 个会话")
        waiting = []
        with self._lock:
            job = self._job
            if job and job.get("state") == "running":
                finished = {item["thread_id"] for item in job.get("results", [])}
                for tid in targets:
                    if tid in job.get("thread_ids", []) and tid not in finished:
                        if tid not in job["skipped"]:
                            job["skipped"].append(tid)
                        if tid not in job["cancel_queue"]:
                            job["cancel_queue"].append(tid)
                        waiting.append(tid)
        latest = self._pending_by_thread()
        results = []
        session = None
        try:
            for tid in targets:
                entry = latest.get(tid) or {}
                if not entry.get("owned_queue") or not entry.get("queued_id"):
                    results.append({"thread_id": tid, "state": "cancelling" if tid in waiting else "no_owned_message"})
                    continue
                if session is None:
                    session = self._session_factory()
                    session.initialize()
                try:
                    deleted = self._delete_queued(session, tid, entry["queued_id"])
                except AppServerError as exc:
                    results.append({"thread_id": tid, "state": "failed", "error": str(exc)})
                    continue
                if deleted:
                    self._update_history_result(tid, entry["queued_id"], {
                        "cancelled": True, "error": "cancelled", "pending": False, "delivered": False,
                    })
                else:
                    self._update_history_result(tid, entry["queued_id"], {
                        "pending": False, "delivered": True, "delivered_at": _now_iso(),
                    })
                results.append({"thread_id": tid, "state": "cancelled" if deleted else "already_removed"})
        finally:
            if session is not None:
                session.close()
        return {"ok": True, "results": results}

    #: How long a switch waits for the previous job's app-server to die before
    #: giving up on auto-resume (closing the session is normally immediate).
    STALE_JOB_WAIT_SECONDS = 20.0

    def _cancel_stale_job(self) -> bool:
        """Cancel the job still running from the previous account.

        Its app-server was spawned before ``auth.json`` was replaced, so it
        keeps using the old token: the turns burn the old account's quota and
        its writer locks block both the re-launched desktop app and our next
        resume. Cancelling closes the app-server, which releases them.
        """
        with self._lock:
            job = self._job
        if job is not None:
            self._log(job, "warn", "切号前取消旧账号下的续跑任务（释放线程写锁）")
        self.cancel()
        # A terminal result may already be published while the worker is
        # closing its app-server or restoring the desktop. Wait for that
        # cleanup as well, before starting a job for the new account.
        deadline = time.monotonic() + self.STALE_JOB_WAIT_SECONDS
        while time.monotonic() < deadline:
            if not self.running():
                return True
            time.sleep(0.05)
        return not self.running()

    def stop(self, thread_ids: list[str] | None = None) -> dict:
        """Stop part (or all) of the running job.

        One job runs threads serially, so "pause this one" has two honest
        outcomes: threads that are still queued are skipped, and if the caller
        targets the thread that is executing right now the only way to stop it
        is to cancel the job (closing the app-server aborts the turn).
        """
        targets = [
            value.strip()
            for value in (thread_ids or [])
            if isinstance(value, str) and value.strip()
        ]
        if not targets:
            return {"ok": True, "cancelled": self.cancel(), "skipped": [], "current": None}
        with self._lock:
            job = self._job
            if not job or job.get("state") != "running":
                return {
                    "ok": False,
                    "error": "job_not_running",
                    "cancelled": False,
                    "skipped": [],
                    "current": None,
                }
            wanted = set(targets)
            finished = {item["thread_id"] for item in job.get("results", [])}
            skipped: list[str] = []
            for tid in job.get("thread_ids") or []:
                if tid in wanted and tid not in finished and tid not in job["skipped"]:
                    job["skipped"].append(tid)
                    skipped.append(tid)
            current = job.get("current_thread")
            if current in finished:
                current = None
        if skipped:
            short = "、".join(sorted(tid[:8] for tid in skipped))
            self._log(job, "warn", f"已跳过：{short}")
        cancelled = False
        if current in wanted:
            cancelled = self.cancel()
        return {
            "ok": True,
            "cancelled": cancelled,
            "skipped": skipped,
            "current": current,
        }

    # --------------------------------------------------------------- internal
    def _log(self, job: dict, level: str, message: str) -> None:
        with self._lock:
            job["events"].append(
                {"ts": _now_iso(), "level": level, "message": message}
            )
            if len(job["events"]) > 300:
                del job["events"][:150]
        line = f"[goal-resume {job['id']}] {message}"
        print(line, flush=True)

    def _run_job(
        self, job: dict, ids: list[str], dry_run: bool, started_via: str | None = None
    ) -> None:
        session = None
        failed = False
        final_state = "failed"
        try:
            with self._lock:
                if job.get("cancel_requested"):
                    raise GoalResumeError("cancelled", "任务已取消")
            if not dry_run and job.get("restart_desktop"):
                try:
                    restart = self._desktop.restart(force=True)
                except Exception as exc:
                    restart = {"ok": False, "error": str(exc)}
                with self._lock:
                    job["desktop_restart"] = restart
                if not restart or not restart.get("ok"):
                    self._log(job, "error", "桌面端重启失败，未投递排队消息")
                    raise GoalResumeError("desktop_restart_failed", "desktop_restart_failed")
                self._log(job, "info", "桌面端已重启，准备续跑")
            with self._lock:
                if job.get("cancel_requested"):
                    raise GoalResumeError("cancelled", "任务已取消")
            session = self._session_factory()
            with self._lock:
                self._session = session
            self._check_cancelled(job)
            session.initialize()
            self._log(
                job,
                "info",
                f"app-server ready (pid {session.pid}); "
                + ("dry-run，仅读取状态" if dry_run else "开始续跑"),
            )
            for tid in ids:
                with self._lock:
                    if job.get("cancel_requested"):
                        raise GoalResumeError("cancelled", "任务已取消")
                    if tid in job.get("skipped", []):
                        skip = True
                    else:
                        skip = False
                        job["current_thread"] = tid
                if skip:
                    self._log(job, "warn", f"{tid[:8]} 已按用户选择跳过")
                    with self._lock:
                        job["results"].append(
                            {
                                "thread_id": tid,
                                "ok": False,
                                "skipped_by_user": True,
                                "status_before": None,
                                "status_after": None,
                                "turns": 0,
                                "error": "skipped_by_user",
                            }
                        )
                    continue
                self._log(job, "info", f"处理 {tid}")
                result = self._resume_one(session, job, tid, dry_run, started_via)
                if tid in job.get("cancel_queue", []) and result.get("owned_queue") and result.get("queued_id"):
                    try:
                        deleted = self._delete_queued(session, tid, result["queued_id"])
                        if deleted:
                            result.update(cancelled=True, error="cancelled", pending=False, delivered=False)
                        else:
                            result.update(pending=False, delivered=True, delivered_at=_now_iso())
                    except AppServerError as exc:
                        result.update(pending=True, error="cancel_queue_failed")
                        self._log(job, "error", f"{tid[:8]} 撤回排队失败：{exc}")
                with self._lock:
                    job["results"].append(result)
                    job["current_thread"] = None
                    job["turn_started_at"] = None
                    job["current_turn_id"] = None
                # Completed dispatches stay cancellable while later chats are
                # still waiting; ownership must not wait for the whole batch.
                self._record_history(job)
                receipt = result.get("execution") or {}
                if result.get("model") and receipt.get("state") in {"completed", "failed"}:
                    self._apply_execution_receipt(tid, result.get("queued_id"), receipt)
                if result.get("ok"):
                    if result.get("cancelled"):
                        outcome = "已撤回排队消息"
                    elif result.get("pending"):
                        outcome = "已排队，等桌面端取走"
                    elif result.get("delivered"):
                        receipt = result.get("execution") or {}
                        outcome = {
                            "running": "已确认本次续跑正在执行",
                            "started": "本次续跑已开始，正在核对运行状态",
                            "completed": "本次续跑已结束",
                            "failed": "本次续跑执行失败",
                            "interrupted": "本次续跑已中断",
                            "unconfirmed": "未找到本次续跑回合，请检查会话",
                        }.get(receipt.get("state"), "消息已出队，正在核对本次回合")
                    elif dry_run:
                        outcome = f"可继续（{result.get('status_after')}）"
                    else:
                        outcome = "已投递"
                    self._log(job, "info", f"{tid[:8]} {outcome}")
                else:
                    self._log(
                        job,
                        "warn",
                        f"{tid[:8]} 未续跑：{result.get('error') or '未知原因'}",
                    )
            with self._lock:
                cancelled = bool(job.get("cancel_requested"))
                results = list(job["results"])
                errors = [
                    item for item in results
                    if not item.get("ok")
                    and not item.get("skipped_by_user")
                    and not item.get("cancelled")
                ]
                succeeded = sum(bool(item.get("ok")) for item in results)
                if cancelled:
                    final_state = "cancelled"
                elif errors:
                    final_state = "partial" if succeeded else "failed"
                    job["error"] = f"{len(errors)} 个目标未成功投递"
                else:
                    final_state = "done"
        except GoalResumeError as exc:
            if exc.code == "cancelled":
                final_state = "cancelled"
                self._log(job, "warn", "任务已取消；目标可能仍处于 active")
            else:
                failed = True
                final_state = "failed"
                job["error"] = exc.message
                self._log(job, "error", exc.message)
        except AppServerError as exc:
            cancelled = bool(job.get("cancel_requested"))
            final_state = "cancelled" if cancelled else "failed"
            if not cancelled:
                failed = True
                job["error"] = str(exc)
                self._log(job, "error", f"app-server: {exc}")
        except Exception as exc:  # noqa: BLE001 - job must always finish
            failed = True
            final_state = "failed"
            job["error"] = str(exc)
            self._log(job, "error", f"内部错误: {exc}")
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:  # noqa: BLE001
                    pass
            with self._lock:
                self._session = None
                job["state"] = "cancelled" if job.get("cancel_requested") else final_state
                job["finished_at"] = _now_iso()
                job["current_thread"] = None
                job["turn_started_at"] = None
                job["current_turn_id"] = None
                if job.get("cancel_requested"):
                    finished = {item["thread_id"] for item in job["results"]}
                    for tid in ids:
                        if tid not in finished:
                            job["results"].append({
                                "thread_id": tid,
                                "ok": False,
                                "cancelled": True,
                                "error": "cancelled",
                                "status_before": None,
                                "status_after": None,
                                "turns": 0,
                                "started_via": started_via,
                            })
            self._record_history(job)
            if failed:
                print(f"[goal-resume {job['id']}] failed: {job.get('error')}", flush=True)

    def _check_cancelled(self, job: dict) -> None:
        with self._lock:
            if job.get("cancel_requested"):
                raise GoalResumeError("cancelled", "任务已取消")

    def _resume_one(
        self,
        session: AppServerSession,
        job: dict,
        thread_id: str,
        dry_run: bool,
        started_via: str | None = None,
    ) -> dict:
        """Dispatch one thread: arm the goal, then queue the resume message."""
        result = {
            "thread_id": thread_id,
            "ok": False,
            "status_before": None,
            "status_after": None,
            "turns": 0,
            "started_via": started_via,
            "error": None,
            "queued_id": None,
            "delivered": None,
            "goal_created": False,
            "continue_once": (job.get("continuation_modes") or {}).get(thread_id, "once" if job.get("continue_once") else "continuous") == "once",
        }
        once = result["continue_once"]
        try:
            response = session.request(
                "thread/goal/get", {"threadId": thread_id}, timeout=30
            )
        except AppServerError as exc:
            if job.get("cancel_requested"):
                result.update(cancelled=True, error="cancelled")
                return result
            # A thread that was deleted (or belongs to another profile) must not
            # abort the whole job — the other marked goals still deserve a wake.
            result["error"] = str(exc).strip() or "thread/goal/get failed"
            if "not found" in result["error"].lower():
                result["missing"] = True
                self._log(
                    job,
                    "warn",
                    f"{thread_id[:8]} 线程不存在，跳过（可以取消它的「自动」标记）",
                )
            return result
        self._check_cancelled(job)
        if thread_id in job.get("cancel_queue", []):
            result.update(cancelled=True, error="cancelled")
            return result
        if thread_archived(config.state_db_path(), thread_id):
            # Archived conversations are never resumed by the app: queueing a
            # 「继续」 would only leave a row nobody drains, so don't pretend the
            # goal was resumed either.
            result["ok"] = True
            result["pending"] = False
            result["queue_abandoned"] = True
            self._log(
                job,
                "warn",
                f"{thread_id[:8]} 会话已归档，跳过续跑（归档会话不会执行消息）",
            )
            return result
        goal = (response or {}).get("goal") or {}
        status = goal.get("status")
        result["status_before"] = status
        result["status_after"] = status
        if self.is_paused(thread_id):
            result.update(skipped_by_user=True, error="paused_by_user")
            return result
        if (once or job.get("skip_running")) and not dry_run and not started_via and (self._execution_provider([thread_id]).get(thread_id) or {}).get("state") == "running":
            result.update(skipped_by_user=True, already_running=True, error=None, message="会话正在运行，未重复投递")
            return result
        if (started_via or "").startswith("switch") and (status in {"complete", "blocked", "budgetLimited"} or (status == "paused" and not once)):
            result.update(skipped_by_user=True, error="state_changed_before_dispatch")
            return result
        # Every chat receives a goal before its first continuation. Legacy
        # continue-once requests still preserve goals that already exist.
        create_goal = not goal
        activate_goal = not once and status in ARMABLE_STATUSES and status != "active"
        pause_goal = once and status in {"active", "usageLimited"}
        desired_status = "paused" if once else "active"
        # An ``active`` goal still needs a nudge when nobody is driving it: that
        # is the normal state after a switch restarted the desktop app. We keep
        # the status as-is (no revert) and only queue the message.
        already_active = status == "active"
        if self.model_guard and not dry_run:
            from .model_access import thread_models
            model = thread_models([thread_id]).get(thread_id)
            if model:
                access = self.model_guard.current_check([model])
                result.update(model=model, account_signature=access.get("account_signature"), model_access=access)
                if not access.get("ok"):
                    result.update(error=access.get("state"), message=access.get("message"))
                    self._log(job, "warn", f"{thread_id[:8]} {access.get('message')}")
                    return result
        if dry_run:
            result["ok"] = True
            planned = []
            if create_goal:
                planned.append(f"thread/goal/set objective=本会话尚未完成的用户任务 status={desired_status} origin=automatic")
                result["goal_objective"] = RESUME_GOAL_OBJECTIVE
            elif activate_goal:
                planned.append("thread/goal/set status=active origin=automatic")
            elif pause_goal:
                planned.append("thread/goal/set status=paused origin=automatic（单次结束不推进下一轮）")
            planned.append(f'thread/queue/add "{RESUME_MESSAGE}"（交给桌面端执行）')
            planned.append(
                "codex://threads/wake/<session>（让已在运行的桌面端加载该会话）"
            )
            result["planned"] = planned
            result["status_after"] = (
                f"{desired_status} (would create)" if create_goal else "active (would set)" if activate_goal else "paused (would set)" if pause_goal else status
            )
            self._log(
                job,
                "info",
                f"{thread_id[:8]} dry-run：{status}，可继续",
            )
            return result

        activated = False
        try:
            self._check_cancelled(job)
            if create_goal:
                try:
                    created = session.request(
                        "thread/goal/set",
                        {"threadId": thread_id, "objective": RESUME_GOAL_OBJECTIVE,
                         "status": desired_status, "origin": "automatic"},
                        timeout=30,
                    )
                    new_goal = (created or {}).get("goal") or {}
                    prepared = new_goal.get("status") == desired_status
                    activated = prepared and not once
                    if prepared:
                        result["status_after"] = desired_status
                    if not prepared or not new_goal.get("objective"):
                        raise AppServerError("创建长期目标未确认成功")
                    result.update(goal_created=True, goal_objective=new_goal["objective"])
                    self._log(job, "info", f"{thread_id[:8]} 已创建{'暂停' if once else '激活'}目标，再投递续跑消息")
                except AppServerError as exc:
                    # Goal preparation is best effort: the user can still
                    # continue this chat through a normal queued message.
                    result["goal_creation_error"] = str(exc)[:300]
                    self._log(job, "warn", f"{thread_id[:8]} 补建长期目标失败，仍将发送「继续」：{exc}")
                    if once:
                        # The create may have succeeded before its reply failed.
                        # Verify/stop any active goal before promising one turn.
                        actual = (session.request("thread/goal/get", {"threadId": thread_id}, timeout=15) or {}).get("goal") or {}
                        if actual.get("status") == "active":
                            stopped = session.request("thread/goal/set", {"threadId": thread_id, "status": "paused", "origin": "automatic"}, timeout=15)
                            if (stopped or {}).get("goal", {}).get("status") != "paused":
                                raise AppServerError("未能暂停长期目标，无法保证只续跑一轮")
            elif pause_goal:
                stopped = session.request("thread/goal/set", {"threadId": thread_id, "status": "paused", "origin": "automatic"}, timeout=30)
                if (stopped or {}).get("goal", {}).get("status") != "paused":
                    raise AppServerError("未能暂停长期目标，无法保证只续跑一轮")
                result["status_after"] = "paused"
                self._log(job, "info", f"{thread_id[:8]} 已暂停目标自动推进，仅投递当前一轮")
            elif already_active:
                self._log(job, "info", f"{thread_id[:8]} 目标已是 active")
            elif activate_goal:
                # ``origin=user`` also records a transcript instruction and
                # needs the writer lock. This derived recovery uses automatic.
                session.request(
                    "thread/goal/set",
                    {
                        "threadId": thread_id,
                        "status": "active",
                        "origin": "automatic",
                    },
                    timeout=30,
                )
                activated = True
                result["status_after"] = "active"
                self._log(job, "info", f"{thread_id[:8]} 目标已置为 active")

            self._check_cancelled(job)
            if self.is_paused(thread_id):
                # Pause may race with goal activation; restore its intent.
                if activated:
                    session.request("thread/goal/set", {"threadId": thread_id, "status": "paused"}, timeout=15)
                    result["status_after"] = "paused"
                result.update(skipped_by_user=True, error="paused_by_user")
                return result
            client_id = str(uuid.uuid4())
            receipt_cursor = rollout_cursor(thread_id)
            result["queued_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            # A previous attempt (or the watchdog) may still have a 「继续」
            # queued for this thread — e.g. the switch fired twice while the
            # desktop app was restarting. Queuing a second one would run two
            # turns (and burn quota), so reuse the row that is already there.
            existing = first_resume_row(
                config.queue_db_path(), thread_id, message=RESUME_MESSAGE
            )
            if existing is not None:
                queued = {
                    "id": existing.get("id"),
                    "clientUserMessageId": existing.get("client_message_id"),
                }
                previous = self._pending_by_thread().get(thread_id) or {}
                result["owned_queue"] = bool(previous.get("owned_queue") and previous.get("queued_id") == queued["id"])
                if previous.get("queued_at"):
                    result["queued_at"] = previous["queued_at"]
                self._log(
                    job,
                    "info",
                    f"{thread_id[:8]} 队列里已有「{RESUME_MESSAGE}」，不再重复排队",
                )
            else:
                queued = self._queue_resume(session, thread_id, client_id)
                result["owned_queue"] = True
                self._log(
                    job,
                    "info",
                    f"{thread_id[:8]} 已把「{RESUME_MESSAGE}」放进该会话的队列",
                )
            queued_id = queued.get("id") or client_id
            result["queued_id"] = queued_id
            result["client_message_id"] = (
                queued.get("clientUserMessageId") or client_id
            )
            self._receipt_tracker.track(thread_id, result, requested_at=result["queued_at"], cursor=receipt_cursor)

            wake = self._wake_desktop(job, thread_id)
            result["wake"] = wake
            delivery: dict = {}
            result["delivered"] = self._wait_queued_delivery(
                session, job, thread_id, queued_id, report=delivery
            )
            if result["delivered"]:
                result["delivered_at"] = _now_iso()
                self._log(
                    job,
                    "info",
                    f"{thread_id[:8]} 消息已出队，继续核对对应的新回合记录",
                )
            else:
                result["pending"] = True
                if wake.get("skipped"):
                    hint = (
                        "（桌面端进程卡在启动状态，没能取走队列）"
                        if wake.get("reason") == "desktop_not_ready"
                        else "（桌面端没有运行，消息保留在队列中）"
                    )
                else:
                    hint = "（等待桌面端加载并处理；后台唤醒可能尚未完成）"
                self._log(
                    job,
                    "warn",
                    f"{thread_id[:8]} 已排队，桌面端暂未取走" + hint,
                )
                # The job is about to end; the watchdog keeps the row owned so
                # a wake lost during an app restart is retried. A removed row
                # still does not prove that a turn started.
                self._watch_queued(
                    job,
                    result,
                    thread_id,
                    queued_id,
                    wake,
                    observed=bool(delivery.get("observed")),
                )

            try:
                current = session.request(
                    "thread/goal/get", {"threadId": thread_id}, timeout=30
                )
            except AppServerError:
                current = None
            final_status = ((current or {}).get("goal") or {}).get("status")
            if final_status:
                result["status_after"] = final_status
            result["ok"] = True
            self._receipt_tracker.track(thread_id, result, requested_at=result["queued_at"], cursor=receipt_cursor)
            result["execution"] = self._receipt_tracker.for_queue(queued_id)
            if (result.get("execution") or {}).get("turn_id"):
                result["turns"] = 1
            return result
        except GoalResumeError as exc:
            if exc.code != "cancelled":
                raise
            result.update(cancelled=True, error="cancelled")
            if activated and not result.get("queued_id"):
                rollback_status = status or "paused"
                if self._revert(session, job, thread_id, rollback_status):
                    result["status_after"] = rollback_status
                else:
                    result["rollback_failed"] = True
            return result
        except AppServerError as exc:
            if job.get("cancel_requested"):
                result.update(cancelled=True, error="cancelled")
                return result
            result["error"] = str(exc)
            if "spawned sub-agent" in result["error"]:
                result["subagent"] = True
                self._log(
                    job,
                    "warn",
                    f"{thread_id[:8]} 是子代理线程，不能直接投喂消息",
                )
            # If activation succeeded but the message never made it into the
            # queue, do not leave the goal falsely active.
            if activated and not result.get("queued_id"):
                rollback_status = status or "paused"
                if self._revert(session, job, thread_id, rollback_status):
                    result["status_after"] = rollback_status
                else:
                    result["rollback_failed"] = True
            return result

    def _queue_resume(
        self, session: AppServerSession, thread_id: str, client_id: str
    ) -> dict:
        """Put the resume nudge into the thread queue.

        ``thread/queue/add`` only writes a row (the ``queued_thread_revisions``
        trigger publishes it); it never needs the thread writer lock. The
        desktop app's own app-server notices the revision and runs the turn.
        """
        response = session.request(
            "thread/queue/add",
            {
                "threadId": thread_id,
                "input": [
                    {
                        "type": "text",
                        "text": RESUME_MESSAGE,
                        "text_elements": [],
                    }
                ],
                "clientUserMessageId": client_id,
            },
            timeout=60,
        )
        queued = (response or {}).get("queuedSubmission")
        return queued if isinstance(queued, dict) else {}

    def _wake_desktop(self, job: dict, thread_id: str) -> dict:
        """Request background loading of a thread with pending messages.

        Writing the row is not enough: the desktop only takes queued messages for
        threads it has loaded, so a resume used to stall until the user clicked
        the conversation. ``codex://threads/wake/<thread>`` requests that load: the
        app's main process lists the queue itself and resumes the thread when
        something is pending. Sending the URI does not confirm that the desktop
        loaded the conversation or executed the queued message. No process is
        started, closed, or restarted.

        Best effort: a failed wake leaves the message queued for later handling
        and never fails the dispatch.
        """
        if not config.goal_resume_wake_enabled():
            self._log(
                job, "info", f"{thread_id[:8]} 未唤醒桌面端（设置里已关闭自动唤醒）"
            )
            return {"ok": False, "skipped": True, "reason": "wake_disabled"}
        wake_fn = getattr(self._desktop, "wake_thread", None)
        if wake_fn is None:  # a client double without the deep-link helper
            return {"ok": False, "skipped": True, "reason": "unsupported"}
        try:
            wake = wake_fn(thread_id)
        except Exception as exc:  # noqa: BLE001 - dispatch must not fail on this
            self._log(job, "warn", f"{thread_id[:8]} 唤醒桌面端失败: {exc}")
            return {"ok": False, "skipped": False, "error": str(exc)[:200]}
        if not isinstance(wake, dict):
            wake = {"ok": bool(wake)}
        if wake.get("skipped"):
            if wake.get("reason") == "desktop_not_ready":
                self._log(
                    job,
                    "warn",
                    f"{thread_id[:8]} 桌面端进程在，但一直没启动完成，无法唤醒"
                    "（消息留在队列里）",
                )
            else:
                self._log(
                    job,
                    "warn",
                    f"{thread_id[:8]} 桌面端没在运行，无法唤醒（消息留在队列里）",
                )
        elif wake.get("ok"):
            self._log(
                job,
                "info",
                f"{thread_id[:8]} 已发送后台唤醒请求，加载和执行尚待确认",
            )
        return wake

    def _desktop_probe(self) -> dict:
        """``{"running", "pids"}`` for the watchdog's relaunch detection."""
        probe = getattr(self._desktop, "process_snapshot", None)
        if probe is None:
            return {"running": False, "pids": []}
        try:
            state = probe() or {}
        except Exception:  # noqa: BLE001 - the probe is best effort
            return {"running": False, "pids": []}
        if not isinstance(state, dict):
            return {"running": False, "pids": []}
        return {
            "running": bool(state.get("running")),
            "pids": list(state.get("pids") or []),
        }

    def _wake_for_watch(self, thread_id: str) -> dict:
        """Wake callback for the watchdog (there is no job to log against)."""
        wake_fn = getattr(self._desktop, "wake_thread", None)
        if wake_fn is None:
            return {"ok": False, "skipped": True, "reason": "unsupported"}
        try:
            result = wake_fn(thread_id)
        except Exception as exc:  # noqa: BLE001 - retried on the next tick
            return {"ok": False, "error": str(exc)[:200]}
        return result if isinstance(result, dict) else {"ok": bool(result)}

    def _watch_queued(
        self,
        job: dict,
        result: dict,
        thread_id: str,
        queued_id: str,
        wake: dict | None,
        *,
        observed: bool = False,
    ) -> None:
        """Keep ownership of a queued row after this job ends.

        The dispatch cannot wait for delivery: the desktop app may need minutes
        (another thread is mid-turn) and a wake sent while it restarts for a
        switch is dropped. The compatible delivered field only records that
        the queue row disappeared; editing and deletion can also cause this,
        so it must not be presented as confirmed execution.
        """
        watchdog = self._queue_watch
        if watchdog is None or not thread_id or not queued_id:
            return
        try:
            entry = watchdog.track(
                thread_id,
                queued_id,
                job_id=job.get("id"),
                wake=wake,
                observed=observed,
            )
        except Exception as exc:  # noqa: BLE001 - the dispatch already succeeded
            self._log(job, "warn", f"{thread_id[:8]} 队列看护启动失败: {exc}")
            return
        if entry.get("delivered") and not entry.get("pending"):
            result["delivered"] = True
            result["pending"] = False
            result["delivered_at"] = entry.get("delivered_at")
            self._log(job, "info", f"{thread_id[:8]} 已出队，执行待确认（看护确认队列行已消失）")
            return
        if entry.get("error") == "thread_archived":
            result["pending"] = False
            result["queue_abandoned"] = True
            self._log(
                job,
                "warn",
                f"{thread_id[:8]} 会话已归档，排队消息不会被执行",
            )
            return
        watch = watchdog.for_thread(thread_id) or {}
        delay = watch.get("backoff_remaining") or 0
        self._log(
            job,
            "info",
            f"{thread_id[:8]} 队列看护已接管：队列行消失后标记「已出队 · 执行待确认」，"
            + (f"{delay} 秒后重试唤醒" if delay else "稍后重试唤醒"),
        )

    def _apply_queue_entry(self, entry: dict) -> None:
        """Reflect a watchdog transition in the persisted job history."""
        thread_id = entry.get("thread_id")
        queued_id = entry.get("queued_id")
        if entry.get("cancelled") or entry.get("error") == "cancelled":
            fields = {"cancelled": True, "error": "cancelled", "pending": False, "delivered": False}
        elif entry.get("delivered"):
            fields = {
                "delivered": True,
                "delivered_at": entry.get("delivered_at"),
                "pending": False,
                "queue_expired": False,
            }
        elif entry.get("error") == "queue_ttl_expired":
            fields = {
                "pending": False,
                "queue_expired": True,
                "error": "queue_ttl_expired",
            }
        elif entry.get("error") == "thread_archived":
            # The row is still in the queue but the conversation is archived, so
            # the desktop app will never resume it. Say that instead of leaving
            # 「已排队 · 等桌面端取走」 up forever — and never claim delivery.
            fields = {
                "pending": False,
                "queue_abandoned": True,
                "error": "thread_archived",
            }
        else:
            return
        self._update_history_result(
            thread_id if isinstance(thread_id, str) else None,
            queued_id if isinstance(queued_id, str) else None,
            fields,
        )
        tracker = getattr(self, "_receipt_tracker", None)
        if tracker:
            if fields.get("cancelled"):
                tracker.cancel(queued_id)
            elif fields.get("delivered"):
                result = self._last_resume_by_thread().get(thread_id) or {}
                if result.get("queued_id") == queued_id:
                    tracker.track(thread_id, result, requested_at=result.get("queued_at") or result.get("requested_at"))

    def _apply_execution_receipt(self, thread_id, queued_id, receipt) -> None:
        if self.model_guard and (receipt.get("state") == "completed" or receipt.get("error_code") == "model_access_denied"):
            with self._lock:
                source = next((result for entry in reversed(self._history)
                    for result in entry.get("results", []) if result.get("queued_id") == queued_id), {})
            if source.get("account_signature") and source.get("model"):
                self.model_guard.observe_signature(source["account_signature"], source["model"], receipt.get("state") == "completed")
        fields = {"execution": receipt}
        if receipt.get("turn_id"):
            fields.update(turns=1)
        self._update_history_result(thread_id, queued_id, fields)

    def _update_history_result(
        self, thread_id: str | None, queued_id: str | None, fields: dict
    ) -> None:
        """Patch one result in the persisted history (and the live job)."""
        snapshot = None
        with self._lock:
            job = self._job
            if job is not None:
                for result in job.get("results") or []:
                    if queued_id:
                        if result.get("queued_id") != queued_id:
                            continue
                    elif result.get("thread_id") != thread_id:
                        continue
                    result.update(fields)
            for entry in reversed(self._history):
                matched = False
                for result in entry.get("results") or []:
                    if queued_id:
                        if result.get("queued_id") != queued_id:
                            continue
                    elif result.get("thread_id") != thread_id:
                        continue
                    result.update(fields)
                    matched = True
                if matched:
                    snapshot = list(self._history)
                    break
        if snapshot is not None:
            self._write_history(snapshot)

    def _wait_queued_delivery(
        self,
        session: AppServerSession,
        job: dict,
        thread_id: str,
        queued_id: str,
        report: dict | None = None,
    ) -> bool:
        """Whether the queued row disappeared before the wait expired.

        Best effort only: an app-server that predates ``thread/queue/list``
        returns False, leaving the row queued for later handling.
        ``report["observed"]`` records that the row really was listed here.
        Absence confirms removal only: the desktop can also edit or delete a
        queued row without executing it. The return value does not prove a turn.
        """
        deadline = self._clock() + float(
            config.goal_resume_start_timeout_seconds()
        )
        while True:
            # A cancelled job must not pretend the message vanished: the row is
            # already in the queue and the desktop app may still run it. Report
            # it as pending instead of raising.
            if job.get("cancel_requested") or thread_id in job.get("cancel_queue", []):
                return False
            try:
                listing = session.request(
                    "thread/queue/list",
                    {"threadId": thread_id, "limit": 50},
                    timeout=20,
                ) or {}
            except AppServerError:
                return False
            items = listing.get("data") or []
            if not any((item or {}).get("id") == queued_id for item in items):
                return True
            if report is not None:
                report["observed"] = True
            remaining = deadline - self._clock()
            if remaining <= 0:
                return False
            self._sleep(min(QUEUE_POLL_SECONDS, remaining))

    def _revert(
        self, session: AppServerSession, job: dict, thread_id: str,
        status: str = "usageLimited",
    ) -> bool:
        try:
            session.request(
                "thread/goal/set",
                {
                    "threadId": thread_id,
                    "status": status,
                    "origin": "automatic",
                },
                timeout=20,
            )
            self._log(job, "warn", f"{thread_id[:8]} 已回退为 {status}")
            return True
        except Exception as exc:  # noqa: BLE001 - best effort
            self._log(job, "error", f"{thread_id[:8]} 回退失败: {exc}")
            return False
