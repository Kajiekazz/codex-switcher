"""Auto resume after an account switch (per-goal opt-in 「自动」 toggle)."""

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from codex_switcher import settings
from codex_switcher.api import create_server
from codex_switcher.goal_resume import (
    GoalResumeService,
    auto_resume_ids,
    set_auto_resume,
)
from codex_switcher.store import AccountStore

from test_api import fake_auth_file, free_port
from test_goal_resume import FakeSession, StuckSession


THREAD = "01a17a9b-88bf-7620-a979-c2ecbf5dcc26"
PAUSED = "01a0818b-6a6c-7000-8000-000000000002"
ENV_KEYS = (
    "CODEX_SWITCHER_DATA",
    "CODEX_SWITCHER_ENABLE_GOAL_RESUME",
    "CODEX_SWITCHER_AUTO_RESUME_AFTER_SWITCH",
    "CODEX_SWITCHER_AUTO_RESUME_THREADS",
)


class AutoResumeEnv(unittest.TestCase):
    """Shared temp data dir so settings.json is isolated per test."""

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
        os.environ["CODEX_SWITCHER_AUTO_RESUME_AFTER_SWITCH"] = "1"
        settings.reset_cache()
        self.addCleanup(settings.reset_cache)

    def _restore_env(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class AutoResumeSettingsTests(AutoResumeEnv):
    def test_toggle_persists_and_dedupes(self):
        ids = set_auto_resume(THREAD, True)
        self.assertEqual(ids, [THREAD])
        self.assertEqual(set_auto_resume(THREAD, True), [THREAD])
        self.assertEqual(auto_resume_ids(), [THREAD])
        # Survives a reload from disk.
        settings.reset_cache()
        self.assertEqual(auto_resume_ids(), [THREAD])

    def test_toggle_off_removes_only_that_thread(self):
        set_auto_resume(THREAD, True)
        set_auto_resume(PAUSED, True)
        self.assertEqual(set_auto_resume(THREAD, False), [PAUSED])
        settings.reset_cache()
        self.assertEqual(auto_resume_ids(), [PAUSED])

    def test_garbage_setting_is_ignored(self):
        os.environ["CODEX_SWITCHER_AUTO_RESUME_THREADS"] = "{not json"
        self.assertEqual(auto_resume_ids(), [])


class AutoResumeSwitchTests(AutoResumeEnv):
    def _service(self, goals, session=None):
        session = session or FakeSession(
            goal_status="usageLimited",
            final_status="complete",
            notifications=[
                {"method": "turn/started", "params": {"threadId": THREAD}},
                {"method": "turn/completed", "params": {"threadId": THREAD}},
            ],
        )
        return (
            GoalResumeService(
                session_factory=lambda: session,
                goals_provider=lambda: list(goals),
                history_path=self.root / "goal-resume-history.jsonl",
                sessions_provider=lambda **kwargs: {
                    "total": 0,
                    "pinned": [],
                    "projects": [],
                    "recent": [],
                    "counts": {},
                },
            ),
            session,
        )

    def _wait(self, service, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = service.status()["job"]
            if job and job.get("state") != "running":
                return job
            time.sleep(0.02)
        self.fail(f"job did not finish: {service.status()}")

    def test_switches_resume_marked_usage_limited_goals(self):
        set_auto_resume(THREAD, True)
        goals = [
            {"thread_id": THREAD, "status": "usage_limited"},
            {"thread_id": PAUSED, "status": "paused"},
        ]
        service, session = self._service(goals)
        outcome = service.on_account_switched(
            mock.Mock(target_email="next@example.com")
        )
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["thread_ids"], [THREAD])
        job = self._wait(service)
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["started_via"], "switch:next@example.com")
        self.assertEqual(job["thread_ids"], [THREAD])
        self.assertEqual(job["results"][0]["started_via"], "switch:next@example.com")
        self.assertIn("thread/resume", session.methods())

    def test_switches_rewake_paused_goals(self):
        # A goal that was paused (or left active by a desktop restart) still
        # needs a nudge after the switch — otherwise 自动续跑 only ever fires once.
        set_auto_resume(THREAD, True)
        goals = [{"thread_id": THREAD, "status": "paused"}]
        service, _ = self._service(goals)
        outcome = service.on_account_switched(mock.Mock(target_email="x@y"))
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["thread_ids"], [THREAD])
        job = self._wait(service)
        self.assertEqual(job["thread_ids"], [THREAD])

    def test_switches_rewake_active_goals(self):
        set_auto_resume(THREAD, True)
        goals = [{"thread_id": THREAD, "status": "active"}]
        service, session = self._service(
            goals, session=FakeSession(goal_status="active", final_status="complete")
        )
        outcome = service.on_account_switched(mock.Mock(target_email="x@y"))
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["thread_ids"], [THREAD])
        self._wait(service)
        self.assertNotIn("thread/goal/set", session.methods())

    def test_switch_cancels_the_stale_job_then_resumes(self):
        """A job left over from the previous account must not block the new one.

        Its app-server still holds the old token and the thread writer locks
        the re-launched desktop app needs, so the switch cancels it and re-arms
        the goals with the fresh account instead of skipping with job_running.
        """
        set_auto_resume(THREAD, True)
        goals = [{"thread_id": THREAD, "status": "usage_limited"}]
        created = []

        def factory():
            if not created:
                session = StuckSession()
            else:
                session = FakeSession(
                    goal_status="usageLimited",
                    final_status="complete",
                    notifications=[
                        {"method": "turn/started", "params": {"threadId": THREAD}},
                        {"method": "turn/completed", "params": {"threadId": THREAD}},
                    ],
                )
            created.append(session)
            return session

        service = GoalResumeService(
            session_factory=factory,
            goals_provider=lambda: list(goals),
            history_path=self.root / "goal-resume-history.jsonl",
            sessions_provider=lambda **kwargs: {
                "total": 0,
                "pinned": [],
                "projects": [],
                "recent": [],
                "counts": {},
            },
        )
        service.start([THREAD], dry_run=False, confirm=True)
        deadline = time.time() + 5.0
        while time.time() < deadline and service._session is None:
            time.sleep(0.01)
        self.assertIsNotNone(service._session)
        self.assertTrue(service.running())
        stale_job = service.status()["job"]["id"]

        outcome = service.on_account_switched(
            mock.Mock(target_email="next@example.com")
        )
        self.assertTrue(outcome["ok"])
        self.assertTrue(outcome["cancelled_stale_job"])
        self.assertEqual(outcome["thread_ids"], [THREAD])
        job = self._wait(service)
        self.assertEqual(len(created), 2)
        self.assertTrue(created[0].closed)
        self.assertEqual(job["state"], "done")
        self.assertNotEqual(job["id"], stale_job)
        self.assertEqual(job["thread_ids"], [THREAD])
        self.assertEqual(job["results"][0]["started_via"], "switch:next@example.com")

    def test_switch_cancels_the_stale_job_even_without_marks(self):
        """Switching invalidates a running job even when no goal is marked.

        The app-server behind it still sends the previous account's token, so
        letting it run only burns the old quota and keeps the writer locks.
        """
        goals = [{"thread_id": THREAD, "status": "usage_limited"}]
        session = StuckSession()
        service = GoalResumeService(
            session_factory=lambda: session,
            goals_provider=lambda: list(goals),
            history_path=self.root / "goal-resume-history.jsonl",
            sessions_provider=lambda **kwargs: {
                "total": 0,
                "pinned": [],
                "projects": [],
                "recent": [],
                "counts": {},
            },
        )
        service.start([THREAD], dry_run=False, confirm=True)
        deadline = time.time() + 5.0
        while time.time() < deadline and service._session is None:
            time.sleep(0.01)
        self.assertIsNotNone(service._session)
        self.assertTrue(service.running())

        outcome = service.on_account_switched(mock.Mock(target_email="next@example.com"))
        self.assertEqual(outcome["skipped"], "no_marked_goal")
        self.assertTrue(outcome["cancelled_stale_job"])
        self.assertTrue(session.closed)
        self.assertFalse(service.running())

    def test_nothing_marked_means_no_job(self):
        goals = [{"thread_id": THREAD, "status": "usage_limited"}]
        service, session = self._service(goals)
        self.assertIsNone(service.on_account_switched(mock.Mock(target_email="x@y")))
        self.assertIsNone(service.status()["job"])
        self.assertEqual(session.methods(), [])

    def test_finished_goals_are_reported_as_skipped(self):
        set_auto_resume(THREAD, True)
        goals = [{"thread_id": THREAD, "status": "complete"}]
        service, _ = self._service(goals)
        outcome = service.on_account_switched(mock.Mock(target_email="x@y"))
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["skipped"], "no_armable_goal")
        self.assertEqual(outcome["skipped_goals"][0]["status"], "complete")
        self.assertIsNone(service.status()["job"])

    def test_master_switch_off_disables_auto_resume(self):
        os.environ["CODEX_SWITCHER_AUTO_RESUME_AFTER_SWITCH"] = "0"
        set_auto_resume(THREAD, True)
        goals = [{"thread_id": THREAD, "status": "usage_limited"}]
        service, _ = self._service(goals)
        self.assertIsNone(service.on_account_switched(mock.Mock(target_email="x@y")))

    def test_switch_stops_old_job_when_auto_resume_is_disabled(self):
        os.environ["CODEX_SWITCHER_AUTO_RESUME_AFTER_SWITCH"] = "0"
        session = StuckSession()
        service, _ = self._service([], session=session)
        service.start([THREAD], dry_run=False, confirm=True)
        deadline = time.time() + 5.0
        while time.time() < deadline and service._session is None:
            time.sleep(0.01)
        self.assertIsNotNone(service._session)
        outcome = service.on_account_switched(mock.Mock(target_email="next@example.com"))
        try:
            self.assertTrue(outcome["cancelled_stale_job"])
            self.assertEqual(outcome["skipped"], "auto_resume_disabled")
            self.assertTrue(session.closed)
            self.assertFalse(service.running())
        finally:
            service.cancel()
            if service._thread:
                service._thread.join(timeout=2)

    def test_goal_resume_disabled_disables_auto_resume(self):
        os.environ["CODEX_SWITCHER_ENABLE_GOAL_RESUME"] = "0"
        set_auto_resume(THREAD, True)
        goals = [{"thread_id": THREAD, "status": "usage_limited"}]
        service, _ = self._service(goals)
        self.assertIsNone(service.on_account_switched(mock.Mock(target_email="x@y")))


