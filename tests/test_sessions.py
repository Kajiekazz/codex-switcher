"""Session browser (pinned / projects / recent) for the dashboard."""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_switcher import config
from codex_switcher.goals import list_sessions

from test_goals import build_goals_db

ENV_KEYS = (
    "CODEX_SWITCHER_STATE_DB",
    "CODEX_SWITCHER_GOALS_DB",
)

PINNED_SECTION = "sec-pinned"


def build_state_db(path: Path, threads: list[dict], projects: list[dict]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                name TEXT,
                title TEXT,
                preview TEXT,
                first_user_message TEXT,
                cwd TEXT,
                git_branch TEXT,
                model TEXT,
                tokens_used INTEGER,
                archived INTEGER,
                is_pinned INTEGER,
                thread_section_id TEXT,
                thread_source TEXT,
                agent_nickname TEXT,
                source TEXT,
                updated_at_ms INTEGER,
                updated_at INTEGER,
                recency_at_ms INTEGER
            )
            """
        )
        conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, position INTEGER)")
        conn.execute(
            "CREATE TABLE project_roots (project_id TEXT, position INTEGER, path TEXT)"
        )
        conn.execute("CREATE TABLE thread_sections (id TEXT PRIMARY KEY, name TEXT)")
        conn.execute(
            "INSERT INTO thread_sections VALUES (?, ?)", (PINNED_SECTION, "Pinned")
        )
        for project in projects:
            conn.execute(
                "INSERT INTO projects VALUES (?, ?, ?)",
                (project["id"], project.get("name", ""), project.get("position", 0)),
            )
            for index, root in enumerate(project.get("roots", [])):
                conn.execute(
                    "INSERT INTO project_roots VALUES (?, ?, ?)",
                    (project["id"], index, root),
                )
        for thread in threads:
            conn.execute(
                """
                INSERT INTO threads VALUES
                    (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    thread["id"],
                    thread.get("name", ""),
                    thread.get("title", ""),
                    thread.get("preview", ""),
                    thread.get("first_user_message", ""),
                    thread.get("cwd", ""),
                    thread.get("git_branch", ""),
                    thread.get("model", ""),
                    thread.get("tokens_used", 0),
                    thread.get("archived", 0),
                    thread.get("is_pinned", 0),
                    thread.get("thread_section_id"),
                    thread.get("thread_source", "user"),
                    thread.get("agent_nickname"),
                    thread.get("source", ""),
                    thread.get("updated_at_ms", 1_700_000_000_000),
                    thread.get("updated_at", 1_700_000_000),
                    thread.get("recency_at_ms", 1_700_000_000_000),
                ),
            )
        conn.commit()
    finally:
        conn.close()


class SessionBrowserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        state_db = self.root / "state_5.sqlite"
        goals_db = self.root / "goals_1.sqlite"
        build_state_db(
            state_db,
            threads=[
                {
                    "id": "thread-pinned",
                    "name": "置顶会话",
                    "cwd": "\\\\?\\C:\\work\\alpha",
                    "thread_section_id": PINNED_SECTION,
                    "updated_at_ms": 1_700_000_900_000,
                },
                {
                    "id": "thread-alpha",
                    "name": "Alpha 主会话",
                    "cwd": "\\\\?\\C:\\work\\alpha",
                    "updated_at_ms": 1_700_000_800_000,
                },
                {
                    "id": "thread-alpha-child",
                    "name": "子目录会话",
                    "cwd": "\\\\?\\C:\\work\\alpha\\sub",
                    "updated_at_ms": 1_700_000_700_000,
                },
                {
                    "id": "thread-beta",
                    "title": "Beta 只有标题",
                    "cwd": "C:/work/beta",
                    "updated_at_ms": 1_700_000_600_000,
                },
                {
                    "id": "thread-loose",
                    "first_user_message": "游离会话",
                    "cwd": "C:\\temp\\loose",
                    "recency_at_ms": 1_700_000_100_000,
                    "updated_at_ms": 1_700_000_100_000,
                },
                {
                    "id": "thread-sub-no-goal",
                    "cwd": "\\\\?\\C:\\work\\alpha",
                    "thread_source": "subagent",
                    "agent_nickname": "Euclid",
                    "updated_at_ms": 1_700_000_500_000,
                },
                {
                    "id": "thread-sub-goal",
                    "cwd": "\\\\?\\C:\\work\\alpha",
                    "thread_source": "subagent",
                    "agent_nickname": "Turing",
                    "updated_at_ms": 1_700_000_400_000,
                },
                {
                    # Real-world case: a VS Code session the DB tags "subagent"
                    # although the user wrote the prompt and there is no spawn.
                    "id": "thread-vscode-mislabeled",
                    "name": "用户的 VS Code 会话",
                    "cwd": "\\\\?\\C:\\work\\alpha",
                    "thread_source": "subagent",
                    "source": "vscode",
                    "updated_at_ms": 1_700_000_750_000,
                },
                {
                    "id": "thread-spawn",
                    "cwd": "\\\\?\\C:\\work\\alpha",
                    "thread_source": "subagent",
                    "source": '{"subagent":{"thread_spawn":{"parent_thread_id":"thread-alpha","depth":1,"agent_nickname":"Euclid"}}}',
                    "agent_nickname": "Euclid",
                    "updated_at_ms": 1_700_000_650_000,
                },
            ],
            projects=[
                {
                    "id": "proj-alpha",
                    "name": "alpha",
                    "position": 1,
                    "roots": ["C:\\work\\alpha"],
                },
                {
                    "id": "proj-beta",
                    "name": "beta",
                    "position": 0,
                    "roots": ["C:\\work\\beta"],
                },
            ],
        )
        build_goals_db(
            goals_db,
            [
                {"thread_id": "thread-pinned", "status": "usage_limited"},
                {"thread_id": "thread-alpha", "status": "active"},
                {"thread_id": "thread-sub-goal", "status": "paused"},
            ],
        )
        self._old = {key: os.environ.get(key) for key in ENV_KEYS}
        os.environ["CODEX_SWITCHER_STATE_DB"] = str(state_db)
        os.environ["CODEX_SWITCHER_GOALS_DB"] = str(goals_db)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_pinned_projects_and_recent(self):
        sessions = list_sessions()
        pinned_ids = [item["thread_id"] for item in sessions["pinned"]]
        self.assertEqual(pinned_ids, ["thread-pinned"])
        self.assertEqual(
            [project["name"] for project in sessions["projects"]],
            ["beta", "alpha"],
        )
        alpha = sessions["projects"][1]
        self.assertEqual(
            [item["thread_id"] for item in alpha["items"]],
            # thread-sub-goal is a spawned agent thread but carries a goal, so
            # it is always listed even though subagents are hidden by default;
            # thread-vscode-mislabeled is a user session the DB mislabels.
            [
                # A pinned thread is listed twice, exactly like Codex: once under
                # 置顶 and once inside its project. The project copy is the one a
                # user looks for when 置顶 already scrolled past.
                "thread-pinned",
                "thread-alpha",
                "thread-vscode-mislabeled",
                "thread-alpha-child",
                "thread-sub-goal",
            ],
        )
        self.assertEqual([item["thread_id"] for item in sessions["recent"]], ["thread-loose"])
        self.assertEqual(sessions["counts"]["goals"], 3)
        self.assertEqual(sessions["counts"]["pinned"], 1)

    def test_goal_flags_are_attached_to_threads(self):
        sessions = list_sessions()
        pinned = sessions["pinned"][0]
        self.assertTrue(pinned["has_goal"])
        self.assertEqual(pinned["goal_status"], "usage_limited")
        self.assertTrue(pinned["resumable"])
        alpha_items = {
            item["thread_id"]: item for item in sessions["projects"][1]["items"]
        }
        alpha = alpha_items["thread-alpha"]
        self.assertTrue(alpha["has_goal"])
        self.assertFalse(alpha["resumable"])
        child = alpha_items["thread-alpha-child"]
        self.assertFalse(child["has_goal"])
        self.assertIsNone(child["goal_status"])

    def test_subagents_hidden_but_goal_threads_always_listed(self):
        sessions = list_sessions()
        flat = [
            item["thread_id"]
            for project in sessions["projects"]
            for item in project["items"]
        ]
        self.assertNotIn("thread-sub-no-goal", flat)
        self.assertIn("thread-sub-goal", flat)
        self.assertEqual(sessions["counts"]["subagents"], 2)

        with_subagents = list_sessions(include_subagents=True)
        flat_all = [
            item["thread_id"]
            for project in with_subagents["projects"]
            for item in project["items"]
        ]
        self.assertIn("thread-sub-no-goal", flat_all)
        self.assertEqual(with_subagents["counts"]["subagents"], 0)

    def test_vscode_thread_tagged_subagent_is_user_visible(self):
        """Real data has VS Code sessions stored as thread_source='subagent'."""
        sessions = list_sessions()
        items = {
            item["thread_id"]: item
            for project in sessions["projects"]
            for item in project["items"]
        }
        self.assertIn("thread-vscode-mislabeled", items)
        self.assertFalse(items["thread-vscode-mislabeled"]["internal"])
        # A real spawn edge stays hidden.
        self.assertNotIn("thread-spawn", items)
        self.assertEqual(sessions["counts"]["subagents"], 2)

    def test_pinned_agent_thread_is_always_listed(self):
        sessions = list_sessions(include_subagents=False)
        ids = [item["thread_id"] for item in sessions["pinned"]]
        self.assertEqual(ids, ["thread-pinned"])

    def test_pinned_thread_is_not_double_counted_in_shown(self):
        sessions = list_sessions()
        project_ids = [
            item["thread_id"]
            for project in sessions["projects"]
            for item in project["items"]
        ]
        # Same thread in two sections, but "shown" counts distinct threads.
        self.assertIn("thread-pinned", project_ids)
        self.assertEqual(len(project_ids), 6)
        self.assertEqual(sessions["shown"], 7)

    def test_titles_fall_back_to_message_or_nickname(self):
        sessions = list_sessions(include_subagents=True)
        items = {
            item["thread_id"]: item
            for project in sessions["projects"]
            for item in project["items"]
        }
        self.assertEqual(items["thread-alpha"]["title"], "Alpha 主会话")
        self.assertEqual(items["thread-beta"]["title"], "Beta 只有标题")
        self.assertEqual(
            sessions["recent"][0]["title"], "游离会话"
        )

    def test_missing_databases_return_empty_shape(self):
        os.environ["CODEX_SWITCHER_STATE_DB"] = str(self.root / "nope.sqlite")
        sessions = list_sessions()
        self.assertEqual(sessions["total"], 0)
        self.assertEqual(sessions["pinned"], [])
        self.assertEqual(sessions["projects"], [])
        self.assertEqual(sessions["recent"], [])


if __name__ == "__main__":
    unittest.main()
