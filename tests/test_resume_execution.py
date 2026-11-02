"""A real receipt needs this queue message in a new turn, not a vanished row."""
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from codex_switcher import settings
from codex_switcher.goal_resume import GoalResumeService
from codex_switcher.resume_execution import ResumeExecutionTracker, rollout_cursor
from test_goal_resume import FakeSession, NullDesktop


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rollout = self.root / "rollout.jsonl"
        self.rollout.touch()
        self.now = 1793624260.0
        self.present = False
        self.changes = []
        db = self.root / "state.sqlite"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE threads(id TEXT, rollout_path TEXT, archived INTEGER)")
            conn.execute("INSERT INTO threads VALUES ('chat', ?, 0)", (str(self.rollout),))
        # sqlite context managers commit, but Windows requires explicit close.
        conn.close()
        environment = mock.patch.dict(os.environ, {
            "CODEX_SWITCHER_DATA": str(self.root / "data"),
            "CODEX_SWITCHER_STATE_DB": str(db),
            "CODEX_SWITCHER_QUEUE_DB": str(self.root / "absent-queue.sqlite"),
            "CODEX_SWITCHER_GOAL_RESUME_WATCHDOG": "0",
        })
        environment.start()
        self.addCleanup(environment.stop)
        settings.reset_cache()
        self.addCleanup(settings.reset_cache)
        presence = mock.patch("codex_switcher.resume_execution.queue_row_present", side_effect=lambda *_: self.present)
        presence.start()
        self.addCleanup(presence.stop)
        self.tracker = self.new_tracker()

    def new_tracker(self):
        tracker = ResumeExecutionTracker(
            state_path=self.root / "receipts.json", clock=lambda: self.now,
            held=lambda: {"chat"}, autostart=False,
            on_change=lambda *values: self.changes.append(values),
        )
        self.addCleanup(tracker.stop)
        return tracker

    def iso(self):
        return datetime.fromtimestamp(self.now, timezone.utc).isoformat(timespec="milliseconds")

    def write(self, category, payload):
        with self.rollout.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": category, "timestamp": self.iso(), "payload": payload}) + "\n")

    def start_turn(self, tid="new", *, trigger="queue", text="继续", with_message=True):
        self.write("event_msg", {"type": "task_started", "turn_id": tid, "turn_attribution": {"turn_trigger": trigger}})
        if with_message:
            self.message(tid, text)

    def message(self, tid, text):
        self.write("response_item", {"type": "message", "id": "msg-" + tid, "role": "user",
                                    "content": [{"type": "input_text", "text": text}],
                                    "internal_chat_message_metadata_passthrough": {"turn_id": tid}})

    def finish(self, tid="new", error=None, kind="task_complete"):
        self.write("event_msg", {"type": kind, "turn_id": tid, "error": error})

    def track(self, *, pending=False, tracker=None):
        (tracker or self.tracker).track("chat", {
            "queued_id": "queue", "client_message_id": "client", "pending": pending,
            "delivered": not pending, "delivered_at": self.iso() if not pending else None,
        }, requested_at=self.iso(), cursor=rollout_cursor("chat"))

    def receipt(self):
        self.tracker.reconcile()
        return self.tracker.for_queue("queue")

    def test_own_queue_message_goes_from_confirming_to_running_to_completed(self):
        self.track()
        self.assertEqual(self.receipt()["state"], "confirming")
        self.start_turn(with_message=False)
        self.assertEqual(self.receipt()["state"], "confirming")
        self.message("new", "继续")
        self.assertEqual(self.receipt()["state"], "running")
        self.now += 60
        self.finish()
        result = self.receipt()
        self.assertEqual((result["state"], result["turn_id"]), ("completed", "new"))
        self.assertEqual(result["message_id"], "msg-new")

    def test_an_old_running_queue_turn_is_not_the_receipt(self):
        self.start_turn("old")
        self.track()
        self.now += 100
        self.assertEqual(self.receipt()["state"], "unconfirmed")
        self.assertNotIn("turn_id", self.receipt())

    def test_a_confirmed_turn_remains_observed_beyond_the_discovery_timeout(self):
        self.track()
        self.start_turn()
        self.assertEqual(self.receipt()["state"], "running")
        self.now += 2 * 24 * 3600
        self.assertEqual(self.tracker.active(), 1)
        self.finish()
        self.assertEqual(self.receipt()["state"], "completed")
        self.assertEqual(self.tracker.active(), 0)

    def test_manual_continue_goal_wakes_and_other_queue_texts_are_not_receipts(self):
        self.track()
        self.start_turn("manual", trigger="composer")
        self.start_turn("goal", trigger="goal")
        self.start_turn("other", text="different queued task")
        self.now += 100
        self.assertEqual(self.receipt()["state"], "unconfirmed")

    def test_queued_row_is_not_consumed_just_because_another_turn_started(self):
        self.present = True
        self.track(pending=True)
        self.start_turn()
        self.assertEqual(self.receipt()["state"], "queued")
        self.present = False
        self.assertEqual(self.receipt()["state"], "running")

    def test_deleted_queue_without_a_new_turn_becomes_actionable_not_success(self):
        self.track()
        self.now += 91
        self.assertEqual(self.receipt()["state"], "unconfirmed")
        self.assertNotIn("turn_id", self.receipt())
        self.assertEqual(len(self.tracker.records), 1)

    def test_multiple_matching_turns_remain_ambiguous(self):
        self.track()
        self.start_turn("one")
        self.start_turn("two")
        self.now += 91
        result = self.receipt()
        self.assertEqual((result["state"], result["reason"]), ("unconfirmed", "ambiguous_queue_turns"))

    def test_failure_is_the_matched_turns_error_not_dispatch_success(self):
        self.track()
        self.start_turn()
        self.finish(error={"message": "fixture error", "codex_error_info": {"http_connection_failed": {"http_status_code": 502}}})
        result = self.receipt()
        self.assertEqual((result["state"], result["error_code"]), ("failed", "http_connection_failed"))

    def test_interrupted_turn_does_not_become_completed(self):
        self.track()
        self.start_turn()
        self.finish(kind="turn_aborted")
        self.assertEqual(self.receipt()["state"], "interrupted")

    def test_following_goal_turn_cannot_replace_this_continuation_result(self):
        self.track()
        self.start_turn()
        self.assertEqual(self.receipt()["turn_id"], "new")
        self.finish()
        self.start_turn("later-goal", trigger="goal", text="直到完成任务")
        self.assertEqual((self.receipt()["state"], self.receipt()["turn_id"]), ("completed", "new"))

    def test_a_later_turns_writer_lock_does_not_keep_this_turn_running(self):
        self.track()
        self.start_turn()
        self.assertEqual(self.receipt()["state"], "running")
        self.now += 10
        self.start_turn("later", trigger="goal")
        self.assertEqual(self.receipt()["state"], "started")
        self.assertEqual(self.receipt()["turn_id"], "new")

    def test_receipt_survives_restart_and_keeps_following_the_same_turn(self):
        self.track()
        self.start_turn()
        self.assertEqual(self.receipt()["state"], "running")
        self.tracker = self.new_tracker()
        self.now += 10
        self.finish()
        self.assertEqual(self.receipt()["state"], "completed")

    def test_background_observer_updates_after_dispatch_without_api_polling(self):
        running, completed = threading.Event(), threading.Event()
        tracker = ResumeExecutionTracker(
            state_path=self.root / "background-receipts.json", clock=lambda: self.now,
            held=lambda: {"chat"}, interval=.01,
            on_change=lambda _tid, _qid, receipt: (
                running.set() if receipt["state"] == "running" else
                completed.set() if receipt["state"] == "completed" else None
            ),
        )
        self.addCleanup(tracker.stop)
        self.track(tracker=tracker)
        self.start_turn()
        self.assertTrue(running.wait(2), "Observer stopped when the queue became empty")
        self.finish()
        self.assertTrue(completed.wait(2), "Observer did not follow the turn to completion")

    def test_existing_disappeared_message_is_backfilled_from_timestamp_and_content(self):
        requested_at = self.iso()
        self.now += 7
        self.start_turn()
        self.tracker.track("chat", {"queued_id": "queue", "delivered": True,
                                  "delivered_at": self.iso()}, requested_at=requested_at)
        self.assertEqual(self.receipt()["state"], "running")

    def test_cancelled_message_cannot_be_reported_as_an_executed_turn(self):
        self.track()
        self.tracker.cancel("queue")
        self.start_turn()
        self.assertEqual(self.receipt()["state"], "cancelled")

    def test_partial_log_line_is_retried_without_skipping_the_message(self):
        self.track()
        self.start_turn(with_message=False)
        self.receipt()
        part = json.dumps({"type": "response_item", "timestamp": self.iso(), "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "继续"}],
            "internal_chat_message_metadata_passthrough": {"turn_id": "new"}}})
        with self.rollout.open("a", encoding="utf-8") as stream:
            stream.write(part[:len(part)//2])
        self.assertEqual(self.receipt()["state"], "confirming")
        with self.rollout.open("a", encoding="utf-8") as stream:
            stream.write(part[len(part)//2:] + "\n")
        self.assertEqual(self.receipt()["state"], "running")

    def test_no_message_or_tool_contents_are_persisted(self):
        self.track()
        self.start_turn("other", text="private-prompt-fixture")
        self.write("response_item", {"type": "function_call_output", "output": "private-output-fixture"})
        self.receipt()
        saved = self.tracker.path.read_text(encoding="utf-8")
        self.assertNotIn("private-prompt-fixture", saved)
        self.assertNotIn("private-output-fixture", saved)

    def test_service_returns_a_real_receipt_instead_of_just_delivered(self):
        fixture = self
        class Queue(FakeSession):
            def request(self, method, params=None, timeout=30):
                if method == "thread/queue/add":
                    fixture.now = time.time()
                    fixture.start_turn()
                return super().request(method, params, timeout)
        service = GoalResumeService(
            session_factory=lambda: Queue(goal_status=None), goals_provider=lambda: [],
            sessions_provider=lambda **kwargs: {}, execution_provider=lambda ids: {},
            desktop_client=NullDesktop(), receipt_tracker=self.tracker,
            history_path=self.root / "history.jsonl",
        )
        self.tracker.on_change = service._apply_execution_receipt
        service.start(["chat"], dry_run=False, confirm=True, continue_once=True)
        deadline = time.monotonic() + 3
        while service.running() and time.monotonic() < deadline:
            time.sleep(.01)
        row = service.list()["last_resume"]["chat"]
        self.assertTrue(row["delivered"])
        self.assertEqual((row["execution"]["state"], row["execution"]["turn_id"]), ("running", "new"))
        self.now += 5
        self.finish()
        row = service.list()["last_resume"]["chat"]
        self.assertEqual(row["execution"]["state"], "completed")
        saved = json.loads((self.root / "history.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(saved["results"][0]["execution"]["state"], "completed")
