"""Display probes must not delay HTTP or alter real desktop decisions."""
import json
import threading
import time
import unittest
from unittest import mock

from codex_switcher.desktop import (
    CLOSE_SCRIPT, LAUNCH_SCRIPT, PROCESS_SCRIPT, RESTART_SCRIPT, STATUS_SCRIPT,
    DesktopClient, DesktopDisplayCache,
)


class DesktopDisplayCacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.client = mock.Mock()
        self.client.status.return_value = {"ok": True, "running": True, "pids": [1]}
        self.client.processes.return_value = {"ok": True, "processes": [{"pid": 1}]}
        self.cache = DesktopDisplayCache(self.client, clock=lambda: self.now, wall_clock=lambda: 1000.0)
        self.addCleanup(self.cache.close)

    def settled(self, key="status"):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.cache.get(key)
            if not result["pending"]:
                return result
            time.sleep(.005)
        self.fail("display probe did not finish")

    def gate(self, method="status", result=None):
        started, release = threading.Event(), threading.Event()
        value = result or {"ok": True, "running": True, "pids": [1]}
        def probe():
            started.set()
            if not release.wait(3):
                raise RuntimeError("test did not release probe")
            return value
        getattr(self.client, method).side_effect = probe
        self.addCleanup(release.set)
        return started, release

    def test_cold_reads_return_unknown_and_coalesce_slow_probe(self):
        started, release = self.gate()
        first = self.cache.get("status")
        self.assertTrue(started.wait(1))
        self.assertTrue(first["pending"])
        self.assertIsNone(first["value"])
        self.assertIsNone(first["fetched_at"])
        for _ in range(20):
            self.assertTrue(self.cache.get("status")["pending"])
        self.client.status.assert_called_once()
        release.set()
        completed = self.settled()
        self.assertEqual(completed["value"]["pids"], [1])
        self.assertEqual(completed["cache_age_seconds"], 0)

    def test_keys_have_independent_bounded_workers(self):
        status_started, release_status = self.gate()
        process_started, release_process = self.gate("processes", {"ok": True, "processes": []})
        self.cache.get("status")
        self.cache.get("processes")
        self.assertTrue(status_started.wait(1))
        self.assertTrue(process_started.wait(1))
        for _ in range(20):
            self.cache.get("status")
            self.cache.get("processes")
        self.assertEqual(self.client.status.call_count, 1)
        self.assertEqual(self.client.processes.call_count, 1)
        release_status.set()
        release_process.set()
        self.settled()
        self.settled("processes")

    def test_ttl_starts_when_probe_completes_and_copies_cannot_change_cache(self):
        started, release = self.gate()
        self.cache.get("status")
        self.assertTrue(started.wait(1))
        self.now += 40
        release.set()
        completed = self.settled()
        completed["value"]["pids"].append(99)
        self.now += 14.9
        self.assertEqual(self.cache.get("status")["value"]["pids"], [1])
        self.client.status.assert_called_once()
        self.now += .2
        self.cache.get("status")
        self.settled()
        self.assertEqual(self.client.status.call_count, 2)

    def test_failure_preserves_success_and_uses_short_retry_backoff(self):
        self.settled()
        self.client.status.return_value = {"ok": False, "error": "fixture unavailable"}
        self.cache.get("status", force=True)
        failed = self.settled()
        self.assertEqual(failed["value"]["pids"], [1])
        self.assertEqual(failed["refresh_error"]["message"], "fixture unavailable")
        for _ in range(20):
            self.cache.get("status")
        self.assertEqual(self.client.status.call_count, 2)
        self.now += 3
        self.client.status.return_value = {"ok": True, "running": False, "pids": []}
        result = self.settled()
        self.assertFalse(result["value"]["running"])
        self.assertIsNone(result["refresh_error"])
        self.assertEqual(self.client.status.call_count, 3)

    def test_cold_exception_has_no_false_closed_value(self):
        self.client.status.side_effect = RuntimeError("fixture timeout")
        result = self.settled()
        self.assertIsNone(result["value"])
        self.assertIsNone(result["fetched_at"])
        self.assertEqual(result["refresh_error"]["message"], "fixture timeout")

    def test_forced_refresh_discards_inflight_result_and_coalesces_followup(self):
        started, first_release = self.gate()
        second_started, second_release = threading.Event(), threading.Event()
        first_probe = self.client.status.side_effect
        def probe():
            if self.client.status.call_count == 1:
                return first_probe()
            second_started.set()
            second_release.wait(3)
            return {"ok": True, "running": True, "pids": [2]}
        self.client.status.side_effect = probe
        self.addCleanup(second_release.set)
        self.cache.get("status")
        self.assertTrue(started.wait(1))
        for _ in range(10):
            self.cache.get("status", force=True)
        first_release.set()
        self.assertTrue(second_started.wait(1))
        pending = self.cache.get("status")
        self.assertTrue(pending["pending"])
        self.assertIsNone(pending["value"], "pre-refresh result was incorrectly published")
        second_release.set()
        self.assertEqual(self.settled()["value"]["pids"], [2])
        self.assertEqual(self.client.status.call_count, 2)

    def test_mutation_invalidates_both_completed_snapshots(self):
        self.settled()
        self.settled("processes")
        self.cache.invalidate()
        self.settled()
        self.settled("processes")
        self.assertEqual(self.client.status.call_count, 2)
        self.assertEqual(self.client.processes.call_count, 2)

    def test_close_prevents_followup_and_future_workers(self):
        started, release = self.gate()
        self.cache.get("status")
        self.assertTrue(started.wait(1))
        self.cache.get("status", force=True)
        self.cache.close()
        release.set()
        with mock.patch("codex_switcher.desktop.threading.Thread") as thread:
            for key in ("status", "processes"):
                value = self.cache.get(key, force=True)
                self.assertFalse(value["pending"])
                self.assertIsNone(value["value"])
            thread.assert_not_called()
        self.client.status.assert_called_once()

    def test_closed_before_worker_enters_does_not_call_probe(self):
        with mock.patch("codex_switcher.desktop.threading.Thread") as thread:
            self.cache.get("status")
            target = thread.call_args.kwargs["target"]
            args = thread.call_args.kwargs["args"]
        self.cache.close()
        target(*args)
        self.client.status.assert_not_called()

    def test_realtime_desktop_status_never_reads_display_cache(self):
        runner = mock.Mock(side_effect=['{"running":true,"pids":[1]}', '{"running":true,"pids":[2]}'])
        desktop = DesktopClient(runner=runner)
        self.cache.close()
        self.cache = DesktopDisplayCache(desktop)
        self.addCleanup(self.cache.close)
        self.assertEqual(self.settled()["value"]["pids"], [1])
        self.assertEqual(desktop.status()["pids"], [2])
        self.assertEqual(runner.call_count, 2)

    def action_fixture(self, initial_pids, *, pause_first_display=False):
        state = {"pids": list(initial_pids)}
        started, release = threading.Event(), threading.Event()
        display_reads = []
        self.addCleanup(release.set)
        def runner(script):
            if script == STATUS_SCRIPT:
                value = {"running": bool(state["pids"]), "pids": list(state["pids"]),
                         "all_pids": list(state["pids"])}
                if threading.current_thread().name == "desktop-display-status":
                    display_reads.append(value)
                    if pause_first_display and len(display_reads) == 1:
                        started.set()
                        release.wait(3)
                return json.dumps(value)
            if script == PROCESS_SCRIPT:
                return json.dumps({"processes": [{"pid": pid, "name": "ChatGPT.exe", "kind": "desktop"}
                                                 for pid in state["pids"]]})
            if script == RESTART_SCRIPT:
                previous = state["pids"]
                state["pids"] = [2]
                return json.dumps({"previous_pids": previous, "launched": True})
            if script == LAUNCH_SCRIPT:
                state["pids"] = [2]
                return '{"launched":true}'
            if script == CLOSE_SCRIPT:
                state["pids"] = []
                return '{"remaining":[]}'
            raise AssertionError("unexpected script")
        desktop = DesktopClient(runner=runner, enabled=lambda: True, sleep=lambda _: None)
        self.cache.close()
        self.cache = DesktopDisplayCache(desktop)
        self.addCleanup(self.cache.close)
        return desktop, started, release, display_reads

    def test_non_http_restart_and_delayed_recovery_discard_inflight_old_probe(self):
        for delayed in (False, True):
            with self.subTest(delayed_recovery=delayed):
                desktop, started, release, reads = self.action_fixture(
                    [] if delayed else [1], pause_first_display=True)
                self.assertTrue(self.cache.get("status")["pending"])
                self.assertTrue(started.wait(1))
                # These are the exact shared methods used by SwitchManager's
                # automatic switch and delayed recovery, without an HTTP route.
                result = (desktop.complete_restart([1], wait=False) if delayed
                          else desktop.restart(force=True))
                self.assertTrue(result["ok"])
                self.assertEqual(result["pids"], [2])
                release.set()
                self.assertEqual(self.settled()["value"]["pids"], [2])
                self.assertEqual(len(reads), 2, "old observation was refreshed in one worker")

    def test_non_http_start_and_close_invalidate_both_completed_snapshots(self):
        for close in (False, True):
            with self.subTest(close=close):
                desktop, _, _, _ = self.action_fixture([1] if close else [])
                self.settled()
                self.settled("processes")
                result = desktop.close_app(force=True) if close else desktop.start()
                self.assertTrue(result["ok"])
                self.assertTrue(self.cache.get("status")["pending"])
                self.assertTrue(self.cache.get("processes")["pending"])
                self.assertEqual(self.settled()["value"]["pids"], [] if close else [2])
                self.assertEqual([row["pid"] for row in self.settled("processes")["value"]["processes"]],
                                 [] if close else [2])

    def test_display_listener_failure_cannot_change_actions_and_close_unsubscribes(self):
        desktop, _, _, _ = self.action_fixture([])
        bad = mock.Mock(side_effect=RuntimeError("display failure"))
        unsubscribe_bad = desktop.add_display_listener(bad)
        self.assertTrue(desktop.start()["ok"])
        self.assertEqual(bad.call_count, 2)
        unsubscribe_bad()
        self.cache.close()
        with mock.patch.object(self.cache, "invalidate", side_effect=AssertionError("closed cache called")):
            self.assertEqual(len(desktop._display_listeners), 0)
            self.assertTrue(desktop.close_app(force=True)["ok"])


if __name__ == "__main__":
    unittest.main()
