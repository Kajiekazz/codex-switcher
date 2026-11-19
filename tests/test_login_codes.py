"""Verification input belongs to one account, task and current DOM challenge."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_switcher.login import LoginError, LoginManager, LoginSession, PlaywrightDriver


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


class DeviceVerificationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.session = LoginSession("verify", "member@example.com", self.root, driver="playwright")
        self.driver = PlaywrightDriver(self.root)
        self.page = mock.Mock()
        self.page.url = "https://auth.openai.com/email-verification"
        self.page.is_closed.return_value = False

    def test_localized_email_challenge_is_recognized_without_email_word(self):
        fields = self.page.locator.return_value
        fields.count.return_value = 1
        fields.first.is_enabled.return_value = True
        fields.first.get_attribute.return_value = "6"
        fields.first.evaluate.return_value = "current-form"
        with mock.patch.object(self.driver, "_body_text", return_value="检查你的收件箱。输入六位验证码"):
            descriptor = self.driver._code_form(self.page)
        self.assertEqual(descriptor[:2], ("email", 6))
        self.page.url = "https://example.com/email-verification"
        self.assertIsNone(self.driver._code_form(self.page))

    def test_device_authorization_offers_code_before_consulting_other_browser_session(self):
        fields = mock.Mock()
        self.page.wait_for_timeout.side_effect = lambda ms: self.session.cancel_event.set()
        with mock.patch.object(self.driver, "_ensure_visible"), \
                mock.patch.object(self.driver, "_get_chatgpt_email") as probe, \
                mock.patch.object(self.driver, "_code_form", return_value=("email", 6, "current-form", fields)), \
                self.assertRaises(LoginError) as error:
            self.driver._wait_manual_signin(self.page, self.session.email, self.session, "验证", authorization_step=True)
        self.assertEqual(error.exception.code, "cancelled")
        probe.assert_not_called()
        self.assertEqual(self.session.code_challenge()["kind"], "email")

    def test_completed_email_verification_returns_to_device_code_authorization(self):
        self.page.url = "https://auth.openai.com/deviceauth/callback"
        self.page.locator.return_value.count.return_value = 9
        with mock.patch.object(self.driver, "_ensure_visible"), \
                mock.patch.object(self.driver, "_code_form", return_value=None), \
                mock.patch.object(self.driver, "_get_chatgpt_email") as probe:
            self.driver._wait_manual_signin(self.page, self.session.email, self.session, "验证", authorization_step=True)
        probe.assert_not_called()
        self.assertFalse(self.session.needs_input)

    def test_device_flow_waits_for_extra_verification_then_continues_with_same_request(self):
        states = {"verified": False}
        self.page.wait_for_timeout.return_value = None
        def handoff(page, email, session, reason, *, authorization_step):
            self.assertTrue(authorization_step)
            self.assertEqual(email, self.session.email)
            states["verified"] = True
        with mock.patch.object(self.driver, "_goto"), mock.patch.object(self.driver, "_ensure_visible"), \
                mock.patch.object(self.driver, "_retry_error_page", return_value=False), \
                mock.patch.object(self.driver, "_handle_phone_verification", return_value=False), \
                mock.patch.object(self.driver, "_body_text", return_value="检查你的收件箱"), \
                mock.patch.object(self.driver, "_code_form", return_value=("email", 6, "current-form", mock.Mock())), \
                mock.patch.object(self.driver, "_device_flow_done", side_effect=lambda page, submitted: states["verified"]), \
                mock.patch.object(self.driver, "_wait_manual_signin", side_effect=handoff) as signin:
            self.driver._approve_device_code(self.page, self.session.email, "https://auth.openai.com/codex/device", "fixture", self.session)
        signin.assert_called_once()
        self.assertEqual(self.session.phase, "authorizing_device")

    def test_closed_verification_window_has_actionable_error_instead_of_raw_playwright_text(self):
        self.page.is_closed.return_value = True
        self.driver._page = self.page
        with mock.patch.object(self.driver, "_start"), mock.patch.object(self.driver, "_close"), \
                mock.patch.object(self.driver, "_ensure_visible", side_effect=RuntimeError("Target page, context or browser has been closed")), \
                self.assertRaises(LoginError) as error:
            self.driver.complete_login(email=self.session.email, password="fixture", totp="", device_url="https://auth.openai.com/codex/device", device_code="fixture", session=self.session)
        self.assertEqual(error.exception.code, "browser_closed")
        self.assertIn("验证窗口已关闭", error.exception.message)
