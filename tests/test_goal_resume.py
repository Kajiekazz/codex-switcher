import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from codex_switcher.api import create_server
from codex_switcher.appserver import AppServerError
from codex_switcher.goal_resume import GoalResumeError, GoalResumeService
from codex_switcher.goals import list_goals
from codex_switcher.store import AccountStore

from test_api import fake_auth_file, free_port
from test_goals import build_goals_db, build_state_db


THREAD = "01a17a9b-88bf-7620-a979-c2ecbf5dcc26"

class FakeSession:
    """Scripted app-server double. No process, no network, no real goals."""

    def __init__(
        self,
        goal_status="usageLimited",
        final_status="complete",
        notifications=None,
    ):
        self.calls = []
        self.goal_status = goal_status
        self.final_status = final_status
        self.notifications = list(notifications or [])
        self.goal_gets = 0
        self.closed = False
        self.pid = 4242

    def initialize(self, timeout=30.0):
        self.calls.append(("initialize", None))
        return {"userAgent": "fake"}

    def request(self, method, params=None, timeout=30.0):
        self.calls.append((method, params))
        if self.closed:
            raise AppServerError("app-server is closed")
        if method == "thread/goal/get":
            self.goal_gets += 1
            status = self.goal_status if self.goal_gets == 1 else self.final_status
            return {"goal": {"status": status}}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}}
        if method == "thread/goal/set":
            return {"goal": {"status": params["status"]}}
        if method == "turn/start":
            return {"turn": {"id": "turn-fake"}}
        return {}

    def wait_notification(self, predicate, timeout):
        for index, notification in enumerate(self.notifications):
            if predicate(notification):
                return self.notifications.pop(index)
        return None

    def close(self):
        self.closed = True

    def methods(self):
        return [name for name, _ in self.calls]


class MissingThreadSession(FakeSession):
    """A deleted thread must not abort the whole job (its siblings still run)."""

    def __init__(self, missing):
        super().__init__(goal_status="usageLimited", final_status="complete")
        self.missing = missing
        self._seen = {}

    def request(self, method, params=None, timeout=30.0):
        tid = (params or {}).get("threadId")
        if method == "thread/goal/get" and tid == self.missing:
            self.calls.append((method, params))
            raise AppServerError(f"thread/goal/get: thread not found: {tid}")
        if method == "thread/goal/get":
            self.calls.append((method, params))
            self._seen[tid] = self._seen.get(tid, 0) + 1
            status = self.goal_status if self._seen[tid] == 1 else self.final_status
            return {"goal": {"status": status}}
        return super().request(method, params, timeout)


class StuckSession(FakeSession):
    """Keeps a job alive until cancelled; used for the job_running/cancel test."""

    def __init__(self):
        super().__init__(goal_status="usageLimited", final_status="active")
        self._release = threading.Event()

    def request(self, method, params=None, timeout=30.0):
        self.calls.append((method, params))
        if self.closed:
            raise AppServerError("app-server is closed")
        if method == "thread/goal/get":
            self.goal_gets += 1
            return {"goal": {"status": "usageLimited" if self.goal_gets == 1 else "active"}}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}}
        if method == "thread/goal/set":
            return {"goal": {"status": params["status"]}}
        return {}

    def wait_notification(self, predicate, timeout):
        while not self.closed:
            if self._release.wait(0.02):
                return None
        return None

    def close(self):
        self.closed = True
        self._release.set()


class ScriptedSession(FakeSession):
    """FakeSession that stops the resume loop once its script is exhausted.

    ``wait_notification`` raising instead of blocking forever keeps the
    regression tests instant; ``timeout=0`` (the drain path) still returns None
    so draining keeps working.
    """

    def wait_notification(self, predicate, timeout):
        item = super().wait_notification(predicate, timeout)
        if item is None and timeout > 0:
            raise AppServerError("script exhausted")
        return item


