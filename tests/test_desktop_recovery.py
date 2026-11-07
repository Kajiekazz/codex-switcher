"""A failed account-switch restart must eventually activate, without killing twice."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import test_auto_switch as monitor_harness
import test_switch_resume_integration as integration_harness
from codex_switcher.auto_switch import AutoSwitchMonitor
from codex_switcher.desktop import STATUS_SCRIPT
from codex_switcher.goal_resume import set_auto_resume
from codex_switcher.switcher import SwitchManager
from test_auto_switch import _FakeQuotaService, _FakeSwitchManager, _limit_reached, _quota_ok
from test_desktop import _Runner, _client
from test_switch import make_auth
from test_goal_resume import THREAD


class DesktopFinalizationTests(unittest.TestCase):
    def test_stale_remaining_snapshot_is_rechecked_and_missing_launch_is_sent(self):
        runner = _Runner([
            {"running": True, "pids": [111]},
            {"running": False, "pids": [], "exited_pids": [111]},
            {"running": True, "pids": [222]},
        ], restart={"previous_pids": [111], "remaining": [111], "launched": False})
        result = _client(runner).restart()
        self.assertTrue(result["restart_confirmed"])
        self.assertTrue(result["launched"])
        self.assertEqual(result["remaining"], [])
        self.assertEqual(result["exited_pids"], [111])
        self.assertEqual((runner.restart_calls, runner.launch_calls), (1, 1))

    def test_slow_old_process_exit_is_waited_for_before_launch(self):
        runner = _Runner([
            {"running": True, "pids": [111]},
            {"running": True, "pids": [111]},
            {"running": False, "pids": []},
            {"running": True, "pids": [222]},
        ], restart={"previous_pids": [111], "remaining": [111], "launched": False})
        result = _client(runner).restart()
        self.assertTrue(result["ok"])
        self.assertEqual((runner.restart_calls, runner.launch_calls), (1, 1))

    def test_later_recovery_only_activates_and_keeps_a_new_instance(self):
        runner = _Runner([{"running": True, "pids": [111]}],
                         restart={"previous_pids": [111], "remaining": [111], "launched": False})
        desktop = _client(runner, timeout=1)
        self.assertFalse(desktop.restart()["ok"])
        self.assertEqual(runner.launch_calls, 0)
        runner.statuses = [{"running": False, "pids": []}, {"running": True, "pids": [222]}]
        runner.status_calls = 0
        self.assertTrue(desktop.complete_restart([111], wait=False)["ok"])
        self.assertTrue(desktop.complete_restart([111], wait=False)["ok"])
        self.assertEqual((runner.restart_calls, runner.launch_calls), (1, 1))
        self.assertTrue(desktop.ready_for_resume())

    def test_small_live_residual_still_blocks_every_activation(self):
        runner = _Runner([{"running": False, "pids": [], "all_pids": [111],
                           "process_details": [{"pid": 111, "wait_status": 258, "threads": 1}]}])
        result = _client(runner).complete_restart([111], wait=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["remaining"], [111])
        self.assertEqual(result["remaining_processes"][0]["wait_status"], 258)
        self.assertEqual((runner.restart_calls, runner.launch_calls), (0, 0))

    def test_unknown_process_state_never_activates(self):
        runner = _Runner([])
        def fail_status(script):
            if script == STATUS_SCRIPT:
                raise RuntimeError("status unavailable")
            return runner(script)
        result = _client(fail_status).complete_restart([111], wait=False)
        self.assertEqual(result["reason"], "restart_status_unknown")
        self.assertEqual((runner.restart_calls, runner.launch_calls), (0, 0))


class GuardedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.auth = Path(self.temp.name) / "auth.json"
        make_auth(self.auth, "target@example.com", "target")
        self.auth_bytes = self.auth.read_bytes()
        self.desktop = mock.Mock()
        self.desktop.complete_restart.return_value = {"ok": True, "previous_pids": [111], "pids": [222]}
        self.callback = mock.Mock(return_value={"ok": True, "job": {"id": "resumed"}})
        self.manager = SwitchManager(mock.Mock(), self.auth, desktop_client=self.desktop, on_switched=self.callback)
        self.pending = {"target_email": "target@example.com", "previous_pids": [111],
                        "resume_context": {"eligible": {"chat": "running_before_switch"}, "running_turns": {"chat": "old-turn"}}}

    def test_recovery_keeps_auth_and_original_switch_context(self):
        outcome = self.manager.recover_desktop_restart(self.pending)
        self.assertTrue(outcome["ok"])
        self.assertEqual(self.auth.read_bytes(), self.auth_bytes)
        self.desktop.complete_restart.assert_called_once_with([111], wait=False)
        result = self.callback.call_args.args[0]
        self.assertEqual(result.target_email, self.pending["target_email"])
        self.assertEqual(result.resume_context, self.pending["resume_context"])
        self.desktop.restart.assert_not_called()

    def test_changed_account_cancels_without_activating_or_resuming(self):
        self.pending["target_email"] = "other@example.com"
        outcome = self.manager.recover_desktop_restart(self.pending)
        self.assertEqual(outcome["skipped"], "account_changed")
        self.desktop.complete_restart.assert_not_called()
        self.callback.assert_not_called()

    def test_active_switch_is_not_interrupted(self):
        self.manager._lock.acquire()
        try:
            self.assertEqual(self.manager.recover_desktop_restart(self.pending)["skipped"], "switch_running")
        finally:
            self.manager._lock.release()
        self.desktop.complete_restart.assert_not_called()

    def test_failed_activation_never_resumes(self):
        self.desktop.complete_restart.return_value = {"ok": False, "remaining": [111]}
        self.assertFalse(self.manager.recover_desktop_restart(self.pending)["ok"])
        self.callback.assert_not_called()

    def test_account_change_during_activation_prevents_dispatch(self):
        def change_account(*args, **kwargs):
            make_auth(self.auth, "other@example.com", "other")
            return {"ok": True}
        self.desktop.complete_restart.side_effect = change_account
        self.assertEqual(self.manager.recover_desktop_restart(self.pending)["skipped"], "account_changed")
        self.callback.assert_not_called()

    def test_a_new_busy_dispatch_is_kept_pending(self):
        self.callback.return_value = {"ok": True, "skipped": "job_running"}
        self.assertFalse(self.manager.recover_desktop_restart(self.pending)["ok"])
        self.assertTrue(self.callback.call_args.args[0].deferred_restart)


class AutomaticDesktopRecoveryTests(unittest.TestCase):
    setUp = monitor_harness.AutoSwitchMonitorTests.setUp
    _monitor = monitor_harness.AutoSwitchMonitorTests._monitor

    def test_helper_failure_without_a_verified_snapshot_never_activates(self):
        manager = _FakeSwitchManager(desktop_restart={"ok": False, "skipped": False,
            "reason": "restart_failed", "previous_pids": []})
        quota = _FakeQuotaService(_limit_reached(), {"b@example.com": _quota_ok()})
        monitor = self._monitor(manager=manager, quota_service=quota)
        self.assertEqual(monitor.tick_once()["action"], "switched")
        self.assertFalse(monitor.status()["desktop_recovery"]["waiting"])

    def failed_switch(self):
        manager = _FakeSwitchManager(desktop_restart={"ok": False, "skipped": False,
            "reason": "restart_not_confirmed", "previous_pids": [111], "remaining": [111]})
        manager.recover_desktop_restart = mock.Mock(return_value={"ok": True, "desktop_restart": {"ok": True},
            "auto_resume": {"ok": True, "job": {"id": "after-restart"}, "thread_ids": ["chat"]}})
        quota = _FakeQuotaService(_limit_reached(), {"b@example.com": _quota_ok()})
        monitor = self._monitor(manager=manager, quota_service=quota)
        self.assertEqual(monitor.tick_once()["action"], "switched")
        self.assertTrue(monitor.status()["desktop_recovery"]["waiting"])
        return manager, quota, monitor

    def test_pending_restart_survives_monitor_restart_and_bypasses_quota_error(self):
        manager, quota, original = self.failed_switch()
        monitor = self._monitor(manager=manager, quota_service=quota)
        quota.current_quota = {"ok": False, "error": "network_error"}
        self.assertEqual(monitor.tick_once()["action"], "desktop_recovery_pending")
        manager.recover_desktop_restart.assert_not_called()
        self.clock.value += 10
        outcome = monitor.tick_once()
        self.assertEqual(outcome["action"], "desktop_recovered")
        self.assertEqual(outcome["auto_resume"]["job_id"], "after-restart")
        self.assertFalse(monitor.status()["desktop_recovery"]["waiting"])
        self.assertEqual(manager.switched, ["b@example.com"])
        self.assertEqual(monitor.tick_once()["action"], "quota_error")
        manager.recover_desktop_restart.assert_called_once()

    def test_failed_recovery_keeps_retrying_every_ten_seconds(self):
        manager, quota, monitor = self.failed_switch()
        manager.recover_desktop_restart.return_value = {"ok": False, "desktop_restart": {"remaining": [111]}}
        for attempt in range(1, 4):
            self.clock.value += 10
            self.assertEqual(monitor.tick_once()["action"], "desktop_recovery_pending")
            self.assertEqual(monitor.status()["desktop_recovery"]["attempts"], attempt)
            self.assertEqual(monitor._desktop_recovery["after"], self.clock.value + 10)
            monitor.tick_once()
            self.assertEqual(manager.recover_desktop_restart.call_count, attempt)
        self.assertEqual(manager.switched, ["b@example.com"])

    def test_changed_account_clears_persisted_activation_request(self):
        manager, quota, monitor = self.failed_switch()
        manager.email = "manual@example.com"
        manager.recover_desktop_restart.return_value = {"ok": False, "skipped": "account_changed"}
        quota.current_quota = _quota_ok("manual@example.com")
        self.clock.value += 30
        self.assertEqual(monitor.tick_once()["action"], "quota_ok")
        self.assertFalse(monitor._desktop_recovery)
        self.assertFalse(json.loads(monitor._recovery_path.read_text())["desktop_restart"])

    def test_ready_desktop_waits_for_busy_dispatch_then_clears_pending(self):
        manager, quota, monitor = self.failed_switch()
        manager.recover_desktop_restart.side_effect = [
            {"ok": False, "desktop_restart": {"ok": True}, "auto_resume": {"ok": True, "skipped": "job_running"}},
            {"ok": True, "desktop_restart": {"ok": True}, "auto_resume": {"ok": True, "job": {"id": "queued"}}},
        ]
        self.clock.value += 10
        self.assertEqual(monitor.tick_once()["action"], "desktop_recovery_pending")
        self.assertEqual(monitor.status()["desktop_recovery"]["state"], "waiting_dispatch")
        self.clock.value += 10
        self.assertEqual(monitor.tick_once()["action"], "desktop_recovered")
        self.assertFalse(monitor.status()["desktop_recovery"]["waiting"])
        self.assertEqual(manager.switched, ["b@example.com"])


class SwitchRecoveryQueueIntegrationTests(unittest.TestCase):
    setUp = integration_harness.SwitchResumeIntegrationTests.setUp
    stop_server = integration_harness.SwitchResumeIntegrationTests.stop_server

    def test_failed_restart_then_monitor_restart_recovers_original_chat_into_queue(self):
        set_auto_resume(THREAD, True)
        self.desktop.restart = mock.Mock(return_value={"ok": False, "skipped": False,
            "previous_pids": [111], "remaining": [111], "reason": "restart_not_confirmed"})
        self.desktop.complete_restart = mock.Mock(return_value={"ok": True,
            "restart_confirmed": True, "previous_pids": [111], "pids": [222]})
        clock = monitor_harness._Clock()
        target_profile = self.store.resolve_auth_profile("after@example.com")["profile_id"]
        quota = _FakeQuotaService(_limit_reached(), {target_profile: _quota_ok("after@example.com")})
        def monitor():
            return AutoSwitchMonitor(self.store, self.server.switch_manager, quota,
                enabled=lambda: True, clock=clock, log_path=self.root / "auto-switch.log",
                predictive_enabled=lambda: False, reset_enabled=lambda: False)
        original = monitor()
        outcome = original.tick_once()
        self.assertEqual(outcome["action"], "switched")
        self.assertEqual(quota.calls, [target_profile])
        self.assertEqual(original._desktop_recovery["target_profile_id"], target_profile)
        self.assertEqual(outcome["auto_resume"]["skipped"], "desktop_restart_failed")
        self.assertIsNone(self.service.status()["job"])
        self.assertEqual(original._desktop_recovery["resume_context"]["eligible"][THREAD], "usage_limited")
        clock.value += 10
        recovered = monitor().tick_once()
        self.assertEqual(recovered["action"], "desktop_recovered")
        self.assertEqual(recovered["auto_resume"]["thread_count"], 1)
        deadline = time.monotonic() + 3
        while self.service.running() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn("thread/queue/add", self.session.methods())
        self.assertEqual(self.service.status()["job"]["thread_ids"], [THREAD])
        self.assertEqual(self.auth.read_bytes(), self.target)
        self.desktop.restart.assert_called_once()
        self.desktop.complete_restart.assert_called_once_with([111], wait=False)

    def test_deferred_recovery_never_cancels_a_new_dispatch(self):
        from types import SimpleNamespace
        with mock.patch.object(self.service, "running", return_value=True):
            with mock.patch.object(self.service, "_cancel_stale_job") as cancel:
                outcome = self.service.on_account_switched(SimpleNamespace(deferred_restart=True))
        self.assertEqual(outcome["skipped"], "job_running")
        cancel.assert_not_called()
