"""Workspace routing tests with synthetic credentials; no upstream requests."""
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_switcher.auth_state import read_auth_state_bytes
from codex_switcher.quota import QuotaService, _extract_token
from codex_switcher.store import AccountStore
from codex_switcher.onboarding import Onboarding


def credentials(workspace, plan="team", token="fixture-access"):
    claims = {"email": "member@example.com", "exp": 4102444800,
              "https://api.openai.com/auth": {"chatgpt_user_id": "member-1",
              "chatgpt_account_id": workspace, "chatgpt_plan_type": plan}}
    jwt = "x." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".x"
    return json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": token,
                      "id_token": jwt, "account_id": workspace}}).encode()


class WorkspaceQuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = AccountStore(self.root / "accounts.sqlite3")
        self.addCleanup(self.store.close)
        self.store.upsert({"email": "member@example.com"})
        self.personal = credentials("personal", "plus")
        self.team = credentials("team-a")
        self.personal_id = self.store.save_auth_snapshot("member@example.com", self.personal)
        self.team_id = self.store.save_auth_snapshot("member@example.com", self.team)
        self.auth = self.root / "auth.json"
        self.auth.write_bytes(self.personal)
        self.fetch = mock.Mock(side_effect=lambda raw: {"ok": True, "account_id": read_auth_state_bytes(raw)["account_id"],
                  "allowed": True, "primary_window": {"used_percent": 10, "window_seconds": 18000}})
        self.service = QuotaService(self.store, self.auth, fetch_func=self.fetch)

    def test_team_lookup_never_uses_same_email_personal_live_auth(self):
        result = self.service.for_email(self.team_id)
        self.assertEqual(result["account_id"], "team-a")
        self.assertEqual(result["source"], "snapshot")
        self.assertEqual(result["email"], "member@example.com")
        self.fetch.assert_called_once_with(self.team)

    def test_ambiguous_email_is_rejected_for_quota_and_redemption(self):
        self.assertEqual(self.service.for_email("member@example.com")["error"], "workspace_required")
        with mock.patch("codex_switcher.quota.fetch_reset_credits") as credits:
            self.assertEqual(self.service.consume_reset(email="member@example.com")["error"], "workspace_required")
            credits.assert_not_called()
        self.fetch.assert_not_called()

    def test_live_refresh_only_updates_matching_profile_cache(self):
        self.service.for_email(self.team_id)
        self.auth.write_bytes(credentials("personal", "plus", token="fixture-refreshed"))
        result = self.service.for_email(self.personal_id)
        self.assertEqual(result["source"], "live")
        self.assertEqual(result["profile_id"], self.personal_id)
        self.service.for_email(self.team_id)
        self.assertEqual(self.fetch.call_count, 2)

    def test_reset_uses_explicit_workspace_and_returns_real_email(self):
        with mock.patch("codex_switcher.quota.consume_reset_credit", return_value={"ok": True}) as redeem:
            result = self.service.consume_reset(email=self.team_id, credit_id="fixture-credit")
        self.assertEqual(redeem.call_args.args[0], self.team)
        self.assertEqual(result["email"], "member@example.com")
        self.assertEqual(result["profile_id"], self.team_id)

    def test_workspace_history_is_separate(self):
        self.service.for_email(self.personal_id)
        self.service.for_email(self.team_id)
        self.assertEqual(len(self.store.quota_history(self.personal_id)), 1)
        self.assertEqual(len(self.store.quota_history(self.team_id)), 1)

    def test_quota_headers_follow_shared_identity_and_conflicts_make_no_token(self):
        payload = json.loads(self.team)
        del payload["tokens"]["account_id"]
        self.assertEqual(_extract_token(json.dumps(payload).encode())[1], "team-a")
        payload["tokens"]["account_id"] = "different"
        self.assertEqual(_extract_token(json.dumps(payload).encode()), (None, None))

    def test_local_import_adds_same_email_new_workspace(self):
        new = credentials("team-b")
        self.auth.write_bytes(new)
        setup = Onboarding(self.store, components=mock.Mock(), startup=mock.Mock(), auth_path=self.auth)
        result = setup.import_local(read_auth_state_bytes(new)["file_sha256"])
        self.assertEqual(result["result"], "updated")
        self.assertEqual(len(self.store.list_auth_snapshots()), 3)
        self.assertEqual(self.store.get_auth_snapshot(self.team_id), self.team)

    def test_invalid_or_misrouted_snapshot_cannot_redeem(self):
        with mock.patch.object(self.store, "get_auth_snapshot", return_value=self.personal):
            with mock.patch("codex_switcher.quota.consume_reset_credit") as redeem:
                self.assertEqual(self.service.consume_reset(email=self.team_id, credit_id="fixture-credit")["error"], "identity_conflict")
                redeem.assert_not_called()

    def test_local_import_refreshes_same_workspace_credentials(self):
        new = credentials("team-a", token="fixture-refreshed")
        self.auth.write_bytes(new)
        setup = Onboarding(self.store, components=mock.Mock(), startup=mock.Mock(), auth_path=self.auth)
        result = setup.import_local(read_auth_state_bytes(new)["file_sha256"])
        self.assertEqual(result["result"], "updated")
        self.assertEqual(self.store.get_auth_snapshot(self.team_id), new)


if __name__ == "__main__":
    unittest.main()
