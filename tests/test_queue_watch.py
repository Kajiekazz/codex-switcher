"""The queued resume message stays owned until the desktop app takes it.

The dispatch only waits a dozen seconds, but the desktop app may need minutes
(another thread is mid-turn) and loses a wake sent while it restarts for a
switch. These tests pin the watchdog contract:

* truth is the queue row — gone means delivered, still there means pending;
* an unreadable database is "cannot tell", never a delivery;
* a wake is retried on a 15/30/60/120/300/600 s backoff, then reported expired;
* the transition is persisted and reaches ``/api/goals`` (history + badge).
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from codex_switcher import config
from codex_switcher.goal_resume import GoalResumeService
from codex_switcher.queue_watch import QueueWatchdog, queue_row_present

from test_goal_resume import FakeSession


THREAD = "01a17a9b-88bf-7620-a979-c2ecbf5dcc26"
QUEUED = "queue-1"


def iso_at(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


class _QuietDesktop:
    """Never launches anything; records that a wake was attempted."""

    def __init__(self):
        self.wakes: list[str] = []

    def wake_thread(self, thread_id):
        self.wakes.append(thread_id)
        return {"ok": False, "skipped": True, "reason": "desktop_not_running"}

SCHEMA = """
CREATE TABLE queued_items (
    id TEXT PRIMARY KEY NOT NULL,
    thread_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    queue_order INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
"""


def build_queue_db(path: Path, rows=()) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA)
        for queued_id, thread_id in rows:
            connection.execute(
                "INSERT INTO queued_items VALUES (?, ?, '{}', 0, 0, 0)",
                (queued_id, thread_id),
            )
        connection.commit()
    finally:
        connection.close()
    return path


def drop_row(path: Path, queued_id: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("DELETE FROM queued_items WHERE id = ?", (queued_id,))
        connection.commit()
    finally:
        connection.close()


class _Clock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += float(seconds)


class RowProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_row_present_absent_and_unreadable(self):
        db = build_queue_db(self.root / "q.sqlite", [(QUEUED, THREAD)])
        self.assertTrue(queue_row_present(db, QUEUED))
        self.assertFalse(queue_row_present(db, "missing"))
        # Cannot tell: the caller must not read this as a delivery.
        self.assertIsNone(queue_row_present(self.root / "absent.sqlite", QUEUED))
        bare = self.root / "bare.sqlite"
        sqlite3.connect(bare).close()
        self.assertIsNone(queue_row_present(bare, QUEUED))


class QueueWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = build_queue_db(self.root / "queue_1.sqlite", [(QUEUED, THREAD)])
        self.clock = _Clock()
        self.wakes: list[str] = []
        self.events: list[dict] = []

    def _watchdog(self, **options):
        options.setdefault("wake", self._wake)
        options.setdefault("queue_db", self.db)
        options.setdefault("state_path", self.root / "state.json")
        options.setdefault("clock", self.clock)
        options.setdefault("autostart", False)
        options.setdefault("on_change", self.events.append)
        watchdog = QueueWatchdog(**options)
        self.addCleanup(watchdog.stop)
        return watchdog

    def _wake(self, thread_id: str) -> dict:
        self.wakes.append(thread_id)
        return {"ok": True, "skipped": False, "url": f"codex://threads/wake/{thread_id}"}

    def test_a_present_row_stays_pending(self):
        watchdog = self._watchdog()
        entry = watchdog.track(THREAD, QUEUED, job_id="job", observed=True)
        self.assertTrue(entry["pending"])
        self.assertFalse(entry["delivered"])
        self.assertEqual(watchdog.pending(), [entry])
        watchdog.tick(self.clock() + 1)
        # Still there and the first retry is not due yet.
        self.assertEqual(self.wakes, [])
        self.assertTrue(watchdog.pending())

    def test_wake_is_retried_with_backoff(self):
        watchdog = self._watchdog()
        watchdog.track(THREAD, QUEUED, observed=True)
        start = self.clock()
        delays = []
        for index in range(7):
            self.clock.value = start + 601 * (index + 1)
            watchdog.tick()
            self.assertEqual(len(self.wakes), index + 1)
            entry = watchdog.for_thread(THREAD)
            delays.append(round(float(entry["next_wake_at"]) - self.clock.value))
        self.assertEqual(delays, [15, 30, 60, 120, 300, 600, 600])

    def test_a_taken_row_is_delivered(self):
        watchdog = self._watchdog()
        watchdog.track(THREAD, QUEUED, observed=True)
        drop_row(self.db, QUEUED)
        changed = watchdog.tick()
        self.assertEqual([item["queued_id"] for item in changed], [QUEUED])
        entry = watchdog.for_thread(THREAD)
        self.assertFalse(entry["pending"])
        self.assertTrue(entry["delivered"])
        self.assertTrue(entry["delivered_at"])
        self.assertEqual(len(self.events), 1)
        self.assertTrue(self.events[0]["delivered"])
        # Delivery is final: no more wake attempts.
        self.clock.value += 3600
        watchdog.tick()
        self.assertEqual(self.wakes, [])

    def test_absent_row_without_observation_is_not_a_delivery(self):
        """A queue database we never saw the row in proves nothing."""
        watchdog = self._watchdog(queue_db=self.root / "missing.sqlite")
        watchdog.track(THREAD, QUEUED, observed=False)
        watchdog.tick(self.clock() + 60)
        entry = watchdog.for_thread(THREAD)
        self.assertTrue(entry["pending"])
        self.assertFalse(entry["delivered"])
        # The wake is still retried: the message may well be queued elsewhere.
        self.assertEqual(self.wakes, [THREAD])

    def test_ttl_expiry_stops_retrying_and_is_exposed(self):
        watchdog = self._watchdog(ttl_seconds=60)
        watchdog.track(THREAD, QUEUED, observed=True)
        self.clock.value += 61
        watchdog.tick()
        entry = watchdog.for_thread(THREAD)
        self.assertFalse(entry["pending"])
        self.assertFalse(entry["delivered"])
        self.assertEqual(entry["error"], "queue_ttl_expired")
        self.assertEqual(self.events[0]["error"], "queue_ttl_expired")
        self.clock.value += 3600
        watchdog.tick()
        self.assertEqual(self.wakes, [])

    def test_skipped_wake_is_retried_and_recorded(self):
        def wake(_thread_id):
            return {"ok": False, "skipped": True, "reason": "desktop_not_running"}

        watchdog = self._watchdog(wake=wake)
        watchdog.track(THREAD, QUEUED, observed=True)
        self.clock.value += 15
        watchdog.tick()
        entry = watchdog.for_thread(THREAD)
        self.assertEqual(entry["attempts"], 1)
        self.assertEqual(entry["last_wake"]["reason"], "desktop_not_running")
        self.assertTrue(entry["pending"])

    def test_state_survives_a_restart(self):
        watchdog = self._watchdog()
        watchdog.track(THREAD, QUEUED, job_id="job-1", observed=True)
        state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["entries"][0]["queued_id"], QUEUED)

        reloaded = self._watchdog()
        entry = reloaded.for_thread(THREAD)
        self.assertIsNotNone(entry)
        self.assertTrue(entry["pending"])
        self.assertEqual(entry["job_id"], "job-1")
        drop_row(self.db, QUEUED)
        reloaded.tick()
        self.assertTrue(reloaded.for_thread(THREAD)["delivered"])


class ServiceQueueWatchTests(unittest.TestCase):
    """End-to-end: dispatch -> pending -> desktop takes it -> 已续跑."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self._old = {
            key: os.environ.get(key)
            for key in (
                "CODEX_SWITCHER_ENABLE_GOAL_RESUME",
                "CODEX_SWITCHER_DATA",
                "CODEX_SWITCHER_QUEUE_DB",
            )
        }
        self.addCleanup(self._restore_env)
        os.environ["CODEX_SWITCHER_ENABLE_GOAL_RESUME"] = "1"
        os.environ["CODEX_SWITCHER_DATA"] = str(self.root / "data")
        self.db = build_queue_db(self.root / "queue_1.sqlite", [(QUEUED, THREAD)])
        os.environ["CODEX_SWITCHER_QUEUE_DB"] = str(self.db)

    def _restore_env(self):
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _service(self, desktop, clock=None):
        clock = clock or _Clock()
        service = GoalResumeService(
            session_factory=lambda: FakeSession(
                goal_status="active", queue_consumed=False
            ),
            goals_provider=lambda: [],
            history_path=self.root / "goal-resume-history.jsonl",
            sessions_provider=lambda **kwargs: {
                "total": 0, "pinned": [], "projects": [], "recent": [], "counts": {},
            },
            desktop_client=desktop,
            sleep=clock.sleep,
            clock=clock,
        )
        self.addCleanup(service._queue_watch.stop)
        self.addCleanup(lambda: service._thread and service._thread.join(timeout=5))
        return service

    def _wait_job(self, service, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = service.status()["job"]
            if job and job.get("state") != "running":
                return job
            time.sleep(0.02)
        self.fail(f"job did not finish: {service.status()}")

    def test_pending_dispatch_is_watched_until_the_row_is_taken(self):
        class Desktop:
            def __init__(self):
                self.wakes = []

            def wake_thread(self, thread_id):
                self.wakes.append(thread_id)
                return {"ok": True, "skipped": False, "url": f"codex://threads/wake/{thread_id}"}

        desktop = Desktop()
        service = self._service(desktop)
        service.start([THREAD], dry_run=False, confirm=True)
        job = self._wait_job(service)

        result = job["results"][0]
        self.assertTrue(result["pending"])
        self.assertFalse(result["delivered"])
        self.assertEqual(result["queued_id"], QUEUED)

        payload = service.list()
        self.assertEqual(payload["queue_watch"]["pending"], 1)
        last = payload["last_resume"][THREAD]
        self.assertTrue(last["pending"])
        self.assertFalse(last["delivered"])
        self.assertEqual(last["watch"]["queued_id"], QUEUED)

        # The history must keep the truth: still queued, not "已续跑".
        history = json.loads(
            (self.root / "goal-resume-history.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )
        self.assertTrue(history["results"][0]["pending"])
        self.assertFalse(history["results"][0]["delivered"])

        # The desktop app finally takes the row.
        drop_row(self.db, QUEUED)
        service._queue_watch.tick()

        payload = service.list()
        self.assertEqual(payload["queue_watch"]["pending"], 0)
        last = payload["last_resume"][THREAD]
        self.assertTrue(last["delivered"])
        self.assertFalse(last["pending"])
        self.assertTrue(last["delivered_at"])

        # Only now does the badge say "已续跑" — and it survives a restart.
        lines = (
            (self.root / "goal-resume-history.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        persisted = json.loads(lines[-1])
        self.assertTrue(persisted["results"][0]["delivered"])
        self.assertFalse(persisted["results"][0]["pending"])
        self.assertEqual(persisted["results"][0]["queued_id"], QUEUED)
        self.assertEqual(config.queue_db_path(), self.db)


    def test_history_without_a_queue_id_is_adopted_on_restart(self):
        """A row queued by an older build (no queued_id) is not forgotten."""
        finished = 1793582832.0  # matches the row's created_at_ms below
        connection = sqlite3.connect(self.db)
        try:
            connection.execute(
                "INSERT INTO queued_items VALUES (?, ?, ?, 0, ?, ?)",
                (
                    "legacy-row",
                    THREAD,
                    json.dumps(
                        {
                            "UserInput": {
                                "content": [{"type": "text", "text": "继续"}],
                                "client_id": "abc",
                            }
                        }
                    ),
                    int(finished) * 1000,
                    int(finished) * 1000,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        history_path = self.root / "goal-resume-history.jsonl"
        history_path.write_text(
            json.dumps(
                {
                    "id": "old-job",
                    "started_at": "2026-11-02T01:26:59+00:00",
                    "finished_at": iso_at(finished),
                    "dry_run": False,
                    "state": "done",
                    "results": [
                        {
                            "thread_id": THREAD,
                            "ok": True,
                            "status_before": "usageLimited",
                            "status_after": "active",
                            "turns": 0,
                            "error": None,
                        }
                    ],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        service = self._service(_QuietDesktop())
        last = service.list()["last_resume"][THREAD]
        self.assertEqual(last["queued_id"], "legacy-row")
        self.assertTrue(last["pending"])
        self.assertEqual(service.list()["queue_watch"]["pending"], 1)
        self.assertIn("legacy-row", history_path.read_text(encoding="utf-8"))

        drop_row(self.db, "legacy-row")
        service._queue_watch.tick()
        last = service.list()["last_resume"][THREAD]
        self.assertTrue(last["delivered"])
        self.assertFalse(last["pending"])


if __name__ == "__main__":
    unittest.main()
