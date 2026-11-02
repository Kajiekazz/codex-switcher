"""Goal resume dispatcher: hand selected goals back to the Codex desktop app.

A resume is a *dispatch*, not a background run:

1. ``thread/goal/set status=active`` (only when the goal is not active yet) —
   this RPC does not need the thread writer lock.
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
- opt-in (``CODEX_SWITCHER_ENABLE_GOAL_RESUME=1``)
- explicit thread selection from the dashboard
- ``dry_run`` is the default and never mutates goal state
- real runs require ``confirm=true`` and only accept resumable goals
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
    ):
        self._lock = threading.RLock()
        self._job: dict | None = None
        self._thread: threading.Thread | None = None
        self._session: AppServerSession | None = None
        self._session_factory = session_factory
        self._goals_provider = goals_provider
        self._sessions_provider = sessions_provider
        self._desktop = desktop_client if desktop_client is not None else DesktopClient()
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._history_path = (
            Path(history_path)
            if history_path is not None
            else config.data_dir() / "goal-resume-history.jsonl"
        )
        self._history: list[dict] = self._load_history()
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
                    "ok": bool(item.get("ok")),
                    "status_before": item.get("status_before"),
                    "status_after": item.get("status_after"),
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
            for result in entry.get("results") or []:
                tid = result.get("thread_id")
                if not tid or tid in latest:
                    continue
                latest[tid] = {
                    "job_id": entry.get("id"),
                    "finished_at": entry.get("finished_at"),
                    "dry_run": bool(entry.get("dry_run")),
                    "state": entry.get("state"),
                    "ok": result.get("ok"),
                    "status_before": result.get("status_before"),
                    "status_after": result.get("status_after"),
                    "turns": result.get("turns"),
                    "error": result.get("error"),
                    "started_via": result.get("started_via"),
                    "cancelled": bool(result.get("cancelled")),
                    "skipped_by_user": bool(result.get("skipped_by_user")),
                    "rollback_failed": bool(result.get("rollback_failed")),
                    "queued_id": result.get("queued_id"),
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
        return latest

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

    def _resume_after_switch(self, switch_result=None) -> dict:
        cancelled_stale = False
        if self.running():
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
            goals = {goal["thread_id"]: goal for goal in self._goals_provider()}
        except Exception as exc:  # noqa: BLE001 - switch must not fail
            return {
                "ok": False, "skipped": "goals_unavailable", "error": str(exc),
                "cancelled_stale_job": cancelled_stale,
            }
        pending: list[str] = []
        skipped_goals: list[dict] = []
        for tid in wanted:
            status = (goals.get(tid) or {}).get("status")
            if status in ARMABLE_DB_STATUSES:
                pending.append(tid)
            else:
                skipped_goals.append(
                    {"thread_id": tid, "status": status or "missing"}
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
                started_via=("switch:" + target) if target else "switch",
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
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise GoalResumeError("job_running", "已有续跑任务在执行")
            job = {
                "id": uuid.uuid4().hex[:12],
                "started_at": _now_iso(),
                "finished_at": None,
                "last_activity_at": _now_iso(),
                "turn_started_at": None,
                "dry_run": bool(dry_run),
                "state": "running",
                "thread_ids": ids,
                "current_thread": None,
                "started_via": started_via,
                "events": [],
                "results": [],
                "error": None,
                "cancel_requested": False,
                "skipped": [],
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
                with self._lock:
                    job["results"].append(result)
                    job["current_thread"] = None
                    job["turn_started_at"] = None
                    job["current_turn_id"] = None
                if result.get("ok"):
                    if result.get("pending"):
                        outcome = "已排队，等桌面端取走"
                    elif result.get("delivered"):
                        outcome = "已出队，执行待确认"
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
        }
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
        if status not in ARMABLE_STATUSES:
            result["error"] = (
                f"goal is {status}, nothing to resume"
                if status
                else "no goal on this thread"
            )
            return result
        # An ``active`` goal still needs a nudge when nobody is driving it: that
        # is the normal state after a switch restarted the desktop app. We keep
        # the status as-is (no revert) and only queue the message.
        already_active = status == "active"
        if dry_run:
            result["ok"] = True
            planned = []
            if not already_active:
                planned.append("thread/goal/set status=active origin=automatic")
            planned.append(f'thread/queue/add "{RESUME_MESSAGE}"（交给桌面端执行）')
            planned.append(
                "codex://threads/wake/<session>（让已在运行的桌面端加载该会话）"
            )
            result["planned"] = planned
            result["status_after"] = (
                "active (keep)" if already_active else "active (would set)"
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
            if already_active:
                self._log(job, "info", f"{thread_id[:8]} 目标已是 active")
            else:
                # ``thread/goal/set`` works without the writer lock (verified
                # while the desktop held the thread), so arming never fights
                # the app for its conversation.
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
            client_id = str(uuid.uuid4())
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
                self._log(
                    job,
                    "info",
                    f"{thread_id[:8]} 队列里已有「{RESUME_MESSAGE}」，不再重复排队",
                )
            else:
                queued = self._queue_resume(session, thread_id, client_id)
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
                    f"{thread_id[:8]} 排队消息已出队，执行待确认（也可能被编辑或删除）",
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
            return result
        except GoalResumeError as exc:
            if exc.code != "cancelled":
                raise
            result.update(cancelled=True, error="cancelled")
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
            if activated:
                if self._revert(session, job, thread_id, result["status_before"]):
                    result["status_after"] = result["status_before"]
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
        if entry.get("delivered"):
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
            if job.get("cancel_requested"):
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
