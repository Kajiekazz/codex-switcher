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
