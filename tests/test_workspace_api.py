"""Workspace API contracts, using only a temporary service and fake upstreams."""
import unittest
from unittest import mock

import test_api as api_fixtures
from test_workspace_quota import credentials


class WorkspaceApiTests(unittest.TestCase):
    setUp = api_fixtures.ApiTests.setUp
    tearDown = api_fixtures.ApiTests.tearDown
    request = api_fixtures.ApiTests.request

    def profiles(self):
        self.store.upsert({"email": "member@example.com"})
        return [self.store.save_auth_snapshot("member@example.com", credentials(workspace))
                for workspace in ("team-a", "team-b")]

    def test_pool_probes_individual_profiles_and_omits_ambiguous_email_alias(self):
        refs = self.profiles()
        with mock.patch.object(self.quota_service, "for_email", side_effect=lambda ref, **kwargs: {"ok": True, "profile_id": ref}) as probe:
            status, _, body = self.request("GET", "/api/accounts/quotas")
        self.assertEqual(status, 200)
        self.assertEqual(set(body["quotas"]), set(refs))
        self.assertEqual({call.args[0] for call in probe.call_args_list}, set(refs))

    def test_coupon_route_passes_explicit_profile(self):
        ref = self.profiles()[1]
        with mock.patch.object(self.quota_service, "consume_reset", create=True, return_value={"ok": False, "error": "fixture_no_credit"}) as redeem:
            self.request("POST", "/api/quota/reset", {"confirm": True, "email": "member@example.com", "profile_id": ref})
        self.assertEqual(redeem.call_args.kwargs["email"], ref)

    def test_deleting_workspace_preserves_siblings_and_email_credentials(self):
        refs = self.profiles()
        status, _, body = self.request("POST", "/api/profiles/delete", {"confirm": True, "profile_id": refs[0]})
        self.assertEqual(status, 200)
        self.assertTrue(body["deleted"]["profile"])
        self.assertEqual(len(self.store.list_auth_snapshots()), 1)
        self.assertIsNotNone(self.store.get_secret("member@example.com"))
        self.assertIsNotNone(self.store.get_auth_snapshot(refs[1]))

    def test_profile_setting_is_scoped_and_requires_explicit_target(self):
        refs = self.profiles()
        status, _, _ = self.request("POST", "/api/profiles/update", {"confirm": True, "profile_id": refs[0], "disabled": True, "priority": 300})
        self.assertEqual(status, 200)
        self.assertTrue(self.store.resolve_auth_profile(refs[0])["disabled"])
        self.assertFalse(self.store.resolve_auth_profile(refs[1])["disabled"])
        self.assertEqual(self.store.resolve_auth_profile(refs[0])["priority"], 300)
        status, _, _ = self.request("POST", "/api/profiles/delete", {"confirm": True, "profile_id": "member@example.com"})
        self.assertNotEqual(status, 200)

    def test_login_identity_selection_is_validated_before_launch(self):
        status, _, _ = self.request("POST", "/api/accounts/login", {"email": "member@example.com", "identity_type": "wrong"})
        self.assertEqual(status, 400)

    def test_import_recognizes_business_variants_as_existing_team_identity(self):
        self.store.upsert({"email": "member@example.com"})
        for plan in ("self_serve_business_prolite", "self_serve_business_usage_based"):
            with self.subTest(plan=plan):
                self.store.save_auth_snapshot("member@example.com", credentials("team-a", plan=plan))
                with mock.patch("codex_switcher.api.config.auto_login_on_import", return_value=True), \
                        mock.patch("codex_switcher.api.LoginManager.enqueue", return_value={"state": "queued"}) as login:
                    status, _, body = self.request("POST", "/api/accounts/import", {
                        "text": "member@example.com|fixture-password", "identity_type": "team"})
                    self.assertEqual(status, 200, body)
                    self.assertEqual(body["login_queued"], [])
                    login.assert_not_called()
                    status, _, body = self.request("POST", "/api/accounts/import", {
                        "text": "member@example.com|fixture-password", "identity_type": "personal"})
                    self.assertEqual(status, 200, body)
                    self.assertEqual(body["login_queued"], ["member@example.com"])
                    self.assertEqual(login.call_args.kwargs["identity_type"], "personal")

    def test_import_uses_any_healthy_workspace_regardless_of_snapshot_order(self):
        self.profiles()
        snapshots = self.store.list_auth_snapshots()
        snapshots[0]["expired"] = False
        snapshots[1]["expired"] = True
        for ordered in (snapshots, list(reversed(snapshots))):
            with self.subTest(first_expired=ordered[0]["expired"]), \
                    mock.patch.object(self.store, "list_auth_snapshots", return_value=ordered), \
                    mock.patch("codex_switcher.api.config.auto_login_on_import", return_value=True), \
                    mock.patch("codex_switcher.api.LoginManager.enqueue") as login:
                status, _, body = self.request("POST", "/api/accounts/import", {
                    "text": "member@example.com|fixture-password", "identity_type": "team"})
                self.assertEqual(status, 200, body)
                self.assertEqual(body["login_queued"], [])
                login.assert_not_called()

    def test_import_explicit_target_does_not_use_healthy_sibling_or_another_email(self):
        refs = self.profiles()
        snapshots = self.store.list_auth_snapshots()
        for profile in snapshots:
            profile["expired"] = profile["profile_id"] == refs[1]
        with self.store._conn:
            self.store._conn.execute("INSERT INTO auth_profile_aliases(alias_id,profile_id) VALUES(?,?)",
                                     ("profile_legacy_alias", refs[1]))
        with mock.patch.object(self.store, "list_auth_snapshots", return_value=snapshots), \
                mock.patch("codex_switcher.api.config.auto_login_on_import", return_value=True), \
                mock.patch("codex_switcher.api.LoginManager.enqueue", return_value={"state": "queued"}) as login:
            status, _, body = self.request("POST", "/api/accounts/import", {
                "text": "member@example.com|fixture-password\nother@example.com|fixture-password",
                "identity_type": "team", "profile_id": "profile_legacy_alias"})
            self.assertEqual(status, 200, body)
            self.assertEqual(body["login_queued"], ["member@example.com"])
            self.assertEqual(body["login_errors"], [{"email": "other@example.com", "error": "account_mismatch"}])
            self.assertEqual(login.call_count, 1)
            self.assertEqual(login.call_args.kwargs["profile_id"], refs[1])
            login.reset_mock()
            status, _, body = self.request("POST", "/api/accounts/import", {
                "text": "member@example.com|fixture-password", "identity_type": "team", "workspace_id": "missing-team"})
            self.assertEqual(status, 200, body)
            self.assertEqual(body["login_queued"], ["member@example.com"])
            self.assertEqual(login.call_args.kwargs["workspace_id"], "missing-team")
            login.reset_mock()
            status, _, body = self.request("POST", "/api/accounts/import", {
                "text": "member@example.com|fixture-password", "identity_type": "team", "profile_id": "profile_missing"})
            self.assertEqual(status, 200, body)
            self.assertEqual(body["login_errors"], [{"email": "member@example.com", "error": "profile_not_found"}])
            login.assert_not_called()

    def test_import_identity_conflict_does_not_count_as_healthy_authorization(self):
        self.profiles()
        snapshots = self.store.list_auth_snapshots()
        for profile in snapshots:
            profile.update(expired=False, identity_error="workspace_identity_conflict")
        with mock.patch.object(self.store, "list_auth_snapshots", return_value=snapshots), \
                mock.patch("codex_switcher.api.config.auto_login_on_import", return_value=True), \
                mock.patch("codex_switcher.api.LoginManager.enqueue", return_value={"state": "queued"}) as login:
            status, _, body = self.request("POST", "/api/accounts/import", {
                "text": "member@example.com|fixture-password", "identity_type": "team"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["login_queued"], ["member@example.com"])
        login.assert_called_once()

    def test_default_redemption_refreshes_the_redeemed_profile_even_if_live_changes(self):
        ref = self.profiles()[0]
        with mock.patch.object(self.quota_service, "consume_reset", create=True,
                               return_value={"ok": True, "profile_id": ref}) as redeem:
            with mock.patch.object(self.quota_service, "for_email", return_value={"ok": True}) as fetch:
                status, _, _ = self.request("POST", "/api/quota/reset", {"confirm": True})
        self.assertEqual(status, 200)
        self.assertEqual(fetch.call_args.args[0], ref)


if __name__ == "__main__":
    unittest.main()
