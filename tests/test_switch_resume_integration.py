"""Exercise the complete HTTP switch -> verified auth -> auto-resume path.

All credentials and settings are disposable; sessions and desktop are doubles.
"""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from codex_switcher import settings
from codex_switcher.api import create_server
from codex_switcher.goal_resume import GoalResumeService, set_auto_resume
from codex_switcher.store import AccountStore
from test_api import _FakeDesktop, _FakeQuotaService
from test_goal_resume import FakeSession, THREAD
from test_switch import make_auth


class SwitchResumeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = mock.patch.dict(os.environ, {
            "CODEX_SWITCHER_DATA": str(self.root / "data"),
            "CODEX_SWITCHER_AUTH": str(self.root / "auth.json"),
            "CODEX_SWITCHER_STATE_DB": str(self.root / "state.sqlite"),
            "CODEX_SWITCHER_GOALS_DB": str(self.root / "goals.sqlite"),
            "CODEX_SWITCHER_QUEUE_DB": str(self.root / "queue_1.sqlite"),
            "CODEX_SWITCHER_ENABLE_SWITCH": "1",
            "CODEX_SWITCHER_ENABLE_GOAL_RESUME": "1",
            "CODEX_SWITCHER_AUTO_RESUME_AFTER_SWITCH": "1",
            "CODEX_SWITCHER_AUTO_RESUME_THREADS": "[]",
            "CODEX_SWITCHER_ENABLE_AUTO_SWITCH": "0",
            "CODEX_SWITCHER_ENABLE_LOGIN": "0",
            "CODEX_SWITCHER_RESTART_DESKTOP": "0",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        settings.reset_cache()
        self.addCleanup(settings.reset_cache)
        self.auth = self.root / "auth.json"
        make_auth(self.auth, "before@example.com", "before")
        self.target = make_auth(self.root / "target.json", "after@example.com", "after")
        self.store = AccountStore(self.root / "accounts.sqlite")
        self.addCleanup(self.store.close)
        self.store.save_auth_snapshot("after@example.com", self.target, {
            "email": "after@example.com", "plan": "plus", "account_id": "after",
        })
        self.session = FakeSession(notifications=[
            {"method": "turn/started", "params": {"threadId": THREAD}},
            {"method": "turn/completed", "params": {
                "threadId": THREAD, "turn": {"id": "turn-test", "status": "completed", "error": None},
            }},
        ])
        self.factory = mock.Mock(return_value=self.session)
        self.desktop = _FakeDesktop()
        self.service = GoalResumeService(
            session_factory=self.factory,
            goals_provider=lambda: [{"thread_id": THREAD, "status": "usage_limited"}],
            sessions_provider=lambda **kwargs: {"total": 0},
            desktop_client=self.desktop,
            history_path=self.root / "history.jsonl",
        )
        self.server = create_server(
            self.store, self.auth, "127.0.0.1", 0,
            goal_resume=self.service, desktop_client=self.desktop,
            quota_service=_FakeQuotaService(),
        )
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop_server)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def stop_server(self):
        self.service.cancel()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=3)
        deadline = time.monotonic() + 3
        while self.service.running() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.service.running())

    def request(self, path, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.base + path, data=body,
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    def switch(self):
        response = self.request("/api/switch", {
            "email": "after@example.com", "confirm": True, "restart_desktop": False,
        })
        self.assertTrue(response["ok"])
        self.assertEqual(self.auth.read_bytes(), self.target)
        self.assertEqual(self.desktop.restart_calls, [])
        return response["switch"]["auto_resume"]

    def test_switch_returns_and_runs_the_marked_goal(self):
        set_auto_resume(THREAD, True)
        outcome = self.switch()
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["thread_ids"], [THREAD])
        deadline = time.monotonic() + 3
        while self.service.running() and time.monotonic() < deadline:
            time.sleep(0.01)
        state = self.request("/api/goals")
        self.assertEqual(state["session_api_version"], 3)
        self.assertEqual(state["job"]["id"], outcome["job"]["id"])
        self.assertEqual(state["job"]["state"], "done")
        self.assertTrue(state["job"]["results"][0]["ok"])
        self.assertIn("thread/queue/add", self.session.methods())
        self.assertNotIn("thread/resume", self.session.methods())
        self.assertEqual(state["auto_resume"]["last"]["job"]["id"], outcome["job"]["id"])

    def test_switch_without_marked_goals_returns_reason_without_starting_session(self):
        outcome = self.switch()
        self.assertEqual(outcome["skipped"], "no_marked_goal")
        self.factory.assert_not_called()
        self.assertIsNone(self.request("/api/goals")["job"])

    def test_switch_with_master_disabled_returns_reason_without_starting_session(self):
        set_auto_resume(THREAD, True)
        os.environ["CODEX_SWITCHER_AUTO_RESUME_AFTER_SWITCH"] = "0"
        outcome = self.switch()
        self.assertEqual(outcome["skipped"], "auto_resume_disabled")
        self.factory.assert_not_called()
        self.assertEqual(self.request("/api/goals")["auto_resume"]["last"]["skipped"], "auto_resume_disabled")


if __name__ == "__main__":
    unittest.main()
