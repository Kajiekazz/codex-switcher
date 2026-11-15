"""Native display integration stays separate from account-switch decisions."""
import json
import os
import unittest
from unittest import mock

from codex_switcher.desktop import DesktopClient, STATUS_SCRIPT


def process(pid=10, **changes):
    return {"pid": pid, "ppid": 1, "name": "ChatGPT.exe", "threads": 20,
            "handles": 50, "working_set": 30 * 1024 * 1024, "private_memory": 30 * 1024 * 1024,
            "wait_status": 258, "probe_error": 0, "started": "2026-11-15T00:00:00Z",
            "command": "", "has_window": False, "exited": False,
            "identity_confirmed": True, "command_available": True, **changes}


class NativeDesktopDisplayTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.rows = [process()]
        self.aumid = "OpenAI.Codex_fixture!App"
        self.probe = self.patch("_native_display_processes", side_effect=lambda: self.rows)
        self.package = self.patch("_native_display_aumid", side_effect=lambda families: self.aumid)
        self.shell = self.patch("_powershell", side_effect=AssertionError("unexpected helper"))
        self.client = DesktopClient(clock=lambda: self.now)

    def patch(self, name, **kwargs):
        patcher = mock.patch("codex_switcher.desktop." + name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def fallback(self):
        self.shell.side_effect = None
        self.shell.return_value = json.dumps({"status": {"pids": [99], "aumid": self.aumid},
                                              "processes": {"processes": []}})

    def test_native_read_supplies_both_panels_without_helper_or_reopening_pids(self):
        with mock.patch("codex_switcher.desktop._display_exit_observations") as second_probe:
            result = self.client.display_snapshot()
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"]["pids"], [10])
        self.assertEqual(result["processes"]["counts"]["desktop"], 1)
        self.assertEqual(self.client._display_probe_source, "native")
        self.probe.assert_called_once()
        self.shell.assert_not_called()
        second_probe.assert_not_called()

    def test_trimmed_desktop_is_running_but_activation_stub_is_not(self):
        self.rows = [process(1, working_set=1, private_memory=1, threads=4),
                     process(2, working_set=1, private_memory=21 * 1024 * 1024, threads=1),
                     process(3, working_set=1, private_memory=1, threads=1, has_window=True),
                     process(4, working_set=1, private_memory=1, threads=1)]
        result = self.client.display_snapshot()["status"]
        self.assertEqual(result["pids"], [1, 2, 3])
        self.assertEqual(result["all_pids"], [1, 2, 3, 4])

    def test_only_explicitly_exited_rows_are_excluded_and_wait_errors_are_retained(self):
        self.rows = [process(1, wait_status=0, exited=True),
                     process(2, wait_status=-1, probe_error=5)]
        result = self.client.display_snapshot()
        self.assertEqual(result["status"]["pids"], [2])
        self.assertEqual(result["status"]["exited_pids"], [1])
        self.assertEqual(result["status"]["process_details"][0]["probe_error"], 5)
        self.assertEqual([p["pid"] for p in result["processes"]["processes"]], [2])

    def test_unconfirmed_identity_or_incomplete_stub_falls_back(self):
        self.fallback()
        for updates in ({"identity_confirmed": False},
                        {"threads": 1, "working_set": None, "private_memory": 1, "has_window": False}):
            with self.subTest(updates=updates):
                self.rows = [process(**updates)]
                client = DesktopClient(clock=lambda: self.now)
                with mock.patch("codex_switcher.desktop._display_exit_observations", return_value={}):
                    result = client.display_snapshot()
                self.assertEqual(result["status"]["pids"], [99])
                self.assertEqual(client._display_probe_source, "powershell")

    def test_inaccessible_command_is_unknown_and_command_contents_are_not_exposed(self):
        self.rows = [process(1, name="codex.exe", command="codex app-server", ppid=os.getpid()),
                     process(2, name="codex.exe", command="codex resume fixture"),
                     process(3, name="codex.exe", command_available=False, command=None)]
        result = self.client.display_snapshot()["processes"]
        self.assertEqual(result["counts"], {"desktop": 0, "app-server": 1, "cli": 1, "unknown": 1})
        self.assertEqual(result["processes"][0]["owner"], "codex-switcher")
        self.assertTrue(all("command" not in item for item in result["processes"]))

    def test_no_processes_and_no_registered_package_is_successful_observation(self):
        self.rows, self.aumid = [], None
        result = self.client.display_snapshot()
        self.assertTrue(result["ok"])
        self.assertTrue(result["status"]["available"])
        self.assertFalse(result["status"]["running"])
        self.assertIsNone(result["status"]["aumid"])
        self.shell.assert_not_called()

    def test_package_cache_expiry_and_action_invalidation(self):
        self.client.display_snapshot()
        self.now += 299
        self.client.display_snapshot()
        self.assertEqual(self.package.call_count, 1)
        self.now += 1
        self.client.display_snapshot()
        self.assertEqual(self.package.call_count, 2)
        self.client._notify_display_change()
        self.client.display_snapshot()
        self.assertEqual(self.package.call_count, 3)

    def test_missing_package_is_rechecked_after_fifteen_seconds(self):
        self.aumid = None
        self.client.display_snapshot()
        self.now += 14
        self.client.display_snapshot()
        self.assertEqual(self.package.call_count, 1)
        self.now += 1
        self.aumid = "OpenAI.Codex_fixture!App"
        self.assertEqual(self.client.display_snapshot()["status"]["aumid"], self.aumid)
        self.assertEqual(self.package.call_count, 2)

    def test_invalidated_inflight_package_is_not_written_back(self):
        def lookup(families):
            self.client._notify_display_change()
            return self.aumid
        self.package.side_effect = lookup
        self.assertTrue(self.client.display_snapshot()["ok"])
        self.assertIsNone(self.client._display_package)

    def test_failed_native_probe_falls_back_then_recovers_after_cooldown(self):
        self.fallback()
        self.probe.side_effect = OSError("fixture unavailable")
        with mock.patch("codex_switcher.desktop._display_exit_observations", return_value={}), mock.patch(
                "codex_switcher.desktop._find_display_powershell", return_value=None):
            self.assertTrue(self.client.display_snapshot()["ok"])
            self.now += 299
            self.assertTrue(self.client.display_snapshot()["ok"])
        self.assertEqual(self.probe.call_count, 1)
        self.assertEqual(self.shell.call_count, 2)
        self.probe.side_effect = lambda: self.rows
        self.now += 1
        self.assertEqual(self.client.display_snapshot()["status"]["pids"], [10])
        self.assertEqual(self.probe.call_count, 2)
        self.assertEqual(self.shell.call_count, 2)

    def test_package_error_falls_back_without_caching_missing_installation(self):
        self.fallback()
        self.package.side_effect = OSError("fixture query denied")
        with mock.patch("codex_switcher.desktop._display_exit_observations", return_value={}):
            self.assertTrue(self.client.display_snapshot()["ok"])
        self.assertEqual(self.client._display_package["aumid"], self.aumid)
        self.assertEqual(self.client._display_probe_source, "powershell")

    def test_injected_runner_never_probes_native_machine(self):
        runner = mock.Mock(return_value=json.dumps({"status": {}, "processes": {}}))
        with mock.patch("codex_switcher.desktop._display_exit_observations", return_value={}):
            self.assertTrue(DesktopClient(runner=runner).display_snapshot()["ok"])
        self.probe.assert_not_called()
        self.package.assert_not_called()
        runner.assert_called_once()

    def test_action_status_remains_fresh_and_uses_its_original_runner(self):
        self.client.display_snapshot()
        self.shell.side_effect = None
        self.shell.return_value = json.dumps({"pids": [42]})
        self.assertEqual(self.client.status()["pids"], [42])
        self.shell.assert_called_once_with(STATUS_SCRIPT)
        self.probe.assert_called_once()

    def test_package_discovery_receives_confirmed_running_families(self):
        self.rows = [process(package_family="OpenAI.Codex_2p2nqsd0c76g0")]
        self.client.display_snapshot()
        self.package.assert_called_once_with(("OpenAI.Codex_2p2nqsd0c76g0",))


if __name__ == "__main__":
    unittest.main()
