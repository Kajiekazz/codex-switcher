"""Resume takeover: close the desktop app when it holds the thread (L2).

The desktop keeps a writer lock on the conversation open in its UI. A CLI-side
``thread/resume`` of that thread is refused with "already has an active writer"
— which is not a quota problem. The resume job closes the app, retries until the
lock is released, and reopens the app when the job ends.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from codex_switcher.appserver import AppServerError
from codex_switcher.goal_resume import GoalResumeService

from test_goal_resume import FakeSession

THREAD = "01a17a9b-88bf-7620-a979-c2ecbf5dcc26"
OTHER = "01a18bc8-46b4-7650-8ad7-904edfa97117"

ENV_KEYS = (
    "CODEX_SWITCHER_ENABLE_GOAL_RESUME",
    "CODEX_SWITCHER_GOAL_RESUME_TAKEOVER",
    "CODEX_SWITCHER_GOAL_RESUME_TAKEOVER_WAIT",
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
    """Records close/start calls; ``running`` flips like the real process tree."""

    def __init__(self, running: bool = True, close_ok: bool = True) -> None:
        self.running = running
        self.close_ok = close_ok
        self.status_calls = 0
        self.close_calls = 0
        self.start_calls = 0

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

    def close_app(self, force: bool = False) -> dict:
        self.close_calls += 1
        if not force:
            raise AssertionError("takeover must force the close")
        if not self.close_ok:
            return {
                "ok": False,
                "skipped": False,
                "reason": "still_running",
                "previous_pids": [111],
                "pids": [111],
                "killed": [],
                "message": "桌面端仍在运行",
            }
        self.running = False
        return {
            "ok": True,
            "skipped": False,
            "previous_pids": [111],
            "pids": [],
            "killed": [111, 112],
            "message": "桌面端已关闭，续跑结束后会自动打开。",
        }

    def start(self, before: dict | None = None) -> dict:
        self.start_calls += 1
        self.running = True
        return {
            "ok": True,
            "skipped": False,
            "launched": True,
            "previous_pids": [],
            "pids": [900],
            "message": "桌面端原本没在运行，已直接启动。",
        }


class _StickyDesktop(_FakeDesktop):
    """The user keeps reopening the app, so it always reports running."""

    def status(self) -> dict:
        self.running = True
        payload = super().status()
        payload["running"] = True
        payload["pids"] = [111]
        return payload


class _BusySession(FakeSession):
    """``thread/resume`` is refused with the desktop's "active writer" error."""

    def __init__(self, busy_calls: int = 1, stubborn: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.busy_calls = busy_calls
        self.stubborn = stubborn
        self.resume_attempts = 0

    def request(self, method, params=None, timeout=30.0):
        if method == "thread/resume":
            self.resume_attempts += 1
            if self.stubborn or self.busy_calls > 0:
                self.busy_calls = max(0, self.busy_calls - 1)
                self.calls.append((method, params))
                raise AppServerError(
                    f"thread/resume: thread {params['threadId']} "
                    "already has an active writer"
                )
        return super().request(method, params, timeout)


class ResumeTakeoverTests(unittest.TestCase):
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
        return GoalResumeService(
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
            sleep=(clock.sleep if clock else None) or (lambda _s: None),
            clock=clock,
        )

    def _wait_job(self, service, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = service.status()["job"]
            if job and job.get("state") != "running":
                return job
            time.sleep(0.02)
        self.fail(f"job did not finish: {service.status()}")

    def _messages(self, job):
        return [event["message"] for event in job.get("events") or []]

    def test_busy_thread_closes_desktop_and_resumes(self):
        desktop = _FakeDesktop(running=True)
        session = _BusySession(
            busy_calls=1,
            goal_status="usageLimited",
            final_status="complete",
            notifications=[
                {"method": "turn/started", "params": {"threadId": THREAD}},
                {"method": "turn/completed", "params": {"threadId": THREAD}},
            ],
        )
        service = self._service(session, desktop)
        service.start([THREAD], dry_run=False, confirm=True)
        job = self._wait_job(service)

        self.assertEqual(job["state"], "done")
        self.assertTrue(job["results"][0]["ok"], job["results"])
        self.assertEqual(desktop.close_calls, 1)
        self.assertEqual(desktop.start_calls, 1)
        self.assertTrue(job["desktop_taken_over"])
        self.assertEqual(job["takeover_threads"], [THREAD])
        self.assertTrue(job["desktop_relaunch"]["ok"])
        self.assertTrue(desktop.running)
        self.assertGreaterEqual(session.resume_attempts, 2)
        messages = " ".join(self._messages(job))
        self.assertIn("被桌面端占用", messages)
        self.assertIn("接管成功", messages)
        self.assertIn("重新打开桌面端", messages)

        # The takeover survives a restart in the persisted history.
        history = service.history()[0]
        self.assertTrue(history["desktop_taken_over"])
        self.assertEqual(history["takeover_threads"], [THREAD])

    def test_terminal_cli_occupant_is_not_taken_over(self):
        desktop = _FakeDesktop(running=False)
        session = _BusySession(stubborn=True, goal_status="usageLimited")
        service = self._service(session, desktop)
        service.start([THREAD], dry_run=False, confirm=True)
        job = self._wait_job(service)

        result = job["results"][0]
        self.assertFalse(result["ok"])
        self.assertIn("active writer", result["error"])
        self.assertEqual(desktop.close_calls, 0)
        self.assertEqual(desktop.start_calls, 0)
        self.assertFalse(job["desktop_taken_over"])
        self.assertIn("桌面端没在运行", " ".join(self._messages(job)))

    def test_takeover_disabled_still_reports_busy(self):
        os.environ["CODEX_SWITCHER_GOAL_RESUME_TAKEOVER"] = "0"
        desktop = _FakeDesktop(running=True)
        session = _BusySession(stubborn=True, goal_status="usageLimited")
        service = self._service(session, desktop)
        service.start([THREAD], dry_run=False, confirm=True)
        job = self._wait_job(service)

        self.assertFalse(job["results"][0]["ok"])
        self.assertEqual(desktop.close_calls, 0)
        self.assertFalse(job["desktop_taken_over"])
        self.assertIn("自动接管已关闭", " ".join(self._messages(job)))

    def test_close_failure_keeps_the_busy_error(self):
        desktop = _FakeDesktop(running=True, close_ok=False)
        session = _BusySession(stubborn=True, goal_status="usageLimited")
        service = self._service(session, desktop)
        service.start([THREAD], dry_run=False, confirm=True)
        job = self._wait_job(service)

        self.assertFalse(job["results"][0]["ok"])
        self.assertEqual(desktop.close_calls, 1)
        self.assertFalse(job["desktop_taken_over"])
        # The app was never closed, so it must not be "reopened".
        self.assertEqual(desktop.start_calls, 0)
        self.assertIn("关闭桌面端失败", " ".join(self._messages(job)))

    def test_writer_never_frees_gives_up_after_the_wait(self):
        clock = _FakeClock()
        os.environ["CODEX_SWITCHER_GOAL_RESUME_TAKEOVER_WAIT"] = "5"
        desktop = _FakeDesktop(running=True)
        session = _BusySession(stubborn=True, goal_status="usageLimited")
        service = self._service(session, desktop, clock=clock)
        service.start([THREAD], dry_run=False, confirm=True)
        job = self._wait_job(service)

        self.assertFalse(job["results"][0]["ok"])
        self.assertIn("active writer", job["results"][0]["error"])
        # First attempt + one per second until the 5 s budget runs out.
        self.assertLessEqual(session.resume_attempts, 8)
        self.assertEqual(desktop.close_calls, 1)

    def test_takeover_is_capped_per_job(self):
        clock = _FakeClock()
        os.environ["CODEX_SWITCHER_GOAL_RESUME_TAKEOVER_WAIT"] = "3"
        desktop = _StickyDesktop(running=True)
        session = _BusySession(
            stubborn=True,
            goal_status="usageLimited",
            final_status="usageLimited",
        )
        service = self._service(session, desktop, clock=clock)
        ids = [f"01a17a9b-88bf-7620-a979-c2ecbf5dcc{index:02d}" for index in range(5)]
        service.start(ids, dry_run=False, confirm=True)
        job = self._wait_job(service)

        # Every thread is busy, so the app is closed for the first three and
        # then left alone — a failing job must not keep killing the user's UI.
        self.assertEqual(desktop.close_calls, 3)
        self.assertTrue(job["desktop_taken_over"])
        self.assertIn("已接管 3 次", " ".join(self._messages(job)))
        self.assertTrue(all(not item["ok"] for item in job["results"]))


if __name__ == "__main__":
    unittest.main()
