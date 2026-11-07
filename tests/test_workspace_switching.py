"""Workspace switching, recovery and model permissions with one shared email."""
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from codex_switcher.auth_state import read_auth_state_bytes
from codex_switcher.auto_switch import AutoSwitchMonitor
from codex_switcher.model_access import ModelAccessGuard, permission_signature
from codex_switcher.store import AccountStore
from codex_switcher.switcher import SwitchError, SwitchManager


def auth(workspace, *, email="member@example.com", user="member-1", plan="team", refresh="1", role="member"):
    claims = {"email": email, "exp": 4102444800, "https://api.openai.com/auth": {
        "chatgpt_account_id": workspace, "chatgpt_user_id": user,
        "chatgpt_plan_type": plan, "role": role,
    }}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    token = "x." + payload + ".fixture"
    return json.dumps({"tokens": {"id_token": token, "access_token": token,
                                  "account_id": workspace, "refresh_token": refresh},
                       "last_refresh": refresh}).encode()


def quota(available=True, *, profile_id=None):
    result = {"ok": True, "allowed": available, "limit_reached": not available,
              "plan": "team", "primary_window": {"used_percent": 5 if available else 100},
              "secondary_window": {"used_percent": 10}}
    if profile_id:
        result["profile_id"] = profile_id
    return result


class WorkspaceSwitchingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = AccountStore(self.root / "accounts.sqlite3")
        self.addCleanup(self.store.close)
        self.store.upsert({"email": "member@example.com"})
        self.a, self.b = auth("team-a"), auth("team-b")
        self.a_id = self.store.save_auth_snapshot("member@example.com", self.a)
        self.b_id = self.store.save_auth_snapshot("member@example.com", self.b)
        self.path = self.root / "auth.json"
        self.path.write_bytes(self.a)
        self.desktop = Mock()
        self.desktop.complete_restart.return_value = {"ok": True}
        self.callback = Mock(return_value={"ok": True})
        self.manager = SwitchManager(self.store, self.path, self.root / "backups", enabled=lambda: True,
                                     desktop_client=self.desktop, on_switched=self.callback)

    def monitor(self, service=None):
        service = service or Mock()
        return AutoSwitchMonitor(self.store, self.manager, service, enabled=lambda: True,
                                  clock=lambda: 1000, reset_enabled=lambda: False,
                                  log_path=self.root / "auto-switch.log")

    def test_same_email_workspace_switch_preserves_both_authorizations(self):
        result = self.manager.switch_to(self.b_id, confirm=True, restart_desktop=False)
        self.assertEqual(self.path.read_bytes(), self.b)
        self.assertEqual(result.target_profile_id, self.b_id)
        self.assertEqual(result.previous_profile_id, self.a_id)
        self.assertEqual(result.target_email, "member@example.com")
        self.assertEqual(self.store.get_auth_snapshot(self.a_id), self.a)
        self.assertEqual(self.store.get_auth_snapshot(self.b_id), self.b)
        rows = {row["profile_id"]: row for row in self.manager.list_targets()}
        self.assertFalse(rows[self.a_id]["is_current"])
        self.assertTrue(rows[self.b_id]["is_current"])

    def test_bare_email_is_ambiguous_before_any_write_or_restart(self):
        with self.assertRaises(SwitchError) as error:
            self.manager.switch_to("member@example.com", confirm=True)
        self.assertEqual(error.exception.code, "workspace_required")
        self.assertEqual(self.path.read_bytes(), self.a)
        self.desktop.restart.assert_not_called()

    def test_same_workspace_is_already_active(self):
        with self.assertRaises(SwitchError) as error:
            self.manager.switch_to(self.a_id, confirm=True)
        self.assertEqual(error.exception.code, "already_active")

    def test_old_profile_alias_switches_after_user_id_is_filled_in(self):
        old = self.store.save_auth_snapshot("member@example.com", auth("team-c", user=None))
        canonical = self.store.save_auth_snapshot("member@example.com", auth("team-c"))
        self.assertNotEqual(old, canonical)
        result = self.manager.switch_to(old, confirm=True, restart_desktop=False)
        self.assertEqual(result.target_profile_id, canonical)
        self.assertEqual(self.manager.current()["account_id"], "team-c")

    def test_old_profile_alias_recovers_the_canonical_workspace(self):
        old = self.store.save_auth_snapshot("member@example.com", auth("team-c", user=None))
        canonical = self.store.save_auth_snapshot("member@example.com", auth("team-c"))
        self.path.write_bytes(auth("team-c"))
        pending = {"target_email": "member@example.com", "target_profile_id": old}
        self.assertTrue(self.manager.recover_desktop_restart(pending)["ok"])
        self.assertEqual(pending["target_profile_id"], canonical)

    def test_cooldown_survives_profile_promotion(self):
        old = self.store.save_auth_snapshot("member@example.com", auth("team-c", user=None))
        canonical = self.store.save_auth_snapshot("member@example.com", auth("team-c"))
        service = Mock()
        service.for_email.return_value = quota(True)
        monitor = self.monitor(service)
        monitor._cooldowns[old] = 2000
        monitor._cooldowns[self.b_id] = 2000
        self.assertIsNone(monitor._choose_candidate(self.a_id, 1000))
        self.assertIn(canonical, monitor.status()["cooldowns"])
        self.path.write_bytes(auth("team-c"))
        self.assertTrue(monitor._current_matches(old))

    def test_delayed_restart_does_not_resume_another_workspace_with_same_email(self):
        pending = {"target_email": "member@example.com", "target_profile_id": self.b_id}
        self.assertEqual(self.manager.recover_desktop_restart(pending)["skipped"], "account_changed")
        self.desktop.complete_restart.assert_not_called()
        self.callback.assert_not_called()

    def test_legacy_email_only_recovery_does_not_choose_between_workspaces(self):
        pending = {"target_email": "member@example.com"}
        self.assertEqual(self.manager.recover_desktop_restart(pending)["skipped"], "account_changed")
        self.desktop.complete_restart.assert_not_called()

    def test_workspace_change_during_activation_prevents_resume(self):
        def activate(*args, **kwargs):
            self.path.write_bytes(self.b)
            return {"ok": True}
        self.desktop.complete_restart.side_effect = activate
        result = self.manager.recover_desktop_restart({"target_email": "member@example.com",
                                                       "target_profile_id": self.a_id})
        self.assertEqual(result["skipped"], "account_changed")
        self.callback.assert_not_called()

    def test_workspace_changed_by_desktop_restart_is_not_reported_as_success(self):
        def restart(**kwargs):
            self.path.write_bytes(self.a)
            return {"ok": True}
        self.desktop.restart.side_effect = restart
        with self.assertRaises(SwitchError) as error:
            self.manager.switch_to(self.b_id, confirm=True)
        self.assertEqual(error.exception.code, "activation_identity_changed")
        self.assertEqual(self.path.read_bytes(), self.a)
        self.callback.assert_not_called()

    def test_same_email_other_workspace_is_an_automatic_candidate(self):
        service = Mock()
        service.current.return_value = quota(False, profile_id=self.a_id)
        service.for_email.side_effect = lambda ref, **kwargs: quota(ref == self.b_id, profile_id=ref)
        self.desktop.restart.return_value = {"ok": True}
        monitor = self.monitor(service)
        outcome = monitor.tick_once()
        self.assertEqual(outcome["action"], "switched")
        self.assertEqual(outcome["target_profile_id"], self.b_id)
        self.assertEqual(outcome["target_email"], "member@example.com")
        service.for_email.assert_called_once()
        self.assertEqual(service.for_email.call_args.args[0], self.b_id)
        self.assertIn(self.a_id, monitor.status()["cooldowns"])
        self.assertNotIn(self.b_id, monitor.status()["cooldowns"])

    def test_manual_reset_of_other_workspace_never_resumes_current_chats(self):
        monitor = self.monitor()
        monitor.on_quota_recovered = Mock()
        result = monitor.on_manual_reset(self.b_id, {"ok": True}, quota(True))
        self.assertEqual(result["skipped"], "not_current_account")
        monitor.on_quota_recovered.assert_not_called()

    def test_waiting_for_quota_is_persisted_by_workspace(self):
        service = Mock()
        service.current.return_value = quota(True, profile_id=self.b_id)
        monitor = self.monitor(service)
        monitor._remember_waiting(self.a_id)
        self.path.write_bytes(self.b)
        restarted = self.monitor(service)
        restarted.on_quota_recovered = Mock()
        self.assertEqual(restarted.tick_once()["action"], "quota_ok")
        restarted.on_quota_recovered.assert_not_called()
        self.assertFalse(restarted.status()["quota_recovery"]["waiting"])

    def test_ambiguous_legacy_wait_does_not_discard_identified_desktop_recovery(self):
        (self.root / "auto-switch.recovery.json").write_text(json.dumps({
            "email": "member@example.com", "retry": {}, "desktop_restart": {
                "target_email": "member@example.com", "target_profile_id": self.a_id,
                "previous_pids": [123], "after": 0,
            },
        }))
        monitor = self.monitor()
        self.assertFalse(monitor.status()["quota_recovery"]["waiting"])
        self.assertTrue(monitor.status()["desktop_recovery"]["waiting"])
        self.assertEqual(monitor.tick_once()["action"], "desktop_recovered")
        self.assertEqual(self.callback.call_args.args[0].target_profile_id, self.a_id)

    def test_quota_for_other_workspace_cannot_trigger_switch(self):
        service = Mock()
        service.current.return_value = quota(False, profile_id=self.b_id)
        outcome = self.monitor(service).tick_once()
        self.assertEqual(outcome["action"], "account_changed")
        self.desktop.restart.assert_not_called()

    def test_manual_switch_while_probing_invalidates_automatic_decision(self):
        service = Mock()
        service.current.return_value = quota(False, profile_id=self.a_id)
        def probe(ref, **kwargs):
            self.path.write_bytes(self.b)
            return quota(True, profile_id=ref)
        service.for_email.side_effect = probe
        self.assertEqual(self.monitor(service).tick_once()["action"], "account_changed")
        self.desktop.restart.assert_not_called()

    def guard(self, catalog=None):
        return ModelAccessGuard(self.store, self.path, catalog=catalog or (lambda raw: {"luna": {"allowed": None}}),
                                 path=self.root / "models.json", clock=lambda: 1000)

    def test_same_team_plan_does_not_establish_model_entitlement(self):
        self.assertEqual(self.guard().check(self.b_id, ["luna"], automatic=True, source_plan="team")["state"],
                         "model_access_unknown")

    def test_model_observation_is_isolated_by_workspace(self):
        guard = self.guard()
        guard.observe(self.a_id, "luna", True)
        self.assertTrue(guard.check(self.a_id, ["luna"], automatic=True, source_plan="team")["ok"])
        self.assertFalse(guard.check(self.b_id, ["luna"], automatic=True, source_plan="team")["ok"])
        guard.observe(self.b_id, "luna", False)
        self.assertEqual(guard.check(self.b_id, ["luna"])["state"], "model_access_denied")
        self.assertTrue(guard.check(self.a_id, ["luna"])["ok"])

    def test_token_rotation_keeps_recent_model_observation_but_role_change_does_not(self):
        guard = self.guard()
        guard.observe(self.a_id, "luna", True)
        refreshed = auth("team-a", refresh="2")
        self.assertEqual(permission_signature(refreshed), permission_signature(self.a))
        self.path.write_bytes(refreshed)
        self.assertTrue(guard.check(self.a_id, ["luna"], automatic=True, source_plan="team")["ok"])
        self.path.write_bytes(auth("team-a", role="restricted"))
        self.assertFalse(guard.check(self.a_id, ["luna"], automatic=True, source_plan="team")["ok"])

    def test_explicit_model_permission_is_accepted_for_team(self):
        guard = self.guard(lambda raw: {"luna": {"allowed": True}})
        self.assertEqual(guard.check(self.b_id, ["luna"], automatic=True, source_plan="plus")["state"],
                         "confirmed_by_catalog")

    def test_model_query_never_borrows_live_auth_from_sibling_workspace(self):
        catalog = Mock(return_value={"luna": {"allowed": True}})
        self.guard(catalog).check(self.b_id, ["luna"])
        self.assertEqual(read_auth_state_bytes(catalog.call_args.args[0])["account_id"], "team-b")


if __name__ == "__main__":
    unittest.main()
