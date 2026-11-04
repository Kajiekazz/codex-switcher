"""Boot restoration with disposable data and no real app or network calls."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from codex_switcher.startup_recovery import StartupRecovery


class StartupRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "startup.json"
        self.stopped = threading.Event()
        self.desktop, self.goals, self.quota, self.monitor = (mock.Mock() for _ in range(4))
        self.desktop.status.return_value = {"ok": True, "running": False, "pids": []}
        self.desktop.start.return_value = {"ok": True}
        self.goals.startup_recovery_ids.return_value = ["chat"]
        self.goals.on_startup.return_value = {"ok": True, "job": {"id": "boot"}, "thread_ids": ["chat"]}
        self.quota.current.return_value = {"ok": True, "allowed": True}
        self.events = []
        self.desktop.start.side_effect = lambda **kwargs: self.events.append("desktop") or {"ok": True}
        self.monitor.start.side_effect = lambda: self.events.append("monitor")
        self.goals.on_startup.side_effect = lambda: self.events.append("resume") or {
            "ok": True, "job": {"id": "boot"}, "thread_ids": ["chat"]}

    def worker(self, wait=None):
        return StartupRecovery(self.desktop, self.goals, self.quota, self.monitor,
                               self.stopped, path=self.path, wait=wait or (lambda delay: False))

    def test_launches_then_starts_monitor_then_dispatches_and_records_receipt(self):
        worker = self.worker()
        worker.run()
        self.assertEqual(self.events, ["desktop", "monitor", "resume"])
        self.assertEqual(worker.status()["state"], "done")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["job_id"], "boot")

    def test_existing_desktop_is_preserved_and_no_task_is_created_for_disabled_recovery(self):
        self.desktop.status.return_value = {"ok": True, "running": True}
        self.goals.startup_recovery_ids.return_value = []
        worker = self.worker()
        worker.run()
        self.desktop.start.assert_not_called()
        self.goals.on_startup.assert_not_called()
        self.assertEqual(worker.status()["thread_ids"], [])

    def test_waits_for_network_quota_and_busy_dispatcher(self):
        self.quota.current.side_effect = [OSError("offline"), {"ok": True, "allowed": False},
                                           {"ok": True, "allowed": True}, {"ok": True, "allowed": True}]
        self.goals.on_startup.side_effect = [
            {"ok": True, "skipped": "job_running"},
            {"ok": True, "job": {"id": "boot"}, "thread_ids": ["chat"]},
        ]
        wait = mock.Mock(return_value=False)
        worker = self.worker(wait)
        worker.run()
        self.assertEqual(wait.call_count, 3)
        self.assertEqual(worker.status()["job_id"], "boot")
        self.assertEqual(self.desktop.start.call_count, 1)
        self.monitor.start.assert_called_once()

    def test_shutdown_cancels_waiting_before_dispatch(self):
        self.quota.current.return_value = {"ok": True, "allowed": False}
        def wait(delay):
            self.stopped.set()
            return True
        worker = self.worker(wait)
        worker.run()
        self.goals.on_startup.assert_not_called()
        self.assertEqual(worker.status()["state"], "cancelled")

    def test_failed_desktop_never_queues_a_task_and_does_not_stop_the_monitor(self):
        self.desktop.start.side_effect = lambda **kwargs: {"ok": False}
        worker = self.worker()
        worker.run()
        self.assertEqual(self.desktop.start.call_count, 3)
        self.assertEqual(worker.status()["state"], "failed")
        self.goals.on_startup.assert_not_called()
        self.monitor.start.assert_called_once()
