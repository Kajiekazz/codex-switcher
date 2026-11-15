"""Metadata readers stay bounded without caching mutable account state."""
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest import mock

from codex_switcher import goals, settings
from codex_switcher.api import _status_payload
from codex_switcher.store import AccountStore
from test_goals import build_goals_db, build_state_db
from test_store_meta import make_auth


class MetadataResourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = mock.patch.dict(os.environ, {
            "CODEX_SWITCHER_DATA": str(self.root),
            "CODEX_SWITCHER_STATE_DB": str(self.root / "state.sqlite"),
            "CODEX_SWITCHER_GOALS_DB": str(self.root / "goals.sqlite"),
        })
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(settings.reset_cache)
        settings.reset_cache()

    def test_goal_metadata_does_not_materialize_unrelated_threads(self):
        build_state_db(self.root / "state.sqlite", [
            {"id": str(index), "name": f"Thread {index}", "preview": "x" * 2000}
            for index in range(1500)
        ])
        build_goals_db(self.root / "goals.sqlite", [{"thread_id": "10"}, {"thread_id": "1200"}])
        original = goals._connect
        fetched = []

        class Connection:
            def __init__(self, connection):
                self.connection = connection
            def execute(self, *args):
                cursor = self.connection.execute(*args)
                class Cursor:
                    def fetchall(self):
                        rows = cursor.fetchall()
                        fetched.extend(rows)
                        return rows
                return Cursor()
            def close(self):
                self.connection.close()

        def connect(path):
            connection = original(path)
            return Connection(connection) if path == self.root / "state.sqlite" else connection

        with mock.patch.object(goals, "_connect", side_effect=connect):
            result = goals.list_goals()
        self.assertEqual(len(fetched), 2)
        self.assertEqual({item["thread_name"] for item in result}, {"Thread 10", "Thread 1200"})
        # No metadata cache may conceal a rename.
        with closing(sqlite3.connect(self.root / "state.sqlite")) as db:
            with db:
                db.execute("UPDATE threads SET name='Renamed' WHERE id='10'")
        self.assertIn("Renamed", [item["thread_name"] for item in goals.list_goals()])

    def test_goal_metadata_batches_more_than_one_sql_parameter_page(self):
        rows = [{"id": str(index), "name": str(index)} for index in range(1001)]
        build_state_db(self.root / "state.sqlite", rows)
        build_goals_db(self.root / "goals.sqlite", [{"thread_id": row["id"]} for row in rows])
        self.assertEqual(len(goals.list_goals()), 1001)

    def test_session_list_reuses_goal_rows_from_the_same_response(self):
        build_state_db(self.root / "state.sqlite", [{"id": "chat", "name": "Fixture"}])
        row = {"thread_id": "chat", "status": "paused", "status_rpc": "paused"}
        with mock.patch.object(goals, "list_goals", side_effect=AssertionError("duplicate goal read")):
            result = goals.list_sessions(goal_rows=[row])
            self.assertTrue(result["recent"][0]["has_goal"])
            self.assertEqual(result["recent"][0]["goal_status"], "paused")
            self.assertFalse(goals.list_sessions(goal_rows=[])["recent"][0]["has_goal"])

    def test_profile_display_and_lookup_never_read_credential_blobs(self):
        store = AccountStore(self.root / "accounts.sqlite")
        self.addCleanup(store.close)
        store.upsert({"email": "a@example.com", "password": "fixture"})
        ref = store.save_auth_snapshot("a@example.com", make_auth("Fixture"), {"email": "a@example.com", "name": "Fixture"})

        def authorize(action, table, column, *_args):
            if action == sqlite3.SQLITE_READ and column in {"auth_blob", "secret_blob"}:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        store._conn.set_authorizer(authorize)
        self.assertEqual(store.list_auth_snapshots()[0]["name"], "Fixture")
        self.assertEqual(store.resolve_auth_profile(ref)["email"], "a@example.com")
        self.assertEqual(store.resolve_auth_profile("A@EXAMPLE.COM")["profile_id"], ref)
        self.assertTrue(store.has_auth_snapshot("a@example.com"))
        with self.assertRaises(sqlite3.DatabaseError):
            store.get_auth_snapshot(ref)

    def test_status_counts_do_not_fetch_entire_account_rows(self):
        store = mock.Mock()
        store.status_counts.return_value = {"ready": 5, "unknown": 2}
        with mock.patch("codex_switcher.api.read_auth_state", return_value={}):
            payload = _status_payload(store, self.root / "absent-auth.json", mock.Mock())
        self.assertEqual(payload["accounts"]["total"], 7)
        store.list.assert_not_called()

    def test_quota_retention_has_an_index_for_time_based_deletes(self):
        store = AccountStore(self.root / "accounts.sqlite")
        self.addCleanup(store.close)
        plan = store._conn.execute("EXPLAIN QUERY PLAN DELETE FROM quota_samples WHERE epoch < ?", (1000,)).fetchall()
        self.assertTrue(any("SEARCH quota_samples" in row[3] and "INDEX" in row[3] for row in plan), plan)