class GoalResumeTests(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("CODEX_SWITCHER_ENABLE_GOAL_RESUME")
        os.environ["CODEX_SWITCHER_ENABLE_GOAL_RESUME"] = "1"

    def tearDown(self):
        if self._old is None:
            os.environ.pop("CODEX_SWITCHER_ENABLE_GOAL_RESUME", None)
        else:
            os.environ["CODEX_SWITCHER_ENABLE_GOAL_RESUME"] = self._old

    def _service(self, session):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return GoalResumeService(
            session_factory=lambda: session,
            goals_provider=lambda: [],
            history_path=Path(tmp.name) / "goal-resume-history.jsonl",
            sessions_provider=lambda **kwargs: {"total": 0, "pinned": [], "projects": [], "recent": [], "counts": {}},
        )

    def _wait_job(self, service, states=("done", "partial", "failed", "cancelled"), timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = service.status()["job"]
            if job and job.get("state") in states:
                return job
            time.sleep(0.02)
        self.fail(f"job did not finish: {service.status()}")

    def test_disabled_by_default(self):
        os.environ["CODEX_SWITCHER_ENABLE_GOAL_RESUME"] = "0"
        service = self._service(FakeSession())
        with self.assertRaises(GoalResumeError) as ctx:
            service.start([THREAD], dry_run=True)
        self.assertEqual(ctx.exception.code, "goal_resume_disabled")

    def test_confirm_required_for_real_run(self):
        service = self._service(FakeSession())
        with self.assertRaises(GoalResumeError) as ctx:
            service.start([THREAD], dry_run=False, confirm=False)
        self.assertEqual(ctx.exception.code, "confirmation_required")

    def test_thread_selection_required(self):
        service = self._service(FakeSession())
        with self.assertRaises(GoalResumeError) as ctx:
            service.start([], dry_run=True)
        self.assertEqual(ctx.exception.code, "thread_required")

    def test_dry_run_never_mutates(self):
        session = FakeSession(goal_status="usageLimited")
        service = self._service(session)
        service.start([THREAD], dry_run=True)
        job = self._wait_job(service)
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["results"][0]["ok"], True)
        self.assertEqual(job["results"][0]["status_before"], "usageLimited")
        self.assertIn("thread/goal/set status=active origin=automatic", job["results"][0]["planned"])
        self.assertEqual(session.methods(), ["initialize", "thread/goal/get"])
        self.assertEqual(service.status()["job"]["dry_run"], True)

    def test_completed_goal_is_skipped(self):
        session = FakeSession(goal_status="complete")
        service = self._service(session)
        service.start([THREAD], dry_run=True)
        job = self._wait_job(service)
        result = job["results"][0]
        self.assertFalse(result["ok"])
        self.assertIn("nothing to resume", result["error"])
        self.assertNotIn("thread/resume", session.methods())
        self.assertEqual(job["state"], "failed")
        self.assertTrue(job["error"])

    def test_interactive_turn_start_error_is_not_reported_as_running(self):
        session = FakeSession(goal_status="usageLimited", final_status="complete")
        original = session.request

        def request(method, params=None, timeout=30):
            if method == "turn/start":
                session.calls.append((method, params))
                raise AppServerError("turn/start: this operation requires interactive approval")
            return original(method, params, timeout)

        session.request = request
        service = self._service(session)
        service.start([THREAD], dry_run=False, confirm=True)
        job = self._wait_job(service)
        self.assertEqual(job["state"], "failed")
        self.assertFalse(job["results"][0]["ok"])
        self.assertIn("interactive approval", job["results"][0]["error"])

    def test_explicit_existing_turn_error_is_recognized(self):
        session = FakeSession()
        session.request = mock.Mock(side_effect=AppServerError("turn/start: thread already has an active turn"))
        service = self._service(session)
        job = {"id": "known-active-turn", "events": []}
        self.assertTrue(service._wait_or_start_turn(session, job, THREAD))

    def test_unrelated_already_errors_are_not_reported_as_running(self):
        service = self._service(FakeSession())
        for message in ("request already in progress", "account already deactivated"):
            with self.subTest(message=message):
                session = FakeSession()
                session.request = mock.Mock(side_effect=AppServerError(message))
                with self.assertRaisesRegex(AppServerError, message):
                    service._wait_or_start_turn(session, {"id": "error", "events": []}, THREAD)

    def test_failed_start_restores_paused_goal(self):
        session = FakeSession(goal_status="paused", final_status="active")
        original = session.request

        def request(method, params=None, timeout=30):
            if method == "turn/start":
                raise AppServerError("model access denied")
            return original(method, params, timeout)

        session.request = request
        service = self._service(session)
        service.start([THREAD], dry_run=False, confirm=True)
        job = self._wait_job(service)
        sets = [params for method, params in session.calls if method == "thread/goal/set"]
        self.assertEqual(sets[-1]["status"], "paused")
        self.assertEqual(job["results"][0]["status_after"], "paused")
        self.assertEqual(job["state"], "failed")

    def test_failed_rollback_does_not_claim_original_status_was_restored(self):
        session = FakeSession(goal_status="paused", final_status="active")
        original = session.request

        def request(method, params=None, timeout=30):
            if method == "turn/start":
                raise AppServerError("model access denied")
            if method == "thread/goal/set" and params["status"] == "paused":
                raise AppServerError("failed to restore goal")
            return original(method, params, timeout)

        session.request = request
        service = self._service(session)
        service.start([THREAD], dry_run=False, confirm=True)
        job = self._wait_job(service)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["results"][0]["status_after"], "active")
        self.assertTrue(job["results"][0]["rollback_failed"])

    def test_cancel_records_unstarted_queue_without_running_it(self):
        session = StuckSession()
        service = self._service(session)
        queued = THREAD + "-queued"
        service.start([THREAD, queued], dry_run=False, confirm=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(method == "thread/resume" for method, _ in session.calls):
                break
            time.sleep(0.01)
        self.assertTrue(service.cancel())
        job = self._wait_job(service)
        service._thread.join(timeout=2)
        self.assertEqual(job["state"], "cancelled")
        results = {item["thread_id"]: item for item in job["results"]}
        self.assertTrue(results[THREAD]["cancelled"])
        self.assertTrue(results[queued]["cancelled"])
        self.assertFalse(any((params or {}).get("threadId") == queued for _, params in session.calls))
        self.assertEqual(service.list()["last_resume"][queued]["error"], "cancelled")

    def test_cancel_during_goal_read_prevents_resume_and_activation(self):
        session = FakeSession()
        service = self._service(session)
        reading = threading.Event()
        release = threading.Event()

        def request(method, params=None, timeout=30):
            session.calls.append((method, params))
            if method == "thread/goal/get":
                reading.set()
                release.wait(2)
                return {"goal": {"status": "paused"}}
            return {}

        session.request = request
        service.start([THREAD], dry_run=False, confirm=True)
        try:
            self.assertTrue(reading.wait(2))
            self.assertTrue(service.cancel())
        finally:
            release.set()
            service._thread.join(timeout=3)
        job = service.status()["job"]
        self.assertEqual(job["state"], "cancelled")
        self.assertNotIn("thread/resume", session.methods())
        self.assertNotIn("thread/goal/set", session.methods())
        self.assertTrue(job["results"][0]["cancelled"])

    def test_paused_goal_is_rearmed(self):
        session = FakeSession(goal_status="paused")
        service = self._service(session)
        service.start([THREAD], dry_run=True)
        job = self._wait_job(service)
        result = job["results"][0]
        self.assertTrue(result["ok"])
        self.assertEqual(result["status_before"], "paused")
        self.assertIn(
            "thread/goal/set status=active origin=automatic", result["planned"]
        )

    def test_active_goal_is_woken_without_touching_status(self):
        session = FakeSession(
            goal_status="active",
            final_status="complete",
            notifications=[
                {"method": "turn/started", "params": {"threadId": THREAD}},
                {"method": "turn/completed", "params": {"threadId": THREAD}},
            ],
        )
        service = self._service(session)
        service.start([THREAD], dry_run=False, confirm=True)
        job = self._wait_job(service)
        result = job["results"][0]
        self.assertTrue(result["ok"])
        self.assertEqual(result["status_before"], "active")
        self.assertEqual(result["status_after"], "complete")
        self.assertNotIn("thread/goal/set", session.methods())

    def test_stale_goal_update_does_not_abort_a_healthy_resume(self):
        # A queued ``thread/goal/updated`` still carrying the pre-resume status
        # used to end the job 31 ms after the turn started (job 8531015e949c),
        # leaving the goal falsely ``active`` with nothing running.
        session = ScriptedSession(
            goal_status="usageLimited",
            final_status="active",
            notifications=[
                {
                    "method": "thread/goal/updated",
                    "params": {"threadId": THREAD, "goal": {"status": "usageLimited"}},
                },
                {"method": "turn/started", "params": {"threadId": THREAD}},
                {"method": "turn/completed", "params": {"threadId": THREAD}},
            ],
        )
        service = self._service(session)
        service.start([THREAD], dry_run=False, confirm=True)
        job = self._wait_job(service)
        result = job["results"][0]
        self.assertEqual(result["turns"], 1)
        self.assertNotEqual(result["error"], "still_active")
        self.assertEqual(session.notifications, [])

    def test_terminal_goal_update_is_confirmed_by_goal_get(self):
        # Even without the pre-drain, a terminal status inside a notification
        # must not end the job while ``thread/goal/get`` still says active.
        session = ScriptedSession(
            goal_status="active",
            final_status="active",
            notifications=[
                {"method": "turn/started", "params": {"threadId": THREAD}},
                {
                    "method": "thread/goal/updated",
                    "params": {"threadId": THREAD, "goal": {"status": "usageLimited"}},
                },
            ],
        )
        service = self._service(session)
        with mock.patch.object(GoalResumeService, "_drain", lambda *a, **k: 0):
            service.start([THREAD], dry_run=False, confirm=True)
            job = self._wait_job(service)
        self.assertNotEqual(job["results"][0]["error"], "still_active")

    def test_missing_thread_does_not_abort_the_job(self):
        other = "01a17a9b-88bf-7620-a979-c2ecbf5dcc28"
        session = MissingThreadSession(THREAD)
        service = self._service(session)
        service.start([THREAD, other], dry_run=False, confirm=True)
        job = self._wait_job(service)
        self.assertEqual(job["state"], "partial")
        first, second = job["results"]
        self.assertFalse(first["ok"])
        self.assertTrue(first.get("missing"))
        self.assertIn("not found", first["error"])
        self.assertTrue(second["ok"])
        self.assertEqual(second["thread_id"], other)

    def test_stop_skips_queued_threads(self):
        session = StuckSession()
        service = self._service(session)
        other = "01a17a9b-88bf-7620-a979-c2ecbf5dcc27"
        service.start([THREAD, other], dry_run=False, confirm=True)
        deadline = time.time() + 5
        while time.time() < deadline and not service.status()["job"]["current_thread"]:
            time.sleep(0.02)
        outcome = service.stop([other])
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["skipped"], [other])
        self.assertFalse(outcome["cancelled"])
        self.assertIn(other, service.status()["job"]["skipped"])
        service.cancel()
        self._wait_job(service, states=("cancelled", "done", "failed"))

    def test_stop_of_the_running_thread_cancels_the_job(self):
        session = StuckSession()
        service = self._service(session)
        service.start([THREAD], dry_run=False, confirm=True)
        deadline = time.time() + 5
        while time.time() < deadline and not service.status()["job"]["current_thread"]:
            time.sleep(0.02)
        outcome = service.stop([THREAD])
        self.assertTrue(outcome["ok"])
        self.assertTrue(outcome["cancelled"])
        job = self._wait_job(service, states=("cancelled", "done", "failed"))
        self.assertEqual(job["state"], "cancelled")
        self.assertEqual(job["results"][0]["thread_id"], THREAD)
        self.assertTrue(job["results"][0]["cancelled"])
        self.assertEqual(job["results"][0]["error"], "cancelled")
        last = service.list()["last_resume"][THREAD]
        self.assertEqual(last["state"], "cancelled")
        self.assertEqual(last["error"], "cancelled")

    def test_user_skipped_threads_are_not_execution_failures(self):
        session = FakeSession(goal_status="usageLimited", final_status="complete")
        service = self._service(session)
        other = THREAD + "-queued"
        job = {
            "id": "skip-only", "events": [], "results": [], "state": "running",
            "thread_ids": [THREAD, other], "skipped": [other], "dry_run": True,
            "cancel_requested": False, "error": None,
        }
        service._run_job(job, [THREAD, other], True)
        self.assertEqual(job["state"], "done")
        self.assertTrue(job["results"][1]["skipped_by_user"])

    def test_real_run_activates_and_completes(self):
        session = FakeSession(
            goal_status="usageLimited",
            final_status="complete",
            notifications=[
                {"method": "turn/started", "params": {"threadId": THREAD}},
                {"method": "turn/completed", "params": {"threadId": THREAD}},
            ],
        )
        service = self._service(session)
        service.start([THREAD], dry_run=False, confirm=True)
        job = self._wait_job(service)
        self.assertEqual(job["state"], "done")
        result = job["results"][0]
        self.assertTrue(result["ok"])
        self.assertEqual(result["status_before"], "usageLimited")
        self.assertEqual(result["status_after"], "complete")
        self.assertEqual(result["turns"], 1)

        methods = session.methods()
        self.assertIn("thread/resume", methods)
        self.assertIn("thread/goal/set", methods)
        sets = [params for name, params in session.calls if name == "thread/goal/set"]
        self.assertEqual(sets[0]["status"], "active")
        self.assertEqual(sets[0]["origin"], "automatic")
        self.assertNotIn("turn/start", methods)

    def test_real_run_starts_turn_when_no_auto_continuation(self):
        session = FakeSession(goal_status="usageLimited", final_status="complete")
        service = self._service(session)
        service.start([THREAD], dry_run=False, confirm=True)
        job = self._wait_job(service)
        self.assertTrue(job["results"][0]["ok"])
        self.assertIn("turn/start", session.methods())

    def test_requires_confirmation_and_single_job(self):
        session = StuckSession()
        service = self._service(session)
        service.start([THREAD], dry_run=False, confirm=True)
        with self.assertRaises(GoalResumeError) as ctx:
            service.start([THREAD], dry_run=True)
        self.assertEqual(ctx.exception.code, "job_running")
        self.assertTrue(service.cancel())
        job = self._wait_job(service, states=("cancelled", "failed"))
        self.assertEqual(job["state"], "cancelled")

    def test_cancel_without_job_is_noop(self):
        service = self._service(FakeSession())
        self.assertFalse(service.cancel())

    def test_status_payload_shape(self):
        service = self._service(FakeSession())
        status = service.status()
        for key in ("enabled", "running", "job", "max_runtime_seconds", "start_timeout_seconds"):
            self.assertIn(key, status)
        self.assertFalse(status["running"])

    def test_history_persists_last_resume_per_thread(self):
        session = FakeSession(
            goal_status="usageLimited",
            final_status="complete",
            notifications=[
                {"method": "turn/started", "params": {"threadId": THREAD}},
                {"method": "turn/completed", "params": {"threadId": THREAD}},
            ],
        )
        service = self._service(session)
        service.start([THREAD], dry_run=False, confirm=True)
        job = self._wait_job(service)
        self.assertEqual(job["state"], "done")
        history = service.history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["id"], job["id"])
        last = service.list()["last_resume"][THREAD]
        self.assertTrue(last["ok"])
        self.assertEqual(last["status_before"], "usageLimited")
        reloaded = GoalResumeService(
            session_factory=lambda: FakeSession(),
            goals_provider=lambda: [],
            history_path=service.history_path(),
            sessions_provider=lambda **kwargs: {
                "total": 0, "pinned": [], "projects": [], "recent": [], "counts": {},
            },
        )
        self.assertEqual(len(reloaded.history()), 1)
        self.assertIn(THREAD, reloaded.list()["last_resume"])


