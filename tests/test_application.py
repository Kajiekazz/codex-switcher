import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_switcher import settings
from codex_switcher.application import ApplicationRuntime, instance_info, request_exit


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
        with mock.patch("urllib.request.urlopen") as send:
            send.return_value.__enter__.return_value.read.return_value = b'{"ok": true}'
            self.assertTrue(request_exit())
            self.assertEqual(send.call_args.args[0].full_url, "http://127.0.0.1:8787/api/app/exit")
        runtime.close()
        self.assertFalse((self.root / "app-instance.json").exists())

    def test_invalid_instance_file_cannot_send_a_token_to_an_external_site(self):
        for url in ("https://external.example", "http://external.example:8787", "http://user@localhost:8787"):
            (self.root / "app-instance.json").write_text(json.dumps({"url": url, "token": "fixture"}))
            with mock.patch("urllib.request.urlopen") as send:
                self.assertIsNone(instance_info())
                self.assertFalse(request_exit())
                send.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
