"""Login responsiveness without network calls, real accounts or live browser state."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_switcher.login import LoginError, LoginSession, PlaywrightDriver


class LoginResponsivenessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.driver = PlaywrightDriver(Path(temp.name))
        self.session = LoginSession("fixture", "fixture@example.com", Path(temp.name),
                                    login_method="browser", identity_type="team")
        self.page = mock.Mock()
        self.page.url = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
        self.page.is_closed.return_value = False

    def test_visible_focused_page_needs_no_activation_or_fixed_sleep(self):
        self.page.evaluate.return_value = True
        self.driver._ensure_visible(self.page)
        self.page.bring_to_front.assert_not_called()
        self.page.wait_for_timeout.assert_not_called()

    def test_unfocused_or_unreadable_page_still_activates_and_checks_visibility(self):
        for initial in (False, RuntimeError("page navigating")):
            self.page.reset_mock()
            self.page.evaluate.side_effect = [initial, "visible"]
            self.driver._ensure_visible(self.page)
            self.page.bring_to_front.assert_called_once()
            self.page.wait_for_timeout.assert_called_once_with(350)

    def test_hidden_page_still_has_bounded_retries_and_explicit_failure(self):
        self.page.evaluate.side_effect = [False] + ["hidden"] * 8
        with self.assertRaises(LoginError) as error:
            self.driver._ensure_visible(self.page)
        self.assertEqual(error.exception.code, "page_hidden")
        self.assertEqual(self.page.bring_to_front.call_count, 8)

    def test_session_probe_timeout_is_optional_and_preserves_default(self):
        self.page.context.request.get.return_value.json.return_value = {"user": {"email": self.session.email}}
        self.assertEqual(self.driver._get_chatgpt_email(self.page), self.session.email)
        self.page.context.request.get.assert_called_with("https://chatgpt.com/api/auth/session", timeout=10_000)
        self.assertEqual(self.driver._get_chatgpt_email(self.page, timeout_ms=2_000), self.session.email)
        self.page.context.request.get.assert_called_with("https://chatgpt.com/api/auth/session", timeout=2_000)

    def test_oauth_preflight_is_bounded_and_still_clears_wrong_account_cookies(self):
        with mock.patch.object(self.driver, "_goto") as goto, mock.patch.object(self.driver, "_ensure_visible"), \
                mock.patch.object(self.driver, "_get_chatgpt_email", return_value="other@example.com") as probe:
            self.driver._ensure_target_account(self.page, self.session.email, "fixture", "", self.session,
                                               start_url="https://auth.openai.com/oauth/authorize")
        probe.assert_called_once_with(self.page, timeout_ms=2_000)
        self.page.context.clear_cookies.assert_called_once()
        self.assertEqual(goto.call_count, 2)

    def test_cancel_after_session_probe_prevents_cookie_clear_and_more_navigation(self):
        def cancel_during_probe(*args, **kwargs):
            self.session.cancel_event.set()
            return "other@example.com"
        with mock.patch.object(self.driver, "_goto") as goto, mock.patch.object(self.driver, "_ensure_visible"), \
                mock.patch.object(self.driver, "_get_chatgpt_email", side_effect=cancel_during_probe), \
                self.assertRaises(LoginError) as error:
            self.driver._ensure_target_account(self.page, self.session.email, "fixture", "", self.session,
                                               start_url="https://auth.openai.com/oauth/authorize")
        self.assertEqual(error.exception.code, "cancelled")
        self.page.context.clear_cookies.assert_not_called()
        goto.assert_called_once()

    def test_personal_preflight_keeps_original_session_check(self):
        self.session.login_method = "device"
        with mock.patch.object(self.driver, "_goto"), mock.patch.object(self.driver, "_ensure_visible"), \
                mock.patch.object(self.driver, "_get_chatgpt_email", return_value=self.session.email) as probe:
            self.driver._ensure_target_account(self.page, self.session.email, "fixture", "", self.session)
        probe.assert_called_once_with(self.page)

    def test_browser_otp_is_offered_without_blocking_session_request(self):
        self.page.url = "https://auth.openai.com/log-in/mfa"
        self.page.wait_for_timeout.side_effect = lambda ms: self.session.cancel_event.set()
        fields = mock.Mock()
        with mock.patch.object(self.driver, "_ensure_visible"), \
                mock.patch.object(self.driver, "_get_chatgpt_email") as probe, \
                mock.patch.object(self.driver, "_code_form", return_value=("authenticator", 6, "fixture-form", fields)), \
                self.assertRaises(LoginError) as error:
            self.driver._wait_manual_signin(self.page, self.session.email, self.session, "fixture OTP")
        self.assertEqual(error.exception.code, "cancelled")
        probe.assert_not_called()
        self.assertEqual(self.session.code_challenge()["kind"], "authenticator")
        fields.fill.assert_not_called()

    def test_device_manual_login_still_rejects_different_logged_in_account(self):
        self.session.login_method = "device"
        self.page.wait_for_timeout.side_effect = lambda ms: self.session.cancel_event.set()
        with mock.patch.object(self.driver, "_ensure_visible"), \
                mock.patch.object(self.driver, "_get_chatgpt_email", return_value="other@example.com") as probe, \
                mock.patch.object(self.driver, "_code_form") as code_form, \
                self.assertRaises(LoginError) as error:
            self.driver._wait_manual_signin(self.page, self.session.email, self.session, "fixture OTP")
        self.assertEqual(error.exception.code, "cancelled")
        probe.assert_called_once_with(self.page)
        code_form.assert_not_called()
        self.assertIn("其他账号", self.session.message)


if __name__ == "__main__":
    unittest.main()
