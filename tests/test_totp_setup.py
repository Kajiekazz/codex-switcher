"""Provisioning parsing, encrypted staging and manual handoff invariants."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from codex_switcher import settings
from codex_switcher.login import LoginError, LoginSession, ManualDriver, PlaywrightDriver
from codex_switcher.store import AccountStore
from codex_switcher.totp_setup import TotpSetupError, decode_binding_image, provisioning_secret

SEED = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # RFC 6238, never an account credential.
URI = "otpauth://totp/OpenAI:fixture@example.com?secret=" + SEED + "&issuer=OpenAI"
FIXTURE = Path(__file__).parent / "fixtures" / "totp-setup" / "binding.png"


class ProvisioningTests(unittest.TestCase):
    def test_qr_and_manual_seed_are_compatible_with_the_existing_generator(self):
        self.assertEqual(provisioning_secret(URI, "fixture@example.com"), SEED)
        self.assertEqual(provisioning_secret(SEED, "fixture@example.com"), SEED)
        self.assertEqual(decode_binding_image(FIXTURE.read_bytes(), "fixture@example.com"), SEED)

    def test_wrong_account_hotp_unsupported_parameters_and_temporary_codes_are_rejected(self):
        invalid = [URI.replace("fixture@example.com", "other@example.com"), URI.replace("/totp/", "/hotp/"),
                   URI + "&digits=8", URI + "&period=60", URI + "&algorithm=SHA256", URI.replace("issuer=OpenAI", "issuer=Other"), "234567", "not a seed"]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(TotpSetupError) as error:
                provisioning_secret(value, "fixture@example.com")
            self.assertNotIn(SEED, str(error.exception))

    def test_invalid_image_has_a_safe_error_without_returning_provisioning_data(self):
        with self.assertRaisesRegex(TotpSetupError, "手动"):
            decode_binding_image(b"not an image", "fixture@example.com")


class TotpStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "accounts.sqlite3"
        self.store = AccountStore(self.path)
        self.addCleanup(lambda: self.store.close())
        self.store.upsert({"email": "fixture@example.com", "password": "fixture-password", "note": "keep", "priority": 345})

    def test_unconfirmed_seed_survives_restart_encrypted_without_claiming_configured(self):
        self.store.stage_totp("fixture@example.com", SEED)
        self.assertFalse(self.store.get("fixture@example.com")["has_totp"])
        self.assertEqual(self.store.get_secret("fixture@example.com")["totp"], "")
        self.store.close()
        self.assertNotIn(SEED.encode(), self.path.read_bytes())
        self.store = AccountStore(self.path)
        self.assertEqual(self.store.pending_totp("fixture@example.com"), SEED)
        self.store.confirm_totp("fixture@example.com")
        account = self.store.get("fixture@example.com")
        self.assertTrue(account["has_totp"])
        self.assertEqual((account["note"], account["priority"]), ("keep", 345))
        self.assertEqual(self.store.get_secret("fixture@example.com"), {"password": "fixture-password", "totp": SEED})
        self.assertIsNone(self.store.pending_totp("fixture@example.com"))
        self.assertNotIn(SEED, json.dumps(self.store.list()))

    def test_existing_saved_seed_cannot_be_overwritten_by_automation(self):
        self.store.import_totp("fixture@example.com", SEED)
        with self.assertRaises(ValueError):
            self.store.stage_totp("fixture@example.com", "JBSWY3DPEHPK3PXP")
        self.assertEqual(self.store.get_secret("fixture@example.com")["totp"], SEED)

    def test_manual_import_and_account_deletion_clear_unconfirmed_candidates(self):
        self.store.stage_totp("fixture@example.com", SEED)
        self.store.import_totp("fixture@example.com", "JBSWY3DPEHPK3PXP")
        self.assertIsNone(self.store.pending_totp("fixture@example.com"))
        self.store.update_account("fixture@example.com", totp="")
        self.store.stage_totp("fixture@example.com", SEED)
        self.store.delete("fixture@example.com")
        self.assertIsNone(self.store.pending_totp("fixture@example.com"))


class ManualHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.session = LoginSession("fixture", "fixture@example.com", Path(self.temp.name))
        self.driver = PlaywrightDriver(Path(self.temp.name) / "profile")
        self.page = mock.Mock()
        self.page.is_closed.return_value = False

    def test_waiting_for_missing_totp_keeps_the_browser_and_automatically_continues(self):
        changes = []
        self.session.on_change = lambda session: changes.append((session.status, session.needs_input))
        with mock.patch.object(self.driver, "_get_chatgpt_email", side_effect=[None, "fixture@example.com"]), mock.patch.object(self.driver, "_ensure_visible"):
            self.driver._wait_manual_signin(self.page, self.session.email, self.session, "请填写验证码")
        self.assertEqual(changes, [("waiting_for_input", True), ("waiting_for_browser", False)])
        self.page.close.assert_not_called()

    def test_wrong_account_does_not_satisfy_manual_verification(self):
        def poll(page):
            self.session.cancel_event.set()
            return "other@example.com"
        with mock.patch.object(self.driver, "_get_chatgpt_email", side_effect=poll), mock.patch.object(self.driver, "_ensure_visible"):
            with self.assertRaises(LoginError) as error:
                self.driver._wait_manual_signin(self.page, self.session.email, self.session, "请核对账号")
        self.assertEqual(error.exception.code, "cancelled")

    def test_cancel_timeout_and_closed_window_are_terminal(self):
        for code in ("cancelled", "manual_verification_timeout", "browser_closed"):
            self.session.cancel_event.clear()
            self.page.is_closed.return_value = code == "browser_closed"
            if code == "cancelled": self.session.cancel_event.set()
            deadline = time.monotonic() - 1 if code == "manual_verification_timeout" else time.monotonic() + 100
            with self.subTest(code=code), self.assertRaises(LoginError) as error:
                self.driver._manual_check(self.page, self.session, deadline)
            self.assertEqual(error.exception.code, code)

    def test_already_cancelled_totp_setup_never_opens_a_window_or_submits(self):
        self.session.cancel_event.set()
        store = mock.Mock()
        with mock.patch.object(self.driver, "_start") as start:
            with self.assertRaises(LoginError) as error:
                self.driver.configure_totp(email=self.session.email, password="", totp="", session=self.session, store=store)
        self.assertEqual(error.exception.code, "cancelled")
        start.assert_not_called()
        store.stage_totp.assert_not_called()

    def test_missing_password_handoff_can_return_to_the_remaining_automatic_steps(self):
        with mock.patch.object(self.driver, "_ensure_target_account", side_effect=LoginError("password_missing", "请手动登录")), mock.patch.object(self.driver, "_wait_manual_signin") as handoff:
            self.driver._signin_or_manual(self.page, self.session.email, "", "", self.session)
        handoff.assert_called_once()

    def test_device_approval_failure_waits_for_authorization_instead_of_repeating_signin(self):
        self.driver._page = self.page
        with mock.patch.object(self.driver, "_start"), mock.patch.object(self.driver, "_ensure_visible"), mock.patch.object(self.driver, "_signin_or_manual"), mock.patch.object(self.driver, "_approve_device_code", side_effect=LoginError("ui_changed", "fixture")), mock.patch.object(self.driver, "_wait_manual_authorization") as authorize, mock.patch.object(self.driver, "_wait_manual_signin") as signin, mock.patch.object(self.driver, "_close"):
            self.driver.complete_login(email=self.session.email, password="", totp="", device_url="https://auth.openai.com/codex/device", device_code="ABCD-EFGH", session=self.session)
        authorize.assert_called_once()
        signin.assert_not_called()

    def test_manual_driver_opens_the_default_browser_and_reports_a_manual_wait(self):
        with mock.patch("webbrowser.open", return_value=True) as open_browser:
            ManualDriver().complete_login(email=self.session.email, password="", totp="", device_url="https://auth.openai.com/codex/device", device_code="ABCD-EFGH", session=self.session)
        self.assertEqual(self.session.status, "waiting_for_input")
        self.assertTrue(self.session.needs_input)
        open_browser.assert_called_once_with("https://auth.openai.com/codex/device", new=2)
