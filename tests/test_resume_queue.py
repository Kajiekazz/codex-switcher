"""Resume dispatch never touches the user's Codex window.

A resume used to close the desktop app to steal the thread writer lock. It no
longer needs the lock at all: the resume is a ``thread/queue/add`` and the
desktop's own app-server runs the turn. These tests pin that down — the window
must never be closed, restarted, or "reopened" behind the user's back.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from codex_switcher.goal_resume import GoalResumeService

from test_goal_resume import FakeSession


THREAD = "01a17a9b-88bf-7620-a979-c2ecbf5dcc26"

ENV_KEYS = (
    "CODEX_SWITCHER_ENABLE_GOAL_RESUME",
    "CODEX_SWITCHER_GOAL_RESUME_TAKEOVER",
    "CODEX_SWITCHER_GOAL_RESUME_TAKEOVER_WAIT",
    "CODEX_SWITCHER_GOAL_RESUME_WAKE",
    "CODEX_SWITCHER_DATA",
)


class _FakeClock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += float(seconds)


class _FakeDesktop:
    """Records every window-level action; none of them may be used."""

    def __init__(self, running: bool = True, restart_ok: bool = True) -> None:
        self.running = running
        self.restart_ok = restart_ok
        self.status_calls = 0
        self.close_calls = 0
        self.start_calls = 0
        self.restart_calls = 0
        self.wake_calls = []
        self.wake_error = None

    def status(self) -> dict:
        self.status_calls += 1
        return {
            "ok": True,
            "available": True,
            "enabled": True,
            "running": self.running,
            "pids": [111] if self.running else [],
            "aumid": "OpenAI.Codex_test!App",
        }

    def wake_thread(self, thread_id: str) -> dict:
        self.wake_calls.append(thread_id)
        if self.wake_error:
            raise RuntimeError(self.wake_error)
        if not self.running:
            return {"ok": False, "skipped": True, "reason": "desktop_not_running"}
        return {
            "ok": True,
            "skipped": False,
            "url": f"codex://threads/wake/{thread_id}",
        }

    def close_app(self, force: bool = False) -> dict:
        self.close_calls += 1
        self.running = False
        return {"ok": True, "previous_pids": [111], "pids": [], "killed": [111]}

    def start(self, before: dict | None = None) -> dict:
        self.start_calls += 1
        self.running = True
        return {"ok": True, "pids": [900], "message": "started"}

    def restart(self, force: bool = False) -> dict:
        self.restart_calls += 1
        return {"ok": self.restart_ok, "reason": None if self.restart_ok else "test"}


class ResumeQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.old_env = {key: os.environ.get(key) for key in ENV_KEYS}
        self.addCleanup(self._restore_env)
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["CODEX_SWITCHER_DATA"] = str(self.root / "data")
        os.environ["CODEX_SWITCHER_ENABLE_GOAL_RESUME"] = "1"

    def _restore_env(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _service(self, session, desktop, clock=None):
        clock = clock or _FakeClock()
        service = GoalResumeService(
            session_factory=lambda: session,
            goals_provider=lambda: [],
            history_path=self.root / "goal-resume-history.jsonl",
            sessions_provider=lambda **kwargs: {
                "total": 0,
                "pinned": [],
                "projects": [],
                "recent": [],
                "counts": {},
            },
            desktop_client=desktop,
            sleep=clock.sleep,
            clock=clock,
        )
        self.addCleanup(self._join_job, service)
        return service

    @staticmethod
    def _join_job(service):
        thread = service._thread
        if thread is not None:
            thread.join(timeout=5)

    def _wait_job(self, service, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = service.status()["job"]
            if job and job.get("state") != "running":
                return job
            time.sleep(0.02)
        self.fail(f"job did not finish: {service.status()}")

    def test_resume_never_closes_or_starts_the_desktop(self):
        desktop = _FakeDesktop(running=True)
        session = FakeSession(goal_status="usageLimited")
        service = self._service(session, desktop)
        service.start([THREAD], dry_run=False, confirm=True)
        job = self._wait_job(service)

        self.assertEqual(job["state"], "done")
        self.assertEqual(desktop.close_calls, 0)
        self.assertEqual(desktop.start_calls, 0)
        self.assertEqual(desktop.restart_calls, 0)
        self.assertFalse(job["desktop_taken_over"])
        self.assertEqual(job["takeover_threads"], [])
        self.assertIsNone(job["desktop_relaunch"])

    def test_queue_is_woken_through_the_app_deep_link(self):
        """Without a wake the message sits in the queue until someone clicks it."""
        desktop = _FakeDesktop(running=True)
        session = FakeSession(goal_status="usageLimited", queue_consumed=False)
        service = self._service(session, desktop)
        service.start([THREAD], dry_run=False, confirm=True)
        job = self._wait_job(service)

        self.assertEqual(desktop.wake_calls, [THREAD])
        result = job["results"][0]
        self.assertEqual(result["wake"]["url"], f"codex://threads/wake/{THREAD}")
        self.assertTrue(result["delivered"] is False)
        # The dispatch never touches the window itself.
        self.assertEqual(desktop.close_calls, 0)
        self.assertEqual(desktop.start_calls, 0)
        self.assertEqual(desktop.restart_calls, 0)

    def test_wake_is_skipped_when_the_desktop_is_not_running(self):
        desktop = _FakeDesktop(running=False)
        session = FakeSession(goal_status="usageLimited", queue_consumed=False)
        service = self._service(session, desktop)
        service.start([THREAD], dry_run=False, confirm=True)
        job = self._wait_job(service)

        self.assertEqual(desktop.wake_calls, [THREAD])
        result = job["results"][0]
        self.assertTrue(result["wake"]["skipped"])
        self.assertTrue(result["pending"])
        messages = " ".join(event["message"] for event in job["events"])
        self.assertIn("桌面端没在运行", messages)

    def test_wake_failure_does_not_fail_the_dispatch(self):
        desktop = _FakeDesktop(running=True)
        desktop.wake_error = "shell refused"
        session = FakeSession(goal_status="usageLimited", queue_consumed=False)
        service = self._service(session, desktop)
        service.start([THREAD], dry_run=False, confirm=True)
        job = self._wait_job(service)

        self.assertEqual(job["state"], "done")
        result = job["results"][0]
        self.assertFalse(result["wake"]["ok"])
        self.assertIn("shell refused", result["wake"]["error"])
        self.assertIn("thread/queue/add", session.methods())

    def test_wake_can_be_disabled(self):
        os.environ["CODEX_SWITCHER_GOAL_RESUME_WAKE"] = "0"
        desktop = _FakeDesktop(running=True)
        session = FakeSession(goal_status="usageLimited")
        service = self._service(session, desktop)
        service.start([THREAD], dry_run=False, confirm=True)
        job = self._wait_job(service)

        self.assertEqual(desktop.wake_calls, [])
        self.assertEqual(job["results"][0]["wake"]["reason"], "wake_disabled")

    def test_takeover_flag_is_accepted_but_ignored(self):
        desktop = _FakeDesktop(running=True)
        session = FakeSession(goal_status="usageLimited")
        service = self._service(session, desktop)
        service.start([THREAD], dry_run=False, confirm=True, takeover_desktop=True)
        job = self._wait_job(service)

        self.assertEqual(job["state"], "done")
        self.assertEqual(desktop.close_calls, 0)
        messages = " ".join(event["message"] for event in job["events"])
        self.assertIn("takeover_desktop 已不再使用", messages)

    def test_dry_run_ignores_restart_request(self):
        desktop = _FakeDesktop()
        session = FakeSession(goal_status="usageLimited")
        service = self._service(session, desktop)
        service.start([THREAD], dry_run=True, restart_desktop=True, takeover_desktop=True)
        job = self._wait_job(service)

        self.assertEqual(job["state"], "done")
        self.assertEqual(desktop.restart_calls, 0)
        self.assertEqual(desktop.close_calls, 0)
        self.assertNotIn("thread/queue/add", session.methods())

    def test_restart_failure_never_opens_appserver_or_queues(self):
        desktop = _FakeDesktop()
        desktop.restart_ok = False
        session = FakeSession()
        service = self._service(session, desktop)
        service.start([THREAD], dry_run=False, confirm=True, restart_desktop=True)
        job = self._wait_job(service)

        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"], "desktop_restart_failed")
        self.assertEqual(session.methods(), [])

    def test_restart_happens_after_validation_and_job_reservation(self):
        desktop = _FakeDesktop()
        session = FakeSession()
        service = self._service(session, desktop)
        with self.assertRaises(Exception):
            service.start([], dry_run=False, confirm=True, restart_desktop=True)
        with self.assertRaises(Exception):
            service.start([THREAD], dry_run=False, confirm=False, restart_desktop=True)
        self.assertEqual(desktop.restart_calls, 0)

        observed = []

        def factory():
            observed.append((desktop.restart_calls, service.status()["job"]["state"]))
            return session

        service._session_factory = factory
        service.start([THREAD], dry_run=False, confirm=True, restart_desktop=True)
        self._wait_job(service)
        self.assertEqual(observed, [(1, "running")])


if __name__ == "__main__":
    unittest.main()
