"""Goal resume runner: drive selected usage-limited Codex goals again.

Safety model:
- opt-in (``CODEX_SWITCHER_ENABLE_GOAL_RESUME=1``)
- explicit thread selection from the dashboard
- ``dry_run`` is the default and never mutates goal state
- real runs require ``confirm=true`` and only accept ``usageLimited`` goals
- one job at a time, serial threads, bounded runtime
- state changes go through ``thread/goal/set`` in the app-server; the goals
  SQLite database is never written directly
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

TERMINAL_STATUSES = {"complete", "paused", "blocked", "usageLimited", "budgetLimited"}

#: Goal states worth re-arming. ``usageLimited`` is the obvious one, but a goal
#: also needs a nudge when it is ``paused`` (stop) or still ``active`` while no
#: turn is actually running — which is exactly what a switch does: the desktop
#: app is restarted, the running turn dies, and the goal stays ``active``
#: forever. Without this, "自动续跑" only ever fires once per goal.
ARMABLE_STATUSES = {"usageLimited", "paused", "active"}

#: The same set in the goals-SQLite vocabulary (``goals.py`` DB_TO_RPC_STATUS).
ARMABLE_DB_STATUSES = {"usage_limited", "paused", "active"}

HISTORY_LIMIT = 200

#: app-server refuses a second writer while the Codex desktop (or another
#: app-server) already has the thread open.
WRITER_BUSY_MARKERS = ("active writer", "already has an active")

#: A job may take the desktop app offline at most this many times. Each takeover
#: costs the user a closed window, so a loop of failures must not keep killing
#: it — after the cap the remaining threads are reported as busy instead.
MAX_TAKEOVERS_PER_JOB = 3


def _is_writer_busy(exc: BaseException | str) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in WRITER_BUSY_MARKERS)


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


def _thread_of(notification: dict) -> str | None:
    params = notification.get("params")
    if isinstance(params, dict):
        return params.get("threadId") or params.get("thread_id")
    return None


def _method(notification: dict) -> str:
    method = notification.get("method")
    return method if isinstance(method, str) else ""


def _trace_enabled() -> bool:
    """Verbose per-poll logging for the resume loop (``..._TRACE=1``)."""
    return os.environ.get("CODEX_SWITCHER_GOAL_RESUME_TRACE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


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
            "restart_desktop_after": bool(job.get("restart_desktop_after")),
            "results": [
                {
                    "thread_id": item.get("thread_id"),
                    "ok": bool(item.get("ok")),
                    "status_before": item.get("status_before"),
                    "status_after": item.get("status_after"),
                    "turns": int(item.get("turns") or 0),
                    "error": item.get("error"),
                    "started_via": item.get("started_via"),
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
                if entry.get("desktop_taken_over") or entry.get("restart_desktop_after"):
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
                }
        return latest

    def history_path(self) -> str:
        return str(self._history_path)

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
            "max_runtime_seconds": config.goal_resume_max_runtime_seconds(),
            "start_timeout_seconds": config.goal_resume_start_timeout_seconds(),
            "background_mode": config.goal_resume_background(),
            "job": job,
            "history": self.history()[:20],
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

    def on_account_switched(self, switch_result=None) -> dict | None:
        """Resume the goals the user pre-marked as "自动" after a switch.

        This is the only path that starts a real run without a per-click
        ``confirm``: the user already opted in per goal (the 「自动」 toggle),
        which is stricter than the card's one-off confirmation.
        """
        outcome = self._resume_after_switch(switch_result)
        if outcome is not None:
            self._last_auto = dict(outcome)
            self._last_auto["at"] = _now_iso()
        return outcome

    def will_resume_after_switch(self) -> bool:
        """Whether ``on_account_switched`` is about to start a real job.

        Used by the switch path to decide whether restarting the desktop app
        now makes sense: a queued resume needs the thread writer lock the
        desktop holds, so it is closed once and reopened when the job ends
        instead of being restarted and killed again two seconds later.
        """
        if not self.enabled() or not _flag("auto_resume_after_switch"):
            return False
        if not config.goal_resume_background():
            # Desktop mode never needs the app closed: the goal is armed and the
            # user opens the conversation, so the switch keeps its normal restart.
            return False
        return bool(auto_resume_ids())

    def _restart_desktop_after_deferred_switch(self, job: dict | None = None) -> None:
        """Reopen the desktop that the switch closed for a queued resume."""
        result: dict
        try:
            result = self._desktop.start()
        except Exception as exc:  # noqa: BLE001 - never break the caller
            result = {"ok": False, "error": str(exc)}
        if job is not None:
            with self._lock:
                job["desktop_relaunch"] = {
                    "ok": bool(result.get("ok")),
                    "pids": result.get("pids") or [],
                    "reason": result.get("reason"),
                    "message": result.get("message"),
                }

    def _resume_after_switch(self, switch_result=None) -> dict | None:
        restart = getattr(switch_result, "desktop_restart", None) or {}
        deferred = (
            isinstance(restart, dict)
            and restart.get("reason") == "deferred_to_goal_resume"
        )
        outcome = self._resume_after_switch_inner(switch_result, deferred=deferred)
        started = bool(isinstance(outcome, dict) and outcome.get("job"))
        if deferred and not started:
            # Nothing is going to run (no armable goal, goal DB unreadable, …):
            # give the window back immediately instead of leaving it closed.
            self._restart_desktop_after_deferred_switch()
        return outcome

    def _resume_after_switch_inner(
        self, switch_result=None, *, deferred: bool = False
    ) -> dict | None:
        if not self.enabled() or not _flag("auto_resume_after_switch"):
            return None
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
                }
            cancelled_stale = True
        wanted = auto_resume_ids()
        if not wanted:
            if not cancelled_stale:
                return None
            return {
                "ok": True,
                "skipped": "no_marked_goal",
                "cancelled_stale_job": True,
            }
        try:
            goals = {goal["thread_id"]: goal for goal in self._goals_provider()}
        except Exception as exc:  # noqa: BLE001 - switch must not fail
            return {"ok": False, "skipped": "goals_unavailable", "error": str(exc)}
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
                restart_desktop_after=deferred,
            )
        except Exception as exc:  # noqa: BLE001 - switch already succeeded
            return {"ok": False, "error": str(exc), "thread_ids": pending}
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
        restart_desktop_after: bool = False,
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
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise GoalResumeError("job_running", "已有续跑任务在执行")
            job = {
                "id": uuid.uuid4().hex[:12],
                "started_at": _now_iso(),
                "finished_at": None,
                "last_activity_at": _now_iso(),
                "turn_started_at": None,
                "restart_desktop_after": bool(restart_desktop_after),
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
                "takeover_threads": [],
                "desktop_taken_over": False,
                "desktop_relaunch": None,
            }
            self._job = job
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
        if not self.cancel():
            return not self.running()
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
            skipped: list[str] = []
            for tid in job.get("thread_ids") or []:
                if tid in wanted and tid not in job["skipped"]:
                    job["skipped"].append(tid)
                    skipped.append(tid)
            current = job.get("current_thread")
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
        try:
            session = self._session_factory()
            with self._lock:
                self._session = session
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
                if result.get("ok"):
                    self._log(
                        job,
                        "info",
                        f"{tid[:8]} 完成：{result.get('status_after')}"
                        + (
                            f"，{result['turns']} 个回合"
                            if result.get("turns")
                            else ""
                        ),
                    )
                else:
                    self._log(
                        job,
                        "warn",
                        f"{tid[:8]} 未续跑：{result.get('error') or '未知原因'}",
                    )
            with self._lock:
                cancelled = bool(job.get("cancel_requested"))
            job["state"] = "cancelled" if cancelled else "done"
        except GoalResumeError as exc:
            if exc.code == "cancelled":
                job["state"] = "cancelled"
                self._log(job, "warn", "任务已取消；目标可能仍处于 active")
            else:
                failed = True
                job["state"] = "failed"
                job["error"] = exc.message
                self._log(job, "error", exc.message)
        except AppServerError as exc:
            cancelled = bool(job.get("cancel_requested"))
            job["state"] = "cancelled" if cancelled else "failed"
            if not cancelled:
                failed = True
                job["error"] = str(exc)
                self._log(job, "error", f"app-server: {exc}")
        except Exception as exc:  # noqa: BLE001 - job must always finish
            failed = True
            job["state"] = "failed"
            job["error"] = str(exc)
            self._log(job, "error", f"内部错误: {exc}")
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                self._relaunch_desktop_after_takeover(job)
            except Exception as exc:  # noqa: BLE001 - job must always finish
                self._log(job, "error", f"重新打开桌面端失败: {exc}")
            if not config.goal_resume_background():
                # Armed goals only run inside the app; make sure there is a
                # window to open them in.
                try:
                    if not (self._desktop.status() or {}).get("running"):
                        self._desktop.start()
                        self._log(job, "info", "已打开桌面端，去那个会话继续")
                except Exception:  # noqa: BLE001 - arming already succeeded
                    pass
            with self._lock:
                self._session = None
                job["finished_at"] = _now_iso()
                job["current_thread"] = None
            self._record_history(job)
            if failed:
                print(f"[goal-resume {job['id']}] failed: {job.get('error')}", flush=True)

    def _resume_one(
        self,
        session: AppServerSession,
        job: dict,
        thread_id: str,
        dry_run: bool,
        started_via: str | None = None,
    ) -> dict:
        result = {
            "thread_id": thread_id,
            "ok": False,
            "status_before": None,
            "status_after": None,
            "turns": 0,
            "started_via": started_via,
            "error": None,
        }
        try:
            response = session.request(
                "thread/goal/get", {"threadId": thread_id}, timeout=30
            )
        except AppServerError as exc:
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
        # the status as-is (no revert) and only make sure a turn is running.
        already_active = status == "active"
        if dry_run:
            result["ok"] = True
            result["planned"] = (
                ["thread/resume", 'turn/start input="继续" (仅在无活动回合时)']
                if already_active
                else [
                    "thread/resume",
                    "thread/goal/set status=active origin=automatic",
                    'turn/start input="继续" (仅在自动续跑未启动时)',
                ]
            )
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
        turn_started = False
        try:
            self._resume_thread(session, job, thread_id)
            self._log(job, "info", f"{thread_id[:8]} thread/resume 完成")
            if already_active:
                self._log(
                    job,
                    "info",
                    f"{thread_id[:8]} 目标已是 active，检查是否有回合在跑",
                )
            else:
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

            if not config.goal_resume_background():
                # Desktop mode: the goal is armed; the Codex desktop starts the
                # turn when the conversation is opened. We never run a turn
                # ourselves, so nothing happens behind the user's back.
                result["ok"] = True
                result["armed"] = True
                result["status_after"] = "active"
                self._log(
                    job,
                    "info",
                    f"{thread_id[:8]} 已交给桌面端：在 Codex 里打开该会话就会继续",
                )
                return result

            deadline = time.monotonic() + config.goal_resume_max_runtime_seconds()
            # Anything already queued was emitted before this job did anything
            # (typically a ``thread/goal/updated`` carrying the old
            # ``usageLimited`` status). The notification buffer is shared, so
            # that stale item would otherwise be read as "the goal ended" and
            # kill the turn we just started.
            self._drain(session, "thread/goal/updated", thread_id)
            if _trace_enabled():
                self._log(
                    job,
                    "info",
                    f"{thread_id[:8]} trace start runtime="
                    f"{config.goal_resume_max_runtime_seconds():.0f}s "
                    f"start_timeout={config.goal_resume_start_timeout_seconds():.0f}s "
                    f"already_active={already_active}",
                )
            turn_active = False
            while True:
                with self._lock:
                    if job.get("cancel_requested"):
                        raise GoalResumeError("cancelled", "任务已取消")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    result["error"] = "max_runtime_reached"
                    self._log(
                        job,
                        "warn",
                        f"{thread_id[:8]} 达到最长运行时间，目标保持 active",
                    )
                    break
                if not turn_active:
                    if self._wait_or_start_turn(session, job, thread_id):
                        turn_active = True
                        turn_started = True
                        job["last_activity_at"] = _now_iso()
                        job["turn_started_at"] = _now_iso()

                completed = session.wait_notification(
                    lambda n: _thread_of(n) == thread_id
                    and _method(n)
                    in ("turn/started", "turn/completed", "thread/goal/updated"),
                    timeout=min(300.0, max(1.0, remaining)),
                )
                if _trace_enabled():
                    self._log(
                        job,
                        "info",
                        f"{thread_id[:8]} trace wait={_method(completed) if completed else None} "
                        f"remaining={remaining:.0f}s turn_active={turn_active}",
                    )
                if completed is not None:
                    method = _method(completed)
                    job["last_activity_at"] = _now_iso()
                    if method == "turn/started":
                        turn_active = True
                        turn_started = True
                        job.setdefault("turn_started_at", _now_iso())
                    elif method == "turn/completed":
                        turn_active = False
                        job["turn_started_at"] = None
                        result["turns"] += 1
                        self._log(
                            job,
                            "info",
                            f"{thread_id[:8]} 回合完成（累计 {result['turns']}）",
                        )
                    elif method == "thread/goal/updated":
                        goal = completed.get("params", {}).get("goal") or {}
                        status = goal.get("status")
                        result["status_after"] = status
                        # Do not end the job on a notification alone: it may
                        # describe a state from before this job started. The
                        # explicit ``thread/goal/get`` below is authoritative.
                        turn_active = status == "active"

                current = session.request(
                    "thread/goal/get", {"threadId": thread_id}, timeout=30
                )
                status = ((current or {}).get("goal") or {}).get("status")
                result["status_after"] = status
                if status in TERMINAL_STATUSES:
                    if turn_active:
                        self._log(
                            job,
                            "warn",
                            f"{thread_id[:8]} 目标变为 {status}，但回合仍在运行；结束任务会关闭 app-server 并终止它",
                        )
                    break

            final = session.request(
                "thread/goal/get", {"threadId": thread_id}, timeout=30
            )
            final_status = ((final or {}).get("goal") or {}).get("status")
            result["status_after"] = final_status
            if final_status in TERMINAL_STATUSES:
                result["ok"] = True
                if final_status == "usageLimited":
                    self._log(
                        job,
                        "warn",
                        f"{thread_id[:8]} 再次触发额度限制",
                    )
            elif final_status == "active":
                if not result.get("error"):
                    result["error"] = "still_active"
                if not turn_started:
                    self._revert(session, job, thread_id)
            else:
                result["error"] = f"unexpected status {final_status}"
            return result
        except GoalResumeError as exc:
            if exc.code == "desktop_holds_thread":
                # Desktop mode: report it on this thread and keep going, the
                # other selected goals may be free.
                result["error"] = exc.message
                result["blocked_by_desktop"] = True
                self._log(job, "warn", f"{thread_id[:8]} {exc.message}")
                return result
            raise
        except AppServerError as exc:
            # If activation succeeded but nothing ever ran, do not leave the
            # goal falsely active.
            if activated and not turn_started:
                self._revert(session, job, thread_id)
            result["error"] = str(exc)
            return result

    def _resume_thread(
        self, session: AppServerSession, job: dict, thread_id: str
    ) -> None:
        """``thread/resume``, taking the desktop app out of the way if needed.

        The Codex desktop keeps a writer lock on the conversation open in its
        UI. A CLI-side resume of that one thread is refused with "already has
        an active writer" — not a quota problem, and the situation the user sees
        as "会话被占用". Closing the app releases every lock, so: close, retry
        until the writer is free, and let ``_run_job`` reopen it at the end.
        """
        params = {"threadId": thread_id, "excludeTurns": True}
        try:
            session.request("thread/resume", params, timeout=90)
            return
        except AppServerError as exc:
            if not _is_writer_busy(exc):
                raise
            if not config.goal_resume_background():
                # Desktop mode: never take the app down. If it holds the
                # conversation, the user finishes the job there in two clicks.
                raise GoalResumeError(
                    "desktop_holds_thread",
                    "这个会话正被 Codex 桌面端打开。请在桌面端那个会话里点继续；"
                    "或者先切到别的会话让锁释放，再回来点续跑。",
                ) from exc
            if not self._take_over_desktop(job, thread_id, exc):
                raise

        deadline = self._clock() + config.goal_resume_takeover_wait_seconds()
        while True:
            try:
                session.request("thread/resume", params, timeout=90)
            except AppServerError as exc:
                if not _is_writer_busy(exc):
                    raise
                if self._clock() >= deadline:
                    raise
                self._sleep(1.0)
                continue
            self._log(job, "info", f"{thread_id[:8]} 接管成功，线程锁已释放")
            return

    def takeover_enabled(self) -> bool:
        try:
            return bool(config.goal_resume_takeover_desktop())
        except Exception:  # noqa: BLE001 - a settings error must not break resume
            return False

    def _take_over_desktop(
        self, job: dict, thread_id: str, exc: BaseException
    ) -> bool:
        """Close the desktop app so it hands the thread writer back."""
        if not self.takeover_enabled():
            self._log(
                job,
                "warn",
                f"{thread_id[:8]} 被桌面端占用；自动接管已关闭（设置里可打开）",
            )
            return False
        with self._lock:
            takeovers = job.setdefault("takeover_threads", [])
            if len(takeovers) >= MAX_TAKEOVERS_PER_JOB:
                self._log(
                    job,
                    "warn",
                    f"{thread_id[:8]} 仍被占用；本次任务已接管 "
                    f"{MAX_TAKEOVERS_PER_JOB} 次，不再关闭桌面端",
                )
                return False

        status = self._desktop.status()
        if not status.get("running"):
            # Nothing we can take over: a terminal `codex` CLI (or a stray
            # app-server) is the writer and closing the desktop will not help.
            self._log(
                job,
                "warn",
                f"{thread_id[:8]} 被占用，但桌面端没在运行（可能是终端里的 codex）",
            )
            return False

        self._log(
            job,
            "warn",
            f"{thread_id[:8]} 被桌面端占用 → 关闭桌面端接管，任务结束后会自动打开",
        )
        closed = self._desktop.close_app(force=True)
        with self._lock:
            takeovers = job.setdefault("takeover_threads", [])
            takeovers.append(thread_id)
            if closed.get("ok"):
                job["desktop_taken_over"] = True
            job["desktop_close"] = {
                "ok": bool(closed.get("ok")),
                "reason": closed.get("reason"),
                "previous_pids": closed.get("previous_pids") or [],
                "killed": closed.get("killed") or [],
                "message": closed.get("message"),
            }
        if not closed.get("ok"):
            self._log(
                job,
                "error",
                "关闭桌面端失败："
                + str(closed.get("message") or closed.get("reason") or closed.get("error")),
            )
            return False
        # Persist the closed window right away: if the service dies before the
        # job finishes, _mark_interrupted uses this to reopen it on startup.
        self._record_history(job)
        return True

    def _relaunch_desktop_after_takeover(self, job: dict) -> None:
        """Reopen the desktop app after a job that closed it."""
        with self._lock:
            if not (job.get("desktop_taken_over") or job.get("restart_desktop_after")):
                return
            if job.get("desktop_relaunch_attempted"):
                return
            job["desktop_relaunch_attempted"] = True
        self._log(job, "info", "任务结束，重新打开桌面端")
        try:
            result = self._desktop.start()
        except Exception as exc:  # noqa: BLE001 - job must still finish
            result = {"ok": False, "error": str(exc)}
        with self._lock:
            job["desktop_relaunch"] = {
                "ok": bool(result.get("ok")),
                "pids": result.get("pids") or [],
                "reason": result.get("reason"),
                "message": result.get("message"),
            }
        self._log(
            job,
            "info" if result.get("ok") else "warn",
            str(
                result.get("message")
                or ("桌面端已重新打开" if result.get("ok") else "桌面端未能重新打开")
            ),
        )

    def _wait_or_start_turn(
        self, session: AppServerSession, job: dict, thread_id: str
    ) -> bool:
        started = session.wait_notification(
            lambda n: _thread_of(n) == thread_id and _method(n) == "turn/started",
            timeout=config.goal_resume_start_timeout_seconds(),
        )
        if started is not None:
            self._log(job, "info", f"{thread_id[:8]} 自动续跑回合已启动")
            return True
        try:
            session.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": "继续"}],
                },
                timeout=60,
            )
        except AppServerError as exc:
            text = str(exc).lower()
            if "already" in text or "active" in text:
                return True
            raise
        self._log(job, "info", f"{thread_id[:8]} 已发送续跑输入「继续」")
        return True

    def _drain(self, session: AppServerSession, method: str, thread_id: str) -> int:
        """Drop notifications of *method* that were queued before we acted.

        ``AppServerSession.wait_notification`` deliberately keeps non-matching
        notifications so a later wait can still see them. That is helpful for
        ``turn/completed`` but harmful for ``thread/goal/updated``: the buffer
        can still hold the pre-resume status (``usageLimited``) when the new
        turn starts, and acting on it aborts a perfectly healthy resume.
        """
        dropped = 0
        while True:
            item = session.wait_notification(
                lambda n: _thread_of(n) == thread_id and _method(n) == method,
                timeout=0.0,
            )
            if item is None:
                return dropped
            dropped += 1

    def _revert(
        self, session: AppServerSession, job: dict, thread_id: str
    ) -> None:
        try:
            session.request(
                "thread/goal/set",
                {
                    "threadId": thread_id,
                    "status": "usageLimited",
                    "origin": "automatic",
                },
                timeout=20,
            )
            self._log(job, "warn", f"{thread_id[:8]} 已回退为 usageLimited")
        except Exception as exc:  # noqa: BLE001 - best effort
            self._log(job, "error", f"{thread_id[:8]} 回退失败: {exc}")
