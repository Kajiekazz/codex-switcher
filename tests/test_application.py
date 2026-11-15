import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from codex_switcher import settings
from codex_switcher.application import ApplicationRuntime, _wait_for_service, instance_info, main, request_exit


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"CODEX_SWITCHER_DATA": str(self.root),
                                               "CODEX_SWITCHER_HOST": "127.0.0.1", "CODEX_SWITCHER_PORT": "8787"})
        self.env.start()
        settings.reset_cache()

    def tearDown(self):
        self.env.stop()
        settings.reset_cache()
        self.tmp.cleanup()

    def test_running_instance_keeps_its_original_url_until_restart(self):
        runtime = ApplicationRuntime()
        server = mock.Mock()
        runtime.attach(server)
        settings.save({"port": 9898})
        self.assertEqual(runtime.url, "http://127.0.0.1:8787")
        with mock.patch("urllib.request.OpenerDirector.open") as send:
            send.return_value.__enter__.return_value.read.return_value = b'{"ok": true}'
            self.assertTrue(request_exit())
            self.assertEqual(send.call_args.args[0].full_url, "http://127.0.0.1:8787/api/app/exit")
        runtime.close()
        self.assertFalse((self.root / "app-instance.json").exists())

    def test_invalid_instance_file_cannot_send_a_token_to_an_external_site(self):
        for url in ("https://external.example", "http://external.example:8787", "http://user@localhost:8787"):
            (self.root / "app-instance.json").write_text(json.dumps({"url": url, "token": "fixture"}))
            with mock.patch("urllib.request.OpenerDirector.open") as send:
                self.assertIsNone(instance_info())
                self.assertFalse(request_exit())
                send.assert_not_called()

    def test_runtime_records_the_bound_port_without_changing_preference(self):
        runtime = ApplicationRuntime()
        server = mock.Mock(server_address=("127.0.0.1", 8792))
        runtime.attach(server)
        self.assertEqual(runtime.url, "http://127.0.0.1:8792")
        self.assertEqual(instance_info()["url"], runtime.url)
        self.assertEqual(settings.get("port"), None)
        runtime.close()

    def test_closing_an_old_runtime_does_not_remove_a_newer_instance_record(self):
        older, newer = ApplicationRuntime(), ApplicationRuntime()
        older.attach(mock.Mock())
        newer.attach(mock.Mock())
        older.close()
        self.assertEqual(instance_info()["token"], newer.token)
        newer.close()

    def test_normal_launch_never_runs_boot_recovery(self):
        runtime = ApplicationRuntime()
        server = mock.Mock()
        with mock.patch("codex_switcher.startup_recovery.StartupRecovery") as recovery:
            runtime.start_background_tasks(server)
        recovery.assert_not_called()
        server.auto_switch.start.assert_called_once()

    def test_startup_launch_schedules_recovery_without_racing_the_monitor(self):
        runtime = ApplicationRuntime(startup_launch=True)
        server = mock.Mock()
        with mock.patch("codex_switcher.startup_recovery.StartupRecovery") as recovery, mock.patch(
            "codex_switcher.application.threading.Thread") as worker:
            runtime.start_background_tasks(server)
        recovery.assert_called_once()
        worker.assert_called_once_with(target=recovery.return_value.run, name="startup-recovery", daemon=True)
        worker.return_value.start.assert_called_once()
        server.auto_switch.start.assert_not_called()

    def test_slow_initialization_is_allowed_to_finish_after_twenty_seconds(self):
        runtime = ApplicationRuntime()
        clock = [0.0]

        def wait(interval):
            clock[0] += interval
            return clock[0] >= 30.0

        with mock.patch.object(runtime.ready, "wait", side_effect=wait), mock.patch(
                "codex_switcher.application.time.monotonic", side_effect=lambda: clock[0]):
            _wait_for_service(runtime, self.root / "app.log")
        self.assertGreaterEqual(clock[0], 30.0)
        self.assertLess(clock[0], 31.0)

    def test_initialization_failure_is_reported_without_waiting_for_timeout(self):
        runtime = ApplicationRuntime()
        runtime.stopped.set()
        with mock.patch.object(runtime.ready, "wait", return_value=False) as wait:
            with self.assertRaisesRegex(RuntimeError, "后台服务启动失败.*app.log"):
                _wait_for_service(runtime, self.root / "app.log")
        wait.assert_called_once()

    def test_unresponsive_initialization_has_a_bounded_wait_and_log_location(self):
        runtime = ApplicationRuntime()
        clock = [0.0]

        def wait(interval):
            clock[0] += interval
            return False

        with mock.patch.object(runtime.ready, "wait", side_effect=wait), mock.patch(
                "codex_switcher.application.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(RuntimeError, "初始化超时.*app.log"):
                _wait_for_service(runtime, self.root / "app.log", timeout=1.0)
        self.assertGreaterEqual(clock[0], 1.0)
        self.assertLess(clock[0], 1.2)

    def test_waiting_launcher_reuses_healthy_instance_without_another_service(self):
        with mock.patch("codex_switcher.windows_ui.SingleInstance") as mutex, mock.patch(
                "codex_switcher.api._existing_instance", return_value={"service_pid": 123}), mock.patch(
                "codex_switcher.application.ApplicationRuntime") as runtime:
            mutex.return_value.acquired = False
            self.assertEqual(main(["--background", "--no-tray"]), 0)
        runtime.assert_not_called()
        mutex.return_value.try_acquire.assert_not_called()
        mutex.return_value.close.assert_called_once()

    def test_waiting_launcher_starts_service_when_previous_owner_releases(self):
        def launch_worker(*, target, **kwargs):
            worker = mock.Mock()
            worker.start.side_effect = target
            return worker

        with mock.patch("codex_switcher.windows_ui.SingleInstance") as mutex, mock.patch(
                "codex_switcher.api._existing_instance", return_value=None), mock.patch(
                "codex_switcher.application.ApplicationRuntime") as runtime, mock.patch(
                "codex_switcher.api.run") as run, mock.patch(
                "codex_switcher.application.threading.Thread", side_effect=launch_worker), mock.patch(
                "codex_switcher.application._wait_for_service") as ready, mock.patch(
                "codex_switcher.windows_ui.message_box") as error, mock.patch(
                "sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            mutex.return_value.acquired = False

            def acquire():
                mutex.return_value.acquired = True
                return True

            mutex.return_value.try_acquire.side_effect = acquire
            try:
                self.assertEqual(main(["--background", "--no-tray"]), 0)
            finally:
                # main routes service logs into this isolated temporary file.
                sys.stdout.close()
        run.assert_called_once_with(app_runtime=runtime.return_value)
        ready.assert_called_once()
        error.assert_not_called()
        mutex.return_value.try_acquire.assert_called_once()
        mutex.return_value.close.assert_called_once()

    def test_failed_startup_wait_stops_and_joins_service_before_releasing_mutex(self):
        with mock.patch("codex_switcher.windows_ui.SingleInstance") as mutex, mock.patch(
                "codex_switcher.api._existing_instance", return_value=None), mock.patch(
                "codex_switcher.application.ApplicationRuntime") as runtime, mock.patch(
                "codex_switcher.application.threading.Thread") as worker, mock.patch(
                "codex_switcher.application._wait_for_service",
                side_effect=RuntimeError("后台服务初始化超时")), mock.patch(
                "codex_switcher.windows_ui.message_box") as error, mock.patch(
                "codex_switcher.application.webbrowser.open") as browser, mock.patch(
                "sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            mutex.return_value.acquired = True
            cleanup = mock.Mock()
            cleanup.attach_mock(runtime.return_value.stop, "stop")
            cleanup.attach_mock(worker.return_value.join, "join")
            cleanup.attach_mock(mutex.return_value.close, "release")
            try:
                self.assertEqual(main(["--no-tray"]), 1)
            finally:
                sys.stdout.close()
        self.assertEqual(cleanup.mock_calls, [mock.call.stop(), mock.call.join(timeout=10), mock.call.release()])
        worker.return_value.start.assert_called_once()
        runtime.return_value.close.assert_called_once()
        browser.assert_not_called()
        error.assert_called_once_with("后台服务初始化超时")


if __name__ == "__main__":
    unittest.main()
