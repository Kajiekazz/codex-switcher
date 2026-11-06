"""A service launched from Codex must survive termination of the caller's Job."""

import ctypes
import os
import sys
import unittest
from unittest import mock

from codex_switcher import windows_ui


@unittest.skipUnless(os.name == "nt", "Windows process Job isolation")
class JobIsolationTests(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(os.environ)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop("CODEX_SWITCHER_JOB_BREAKAWAY", None)
        self.frozen = mock.patch.object(sys, "frozen", True, create=True)
        self.frozen.start()
        self.addCleanup(self.frozen.stop)
        self.kernel = mock.Mock()
        self.in_job = True

        def is_in_job(process, job, output):
            output._obj.value = self.in_job
            return True

        self.kernel.IsProcessInJob.side_effect = is_in_job
        self.library = mock.patch.object(ctypes, "WinDLL", return_value=self.kernel)
        self.library.start()
        self.addCleanup(self.library.stop)
        self.popen = mock.patch.object(windows_ui.subprocess, "Popen")
        self.launch = self.popen.start()
        self.addCleanup(self.popen.stop)

    def test_managed_launch_preserves_arguments_and_settings_in_independent_child(self):
        os.environ["CODEX_SWITCHER_DATA"] = "fixture-data"
        self.assertTrue(windows_ui.relaunch_outside_job(["--background", "--startup"]))
        self.launch.assert_called_once()
        self.assertEqual(self.launch.call_args.args[0], [sys.executable, "--background", "--startup"])
        options = self.launch.call_args.kwargs
        self.assertTrue(options["creationflags"] & windows_ui.subprocess.CREATE_BREAKAWAY_FROM_JOB)
        self.assertTrue(options["creationflags"] & windows_ui.subprocess.CREATE_NO_WINDOW)
        self.assertEqual(options["env"]["CODEX_SWITCHER_DATA"], "fixture-data")
        self.assertEqual(options["env"]["CODEX_SWITCHER_JOB_BREAKAWAY"], "1")
        self.assertNotIn("CODEX_SWITCHER_JOB_BREAKAWAY", os.environ)

    def test_independent_child_continues_and_drops_one_attempt_marker(self):
        self.in_job = False
        os.environ["CODEX_SWITCHER_JOB_BREAKAWAY"] = "1"
        self.assertFalse(windows_ui.relaunch_outside_job(["--background"]))
        self.assertNotIn("CODEX_SWITCHER_JOB_BREAKAWAY", os.environ)
        self.launch.assert_not_called()

    def test_source_runs_keep_their_existing_lifecycle(self):
        with mock.patch.object(sys, "frozen", False):
            self.assertFalse(windows_ui.relaunch_outside_job(["--background"]))
        self.kernel.IsProcessInJob.assert_not_called()
        self.launch.assert_not_called()

    def test_unexpected_job_inheritance_cannot_relaunch_forever(self):
        os.environ["CODEX_SWITCHER_JOB_BREAKAWAY"] = "1"
        with self.assertRaises(OSError):
            windows_ui.relaunch_outside_job(["--background"])
        self.launch.assert_not_called()

    def test_refused_breakaway_is_reported_before_service_start(self):
        self.launch.side_effect = PermissionError("breakaway denied")
        with self.assertRaises(PermissionError):
            windows_ui.relaunch_outside_job(["--background"])
        self.launch.assert_called_once()

    def test_normal_entry_relaunches_before_acquiring_service_mutex(self):
        from codex_switcher import application
        with mock.patch.object(windows_ui, "relaunch_outside_job", return_value=True) as relaunch:
            with mock.patch.object(windows_ui, "SingleInstance") as mutex:
                self.assertEqual(application.main(["--background", "--startup"]), 0)
        relaunch.assert_called_once_with(["--background", "--startup"])
        mutex.assert_not_called()