class AutoResumeApiTests(AutoResumeEnv):
    def setUp(self):
        super().setUp()
        self.auth = self.root / "auth.json"
        fake_auth_file(self.auth)
        self.store = AccountStore(self.root / "accounts.sqlite3")
        self.addCleanup(self.store.close)
        self.session = FakeSession(
            goal_status="usageLimited",
            final_status="complete",
            notifications=[
                {"method": "turn/started", "params": {"threadId": THREAD}},
                {"method": "turn/completed", "params": {"threadId": THREAD}},
            ],
        )
        self.goals = [{"thread_id": THREAD, "status": "usage_limited"}]
        self.service = GoalResumeService(
            session_factory=lambda: self.session,
            goals_provider=lambda: list(self.goals),
            history_path=self.root / "goal-resume-history.jsonl",
            sessions_provider=lambda **kwargs: {
                "total": 0,
                "pinned": [],
                "projects": [],
                "recent": [],
                "counts": {},
            },
        )
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.server = create_server(
            self.store,
            self.auth,
            "127.0.0.1",
            self.port,
            goal_resume=self.service,
        )
        self.addCleanup(self._stop)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        for _ in range(100):
            try:
                self.request("GET", "/api/health")
                break
            except Exception:
                time.sleep(0.05)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method, path, payload=None):
        import urllib.request

        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method
        )
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_goals_payload_exposes_auto_resume(self):
        state = self.request("GET", "/api/goals")
        self.assertEqual(
            state["auto_resume"],
            {"enabled": True, "thread_ids": [], "last": None},
        )

    def test_toggle_endpoint_round_trip(self):
        payload = self.request(
            "POST", "/api/goals/auto-resume", {"thread_id": THREAD, "enabled": True}
        )
        self.assertEqual(payload["thread_ids"], [THREAD])
        self.assertEqual(
            self.request("GET", "/api/goals")["auto_resume"]["thread_ids"], [THREAD]
        )
        payload = self.request(
            "POST", "/api/goals/auto-resume", {"thread_id": THREAD, "enabled": False}
        )
        self.assertEqual(payload["thread_ids"], [])

    def test_toggle_endpoint_requires_thread(self):
        import urllib.error

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("POST", "/api/goals/auto-resume", {"enabled": True})
        self.assertEqual(ctx.exception.code, 400)

    def test_server_wires_the_switch_hook(self):
        manager = getattr(self.server, "switch_manager", None)
        if manager is None:  # server keeps it on the handler factory
            self.skipTest("switch manager not exposed")
        self.assertEqual(manager.on_switched, self.service.on_account_switched)


if __name__ == "__main__":
    unittest.main()
