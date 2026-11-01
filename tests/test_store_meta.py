"""Tests for account metadata, deletion and quota-sample history."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_switcher.store import AccountStore


def _b64url(payload: dict) -> str:
    import base64
    import json

    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _jwt(payload: dict) -> str:
    # auth_state.decode_jwt_payload only needs three dot-separated segments.
    header = _b64url({"alg": "none", "typ": "JWT"})
    return header + "." + _b64url(payload) + ".sig"


def make_auth(name: str, email: str = "a@example.com") -> bytes:
    import json

    return json.dumps(
        {
            "tokens": {
                "id_token": _jwt({"email": email, "name": name}),
                "access_token": _jwt({"email": email, "exp": 4102444800}),
                "account_id": "acct-a",
            }
        }
    ).encode("utf-8")


class StoreMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = AccountStore(self.root / "accounts.sqlite3")
        self.addCleanup(self.store.close)

    def test_defaults_and_metadata_roundtrip(self):
        self.assertEqual(
            self.store.upsert({"email": "a@example.com", "password": "p"}),
            "inserted",
        )
        account = self.store.get("a@example.com")
        self.assertEqual(account["tags"], "")
        self.assertEqual(account["priority"], 100)
        self.assertFalse(account["disabled"])
        self.assertTrue(account["has_password"])
        self.assertNotIn("password", account)

        updated = self.store.update_account(
            "a@example.com",
            tags="work,backup",
            priority=250,
            note="主号",
            disabled=True,
        )
        self.assertEqual(updated["tags"], "work,backup")
        self.assertEqual(updated["priority"], 250)
        self.assertTrue(updated["disabled"])
        self.assertEqual(updated["note"], "主号")

    def test_upsert_preserves_metadata_unless_provided(self):
        self.store.upsert(
            {"email": "a@example.com", "password": "p", "tags": "x", "priority": 7}
        )
        self.store.upsert({"email": "a@example.com", "password": "new"})
        account = self.store.get("a@example.com")
        self.assertEqual(account["tags"], "x")
        self.assertEqual(account["priority"], 7)

    def test_update_rotates_secret_without_exposing_it(self):
        self.store.upsert({"email": "a@example.com", "password": "old"})
        self.store.update_account("a@example.com", password="new", totp="JBSWY3DPEHPK3PXP")
        secret = self.store.get_secret("a@example.com")
        self.assertEqual(secret["password"], "new")
        self.assertEqual(secret["totp"], "JBSWY3DPEHPK3PXP")
        account = self.store.get("a@example.com")
        self.assertTrue(account["has_totp"])
        self.assertEqual(account["totp_format"], "base32")
        self.assertNotIn("new", str(account))

        self.store.update_account("a@example.com", clear_totp=True)
        account = self.store.get("a@example.com")
        self.assertFalse(account["has_totp"])

    def test_update_missing_returns_none(self):
        self.assertIsNone(self.store.update_account("none@example.com", tags="x"))

    def test_delete_removes_account_snapshot_and_samples(self):
        self.store.upsert({"email": "a@example.com", "password": "p"})
        self.store.save_auth_snapshot(
            "a@example.com",
            b'{"tokens": {}}',
            {"email": "a@example.com", "plan": "plus", "expires_at": 4102444800},
        )
        self.store.record_quota_sample(
            "a@example.com",
            {
                "ok": True,
                "plan": "plus",
                "primary_window": {"used_percent": 10, "reset_at": 123, "window_seconds": 18000},
                "secondary_window": {"used_percent": 1},
            },
            epoch=1000,
        )
        result = self.store.delete("A@Example.com")
        self.assertTrue(result["account"])
        self.assertTrue(result["snapshot"])
        self.assertGreaterEqual(result["samples"], 1)
        self.assertIsNone(self.store.get("a@example.com"))
        self.assertFalse(self.store.has_auth_snapshot("a@example.com"))
        self.assertEqual(self.store.quota_history("a@example.com"), [])

    def test_snapshot_name_roundtrip(self):
        self.store.upsert({"email": "a@example.com", "password": "p"})
        self.store.save_auth_snapshot(
            "a@example.com",
            make_auth("Ada Lovelace"),
            {"email": "a@example.com", "name": "Ada Lovelace", "plan": "plus"},
        )
        account = self.store.get("a@example.com")
        self.assertEqual(account["name"], "Ada Lovelace")
        snaps = self.store.list_auth_snapshots()
        self.assertEqual(snaps[0]["name"], "Ada Lovelace")

    def test_backfill_reads_name_from_snapshot(self):
        self.store.upsert({"email": "a@example.com", "password": "p"})
        self.store.save_auth_snapshot(
            "a@example.com", make_auth("Ada Lovelace"), {"email": "a@example.com"}
        )
        self.assertIsNone(self.store.get("a@example.com")["name"])

        reopened = AccountStore(self.root / "accounts.sqlite3")
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get("a@example.com")["name"], "Ada Lovelace")
        self.assertEqual(reopened.list_auth_snapshots()[0]["name"], "Ada Lovelace")

    def test_quota_history_is_throttled_and_sorted(self):
        now = int(__import__("time").time())
        quota = {
            "ok": True,
            "plan": "plus",
            "primary_window": {"used_percent": 10, "reset_at": now + 18000, "window_seconds": 18000},
            "secondary_window": {"used_percent": 2},
        }
        self.assertTrue(self.store.record_quota_sample("a@example.com", quota, epoch=now))
        # Too soon: throttled.
        self.assertFalse(self.store.record_quota_sample("a@example.com", quota, epoch=now + 10))
        quota2 = {
            "ok": True,
            "plan": "plus",
            "primary_window": {"used_percent": 25, "reset_at": now + 18000, "window_seconds": 18000},
            "secondary_window": {"used_percent": 3},
        }
        self.assertTrue(self.store.record_quota_sample("a@example.com", quota2, epoch=now + 200))
        # Error payloads are never stored.
        self.assertFalse(
            self.store.record_quota_sample(
                "a@example.com", {"ok": False, "error": "auth_error"}, epoch=now + 300
            )
        )
        rows = self.store.quota_history("a@example.com", hours=24)
        self.assertEqual([row["primary_used"] for row in rows], [10.0, 25.0])

    def test_old_schema_is_migrated_in_place(self):
        path = self.root / "legacy.sqlite3"
        legacy = sqlite3.connect(path)
        legacy.executescript(
            """
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                email_masked TEXT NOT NULL,
                secret_blob BLOB NOT NULL,
                has_password INTEGER NOT NULL DEFAULT 0,
                has_totp INTEGER NOT NULL DEFAULT 0,
                totp_format TEXT NOT NULL DEFAULT 'none',
                status TEXT NOT NULL DEFAULT 'unknown',
                last_login_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO accounts
                (email, email_masked, secret_blob, has_password, has_totp,
                 totp_format, status, created_at, updated_at)
            VALUES ('legacy@example.com', 'le***@example.com', x'00', 0, 0,
                    'none', 'unknown', '2026-01-01T00:00:00+00:00',
                    '2026-01-01T00:00:00+00:00');
            """
        )
        legacy.commit()
        legacy.close()

        store = AccountStore(path)
        try:
            account = store.get("legacy@example.com")
            self.assertEqual(account["priority"], 100)
            self.assertEqual(account["tags"], "")
            self.assertFalse(account["disabled"])
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
