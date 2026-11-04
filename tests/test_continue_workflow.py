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
from codex_switcher.appserver import AppServerError
from codex_switcher.goal_resume import GoalResumeService, RESUME_GOAL_OBJECTIVE, auto_resume_ids, set_auto_resume
from test_goal_resume import FakeSession, NullDesktop, _FakeClock


class ExecutionTests(unittest.TestCase):
    def test_nested_completion_error_is_detected_and_does_not_leak_into_the_next_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / "rollout.jsonl"
            events = [
                {"type": "task_started", "turn_id": "one"},
                {"type": "task_complete", "turn_id": "one", "error": {
                    "message": "Limit", "codex_error_info": "usage_limit_exceeded"}},
            ]
            def write():
                rollout.write_text("".join(json.dumps({"type": "event_msg", "payload": row}) + "\n"
                                           for row in events), encoding="utf-8")
            write()
            self.assertTrue(execution._rollout_state(rollout)["usage_limited"])
            events.append({"type": "task_started", "turn_id": "two"})
            write()
            self.assertFalse(execution._rollout_state(rollout)["usage_limited"])
            events.append({"type": "task_complete", "turn_id": "two", "error": {"message": "network failed"}})
            write()
            self.assertFalse(execution._rollout_state(rollout)["usage_limited"])

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
            "CODEX_SWITCHER_AUTO_RESUME_DISABLED_THREADS": "[]",
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

    def test_continuous_continuation_enables_account_switch_recovery_by_default(self):
        service = self.service(lambda: FakeSession(goal_status=None),
                               [{"thread_id": "continuous", "status": "usage_limited"}])
        service.start(["continuous"], dry_run=False, confirm=True, continue_once=False)
        self.assertTrue(self.wait(service)["results"][0]["ok"])
        self.assertEqual(auto_resume_ids(), ["continuous"])
        settings.reset_cache()
        self.assertEqual(service.auto_resume_status()["thread_ids"], ["continuous"])
        outcome = service.on_account_switched(SimpleNamespace(target_email="next@example.com"))
        self.assertEqual(outcome["thread_ids"], ["continuous"])
        self.assertTrue(self.wait(service)["results"][0]["ok"])

    def test_single_turn_does_not_enable_or_disable_account_switch_recovery(self):
        set_auto_resume("marked", True)
        for tid in ("ordinary", "marked"):
            service = self.service(lambda: FakeSession(goal_status=None))
            service.start([tid], dry_run=False, confirm=True, continue_once=True)
            self.wait(service)
        self.assertEqual(auto_resume_ids(), ["marked"])

    def test_continuous_continuation_keeps_an_explicit_opt_out_across_reload(self):
        set_auto_resume("continuous", False)
        settings.reset_cache()
        service = self.service(lambda: FakeSession(goal_status=None))
        service.start(["continuous"], dry_run=False, confirm=True, continue_once=False)
        self.assertTrue(self.wait(service)["results"][0]["ok"])
        self.assertEqual(auto_resume_ids(), [])
        settings.reset_cache()
        self.assertEqual(auto_resume_ids(), [])
        set_auto_resume("continuous", True)
        settings.reset_cache()
        self.assertEqual(auto_resume_ids(), ["continuous"])

    def test_mixed_modes_enable_only_continuous_chats_and_keep_the_master_paused(self):
        settings.save({"auto_resume_after_switch": False})
        service = self.service(lambda: FakeSession(goal_status=None))
        service.start(["single", "continuous"], dry_run=False, confirm=True,
                      continuation_modes={"single": "once", "continuous": "continuous"})
        self.wait(service)
        self.assertEqual(auto_resume_ids(), ["continuous"])
        self.assertFalse(service.auto_resume_status()["enabled"])

    def test_preview_does_not_enable_automatic_recovery(self):
        service = self.service(lambda: FakeSession(goal_status=None))
        service.start(["preview"], dry_run=True, continue_once=False)
        self.wait(service)
        self.assertEqual(auto_resume_ids(), [])
        self.assertIsNone(settings.get("auto_resume_threads"))

    def test_single_turn_preserves_inactive_goals(self):
        for status in ("complete", "blocked", "paused"):
            with self.subTest(status=status):
                session = FakeSession(goal_status=status)
                service = self.service(lambda: session)
                service.start(["ordinary"], dry_run=False, confirm=True, continue_once=True)
                result = self.wait(service)["results"][0]
                self.assertTrue(result["ok"])
                self.assertIn("thread/queue/add", session.methods())
                self.assertNotIn("thread/goal/set", session.methods())

    def test_missing_goal_is_created_with_the_selected_mode_before_the_message(self):
        for once in (False, True):
            with self.subTest(continue_once=once):
                session = FakeSession(goal_status=None)
                service = self.service(lambda: session)
                service.start(["ordinary"], dry_run=False, confirm=True, continue_once=once)
                result = self.wait(service)["results"][0]
                self.assertTrue(result["ok"])
                self.assertTrue(result["goal_created"])
                self.assertEqual(result["status_before"], None)
                desired = "paused" if once else "active"
                self.assertEqual(result["status_after"], desired)
                self.assertEqual(session.goal_objective, RESUME_GOAL_OBJECTIVE)
                self.assertLess(session.methods().index("thread/goal/set"), session.methods().index("thread/queue/add"))
                self.assertEqual([p for m, p in session.calls if m == "thread/goal/set"], [{
                    "threadId": "ordinary", "objective": RESUME_GOAL_OBJECTIVE,
                    "status": desired, "origin": "automatic",
                }])
                self.assertNotIn("thread/resume", session.methods())

    def test_single_turn_pauses_an_active_goal_before_sending_one_message(self):
        for status in ("active", "usageLimited"):
            with self.subTest(status=status):
                session = FakeSession(goal_status=status)
                service = self.service(lambda: session)
                service.start(["ordinary"], dry_run=False, confirm=True, continue_once=True)
                result = self.wait(service)["results"][0]
                self.assertTrue(result["ok"])
                self.assertEqual(session.goal_status, "paused")
                self.assertEqual(session.goal_objective, "existing user objective")
                self.assertEqual(session.methods().count("thread/queue/add"), 1)
                self.assertLess(session.methods().index("thread/goal/set"), session.methods().index("thread/queue/add"))

    def test_single_turn_does_not_dispatch_if_an_active_goal_cannot_be_paused(self):
        class CannotPause(FakeSession):
            def request(self, method, params=None, timeout=30):
                if method == "thread/goal/set":
                    raise AppServerError("fixture: cannot pause goal")
                return super().request(method, params, timeout)
        session = CannotPause(goal_status="active")
        service = self.service(lambda: session)
        service.start(["ordinary"], dry_run=False, confirm=True, continue_once=True)
        result = self.wait(service)["results"][0]
        self.assertFalse(result["ok"])
        self.assertNotIn("thread/queue/add", session.methods())

    def test_single_turn_does_not_queue_an_extra_turn_while_running(self):
        session = FakeSession(goal_status="active")
        service = self.service(lambda: session, states={"ordinary": {"state": "running"}})
        service.start(["ordinary"], dry_run=False, confirm=True, continue_once=True)
        result = self.wait(service)["results"][0]
        self.assertTrue(result["already_running"])
        self.assertIsNone(result["error"])
        self.assertNotIn("thread/queue/add", session.methods())
        self.assertNotIn("thread/goal/set", session.methods())

    def test_a_running_manual_request_is_noop_and_keeps_old_history_and_mode(self):
        session = FakeSession(goal_status="active")
        service = self.service(lambda: session, states={"ordinary": {"state": "running"}})
        result = service.start(["ordinary"], dry_run=False, confirm=True, skip_running=True)
        self.assertTrue(result["noop"])
        self.assertEqual(result["skipped_running"], ["ordinary"])
        self.assertEqual(session.calls, [])
        self.assertEqual(service._history, [])
        self.assertFalse(service._mode_path.exists())

    def test_batch_skips_running_sessions_and_dispatches_the_rest(self):
        session = FakeSession(goal_status=None)
        service = self.service(lambda: session, states={"busy": {"state": "running"}})
        job = service.start(["busy", "ordinary"], dry_run=False, confirm=True, skip_running=True)
        self.assertEqual(job["skipped_running"], ["busy"])
        self.assertEqual(job["thread_ids"], ["ordinary"])
        result = self.wait(service)["results"]
        self.assertEqual([row["thread_id"] for row in result], ["ordinary"])

    def test_single_turn_interrupted_by_quota_keeps_its_mode_after_switch_and_restart(self):
        set_auto_resume("ordinary", True)
        states = {"ordinary": {"state": "ended", "usage_limited": True}}
        session = FakeSession(goal_status=None)
        service = self.service(lambda: session, goals=[{"thread_id": "ordinary", "status": "paused"}], states=states)
        service.start(["ordinary"], dry_run=False, confirm=True, continue_once=True)
        self.wait(service)
        rebuilt = self.service(lambda: FakeSession(goal_status="paused"), goals=[{"thread_id": "ordinary", "status": "paused"}], states=states)
        context = rebuilt.capture_before_switch()
        self.assertEqual(context["eligible"], {"ordinary": "usage_limited"})
        self.assertEqual(context["continuation_modes"]["ordinary"], "once")
        outcome = rebuilt.on_account_switched(SimpleNamespace(resume_context=context, target_email="next@example.com", desktop_restart=None))
        self.assertTrue(outcome["ok"])
        job = self.wait(rebuilt)
        self.assertTrue(job["results"][0]["continue_once"])
        self.assertEqual(job["results"][0]["status_after"], "paused")

    def test_finished_single_turn_is_not_resumed_by_a_later_switch(self):
        set_auto_resume("ordinary", True)
        session = FakeSession(goal_status=None)
        service = self.service(lambda: session, goals=[{"thread_id": "ordinary", "status": "paused"}],
                               states={"ordinary": {"state": "ended", "usage_limited": True}})
        service.start(["ordinary"], dry_run=False, confirm=True, continue_once=True)
        job = self.wait(service)
        result = job["results"][0]
        service._apply_execution_receipt("ordinary", result["queued_id"], {"state": "completed", "turn_id": "finished"})
        self.assertEqual(service.capture_before_switch()["eligible"], {})

    def test_one_batch_can_restore_independent_single_and_continuous_modes(self):
        class PerThread(FakeSession):
            def __init__(self):
                super().__init__(goal_status=None)
                self.goals = {}
            def request(self, method, params=None, timeout=30):
                tid = (params or {}).get("threadId")
                if tid:
                    self.goal_status, self.goal_objective = self.goals.get(tid, (None, None))
                result = super().request(method, params, timeout)
                if tid:
                    self.goals[tid] = (self.goal_status, self.goal_objective)
                return result
        session = PerThread()
        states = {tid: {"state": "ended", "usage_limited": True} for tid in ("single", "long")}
        service = self.service(lambda: session, states=states)
        service.start(["single", "long"], dry_run=False, confirm=True,
                      continuation_modes={"single": "once", "long": "continuous"})
        result = {row["thread_id"]: row for row in self.wait(service)["results"]}
        self.assertTrue(result["single"]["continue_once"])
        self.assertFalse(result["long"]["continue_once"])
        self.assertEqual(session.goals["single"][0], "paused")
        self.assertEqual(session.goals["long"][0], "active")
        for tid in ("single", "long"):
            set_auto_resume(tid, True)
        rebuilt = self.service(lambda: PerThread(), states=states,
            goals=[{"thread_id": "single", "status": "paused"}, {"thread_id": "long", "status": "active"}])
        self.assertTrue(rebuilt._last_resume_by_thread()["single"]["continue_once"])
        self.assertFalse(rebuilt._last_resume_by_thread()["long"]["continue_once"])
        context = rebuilt.capture_before_switch()
        self.assertEqual(context["continuation_modes"], {"single": "once", "long": "continuous"})
        outcome = rebuilt.on_account_switched(SimpleNamespace(resume_context=context, target_email="next@example.com", desktop_restart=None))
        self.assertEqual(set(outcome["thread_ids"]), {"single", "long"})
        result = {row["thread_id"]: row for row in self.wait(rebuilt)["results"]}
        self.assertTrue(result["single"]["continue_once"])
        self.assertFalse(result["long"]["continue_once"])

    def test_model_permission_feedback_uses_the_persisted_account_and_model(self):
        service = self.service(lambda: FakeSession())
        service.model_guard = mock.Mock()
        service._record_history({"id": "model-job", "thread_ids": ["ordinary"], "results": [{
            "thread_id": "ordinary", "ok": True, "queued_id": "model-queue", "continue_once": True,
            "model": "fixture-model", "account_signature": "fixture-account-signature"}]})
        service._apply_execution_receipt("ordinary", "model-queue", {"state": "failed", "error_code": "model_access_denied"})
        service.model_guard.observe_signature.assert_called_once_with("fixture-account-signature", "fixture-model", False)
        restored = self.service(lambda: FakeSession())
        result = restored._last_resume_by_thread()["ordinary"]
        self.assertEqual(result["model"], "fixture-model")
        self.assertTrue(result["continue_once"])

    def test_goal_creation_failure_still_queues_and_keeps_a_separate_warning(self):
        class Failing(FakeSession):
            def request(self, method, params=None, timeout=30):
                if method == "thread/goal/set":
                    raise AppServerError("goal store unavailable")
                return super().request(method, params, timeout)
        session = Failing(goal_status=None)
        service = self.service(lambda: session)
        service.start(["ordinary"], dry_run=False, confirm=True)
        result = self.wait(service)["results"][0]
        self.assertTrue(result["ok"])
        self.assertFalse(result["goal_created"])
        self.assertIsNone(result["error"])
        self.assertIn("goal store unavailable", result["goal_creation_error"])
        self.assertIn("thread/queue/add", session.methods())
        restored = self.service(lambda: Failing(goal_status=None))
        persisted = restored._last_resume_by_thread()["ordinary"]
        self.assertEqual(persisted["goal_creation_error"], result["goal_creation_error"])
        self.assertFalse(persisted["goal_created"])

    def test_unconfirmed_goal_creation_also_falls_back_to_the_queue(self):
        class Unconfirmed(FakeSession):
            def request(self, method, params=None, timeout=30):
                if method == "thread/goal/set":
                    self.calls.append((method, params))
                    return {"goal": None}
                return super().request(method, params, timeout)
        session = Unconfirmed(goal_status=None)
        service = self.service(lambda: session)
        service.start(["ordinary"], dry_run=False, confirm=True)
        result = self.wait(service)["results"][0]
        self.assertTrue(result["ok"])
        self.assertFalse(result["goal_created"])
        self.assertIn("未确认成功", result["goal_creation_error"])
        self.assertIn("thread/queue/add", session.methods())

    def test_new_goal_is_paused_when_queue_submission_fails_and_reused_on_retry(self):
        session = FakeSession(goal_status=None, queue_error="queue unavailable")
        service = self.service(lambda: session)
        service.start(["ordinary"], dry_run=False, confirm=True)
        result = self.wait(service)["results"][0]
        self.assertFalse(result["ok"])
        self.assertTrue(result["goal_created"])
        self.assertEqual(result["status_after"], "paused")
        self.assertEqual(session.goal_status, "paused")
        # Real requests use a fresh RPC connection but the same persisted goal.
        retry = FakeSession(goal_status="paused")
        retry.goal_objective = session.goal_objective
        service._session_factory = lambda: retry
        service.start(["ordinary"], dry_run=False, confirm=True)
        result = self.wait(service)["results"][0]
        self.assertTrue(result["ok"])
        self.assertFalse(result["goal_created"])
        self.assertEqual(retry.goal_objective, RESUME_GOAL_OBJECTIVE)
        self.assertTrue(all("objective" not in params for method, params in retry.calls if method == "thread/goal/set"))

    def test_default_resume_reactivates_without_replacing_the_user_objective(self):
        session = FakeSession(goal_status="paused")
        session.goal_objective = "用户手工设置的明确目标"
        service = self.service(lambda: session)
        service.start(["ordinary"], dry_run=False, confirm=True)
        result = self.wait(service)["results"][0]
        self.assertTrue(result["ok"])
        self.assertFalse(result["goal_created"])
        self.assertEqual(session.goal_status, "active")
        self.assertEqual(session.goal_objective, "用户手工设置的明确目标")

    def test_missing_goal_preview_plans_creation_without_writing_anything(self):
        session = FakeSession(goal_status=None)
        service = self.service(lambda: session)
        service.start(["ordinary"], dry_run=True)
        result = self.wait(service)["results"][0]
        self.assertIn("objective=", result["planned"][0])
        self.assertEqual(result["status_after"], "active (would create)")
        self.assertEqual(session.methods(), ["initialize", "thread/goal/get"])

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
