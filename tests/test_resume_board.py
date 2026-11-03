"""Managed queue lifecycle and durable pause intent, with no live app access."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codex_switcher import settings
from codex_switcher.appserver import AppServerError
from codex_switcher.goal_resume import GoalResumeService, auto_resume_ids, set_auto_resume
from codex_switcher.resume_queue import build_queue
from test_goal_resume import FakeSession, NullDesktop, _FakeClock

STAMP = "2026-11-02T10:00:00Z"
START = "2026-11-02T10:00:01Z"
END = "2026-11-02T10:01:00Z"


class BoardTests(unittest.TestCase):
    def board(self, result, *, goal=None, current=None, pause=None, other_history=None):
        history = [{"id": "request", "thread_ids": ["chat"], "state": "done", "started_at": STAMP,
                    "continue_once": False, "results": [{"thread_id": "chat", **result}]}]
        history.extend(other_history or [])
        with mock.patch("codex_switcher.resume_queue.thread_labels", return_value={}):
            return build_queue(history=history, latest={"chat": result}, pending={}, job=None,
                               goals=[{"thread_id": "chat", **goal}] if goal else [],
                               items={"chat": {"title": "same title"}}, execution={"chat": current or {}},
                               pauses={"chat": pause} if pause else {})

    def test_removal_is_confirming_or_attention_and_never_running(self):
        for receipt, group in (({}, "attention"), ({"state": "confirming"}, "waiting"),
                               ({"state": "unconfirmed"}, "attention")):
            row = self.board({"ok": True, "delivered": True, "execution": receipt})["items"][0]
            self.assertEqual(row["group"], group)
            self.assertNotEqual(row["state"], "running")

    def test_later_goal_turn_stays_in_the_same_managed_row_until_goal_finishes(self):
        result = {"ok": True, "execution": {"state": "completed", "turn_id": "first", "started_at": START, "completed_at": END}}
        current = {"state": "running", "turn_id": "next", "trigger": "goal", "at": END}
        row = self.board(result, goal={"status": "active"}, current=current)["items"][0]
        self.assertEqual(row["state"], "running")
        row = self.board(result, goal={"status": "active"}, current={**current, "state": "ended"})["items"][0]
        self.assertEqual(row["state"], "waiting_goal")
        row = self.board(result, goal={"status": "complete"}, current={**current, "state": "ended"})["items"][0]
        self.assertEqual(row["label"], "长期目标已完成")
        self.assertEqual(row["group"], "recent")

    def test_single_turn_completion_is_not_counted_as_goal_continuation(self):
        result = {"ok": True, "continue_once": True, "execution": {"state": "completed", "turn_id": "first", "started_at": START, "completed_at": END}}
        current = {"state": "running", "turn_id": "other", "trigger": "goal", "at": END}
        row = self.board(result, goal={"status": "paused"}, current=current)["items"][0]
        self.assertEqual(row["group"], "recent")
        self.assertEqual(row["label"], "单次续跑已结束")

    def test_a_new_manual_turn_is_not_relabelled_as_a_goal_continuation(self):
        result = {"ok": True, "execution": {"state": "completed", "turn_id": "first", "started_at": START}}
        current = {"state": "running", "turn_id": "manual", "trigger": "user", "at": END}
        row = self.board(result, goal={"status": "active"}, current=current)["items"][0]
        self.assertEqual(row["state"], "completed")

    def test_an_unmatched_request_does_not_label_a_running_chat_as_failed(self):
        result = {"ok": True, "execution": {"state": "unconfirmed"}}
        current = {"state": "running", "turn_id": "manual", "trigger": "user", "at": END}
        row = self.board(result, goal={"status": "active"}, current=current)["items"][0]
        self.assertEqual(row["group"], "waiting")
        self.assertEqual(row["label"], "会话运行中 · 本次续跑待确认")

    def test_a_queued_request_is_waiting_even_when_an_older_turn_is_running(self):
        row = self.board({"ok": True, "pending": True, "owned_queue": True},
                         current={"state": "running", "turn_id": "old"})["items"][0]
        self.assertEqual(row["state"], "queued")
        self.assertTrue(row["can_cancel"])

    def test_pause_does_not_claim_the_current_turn_was_stopped(self):
        result = {"ok": True, "execution": {"state": "running", "turn_id": "turn", "started_at": START}}
        paused = {"at": START, "needs_manual_stop": True}
        row = self.board(result, current={"state": "running"}, pause=paused)["items"][0]
        self.assertEqual(row["group"], "running")
        self.assertTrue(row["needs_manual_stop"])
        self.assertFalse(row["can_pause"])
        self.assertIn("待停止", row["label"])
        row = self.board(result, current={"state": "unknown"}, pause=paused)["items"][0]
        self.assertEqual(row["state"], "stop_unconfirmed")
        row = self.board(result, current={"state": "interrupted", "at": END}, pause=paused)["items"][0]
        self.assertEqual(row["state"], "paused")
        self.assertFalse(row["needs_manual_stop"])

    def test_history_is_collapsed_by_chat_and_external_chats_are_excluded(self):
        previous = [{"id": "old", "thread_ids": ["chat"], "started_at": STAMP, "results": []},
                    {"id": "preview", "dry_run": True, "thread_ids": ["preview"], "results": []}]
        board = self.board({"ok": True, "pending": True}, other_history=previous)
        self.assertEqual(len(board["items"]), 1)
        self.assertEqual(len(board["items"][0]["attempts"]), 2)
        self.assertTrue(board["parallel"])

    def test_same_title_chats_stay_distinct_and_unknown_old_dispatches_stay_unconfirmed(self):
        with mock.patch("codex_switcher.resume_queue.thread_labels", return_value={}):
            board = build_queue(history=[{"id": "job", "thread_ids": ["one", "two"], "results": []}],
                                latest={}, pending={}, job=None, goals=[], pauses={}, execution={},
                                items={tid: {"title": "same title"} for tid in ("one", "two")})
        self.assertEqual({row["thread_id"] for row in board["items"]}, {"one", "two"})
        self.assertTrue(all(row["state"] == "failed" for row in board["items"]))

    def test_a_new_failed_request_does_not_borrow_an_older_running_receipt(self):
        with mock.patch("codex_switcher.resume_queue.thread_labels", return_value={}):
            board = build_queue(history=[{"id": "new", "thread_ids": ["chat"], "started_at": END, "results": [], "state": "interrupted"}],
                latest={"chat": {"job_id": "old", "ok": True, "execution": {"state": "running", "turn_id": "old"}}},
                pending={}, job=None, goals=[], items={}, execution={}, pauses={})
        self.assertEqual(board["items"][0]["state"], "failed")
        self.assertNotEqual(board["items"][0]["group"], "running")

    def test_a_goal_successor_without_a_writer_stays_unconfirmed(self):
        result = {"ok": True, "execution": {"state": "completed", "turn_id": "first", "started_at": START}}
        row = self.board(result, goal={"status": "active"}, current={"state": "unknown", "reason": "writer_not_confirmed",
                         "turn_id": "next", "trigger": "goal", "at": END})["items"][0]
        self.assertEqual(row["group"], "attention")

    def test_pause_does_not_turn_an_unknown_delivery_into_a_stopped_claim(self):
        row = self.board({"ok": True, "delivered": True, "execution": {"state": "confirming"}},
                         current={"state": "unknown"}, pause={"at": START})["items"][0]
        self.assertEqual(row["state"], "stop_unconfirmed")
        self.assertTrue(row["needs_manual_stop"])

    def test_pause_leaves_user_owned_messages_visible_and_not_cancellable(self):
        row = self.board({"ok": True, "pending": True, "owned_queue": False}, pause={"at": START})["items"][0]
        self.assertEqual(row["state"], "external_queue")
        self.assertFalse(row["can_cancel"])


class PauseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patch = mock.patch.dict(os.environ, {"CODEX_SWITCHER_DATA": str(self.root),
            "CODEX_SWITCHER_STATE_DB": str(self.root / "state.sqlite"),
            "CODEX_SWITCHER_QUEUE_DB": str(self.root / "queue.sqlite"),
            "CODEX_SWITCHER_AUTO_RESUME_THREADS": "[]", "CODEX_SWITCHER_AUTO_RESUME_AFTER_SWITCH": "1"})
        patch.start()
        self.addCleanup(patch.stop)
        settings.reset_cache()
        self.addCleanup(settings.reset_cache)
        patch = mock.patch("codex_switcher.config.goal_resume_watchdog_enabled", return_value=False)
        patch.start()
        self.addCleanup(patch.stop)
        self.goals, self.states, self.queue, self.calls = {"chat": "active"}, {}, [], []
        self.block, self.release = threading.Event(), threading.Event()
        self.block_add = False

    def service(self, fail_pause=False):
        owner = self
        class Session(FakeSession):
            def request(self, method, params=None, timeout=30):
                owner.calls.append((method, params))
                tid = (params or {}).get("threadId")
                if method == "thread/goal/get":
                    status = owner.goals.get(tid)
                    return {"goal": {"status": status} if status else None}
                if method == "thread/goal/set":
                    if fail_pause and params["status"] == "paused":
                        raise AppServerError("fixture goal pause failed")
                    owner.goals[tid] = params["status"]
                    return {"goal": {"status": params["status"]}}
                if method == "thread/queue/add":
                    if owner.block_add:
                        owner.block.set()
                        owner.release.wait(3)
                    row = {"id": params["clientUserMessageId"], "thread": tid}
                    owner.queue.append(row)
                    return {"queuedSubmission": row}
                if method == "thread/queue/list":
                    return {"data": [row for row in owner.queue if row["thread"] == tid]}
                if method == "thread/queue/delete":
                    row = next((row for row in owner.queue if row["id"] == params["queuedSubmissionId"]), None)
                    if row:
                        owner.queue.remove(row)
                    return {"deleted": bool(row)}
                return super().request(method, params, timeout)
        clock = _FakeClock()
        service = GoalResumeService(session_factory=Session,
            goals_provider=lambda: [{"thread_id": tid, "status": status} for tid, status in self.goals.items()],
            execution_provider=lambda ids: {tid: self.states.get(tid, {}) for tid in ids},
            sessions_provider=lambda **_: {"total": 1, "recent": [{"thread_id": "chat", "title": "test"}]},
            history_path=self.root / "history.jsonl", desktop_client=NullDesktop(), sleep=clock.sleep, clock=clock)
        self.addCleanup(service._receipt_tracker.stop)
        return service

    def wait(self, service):
        deadline = time.monotonic() + 4
        while service.running() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(service.running())

    def test_pause_persists_and_switch_cannot_resume_it_even_after_service_restart(self):
        service = self.service()
        set_auto_resume("chat", True)
        self.states["chat"] = {"state": "running", "turn_id": "turn"}
        before = service.capture_before_switch()
        result = service.pause(["chat"])["results"][0]
        self.assertTrue(result["goal_paused"])
        self.assertTrue(result["needs_manual_stop"])
        self.assertEqual(self.goals["chat"], "paused")
        self.assertEqual(auto_resume_ids(), ["chat"])
        self.assertFalse(any(method == "turn/interrupt" for method, _ in self.calls))
        # A stale pre-switch snapshot and a stale goal flag cannot override pause.
        self.goals["chat"] = "active"
        self.states["chat"] = {}
        restarted = self.service()
        self.assertTrue(restarted.is_paused("chat"))
        outcome = restarted.on_account_switched(SimpleNamespace(resume_context=before, desktop_restart={"ok": True}))
        self.wait(restarted)
        self.assertFalse(self.queue)
        self.assertFalse(any(method == "thread/queue/add" for method, _ in self.calls))
        self.assertEqual(outcome.get("skipped"), "no_armable_goal")

    def test_pause_failure_is_explicit_and_still_prevents_switcher_recovery(self):
        service = self.service(fail_pause=True)
        result = service.pause(["chat"])
        self.assertFalse(result["ok"])
        self.assertIn("fixture goal pause failed", result["results"][0]["error"])
        self.assertTrue(service.is_paused("chat"))
        set_auto_resume("chat", True)
        self.states["chat"] = {"state": "running"}
        self.assertEqual(service.capture_before_switch()["skipped"][0]["reason"], "paused_by_user")

    def test_explicit_resume_clears_pause_but_a_preview_does_not(self):
        service = self.service()
        service.pause(["chat"])
        service.start(["chat"], dry_run=True, continue_once=True)
        self.wait(service)
        self.assertTrue(service.is_paused("chat"))
        service.start(["chat"], dry_run=False, confirm=True, continue_once=False)
        self.wait(service)
        self.assertFalse(service.is_paused("chat"))
        self.assertEqual(self.goals["chat"], "active")
        self.assertEqual(len(self.queue), 1)

    def test_pause_during_queue_add_withdraws_the_late_message_and_keeps_other_chat_running(self):
        service = self.service()
        self.goals["other"] = "active"
        self.block_add = True
        service.start(["chat", "other"], dry_run=False, confirm=True)
        self.assertTrue(self.block.wait(2))
        service.pause(["chat"])
        self.release.set()
        self.wait(service)
        self.assertEqual([row["thread"] for row in self.queue], ["other"])
        self.assertEqual(self.goals["chat"], "paused")
        self.assertEqual(self.goals["other"], "active")

    def test_cancel_only_leaves_goal_and_auto_preference_intact(self):
        service = self.service()
        set_auto_resume("chat", True)
        service.start(["chat"], dry_run=False, confirm=True)
        self.wait(service)
        self.assertEqual(service.cancel_queued(["chat"])["results"][0]["state"], "cancelled")
        self.assertEqual(self.goals["chat"], "active")
        self.assertEqual(auto_resume_ids(), ["chat"])
        self.assertFalse(service.is_paused("chat"))

    def test_remove_force_stops_the_exact_chat_and_hides_only_that_request(self):
        service = self.service()
        service.start(["chat"], dry_run=False, confirm=True)
        self.wait(service)
        self.states["chat"] = {"state": "running", "turn_id": "actual-turn"}
        control = mock.Mock()
        control.interrupt.return_value = {"stopped": True, "turn_id": "actual-turn", "evidence": "desktop_ipc"}
        service._desktop_control = control
        result = service.remove(["chat"])["results"][0]
        self.assertTrue(result["removed"])
        self.assertTrue(result["stopped"])
        control.interrupt.assert_called_once_with("chat", "actual-turn")
        self.assertEqual(self.goals["chat"], "paused")
        self.assertFalse(self.queue)
        self.assertEqual(service.list()["resume_queue"]["items"], [])
        self.assertEqual(self.service().list()["resume_queue"]["items"], [])
        self.states.clear()
        service.start(["chat"], dry_run=False, confirm=True)
        self.wait(service)
        self.assertEqual(len(service.list()["resume_queue"]["items"]), 1)

    def test_a_failed_force_stop_keeps_the_record_and_error_visible(self):
        from codex_switcher.desktop_ipc import DesktopIpcError
        service = self.service()
        service.start(["chat"], dry_run=False, confirm=True)
        self.wait(service)
        self.states["chat"] = {"state": "running", "turn_id": "turn"}
        service._desktop_control = mock.Mock()
        service._desktop_control.interrupt.side_effect = DesktopIpcError("fixture refused to stop")
        result = service.remove(["chat"])
        self.assertFalse(result["ok"])
        self.assertFalse(result["results"][0]["removed"])
        row = service.list()["resume_queue"]["items"][0]
        self.assertIn("fixture refused", row["error"])
        self.assertTrue(row["can_remove"])