class _CreateSession:
    """app-server double for goal creation: ``thread/goal/get`` -> null first."""

    def __init__(self, existing: dict | None = None, fail_on: str | None = None):
        self.calls = []
        self.existing = existing
        self.fail_on = fail_on
        self.closed = False
        self.pid = 5151

    def initialize(self, timeout=30.0):
        return {"userAgent": "fake"}

    def request(self, method, params=None, timeout=30.0):
        self.calls.append((method, params))
        if method == self.fail_on:
            raise AppServerError("boom")
        if method == "thread/goal/get":
            if isinstance(self.existing, dict):
                return {"goal": self.existing}
            return {"goal": None}
        if method == "thread/goal/set":
            return {
                "goal": {
                    "threadId": params["threadId"],
                    "objective": params.get("objective"),
                    "status": params.get("status"),
                }
            }
        return {}

    def close(self):
        self.closed = True


class CreateGoalsTests(unittest.TestCase):
    def setUp(self):
        enabled = mock.patch("codex_switcher.goal_resume.config.goal_resume_enabled", return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)

    def _service(self, session):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return GoalResumeService(
            session_factory=lambda: session,
            goals_provider=lambda: [],
            history_path=Path(tmp.name) / "goal-resume-history.jsonl",
        )

    def test_creates_paused_goal_with_the_objective(self):
        session = _CreateSession()
        service = self._service(session)
        result = service.create_goals(["aaa"], "直到完成任务")
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["created"]), 1)
        self.assertEqual(result["created"][0]["goal_status"], "paused")
        sets = [call for call in session.calls if call[0] == "thread/goal/set"]
        self.assertEqual(len(sets), 1)
        self.assertEqual(
            sets[0][1],
            {
                "threadId": "aaa",
                "objective": "直到完成任务",
                "status": "paused",
            },
        )
        self.assertTrue(session.closed)

    def test_disabled_mode_does_not_create_goal_or_start_session(self):
        session = _CreateSession()
        service = self._service(session)
        with mock.patch("codex_switcher.goal_resume.config.goal_resume_enabled", return_value=False):
            with self.assertRaises(GoalResumeError) as ctx:
                service.create_goals([THREAD], "should not be written")
        self.assertEqual(ctx.exception.code, "goal_resume_disabled")
        self.assertEqual(session.calls, [])

    def test_active_status_is_forwarded(self):
        session = _CreateSession()
        service = self._service(session)
        service.create_goals(["aaa"], "", status="active")
        sets = [call for call in session.calls if call[0] == "thread/goal/set"]
        self.assertEqual(sets[0][1]["status"], "active")
        # empty objective falls back to the default
        self.assertEqual(sets[0][1]["objective"], "直到完成任务")

    def test_skips_threads_that_already_have_a_goal(self):
        session = _CreateSession(existing={"status": "active", "objective": "老目标"})
        service = self._service(session)
        result = service.create_goals(["aaa"], "新目标")
        self.assertEqual(result["created"], [])
        self.assertEqual(result["skipped"][0]["reason"], "already_has_goal")
        self.assertFalse([call for call in session.calls if call[0] == "thread/goal/set"])

    def test_rejects_unknown_status_and_empty_selection(self):
        service = self._service(_CreateSession())
        with self.assertRaises(GoalResumeError) as ctx:
            service.create_goals(["aaa"], "x", status="weird")
        self.assertEqual(ctx.exception.code, "invalid_status")
        with self.assertRaises(GoalResumeError) as ctx:
            service.create_goals([], "x")
        self.assertEqual(ctx.exception.code, "thread_required")

    def test_per_thread_error_is_reported_not_raised(self):
        session = _CreateSession(fail_on="thread/goal/set")
        service = self._service(session)
        result = service.create_goals(["aaa", "bbb"], "x")
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["errors"]), 2)
        self.assertTrue(session.closed)


