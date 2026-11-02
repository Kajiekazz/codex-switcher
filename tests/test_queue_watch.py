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


def insert_resume_row(path: Path, queued_id: str, thread_id: str, text: str = "继续") -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO queued_items VALUES (?, ?, ?, 0, ?, ?)",
            (
                queued_id,
                thread_id,
                json.dumps(
                    {
                        "UserInput": {
                            "content": [{"type": "text", "text": text}],
                            "client_id": "old-client-id",
                        }
                    }
                ),
                int(time.time() * 1000),
                int(time.time() * 1000),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def build_state_db(path: Path, rows=()) -> Path:
    """Minimal ``state_5.sqlite``: just the archived flag we read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, archived INTEGER, "
            "updated_at_ms INTEGER)"
        )
        for thread_id, archived in rows:
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, 0)", (thread_id, archived)
            )
        connection.commit()
    finally:
        connection.close()
    return path


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

    def test_a_relaunched_app_is_woken_immediately(self):
        """A switch/update restarts the app and drops the in-flight wake."""
        state = {"running": True, "pids": [111, 222]}
        # A long backoff makes it unambiguous that the wake came from the
        # relaunch, not from the normal retry schedule.
        watchdog = self._watchdog(probe=lambda: dict(state), backoff=(600,))
        watchdog.track(THREAD, QUEUED, observed=True)
        start = self.clock()

        watchdog.tick(start + 1)  # learns the process set; retry not due yet
        self.assertEqual(self.wakes, [])

        state["pids"] = [333, 444]  # same app, brand-new processes
        watchdog.tick(start + 61)
        self.assertEqual(self.wakes, [THREAD])
        entry = watchdog.for_thread(THREAD)
        self.assertTrue(entry["pending"])
        self.assertIn("restarted_at", entry)

    def test_surviving_processes_do_not_trigger_an_early_wake(self):
        state = {"running": True, "pids": [111, 222]}
        watchdog = self._watchdog(probe=lambda: dict(state), backoff=(600,))
        watchdog.track(THREAD, QUEUED, observed=True)
        start = self.clock()
        watchdog.tick(start + 1)
        state["pids"] = [111, 222, 555]  # a renderer came up
        watchdog.tick(start + 61)
        self.assertEqual(self.wakes, [])

    def test_an_archived_conversation_is_retired_not_delivered(self):
        """The row stays queued, but no turn will ever run: say so, stop waking."""
        archive = {"value": False}
        watchdog = self._watchdog(archived=lambda _tid: archive["value"])
        watchdog.track(THREAD, QUEUED, observed=True)
        self.clock.value += 61
        watchdog.tick()
        self.assertTrue(watchdog.for_thread(THREAD)["pending"])
        self.assertEqual(self.wakes, [THREAD])
        self.wakes.clear()

        archive["value"] = True
        self.clock.value += 61
        changed = watchdog.tick()
        self.assertEqual([item["queued_id"] for item in changed], [QUEUED])
        entry = watchdog.for_thread(THREAD)
        self.assertFalse(entry["pending"])
        # Never a delivery: the row is still in queue_1.sqlite.
        self.assertFalse(entry["delivered"])
        self.assertIsNone(entry["delivered_at"])
        self.assertEqual(entry["error"], "thread_archived")
        self.assertEqual(self.wakes, [])
        self.assertEqual(self.events[-1]["error"], "thread_archived")

        # A service restart must not resurrect it either.
        self.clock.value += 3600
        reloaded = self._watchdog(archived=lambda _tid: True)
        self.assertEqual(reloaded.pending(), [])
        reloaded.tick()
        self.assertEqual(self.wakes, [])

    def test_an_unknown_archive_state_keeps_waiting(self):
        """"Cannot tell" (old schema, missing db) must not abandon the entry."""
        watchdog = self._watchdog(archived=lambda _tid: None)
        watchdog.track(THREAD, QUEUED, observed=True)
        self.clock.value += 15
        watchdog.tick()
        entry = watchdog.for_thread(THREAD)
        self.assertTrue(entry["pending"])
        self.assertEqual(self.wakes, [THREAD])

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
                "CODEX_SWITCHER_STATE_DB",
            )
        }
        self.addCleanup(self._restore_env)
        os.environ["CODEX_SWITCHER_ENABLE_GOAL_RESUME"] = "1"
        os.environ["CODEX_SWITCHER_DATA"] = str(self.root / "data")
        self.db = build_queue_db(self.root / "queue_1.sqlite", [(QUEUED, THREAD)])
        os.environ["CODEX_SWITCHER_QUEUE_DB"] = str(self.db)
        # Never read the real ~/.codex/state_5.sqlite: whether THREAD happens to
        # be archived on this machine must not change test behaviour.
        self.state_db = self.root / "state_5.sqlite"
        os.environ["CODEX_SWITCHER_STATE_DB"] = str(self.state_db)

    def _restore_env(self):
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _service(self, desktop, clock=None, session=None):
        clock = clock or _Clock()
        session = session or FakeSession(goal_status="active", queue_consumed=False)
        service = GoalResumeService(
            session_factory=lambda: session,
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

    def test_an_already_queued_resume_is_reused_not_duplicated(self):
        """A second switch must not queue a second 继续 (two turns, wasted quota)."""
        insert_resume_row(self.db, "old-row", THREAD, "继续")
        session = FakeSession(goal_status="active", queue_consumed=False)
        # The app-server knows about the row that is already waiting.
        session.queue_items.append({"id": "old-row", "clientUserMessageId": "old-client-id"})
        service = self._service(_QuietDesktop(), session=session)

        service.start([THREAD], dry_run=False, confirm=True)
        job = self._wait_job(service)

        result = job["results"][0]
        self.assertTrue(result["ok"])
        self.assertEqual(result["queued_id"], "old-row")
        self.assertNotIn("thread/queue/add", session.methods())

        # The reused row is owned by the watchdog exactly like a fresh one.
        self.assertEqual(service.list()["queue_watch"]["pending"], 1)
        drop_row(self.db, "old-row")
        service._queue_watch.tick()
        last = service.list()["last_resume"][THREAD]
        self.assertTrue(last["delivered"])
        self.assertFalse(last["pending"])

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


    def test_dispatch_into_an_archived_conversation_is_not_promised(self):
        """An archived conversation cannot run a turn: don't queue, don't claim."""
        build_state_db(self.state_db, [(THREAD, 1)])
        session = FakeSession(goal_status="active", queue_consumed=False)
        service = self._service(_QuietDesktop(), session=session)
        service.start([THREAD], dry_run=False, confirm=True)
        job = self._wait_job(service)

        result = job["results"][0]
        self.assertTrue(result["ok"])
        self.assertTrue(result["queue_abandoned"])
        self.assertFalse(result["pending"])
        self.assertFalse(result["delivered"])
        # Nothing was queued: the row would never be drained.
        self.assertNotIn("thread/queue/add", session.methods())

        payload = service.list()
        self.assertEqual(payload["queue_watch"]["pending"], 0)
        last = payload["last_resume"][THREAD]
        self.assertFalse(last["pending"])
        self.assertFalse(last["delivered"])

        # Whatever the page renders, "已续跑" is ruled out by the data.
        persisted = json.loads(
            (self.root / "goal-resume-history.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[-1]
        )
        self.assertTrue(persisted["results"][0]["queue_abandoned"])
        self.assertFalse(persisted["results"][0]["delivered"])

    def test_a_row_for_an_archived_thread_is_not_adopted(self):
        """Adoption after a restart must not resurrect a row that cannot run."""
        build_state_db(self.state_db, [(THREAD, 1)])
        finished = 1793582832.0
        insert_resume_row(self.db, "legacy-row", THREAD, "继续")
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
        payload = service.list()
        self.assertEqual(payload["queue_watch"]["pending"], 0)
        self.assertNotIn("legacy-row", history_path.read_text(encoding="utf-8"))

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
