"""Continuation semantics using local files and queue doubles, never live chats."""
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codex_switcher import execution, settings
from codex_switcher.goal_resume import GoalResumeService, set_auto_resume
from test_goal_resume import FakeSession, NullDesktop, _FakeClock


class ExecutionTests(unittest.TestCase):
    def test_turn_evidence_needs_a_start_and_a_writer_and_tracks_the_last_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db, rollout = root / "state.sqlite", root / "rollout.jsonl"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE threads(id TEXT, rollout_path TEXT, archived INTEGER)")
            conn.execute("INSERT INTO threads VALUES ('chat', ?, 0)", (str(rollout),))
            conn.commit()
            conn.close()
            def event(kind, **values):
                with rollout.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"type": "event_msg", "timestamp": "2026-11-02T10:00:00Z",
                                             "payload": {"type": kind, "turn_id": "turn", **values}}) + "\n")
            with mock.patch("codex_switcher.config.state_db_path", return_value=db), mock.patch(
                "codex_switcher.goals.held_thread_ids", return_value={"chat"}
            ):
                event("task_started")
                self.assertEqual(execution.snapshot_threads(["chat"])["chat"]["state"], "running")
                with mock.patch("codex_switcher.goals.held_thread_ids", return_value=set()):
                    self.assertEqual(execution.snapshot_threads(["chat"])["chat"]["state"], "unknown")
                event("error", message="usage_limit_reached")
                event("task_complete")
                state = execution.snapshot_threads(["chat"])["chat"]
                self.assertEqual(state["state"], "ended")
                self.assertTrue(state["usage_limited"])
                event("task_started")
                self.assertFalse(execution.snapshot_threads(["chat"])["chat"]["usage_limited"])


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patch = mock.patch.dict(os.environ, {
            "CODEX_SWITCHER_DATA": str(self.root),
            "CODEX_SWITCHER_STATE_DB": str(self.root / "absent.sqlite"),
            "CODEX_SWITCHER_QUEUE_DB": str(self.root / "queue.sqlite"),
            "CODEX_SWITCHER_AUTO_RESUME_AFTER_SWITCH": "1",
            "CODEX_SWITCHER_AUTO_RESUME_THREADS": "[]",
        })
        patch.start()
        self.addCleanup(patch.stop)
        settings.reset_cache()
        self.addCleanup(settings.reset_cache)
        patch = mock.patch("codex_switcher.config.goal_resume_watchdog_enabled", return_value=False)
        patch.start()
        self.addCleanup(patch.stop)

    def service(self, factory, goals=(), states=None):
        clock = _FakeClock()
        return GoalResumeService(
            session_factory=factory, goals_provider=lambda: list(goals),
            execution_provider=lambda ids: {tid: (states or {}).get(tid, {}) for tid in ids},
            sessions_provider=lambda **kwargs: {}, desktop_client=NullDesktop(),
            history_path=self.root / "history.jsonl", sleep=clock.sleep, clock=clock,
        )

    def wait(self, service):
        deadline = time.monotonic() + 5
        while service.running() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(service.running())
        return service.status()["job"]

    def test_continue_once_accepts_no_goal_and_preserves_a_completed_goal(self):
        for status in (None, "complete", "blocked", "paused", "usageLimited"):
            with self.subTest(status=status):
                session = FakeSession(goal_status=status)
                service = self.service(lambda: session)
                service.start(["ordinary"], dry_run=False, confirm=True, continue_once=True)
                result = self.wait(service)["results"][0]
                self.assertTrue(result["ok"])
                self.assertIn("thread/queue/add", session.methods())
                self.assertNotIn("thread/goal/set", session.methods())

    def test_auto_uses_the_pre_switch_snapshot_and_does_not_revive_idle_or_paused_chats(self):
        goals = [{"thread_id": tid, "status": status} for tid, status in (
            ("paused", "paused"), ("complete", "complete"), ("idle", "active"),
        )]
        states = {"running": {"state": "running"}, "limited": {"usage_limited": True}}
        for tid in ("running", "limited", "paused", "complete", "idle"):
            set_auto_resume(tid, True)
        session = FakeSession(goal_status=None)
        service = self.service(lambda: session, goals, states)
        context = service.capture_before_switch()
        states.clear()  # A restart has now destroyed the previous writer state.
        outcome = service.on_account_switched(SimpleNamespace(
            target_email="new@example.com", resume_context=context,
            desktop_restart={"ok": True},
        ))
        self.assertEqual(outcome["thread_ids"], ["running", "limited"])
        self.wait(service)

    def test_running_chat_is_not_nudged_when_the_switch_did_not_interrupt_it(self):
        set_auto_resume("running", True)
        service = self.service(FakeSession, states={"running": {"state": "running"}})
        outcome = service.on_account_switched(SimpleNamespace(
            resume_context=service.capture_before_switch(), desktop_restart={"ok": True, "skipped": True},
        ))
        self.assertEqual(outcome["skipped"], "no_armable_goal")
        self.assertFalse(service.running())

    def test_a_turn_that_finishes_during_the_switch_is_not_restarted(self):
        set_auto_resume("running", True)
        states = {"running": {"state": "running", "turn_id": "before"}}
        service = self.service(FakeSession, states=states)
        context = service.capture_before_switch()
        states["running"] = {"state": "ended", "turn_id": "before"}
        outcome = service.on_account_switched(SimpleNamespace(
            resume_context=context, desktop_restart={"ok": True},
        ))
        self.assertEqual(outcome["skipped"], "no_armable_goal")
        self.assertEqual(outcome["skipped_goals"][0]["reason"], "finished_or_already_recovered")

    def queue_service(self, block=False):
        rows, calls = [], []
        entered, release = threading.Event(), threading.Event()
        class Queue(FakeSession):
            def request(self, method, params=None, timeout=30):
                calls.append((method, params))
                if method == "thread/queue/add":
                    if block and params["threadId"] == "one":
                        entered.set()
                        release.wait(3)
                    row = {"id": params["clientUserMessageId"], "thread": params["threadId"]}
                    rows.append(row)
                    return {"queuedSubmission": row}
                if method == "thread/queue/list":
                    return {"data": [row for row in rows if row["thread"] == params["threadId"]]}
                if method == "thread/queue/delete":
                    row = next((row for row in rows if row["id"] == params["queuedSubmissionId"]), None)
                    if row:
                        rows.remove(row)
                    return {"deleted": row is not None}
                return super().request(method, params, timeout)
        return self.service(lambda: Queue(goal_status=None)), rows, calls, entered, release

    def test_cancel_deletes_our_pending_message_and_does_not_claim_to_stop_a_turn(self):
        service, rows, calls, _, _ = self.queue_service()
        service.start(["one"], dry_run=False, confirm=True)
        self.wait(service)
        self.assertEqual(len(rows), 1)
        result = service.cancel_queued(["one"])["results"][0]
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(rows, [])
        self.assertTrue(service._last_resume_by_thread()["one"]["cancelled"])
        self.assertFalse(any(method == "turn/interrupt" for method, _ in calls))

    def test_cancel_during_add_removes_that_message_and_still_dispatches_other_chats(self):
        service, rows, _, entered, release = self.queue_service(block=True)
        service.start(["one", "two"], dry_run=False, confirm=True)
        self.assertTrue(entered.wait(2))
        self.assertEqual(service.cancel_queued(["one"])["results"][0]["state"], "cancelling")
        release.set()
        job = self.wait(service)
        self.assertTrue(job["results"][0]["cancelled"])
        self.assertEqual([row["thread"] for row in rows], ["two"])

    def test_preview_does_not_lose_the_ability_to_withdraw_an_existing_message(self):
        service, rows, _, _, _ = self.queue_service()
        service.start(["one"], dry_run=False, confirm=True)
        self.wait(service)
        service.start(["one"], dry_run=True, continue_once=True)
        self.wait(service)
        self.assertTrue(service._pending_by_thread()["one"]["owned_queue"])
        self.assertEqual(service.cancel_queued(["one"])["results"][0]["state"], "cancelled")
        self.assertFalse(rows)

    def test_reused_queue_history_does_not_resurrect_an_already_consumed_message(self):
        service, rows, _, _, _ = self.queue_service()
        service.start(["one"], dry_run=False, confirm=True)
        self.wait(service)
        queued_id = rows[0]["id"]
        with mock.patch("codex_switcher.goal_resume.first_resume_row", return_value={"id": queued_id}):
            service.start(["one"], dry_run=False, confirm=True)
            self.wait(service)
        service._apply_queue_entry({"thread_id": "one", "queued_id": queued_id, "delivered": True})
        self.assertEqual(service._pending_by_thread(), {})

    def test_losing_the_delete_race_clears_pending_without_claiming_cancellation(self):
        service, rows, _, _, _ = self.queue_service()
        service.start(["one"], dry_run=False, confirm=True)
        self.wait(service)
        rows.clear()  # The desktop took it just before the delete request.
        outcome = service.cancel_queued(["one"])["results"][0]
        self.assertEqual(outcome["state"], "already_removed")
        self.assertEqual(service._pending_by_thread(), {})
        self.assertFalse(service._last_resume_by_thread()["one"]["cancelled"])

    def test_a_user_queued_continue_is_never_owned_or_deleted(self):
        service, rows, calls, _, _ = self.queue_service()
        rows.append({"id": "user-message", "thread": "one"})
        with mock.patch("codex_switcher.goal_resume.first_resume_row", return_value={"id": "user-message"}):
            service.start(["one"], dry_run=False, confirm=True)
            self.wait(service)
        self.assertEqual(service.cancel_queued(["one"])["results"][0]["state"], "no_owned_message")
        self.assertEqual(len(rows), 1)
        self.assertNotIn("thread/queue/delete", [method for method, _ in calls])
