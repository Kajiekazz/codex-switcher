import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_switcher import config
from codex_switcher.goals import get_goal, list_goals


def build_goals_db(path: Path, rows: list[dict]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE thread_goals (
                thread_id TEXT PRIMARY KEY,
                goal_id TEXT,
                objective TEXT,
                status TEXT,
                token_budget INTEGER,
                tokens_used INTEGER,
                time_used_seconds INTEGER,
                created_at_ms INTEGER,
                updated_at_ms INTEGER
            )
            """
        )
        for row in rows:
            conn.execute(
                "INSERT INTO thread_goals VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    row["thread_id"],
                    row.get("goal_id", "goal-" + row["thread_id"]),
                    row.get("objective", ""),
                    row.get("status", "usage_limited"),
                    row.get("token_budget"),
                    row.get("tokens_used", 0),
                    row.get("time_used_seconds", 0),
                    row.get("created_at_ms", 1_700_000_000_000),
                    row.get("updated_at_ms", 1_700_000_000_000),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def build_state_db(path: Path, rows: list[dict]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                name TEXT,
                title TEXT,
                preview TEXT,
                cwd TEXT,
                model TEXT,
                archived INTEGER,
                updated_at_ms INTEGER
            )
            """
        )
        for row in rows:
            conn.execute(
                "INSERT INTO threads VALUES (?,?,?,?,?,?,?,?)",
                (
                    row["id"],
                    row.get("name", ""),
                    row.get("title", ""),
                    row.get("preview", ""),
                    row.get("cwd", ""),
                    row.get("model", ""),
                    row.get("archived", 0),
                    row.get("updated_at_ms", 1_700_000_000_000),
                ),
            )
        conn.commit()
    finally:
        conn.close()


class GoalsReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.goals_db = self.root / "goals_1.sqlite"
        self.state_db = self.root / "state_5.sqlite"
        build_goals_db(
            self.goals_db,
            [
                {
                    "thread_id": "01aaa",
                    "objective": "继续任务",
                    "status": "usage_limited",
                    "tokens_used": 1_500_000,
                    "time_used_seconds": 90,
                    "updated_at_ms": 1_700_000_300_000,
                },
                {
                    "thread_id": "01bbb",
                    "objective": "完成任务",
                    "status": "paused",
                    "tokens_used": 200,
                    "time_used_seconds": 10,
                    "updated_at_ms": 1_700_000_200_000,
                },
                {
                    "thread_id": "01ccc",
                    "objective": "已完成",
                    "status": "complete",
                    "tokens_used": 42,
                    "updated_at_ms": 1_700_000_100_000,
                },
            ],
        )
        build_state_db(
            self.state_db,
            [
                {
                    "id": "01aaa",
                    "name": "修复 EXE 解包输出",
                    "title": "long title",
                    "cwd": "C:\\work\\riz",
                    "model": "gpt-6",
                },
                {"id": "01bbb", "name": "旧任务"},
            ],
        )
        self._old_env = {
            key: os.environ.get(key)
            for key in ("CODEX_SWITCHER_GOALS_DB", "CODEX_SWITCHER_STATE_DB")
        }
        os.environ["CODEX_SWITCHER_GOALS_DB"] = str(self.goals_db)
        os.environ["CODEX_SWITCHER_STATE_DB"] = str(self.state_db)

    def tearDown(self):
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_list_goals_joins_thread_metadata_newest_first(self):
        goals = list_goals()
        self.assertEqual([g["thread_id"] for g in goals], ["01aaa", "01bbb", "01ccc"])
        first = goals[0]
        self.assertEqual(first["thread_name"], "修复 EXE 解包输出")
        self.assertEqual(first["cwd"], "C:\\work\\riz")
        self.assertEqual(first["status_rpc"], "usageLimited")
        self.assertTrue(first["resumable"])
        self.assertFalse(goals[1]["resumable"])
        self.assertFalse(goals[2]["resumable"])
        # ``armable`` is the broader "resume can wake this up" set: quota-limited,
        # paused, or still active with a dead turn (post switch/desktop restart).
        self.assertTrue(first["armable"])
        self.assertTrue(goals[1]["armable"])
        self.assertFalse(goals[2]["armable"])
        self.assertEqual(first["tokens_used"], 1_500_000)
        self.assertIsNotNone(first["updated_at"])

    def test_missing_thread_row_is_tolerated(self):
        goals = list_goals()
        missing = [g for g in goals if g["thread_id"] == "01ccc"][0]
        self.assertEqual(missing["thread_name"], "")
        self.assertEqual(missing["cwd"], "")

    def test_get_goal(self):
        goal = get_goal("01bbb")
        self.assertIsNotNone(goal)
        self.assertEqual(goal["status"], "paused")
        self.assertIsNone(get_goal("nope"))

    def test_missing_databases_return_empty(self):
        os.environ["CODEX_SWITCHER_GOALS_DB"] = str(self.root / "absent.sqlite")
        self.assertEqual(list_goals(), [])
        self.assertEqual(config.goals_db_path(), self.root / "absent.sqlite")

    def test_invalid_goals_database_is_reported(self):
        path = self.root / "invalid.sqlite"
        path.write_bytes(b"not a sqlite database")
        os.environ["CODEX_SWITCHER_GOALS_DB"] = str(path)
        with self.assertRaises(sqlite3.Error):
            list_goals()

    def test_invalid_metadata_database_is_reported(self):
        path = self.root / "invalid-state.sqlite"
        path.write_bytes(b"not a sqlite database")
        os.environ["CODEX_SWITCHER_STATE_DB"] = str(path)
        with self.assertRaises(sqlite3.Error):
            list_goals()


if __name__ == "__main__":
    unittest.main()
