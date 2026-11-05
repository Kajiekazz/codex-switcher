"""Overload recovery uses fake chats and clocks; never real quota or messages."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from codex_switcher import execution
from codex_switcher.capacity_recovery import CapacityRecovery
from codex_switcher.resume_queue import build_queue
import test_auto_resume as auto_resume_tests
from test_goal_resume import FakeSession

THREAD = auto_resume_tests.THREAD


class CapacityErrorTests(unittest.TestCase):
    def test_only_error_events_in_the_current_turn_mark_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            rows = []
            def event(payload, category="event_msg"):
                rows.append({"type": category, "payload": payload})
                path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
                return execution._rollout_state(path)
            event({"type": "task_started", "turn_id": "one"})
            state = event({"type": "task_complete", "turn_id": "one", "error": {
                "message": "Selected model is at capacity. Please try a different model.",
                "codex_error_info": "server_overloaded"}})
            self.assertTrue(state["capacity_limited"])
            self.assertFalse(state["usage_limited"])
            self.assertFalse(event({"type": "task_started", "turn_id": "two"})["capacity_limited"])
            event({"type": "user_message", "message": "Selected model is at capacity."})
            self.assertFalse(event({"type": "task_complete", "turn_id": "two"})["capacity_limited"])
            event({"type": "task_started", "turn_id": "three"})
            event({"type": "error", "codex_error_info": {"server_overloaded": {}}})
            self.assertTrue(event({"type": "task_complete", "turn_id": "three"})["capacity_limited"])

    def test_quota_and_permission_errors_are_separate(self):
        for value in ("usage_limit_exceeded", "model_access_denied", "capacity planning document", None):
            self.assertFalse(execution.is_capacity_error(value))
        self.assertTrue(execution.is_capacity_error({"message": "Selected model is at capacity."}))

    def test_remote_compaction_capacity_error_remains_a_retryable_capacity_failure(self):
        error = {"message": "Error running remote compact task: Selected model is at capacity. Please try a different model.",
                 "codex_error_info": "server_overloaded"}
        self.assertTrue(execution.is_capacity_error(error))
        self.assertFalse(execution.is_capacity_error({"message": "Error running remote compact task: network timeout"}))
        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / "rollout.jsonl"
            records = [
                {"type": "event_msg", "timestamp": "2026-11-05T14:23:00Z", "payload": {
                    "type": "task_started", "turn_id": "compact-turn"}},
                {"type": "event_msg", "timestamp": "2026-11-05T14:23:25Z", "payload": {
                    "type": "task_complete", "turn_id": "compact-turn", "error": error}},
            ]
            rollout.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
            state = execution._rollout_state(rollout)
            self.assertEqual(state["state"], "ended")
            self.assertTrue(state["capacity_limited"])
            self.assertFalse(state["usage_limited"])


class CapacityWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "capacity.json"
        self.now = 1000.0
        self.snapshot = {"targets": {"chat": {"turn_id": "failed-1"}}, "active": ["chat"]}
        self.dispatch = mock.Mock(return_value={"ok": True, "job": {"id": "retry-1"}, "thread_ids": ["chat"]})
        self.worker = self.make_worker()

    def make_worker(self):
        worker = CapacityRecovery(lambda: self.snapshot, self.dispatch,
                                  path=self.path, clock=lambda: self.now, interval=.01)
        self.addCleanup(worker.stop)
        return worker

    def test_initial_delay_and_submitted_turn_are_not_duplicated_even_after_restart(self):
        self.worker.tick_once()
        self.assertEqual(self.worker.status()["entries"]["chat"]["after"], 1010)
        self.now += 9
        self.worker.tick_once()
        self.dispatch.assert_not_called()
        self.now += 1
        self.worker.tick_once()
        self.dispatch.assert_called_once_with({"chat": "failed-1"})
        self.now += 1000
        self.make_worker().tick_once()
        self.assertEqual(self.dispatch.call_count, 1)

    def test_consecutive_capacity_failures_keep_the_same_ten_second_delay(self):
        self.worker.tick_once()
        self.now += 10
        self.worker.tick_once()
        self.snapshot["targets"] = {}
        self.worker.tick_once()  # A retry is running; retain its deadline.
        self.snapshot["targets"] = {"chat": {"turn_id": "failed-2"}}
        self.worker.tick_once()
        self.now += 9
        self.worker.tick_once()
        self.assertEqual(self.dispatch.call_count, 1)
        self.now += 1
        self.dispatch.return_value["job"]["id"] = "retry-2"
        self.worker.tick_once()
        self.assertEqual(self.dispatch.call_count, 2)
        self.assertEqual(self.worker.status()["entries"]["chat"]["attempts"], 2)
        for attempt in range(3, 9):
            self.snapshot["targets"] = {"chat": {"turn_id": f"failed-{attempt}"}}
            self.worker.tick_once()
            self.now += 9
            self.worker.tick_once()
            self.assertEqual(self.dispatch.call_count, attempt - 1)
            self.now += 1
            self.worker.tick_once()
            self.assertEqual(self.dispatch.call_count, attempt)
        self.snapshot = {"targets": {}, "active": []}
        self.worker.tick_once()
        self.assertEqual(self.worker.status()["entries"], {})

    def test_a_failed_queue_submission_can_retry_after_ten_seconds(self):
        self.worker.tick_once()
        self.now += 10
        self.worker.tick_once()
        self.snapshot["unsubmitted"] = {"chat": "retry-1"}
        self.now += 9
        self.worker.tick_once()
        self.assertEqual(self.dispatch.call_count, 1)
        self.now += 1
        self.worker.tick_once()
        self.assertEqual(self.dispatch.call_count, 2)

    def test_busy_dispatch_or_closed_desktop_does_not_consume_an_attempt(self):
        self.worker.tick_once()
        self.now += 10
        self.dispatch.return_value = {"ok": True, "skipped": "job_running"}
        self.worker.tick_once()
        self.assertEqual(self.worker.status()["entries"]["chat"]["attempts"], 0)
        self.dispatch.return_value = {"ok": True, "skipped": "desktop_not_running"}
        self.worker.tick_once()
        self.assertEqual(self.worker.status()["entries"]["chat"]["attempts"], 0)

    def test_disabled_or_paused_chat_drops_the_retry_without_dispatch(self):
        self.worker.tick_once()
        self.now += 10
        self.snapshot = {"targets": {}, "active": []}
        self.worker.tick_once()
        self.dispatch.assert_not_called()
        self.assertEqual(self.make_worker().status()["entries"], {})

    def test_worker_runs_without_a_dashboard_or_auto_switch_monitor(self):
        ready = threading.Event()
        self.now += 40
        self.snapshot["targets"]["chat"]["at"] = "1970-01-01T00:16:40Z"
        self.dispatch.side_effect = lambda _ids: (ready.set() or {"ok": True, "job": {"id": "background"}, "thread_ids": ["chat"]})
        self.worker.start()
        self.assertTrue(ready.wait(2))
        self.worker.stop()


class CapacityDispatchTests(auto_resume_tests.AutoResumeEnv):
    _service = auto_resume_tests.AutoResumeSwitchTests._service
    _wait = auto_resume_tests.AutoResumeSwitchTests._wait

    def setUp(self):
        super().setUp()
        from codex_switcher.goal_resume import set_auto_resume
        set_auto_resume(THREAD, True)
        self.goals = [{"thread_id": THREAD, "status": "active"}]
        self.service, self.session = self._service(self.goals, FakeSession(goal_status="active"))
        self.service._desktop.status = mock.Mock(return_value={"ok": True, "running": True})
        self.states = {THREAD: {"state": "ended", "capacity_limited": True,
                               "turn_id": "overload", "at": "2026-11-05T08:34:24Z"}}
        self.service._execution_provider = lambda ids: {tid: self.states[tid] for tid in ids if tid in self.states}
        self.addCleanup(lambda: self.service._thread.join(5) if self.service._thread else None)

    def test_capacity_dispatch_preserves_mode_and_appears_as_automatic_in_queue(self):
        result = self.service.on_capacity_recovered({THREAD: "overload"})
        self.assertEqual(result["thread_ids"], [THREAD])
        job = self._wait(self.service)
        self.assertEqual(job["started_via"], "capacity_recovery")
        self.assertEqual(job["continuation_modes"], {THREAD: "continuous"})
        self.assertIn("thread/queue/add", self.session.methods())
        rows = build_queue(history=self.service.history(), latest=self.service._last_resume_by_thread(),
                           pending={}, job=None, goals=self.goals, items={}, execution=self.states, pauses={})
        row = next(row for row in rows["items"] if row["thread_id"] == THREAD)
        self.assertEqual(row["source"], "automatic")
        self.assertEqual(row["source_label"], "模型繁忙重试")

    def test_pause_archive_completed_running_pending_and_optout_are_not_resumed(self):
        for status in ("paused", "complete", "budget_limited"):
            with self.subTest(status=status):
                self.goals[0]["status"] = status
                self.assertEqual(self.service.capacity_recovery_snapshot()["targets"], {})
        self.goals[0]["status"] = "active"
        for state in ({"state": "running", "capacity_limited": True},
                      {"state": "ended", "capacity_limited": True, "archived": True},
                      {"state": "ended", "usage_limited": True}):
            self.states[THREAD] = state
            self.assertEqual(self.service.capacity_recovery_snapshot()["targets"], {})
        self.states[THREAD] = {"state": "ended", "capacity_limited": True, "turn_id": "overload"}
        with mock.patch.object(self.service, "_pending_by_thread", return_value={THREAD: {}}):
            self.assertEqual(self.service.capacity_recovery_snapshot()["targets"], {})
        os.environ["CODEX_SWITCHER_AUTO_RESUME_AFTER_SWITCH"] = "0"
        self.assertEqual(self.service.capacity_recovery_snapshot()["targets"], {})
        self.assertEqual(self.session.methods(), [])

    def test_remote_compaction_recovers_a_blocked_goal_in_the_saved_mode(self):
        self.goals[0]["status"] = "blocked"
        self.session.goal_status = "blocked"
        # Account switches retain their existing blocked-goal rule.
        self.assertNotIn(THREAD, self.service.capture_before_switch()["eligible"])
        self.assertIn(THREAD, self.service.capacity_recovery_snapshot()["targets"])
        outcome = self.service.on_capacity_recovered({THREAD: "overload"})
        self.assertEqual(outcome["thread_ids"], [THREAD])
        job = self._wait(self.service)
        self.assertEqual(job["continuation_modes"], {THREAD: "continuous"})
        self.assertEqual(self.session.goal_status, "active")
        self.assertIn("thread/queue/add", self.session.methods())

    def test_blocked_goal_without_a_current_capacity_failure_is_not_recovered(self):
        self.goals[0]["status"] = "blocked"
        for state in ({"state": "ended", "capacity_limited": False},
                      {"state": "running", "capacity_limited": False},
                      {"state": "ended", "capacity_limited": True, "usage_limited": True}):
            with self.subTest(state=state):
                self.states[THREAD] = state
                self.assertEqual(self.service.capacity_recovery_snapshot()["targets"], {})
        self.assertEqual(self.session.methods(), [])

    def test_single_turn_capacity_retry_keeps_a_blocked_goal_from_advancing(self):
        self.goals[0]["status"] = "blocked"
        self.session.goal_status = "blocked"
        self.service._modes[THREAD] = "once"
        self.service.on_capacity_recovered({THREAD: "overload"})
        job = self._wait(self.service)
        self.assertEqual(job["continuation_modes"], {THREAD: "once"})
        self.assertEqual(self.session.goal_status, "blocked")
        self.assertNotIn("thread/goal/set", self.session.methods())
        self.assertIn("thread/queue/add", self.session.methods())

    def test_queue_failure_restores_blocked_goal_and_can_retry_it(self):
        self.goals[0]["status"] = "blocked"
        failed = FakeSession(goal_status="blocked", queue_error="fixture queue unavailable")
        self.service._session_factory = lambda: failed
        self.service.on_capacity_recovered({THREAD: "overload"})
        self._wait(self.service)
        self.service._thread.join(5)
        self.assertEqual(failed.goal_status, "blocked")
        self.assertIn(THREAD, self.service.capacity_recovery_snapshot()["targets"])
        self.session.goal_status = "blocked"
        self.service._session_factory = lambda: self.session
        self.service.on_capacity_recovered({THREAD: "overload"})
        self._wait(self.service)
        self.assertEqual(self.session.goal_status, "active")
        self.assertIn("thread/queue/add", self.session.methods())

    def test_a_changed_turn_or_closed_desktop_does_not_dispatch(self):
        self.assertEqual(self.service.on_capacity_recovered({THREAD: "stale"})["skipped"], "no_capacity_error")
        self.service._desktop.status.return_value = {"ok": True, "running": False}
        self.assertEqual(self.service.on_capacity_recovered({THREAD: "overload"})["skipped"], "desktop_not_running")
        self.assertEqual(self.session.methods(), [])

    def test_pausing_during_desktop_check_is_still_respected_before_queueing(self):
        def desktop_status():
            self.service._pauses[THREAD] = {"at": "2026-11-05T09:00:00Z"}
            return {"ok": True, "running": True}
        self.service._desktop.status.side_effect = desktop_status
        self.service.on_capacity_recovered({THREAD: "overload"})
        job = self._wait(self.service)
        self.assertTrue(self.service.is_paused(THREAD))
        self.assertEqual(job["results"][0]["error"], "paused_by_user")
        self.assertNotIn("thread/queue/add", self.session.methods())

    def test_initialization_failure_is_retried_after_ten_seconds(self):
        from codex_switcher.appserver import AppServerError
        broken = FakeSession(goal_status="active")
        broken.initialize = mock.Mock(side_effect=AppServerError("fixture initialization failed"))
        self.service._session_factory = mock.Mock(side_effect=[broken, self.session])
        now = [1800000000.0]
        worker = self.service.capacity_recovery
        worker.clock = lambda: now[0]
        worker.tick_once()
        first = self._wait(self.service)
        self.service._thread.join(5)
        self.assertEqual(first["state"], "failed")
        self.assertEqual(first["results"], [])
        self.assertEqual(self.service.capacity_recovery_snapshot()["unsubmitted"], {THREAD: first["id"]})
        now[0] += 9
        worker.tick_once()
        self.assertEqual(self.service._session_factory.call_count, 1)
        now[0] += 1
        worker.tick_once()
        second = self._wait(self.service)
        self.assertNotEqual(first["id"], second["id"])
        self.assertIn("thread/queue/add", self.session.methods())

    def test_only_pre_dispatch_interruption_can_retry_without_a_receipt(self):
        for initialized in (False, True):
            with self.subTest(initialized=initialized), mock.patch.object(self.service, "history", return_value=[{
                "id": "interrupted-job", "thread_ids": [THREAD], "started_via": "capacity_recovery",
                "state": "interrupted", "dispatch_initialized": initialized, "results": []}]):
                actual = self.service.capacity_recovery_snapshot()["unsubmitted"]
                self.assertEqual(actual, {} if initialized else {THREAD: "interrupted-job"})

    def test_capacity_is_not_treated_as_quota_even_with_a_stale_goal_status(self):
        self.goals[0]["status"] = "usage_limited"
        self.assertFalse(self.service.quota_recovery_needed())
        self.assertIn(THREAD, self.service.capacity_recovery_snapshot()["targets"])

    def test_a_temporarily_unconfirmed_running_turn_keeps_its_retry_state(self):
        self.states[THREAD] = {"state": "unknown", "reason": "writer_not_confirmed"}
        context = self.service.capacity_recovery_snapshot()
        self.assertEqual(context["targets"], {})
        self.assertIn(THREAD, context["active"])

    def test_unfinished_single_turn_keeps_its_paused_goal_and_single_turn_mode(self):
        self.goals[0]["status"] = "paused"
        self.session.goal_status = "paused"
        self.service._modes[THREAD] = "once"
        with mock.patch.object(self.service, "_last_resume_by_thread", return_value={
            THREAD: {"continue_once": True, "execution": {"state": "failed"}}}):
            outcome = self.service.on_capacity_recovered({THREAD: "overload"})
        self.assertEqual(outcome["thread_ids"], [THREAD])
        job = self._wait(self.service)
        self.assertEqual(job["continuation_modes"], {THREAD: "once"})
        self.assertEqual(self.session.goal_status, "paused")


if __name__ == "__main__":
    unittest.main()