class HistoryRestartSafetyTests(unittest.TestCase):
    def _service(self, path):
        return GoalResumeService(
            session_factory=lambda: FakeSession(),
            goals_provider=lambda: [],
            history_path=path,
        )

    def test_running_record_becomes_interrupted_after_restart(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "goal-resume-history.jsonl"
        path.write_text(
            json.dumps(
                {
                    "id": "abc123",
                    "started_at": "2026-10-31T13:30:00+00:00",
                    "finished_at": None,
                    "state": "running",
                    "thread_ids": [THREAD],
                    "results": [],
                    "error": None,
                    "dry_run": False,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        service = self._service(path)
        history = service.history()
        self.assertEqual(history[0]["state"], "interrupted")
        self.assertEqual(history[0]["error"], "service_restarted")
        self.assertTrue(history[0]["finished_at"])
        on_disk = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(on_disk["state"], "interrupted")

    def test_record_history_upserts_instead_of_duplicating(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "goal-resume-history.jsonl"
        service = self._service(path)
        job = {
            "id": "same-id",
            "started_at": "2026-10-31T13:30:00+00:00",
            "finished_at": None,
            "state": "running",
            "thread_ids": [THREAD],
            "results": [],
        }
        service._record_history(job)
        job["state"] = "done"
        job["finished_at"] = "2026-10-31T13:31:00+00:00"
        service._record_history(job)
        self.assertEqual(len(service.history()), 1)
        self.assertEqual(service.history()[0]["state"], "done")


class GoalsApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.auth = self.root / "auth.json"
        fake_auth_file(self.auth)
        goals_db = self.root / "goals_1.sqlite"
        state_db = self.root / "state_5.sqlite"
        build_goals_db(
            goals_db,
            [
                {
                    "thread_id": THREAD,
                    "objective": "继续任务",
                    "status": "usage_limited",
                    "tokens_used": 1234,
                    "time_used_seconds": 60,
                }
            ],
        )
        build_state_db(
            state_db, [{"id": THREAD, "name": "修复 EXE 解包输出 (2)"}]
        )
        self.env_keys = (
            "CODEX_SWITCHER_GOALS_DB",
            "CODEX_SWITCHER_STATE_DB",
            "CODEX_SWITCHER_ENABLE_GOAL_RESUME",
            "CODEX_SWITCHER_DATA",
            "CODEX_SWITCHER_AUTH",
            "CODEX_SWITCHER_ENABLE_SWITCH",
        )
        self.old_env = {key: os.environ.get(key) for key in self.env_keys}
        os.environ["CODEX_SWITCHER_GOALS_DB"] = str(goals_db)
        os.environ["CODEX_SWITCHER_STATE_DB"] = str(state_db)
        os.environ["CODEX_SWITCHER_ENABLE_GOAL_RESUME"] = "1"
        os.environ["CODEX_SWITCHER_DATA"] = str(self.root / "data")
        os.environ["CODEX_SWITCHER_AUTH"] = str(self.auth)
        os.environ["CODEX_SWITCHER_ENABLE_SWITCH"] = "0"

        self.store = AccountStore(self.root / "accounts.sqlite3")
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.session = FakeSession(
            goal_status="usageLimited",
            final_status="complete",
            notifications=[
                {"method": "turn/started", "params": {"threadId": THREAD}},
                {"method": "turn/completed", "params": {"threadId": THREAD}},
            ],
        )
        self.service = GoalResumeService(
            session_factory=lambda: self.session,
            goals_provider=list_goals,
        )
        self.server = create_server(
            self.store,
            self.auth,
            "127.0.0.1",
            self.port,
            goal_resume=self.service,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        for _ in range(100):
            try:
                self.request("GET", "/api/health")
                break
            except Exception:
                time.sleep(0.05)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def request(self, method, path, body=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method)
        for key, value in headers.items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            exc.close()
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {"raw": raw}
            return exc.code, payload

    def _wait_done(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, body = self.request("GET", "/api/goals")
            job = body.get("job") or {}
            if job.get("state") in ("done", "failed", "cancelled"):
                return body
            time.sleep(0.05)
        self.fail("goal job did not finish")

    def test_list_goals_endpoint(self):
        status, body = self.request("GET", "/api/goals")
        self.assertEqual(status, 200)
        self.assertTrue(body["enabled"])
        self.assertEqual(len(body["goals"]), 1)
        goal = body["goals"][0]
        self.assertEqual(goal["thread_id"], THREAD)
        self.assertEqual(goal["thread_name"], "修复 EXE 解包输出 (2)")
        self.assertTrue(goal["resumable"])
        self.assertIsNone(body["job"])
        # Never leak secrets.
        self.assertNotIn("password", json.dumps(body))

    def test_resume_dry_run(self):
        status, body = self.request(
            "POST",
            "/api/goals/resume",
            {"thread_ids": [THREAD], "dry_run": True},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["job"]["dry_run"])
        final = self._wait_done()
        self.assertEqual(final["job"]["state"], "done")
        self.assertTrue(final["job"]["results"][0]["ok"])
        self.assertEqual(self.session.methods(), ["initialize", "thread/goal/get"])

    def test_resume_requires_confirm(self):
        status, body = self.request(
            "POST",
            "/api/goals/resume",
            {"thread_ids": [THREAD], "dry_run": False},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "confirmation_required")
        self.assertEqual(self.session.methods(), [])

    def test_resume_disabled(self):
        os.environ["CODEX_SWITCHER_ENABLE_GOAL_RESUME"] = "0"
        status, body = self.request(
            "POST",
            "/api/goals/resume",
            {"thread_ids": [THREAD], "dry_run": True},
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "goal_resume_disabled")

    def test_resume_real_run(self):
        status, body = self.request(
            "POST",
            "/api/goals/resume",
            {"thread_ids": [THREAD], "dry_run": False, "confirm": True},
        )
        self.assertEqual(status, 200)
        final = self._wait_done()
        result = final["job"]["results"][0]
        self.assertTrue(result["ok"])
        self.assertEqual(result["status_after"], "complete")
        self.assertIn("thread/goal/set", self.session.methods())

    def test_cancel_requires_confirm(self):
        status, body = self.request("POST", "/api/goals/cancel", {})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "confirmation_required")


if __name__ == "__main__":
    unittest.main()
