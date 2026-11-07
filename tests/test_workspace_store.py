"""Workspace identities must stay separate through storage and migration."""

import base64
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from codex_switcher import dpapi
from codex_switcher.auth_state import read_auth_state_bytes
from codex_switcher.identity import profile_id_for, profile_ref, same_identity
from codex_switcher.store import AccountStore, AmbiguousProfileError, InvalidProfileError


def auth(email="member@example.com", workspace="personal", user="user-1", **overrides):
    claims = {"chatgpt_account_id": workspace, "chatgpt_plan_type": "plus" if workspace == "personal" else "team"}
    if user:
        claims["chatgpt_user_id"] = user
    payload = {"email": email, "exp": 4102444800, "https://api.openai.com/auth": claims}
    token = "header." + base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode() + ".signature"
    data = {"tokens": {"id_token": token, "access_token": token, "account_id": workspace}, "refresh": "private-fixture"}
    data.update(overrides)
    return json.dumps(data).encode()


class WorkspaceIdentityTests(unittest.TestCase):
    def test_token_rotation_keeps_id_and_workspace_separates(self):
        first = read_auth_state_bytes(auth())
        renewed = read_auth_state_bytes(auth(last_refresh="new"))
        team = read_auth_state_bytes(auth(workspace="team-a"))
        other_member = read_auth_state_bytes(auth(email="other@example.com", user="user-2"))
        self.assertEqual(first["profile_id"], renewed["profile_id"])
        self.assertTrue(same_identity(first, renewed))
        self.assertFalse(same_identity(first, team))
        self.assertNotEqual(first["profile_id"], other_member["profile_id"])
        self.assertEqual(profile_ref(first), first["profile_id"])
        self.assertEqual(profile_ref({"email": "old@example.com"}), "old@example.com")

    def test_conflicting_workspace_rejected_by_shared_parser(self):
        data = json.loads(auth())
        data["tokens"]["account_id"] = "other-workspace"
        state = read_auth_state_bytes(json.dumps(data).encode())
        self.assertFalse(state["logged_in"])
        self.assertEqual(state["identity_error"], "workspace_identity_conflict")
        self.assertIsNone(state["profile_id"])
        self.assertEqual(profile_ref(state), "")

    def test_old_email_identity_is_case_insensitive(self):
        self.assertEqual(profile_id_for({"email": "Member@Example.com"}), profile_id_for({"email": "member@example.com"}))
        self.assertFalse(same_identity({"email": "a@b.com", "account_id": "a"}, {"email": "a@b.com"}))


class WorkspaceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "accounts.sqlite3"
        self.store = AccountStore(self.path)
        self.addCleanup(lambda: self.store.close())
        self.store.upsert({"email": "member@example.com", "password": "private-password", "priority": 15})

    def test_same_email_three_profiles_and_ambiguous_legacy_lookup(self):
        raw = [auth(workspace=w) for w in ("personal", "team-a", "team-b")]
        ids = [self.store.save_auth_snapshot("member@example.com", data) for data in raw]
        self.assertEqual(len(set(ids)), 3)
        for profile_id, data in zip(ids, raw):
            self.assertEqual(self.store.get_auth_snapshot(profile_id), data)
        for function in (self.store.resolve_auth_profile, self.store.get_auth_snapshot, self.store.has_auth_snapshot):
            with self.assertRaises(AmbiguousProfileError) as caught:
                function("MEMBER@example.com")
            self.assertEqual(caught.exception.code, "workspace_required")
        account = self.store.get("member@example.com")
        self.assertEqual(account["profile_count"], 3)
        self.assertEqual(len(self.store.list()), 1)
        self.assertNotIn("private-fixture", json.dumps(account))
        self.assertNotIn("private-password", json.dumps(account))

    def test_refresh_and_profile_metadata_do_not_touch_sibling(self):
        personal = self.store.save_auth_snapshot("member@example.com", auth())
        team = self.store.save_auth_snapshot("member@example.com", auth(workspace="team-a"))
        self.store.update_auth_profile(team, priority=80, disabled=True, workspace_name="Team A")
        renewed = auth(workspace="team-a", last_refresh="new")
        self.assertEqual(self.store.save_auth_snapshot("member@example.com", renewed), team)
        self.assertEqual(self.store.resolve_auth_profile(personal)["priority"], 15)
        self.assertFalse(self.store.resolve_auth_profile(personal)["disabled"])
        self.assertTrue(self.store.resolve_auth_profile(team)["disabled"])
        self.assertEqual(self.store.resolve_auth_profile(team)["workspace_name"], "Team A")
        self.assertEqual(self.store.get_auth_snapshot(personal), auth())

    def test_member_in_same_workspace_has_distinct_profile(self):
        a = self.store.save_auth_snapshot("member@example.com", auth(workspace="team"))
        b = self.store.save_auth_snapshot("other@example.com", auth(email="other@example.com", workspace="team", user="user-2"))
        self.assertNotEqual(a, b)
        self.assertEqual(len(self.store.list_auth_snapshots()), 2)

    def test_history_throttle_and_lookup_are_per_profile(self):
        a = self.store.save_auth_snapshot("member@example.com", auth())
        b = self.store.save_auth_snapshot("member@example.com", auth(workspace="team-a"))
        now = int(time.time())
        quota = {"ok": True, "primary_window": {"used_percent": 10}}
        self.assertTrue(self.store.record_quota_sample(a, quota, epoch=now))
        self.assertTrue(self.store.record_quota_sample(b, {**quota, "primary_window": {"used_percent": 80}}, epoch=now))
        self.assertEqual(self.store.quota_history(a)[0]["primary_used"], 10)
        self.assertEqual(self.store.quota_history(b)[0]["primary_used"], 80)
        with self.assertRaises(AmbiguousProfileError):
            self.store.quota_history("member@example.com")

    def test_live_profile_can_record_history_before_import(self):
        state = read_auth_state_bytes(auth())
        quota = {"ok": True, "profile_id": state["profile_id"], "email": state["email"],
                 "primary_window": {"used_percent": 35}}
        self.assertTrue(self.store.record_quota_sample(state["profile_id"], quota))
        self.assertEqual(self.store.quota_history(state["profile_id"])[0]["primary_used"], 35)
        self.assertIsNone(self.store.resolve_auth_profile(state["profile_id"]))
        self.assertFalse(self.store.record_quota_sample("profile_wrong", quota))

    def test_deleting_workspace_keeps_login_and_other_snapshot(self):
        a = self.store.save_auth_snapshot("member@example.com", auth())
        b = self.store.save_auth_snapshot("member@example.com", auth(workspace="team-a"))
        self.assertTrue(self.store.delete_auth_profile(b)["profile"])
        self.assertIsNone(self.store.resolve_auth_profile(b))
        self.assertEqual(self.store.get_secret("member@example.com")["password"], "private-password")
        self.assertEqual(self.store.resolve_auth_profile("member@example.com")["profile_id"], a)
        self.assertTrue(self.store.delete("member@example.com")["snapshot"])
        self.assertEqual(self.store.list_auth_snapshots(), [])

    def test_bad_auth_cannot_replace_existing_profile(self):
        original = auth()
        profile_id = self.store.save_auth_snapshot("member@example.com", original)
        bad = json.loads(original)
        bad["tokens"]["account_id"] = "wrong"
        for raw in (b"not json", json.dumps(bad).encode(), auth(email="wrong@example.com")):
            with self.subTest(raw=raw[:12]):
                with self.assertRaises(InvalidProfileError):
                    self.store.save_auth_snapshot("member@example.com", raw)
        self.assertEqual(self.store.get_auth_snapshot(profile_id), original)

    def test_new_user_id_promotes_legacy_profile_and_preserves_references(self):
        old_id = self.store.save_auth_snapshot("member@example.com", auth(user=None))
        self.store.update_auth_profile(old_id, priority=91, workspace_name="My workspace")
        self.store.record_quota_sample(old_id, {"ok": True, "primary_window": {"used_percent": 12}})
        new_auth = auth()
        new_id = self.store.save_auth_snapshot("member@example.com", new_auth)
        self.assertNotEqual(old_id, new_id)
        self.assertEqual(len(self.store.list_auth_snapshots()), 1)
        self.assertEqual(self.store.get_auth_snapshot(old_id), new_auth)
        self.assertEqual(self.store.resolve_auth_profile(old_id)["profile_id"], new_id)
        self.assertEqual(self.store.resolve_auth_profile(new_id)["priority"], 91)
        self.assertEqual(self.store.quota_history(new_id)[0]["primary_used"], 12)
        self.assertEqual(self.store.quota_history(old_id)[0]["profile_id"], new_id)


class WorkspaceMigrationTests(unittest.TestCase):
    def test_migration_keeps_ciphertext_and_is_not_replayed_after_delete(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "legacy.sqlite3"
            encrypted = dpapi.protect(auth())
            connection = sqlite3.connect(path)
            connection.executescript("""
                CREATE TABLE auth_snapshots (
                    email TEXT PRIMARY KEY,auth_blob BLOB NOT NULL,auth_email TEXT,
                    auth_plan TEXT,auth_account_id TEXT,auth_expires_at INTEGER,saved_at TEXT NOT NULL
                );
                CREATE TABLE accounts (
                    id INTEGER PRIMARY KEY,email TEXT UNIQUE NOT NULL,email_masked TEXT NOT NULL,
                    secret_blob BLOB NOT NULL,has_password INTEGER DEFAULT 0,has_totp INTEGER DEFAULT 0,
                    totp_format TEXT DEFAULT 'none',status TEXT DEFAULT 'ready',last_login_at TEXT,
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL
                );
            """)
            connection.execute("INSERT INTO auth_snapshots VALUES(?,?,?,?,?,?,?)", ("member@example.com", encrypted, "member@example.com", "plus", "personal", 4102444800, "2026-01-01"))
            connection.commit()
            connection.close()
            store = AccountStore(path)
            profile = store.resolve_auth_profile("member@example.com")
            self.assertTrue(path.with_name(path.name + ".pre-workspaces.bak").exists())
            self.assertEqual(store.get_auth_snapshot(profile["profile_id"]), auth())
            self.assertEqual(bytes(store._conn.execute("SELECT auth_blob FROM auth_profiles").fetchone()[0]), encrypted)
            self.assertEqual(bytes(store._conn.execute("SELECT auth_blob FROM auth_snapshots").fetchone()[0]), encrypted)
            store.close()
            store = AccountStore(path)
            self.assertEqual(len(store.list_auth_snapshots()), 1)
            store.delete_auth_profile(profile["profile_id"])
            store.close()
            store = AccountStore(path)
            self.assertEqual(store.list_auth_snapshots(), [])
            store.close()


if __name__ == "__main__":
    unittest.main()
