"""Account status uses current authorization while retaining failed attempts."""
import unittest
from unittest import mock

from codex_switcher.api import _login_overlay


class LoginStatusTests(unittest.TestCase):
    def setUp(self):
        self.session = {"id": "attempt", "email": "member@example.com", "status": "failed",
                        "updated_at": 100, "message": "previous login failed", "error": "browser_closed"}
        self.profile = {"profile_id": "profile-personal", "saved_at": "1970-01-01T00:02:00+00:00",
                        "expired": False, "identity_error": None}
        self.account = {"email": self.session["email"], "profiles": [self.profile]}
        self.manager = mock.Mock()
        self.manager.list.return_value = [self.session]
        self.manager.active_map.return_value = {}
        self.manager.queue_info.return_value = []

    def overlay(self):
        return _login_overlay(self.manager, [self.account])

    def test_later_authorization_supersedes_failure_without_deleting_history(self):
        self.assertEqual(self.overlay(), {})
        self.assertEqual(self.session["status"], "failed")
        self.assertEqual(self.manager.list()[0]["id"], "attempt")

    def test_newer_failure_and_invalid_authorization_remain_visible(self):
        for values in ({"saved_at": "1970-01-01T00:01:30+00:00"}, {"expired": True},
                       {"identity_error": "workspace_identity_conflict"}, {"saved_at": "invalid"}):
            with self.subTest(values=values):
                self.account["profiles"] = [{**self.profile, **values}]
                self.assertEqual(self.overlay()[self.session["email"]]["state"], "failed")

    def test_sibling_workspace_and_totp_operation_do_not_hide_failures(self):
        self.session["profile_id"] = "profile-team"
        self.assertIn(self.session["email"], self.overlay())
        self.session["profile_id"] = "profile-personal"
        self.session["purpose"] = "totp_setup"
        self.assertIn(self.session["email"], self.overlay())

    def test_multiple_workspaces_require_the_failed_attempts_exact_profile(self):
        self.account["profiles"].append({**self.profile, "profile_id": "profile-team"})
        self.assertIn(self.session["email"], self.overlay())
        self.session["profile_id"] = "profile-personal"
        self.assertEqual(self.overlay(), {})

    def test_active_or_queued_login_stays_visible_after_credentials_exist(self):
        active = {**self.session, "status": "waiting_for_input", "device_code": None, "profile_id": "profile-personal"}
        self.manager.active_map.return_value = {"profile-personal": active}
        self.assertEqual(self.overlay()[self.session["email"]]["state"], "waiting_for_input")
        self.manager.active_map.return_value = {}
        self.manager.queue_info.return_value = [{"email": self.session["email"], "position": 1,
                                                 "profile_id": "profile-personal", "session_id": "queued"}]
        self.assertEqual(self.overlay()[self.session["email"]]["state"], "queued")
