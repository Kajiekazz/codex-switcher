"""Verification input belongs to one account, task and current DOM challenge."""
import json
from pathlib import Path
import tempfile
import unittest

from codex_switcher.login import LoginError, LoginManager, LoginSession


class VerificationInputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = LoginSession("login-1", "one@example.com", self.root, driver="playwright")
        self.session.waiting_for_input("等待邮箱验证码")
        self.session.offer_code("email", 6, "document-1")

    def submit(self, **overrides):
        args = dict(challenge_id=self.session.challenge["id"], email=self.session.email, code="123456")
        args.update(overrides)
        self.session.submit_code(**args)

    def test_code_is_private_and_consumed_only_once(self):
        self.submit()
        self.assertTrue(self.session.to_public()["challenge"]["submitting"])
        self.assertNotIn("123456", json.dumps(self.session.to_public()))
        self.assertNotIn("document-1", json.dumps(self.session.to_public()))
        self.assertEqual(self.session.take_code()[1], "123456")
        self.assertIsNone(self.session.take_code())

    def test_rejects_other_account_stale_step_and_wrong_length(self):
        for values in ({"email": "two@example.com"}, {"challenge_id": "old"}, {"code": "12345"}, {"code": 123456}, {"code": "１２３４５６"}):
            with self.subTest(values=values), self.assertRaises(LoginError):
                self.submit(**values)
        self.assertIsNone(self.session.take_code())

    def test_double_submission_is_rejected(self):
        self.submit()
        with self.assertRaises(LoginError) as raised:
            self.submit()
        self.assertEqual(raised.exception.code, "verification_busy")

    def test_new_step_invalidates_pending_input(self):
        old = self.session.challenge["id"]
        self.submit()
        self.session.offer_code("authenticator", 6, "document-2")
        self.assertIsNone(self.session.take_code())
        with self.assertRaises(LoginError):
            self.submit(challenge_id=old)
        self.assertEqual(self.session.challenge["kind"], "authenticator")

    def test_retry_has_a_new_id_and_keeps_task_waiting(self):
        old = self.session.challenge["id"]
        self.session.offer_code("email", 6, "document-1", "验证码已过期")
        self.assertNotEqual(old, self.session.challenge["id"])
        self.assertEqual(self.session.status, "waiting_for_input")
        self.assertTrue(self.session.needs_input)

    def test_finished_and_cancelled_tasks_reject_input(self):
        self.session.cancel_event.set()
        with self.assertRaises(LoginError):
            self.submit()
        self.session.cancel_event.clear()
        self.session.input_finished()
        self.assertIsNone(self.session.challenge)
        with self.assertRaises(LoginError):
            self.session.submit_code("old", self.session.email, "123456")

    def test_pending_verification_is_not_written_to_history(self):
        manager = LoginManager(None, sessions_dir=self.root / "sessions", history_path=self.root / "history.json")
        manager._sessions[self.session.id] = self.session
        self.submit()
        manager._persist()
        saved = manager.history_path.read_text(encoding="utf-8")
        self.assertNotIn("123456", saved)
        self.assertNotIn('"challenge"', saved)
        self.assertNotIn("document-1", saved)
