"""Tests for the ChatGPT desktop restart helper (no real processes)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_switcher.desktop import (
    CLOSE_SCRIPT,
    DISPLAY_SCRIPT,
    LAUNCH_SCRIPT,
    PROCESS_EXIT_SCRIPT,
    PROCESS_SCRIPT,
    RESTART_SCRIPT,
    STATUS_SCRIPT,
    _powershell,
    _display_exit_observations,
    _find_display_powershell,
    _registered_display_powershell,
    DesktopClient,
    _windows_pids,
    _windows_processes,
)


class _Clock:
    def __init__(self, value: float = 1000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += float(seconds)


class _Runner:
    def __init__(
        self,
        statuses: list[dict],
        restart: dict | None = None,
        launch: dict | None = None,
        processes: dict | None = None,
        close: dict | None = None,
    ):
        self.statuses = statuses
        self.restart_payload = restart if restart is not None else {
            "previous_pids": [111],
            "killed": [111, 112],
            "aumid": "OpenAI.Codex_abc!App",
        }
        self.launch_payload = launch if launch is not None else {
            "aumid": "OpenAI.Codex_abc!App",
            "launched": True,
        }
        self.close_payload = close if close is not None else {
            "previous_pids": [111],
            "killed": [111, 112],
            "remaining": [],
        }
        self.processes_payload = processes
        self.status_calls = 0
        self.restart_calls = 0
        self.launch_calls = 0
        self.close_calls = 0
        self.process_calls = 0

    def __call__(self, script: str) -> str:
        if script == STATUS_SCRIPT:
            index = min(self.status_calls, len(self.statuses) - 1)
            self.status_calls += 1
            return json.dumps(self.statuses[index])
        if script == RESTART_SCRIPT:
            self.restart_calls += 1
            return json.dumps(self.restart_payload)
        if script == CLOSE_SCRIPT:
            self.close_calls += 1
            return json.dumps(self.close_payload)
        if script == LAUNCH_SCRIPT:
            self.launch_calls += 1
            return json.dumps(self.launch_payload)
        if script == PROCESS_SCRIPT:
            self.process_calls += 1
            return json.dumps(self.processes_payload or {"processes": []})
        raise AssertionError("unexpected script")


def _client(runner, *, enabled=True, clock=None, timeout=5):
    clock = clock or _Clock()
    return DesktopClient(
        enabled=lambda: enabled,
        runner=runner,
        sleep=clock.sleep,
        clock=clock,
        timeout_seconds=timeout,
        poll_seconds=0.5,
        native_backend=mock.Mock(),
    )


class DesktopClientTests(unittest.TestCase):
    def setUp(self):
        # These tests exercise the compatibility helper, never the host machine.
        self.native_probe = mock.patch("codex_switcher.desktop._native_display_processes", side_effect=OSError("fixture unavailable"))
        self.native_probe.start()
        self.addCleanup(self.native_probe.stop)

    def test_warm_display_prefers_existing_pwsh_but_live_status_uses_original_runner(self):
        snapshot = {"status": {"pids": [], "aumid": "Package!App"}, "processes": {"processes": []}}
        def run(script, **kwargs):
            return json.dumps({"pids": [2]} if script == STATUS_SCRIPT else snapshot)
        with mock.patch("codex_switcher.desktop._powershell", side_effect=run) as runner, mock.patch(
            "codex_switcher.desktop._find_display_powershell", return_value=("fixture-pwsh.exe", "path")) as discovery:
            client = DesktopClient(enabled=lambda: False)
            self.assertTrue(client._script_display_snapshot()["ok"])
            self.assertNotIn("executable", runner.call_args.kwargs)
            discovery.assert_not_called()
            self.assertTrue(client._script_display_snapshot()["ok"])
            self.assertEqual(runner.call_args.kwargs["executable"], "fixture-pwsh.exe")
            self.assertEqual(client._display_shell_source, "path")
            self.assertEqual(json.loads(client._runner(STATUS_SCRIPT))["pids"], [2])
            self.assertNotIn("executable", runner.call_args.kwargs)
            self.assertEqual(runner.call_args.args[0], STATUS_SCRIPT)

    def test_fast_display_discovery_or_execution_failure_falls_back_and_retries_after_cooldown(self):
        snapshot = {"status": {"pids": [], "aumid": "Package!App"}, "processes": {"processes": []}}
        for failure in ("discovery", "missing", "execution", "malformed"):
            with self.subTest(failure=failure):
                clock = _Clock()
                broken = [True]
                def discover():
                    if broken[0] and failure == "discovery":
                        raise OSError("fixture discovery denied")
                    if broken[0] and failure == "missing":
                        return None
                    return "fixture-pwsh.exe", "codex_runtime"
                def run(script, **kwargs):
                    if kwargs.get("executable") and broken[0]:
                        if failure == "execution":
                            raise RuntimeError("fixture shell unavailable")
                        if failure == "malformed":
                            return "{}"
                    return json.dumps(snapshot)
                with mock.patch("codex_switcher.desktop._powershell", side_effect=run) as runner, mock.patch(
                    "codex_switcher.desktop._find_display_powershell", side_effect=discover) as discovery:
                    client = DesktopClient(clock=clock)
                    client._script_display_snapshot()
                    before = runner.call_count
                    self.assertTrue(client._script_display_snapshot()["ok"])
                    self.assertEqual(runner.call_count - before, 2 if failure in {"execution", "malformed"} else 1)
                    self.assertNotIn("executable", runner.call_args.kwargs)
                    self.assertEqual(client._display_shell_source, "system")
                    client._script_display_snapshot()
                    self.assertEqual(discovery.call_count, 1)
                    self.assertNotIn("executable", runner.call_args.kwargs)
                    clock.sleep(300)
                    broken[0] = False
                    client._script_display_snapshot()  # refresh expired Appx metadata
                    self.assertTrue(client._script_display_snapshot()["ok"])
                    self.assertEqual(discovery.call_count, 2)
                    self.assertEqual(runner.call_args.kwargs["executable"], "fixture-pwsh.exe")

    def test_injected_display_runner_is_not_replaced_by_shell_discovery(self):
        snapshot = {"status": {"pids": [], "aumid": "Package!App"}, "processes": {"processes": []}}
        runner = mock.Mock(return_value=json.dumps(snapshot))
        client = _client(runner)
        with mock.patch("codex_switcher.desktop._find_display_powershell") as discovery:
            client.display_snapshot()
            client.display_snapshot()
        self.assertEqual(runner.call_count, 2)
        discovery.assert_not_called()

    def test_fast_display_shell_discovery_uses_existing_path(self):
        with mock.patch("codex_switcher.desktop.shutil.which", return_value="fixture-pwsh.exe"):
            self.assertEqual(_find_display_powershell(), ("fixture-pwsh.exe", "path"))
        with mock.patch("codex_switcher.desktop.shutil.which", return_value=None):
            self.assertIsNone(_find_display_powershell())

    def test_minimal_path_uses_registered_runtime_without_enumerating_windowsapps(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {
            "PATH": os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32"),
        }), mock.patch("codex_switcher.desktop.shutil.which", return_value=None) as discovery, mock.patch(
            "codex_switcher.desktop.Path.glob", side_effect=PermissionError("fixture inaccessible directory")) as enumeration:
            root = Path(folder) / "OpenAI.CodexPrimaryRuntime.fixture"
            shell = root / "dependencies/native/powershell/pwsh.exe"
            shell.parent.mkdir(parents=True)
            shell.write_bytes(b"fixture")
            snapshot = {"status": {"pids": [], "aumid": "Package!App"}, "processes": {"processes": []},
                        "runtime": {"name": "OpenAI.CodexPrimaryRuntime.fixture", "publisher_id": "3k8sg7r9htsxt",
                                    "status": "Ok", "install_location": str(root)}}
            with mock.patch("codex_switcher.desktop._powershell", return_value=json.dumps(snapshot)) as runner:
                client = DesktopClient()
                self.assertTrue(client._script_display_snapshot()["ok"])
                self.assertIn("Get-AppxPackage -Name 'OpenAI.CodexPrimaryRuntime*'", runner.call_args.args[0])
                self.assertTrue(client._script_display_snapshot()["ok"])
                self.assertNotIn("Get-AppxPackage", runner.call_args.args[0])
                self.assertEqual(runner.call_args.kwargs["executable"], str(shell))
                self.assertEqual(client._display_shell_source, "codex_runtime")
                client._runner(STATUS_SCRIPT)
                self.assertEqual(runner.call_args.args[0], STATUS_SCRIPT)
                self.assertNotIn("executable", runner.call_args.kwargs)
            discovery.assert_not_called()
            enumeration.assert_not_called()

    def test_registered_runtime_rejects_unhealthy_or_untrusted_packages_and_missing_shell(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shell = root / "dependencies/native/powershell/pwsh.exe"
            shell.parent.mkdir(parents=True)
            shell.write_bytes(b"fixture")
            runtime = {"name": "OpenAI.CodexPrimaryRuntime.fixture", "publisher_id": "3k8sg7r9htsxt",
                       "status": "Ok", "install_location": str(root)}
            self.assertEqual(_registered_display_powershell(runtime), (str(shell), "codex_runtime"))
            for update in ({"status": "NotAvailable"}, {"publisher_id": "other-publisher"},
                           {"name": "Other.CodexPrimaryRuntime"}, {"install_location": "relative-folder"}):
                with self.subTest(update=update):
                    self.assertIsNone(_registered_display_powershell({**runtime, **update}))
            shell.unlink()
            self.assertIsNone(_registered_display_powershell(runtime))

    def test_unavailable_registered_runtime_with_minimal_path_falls_back_to_system_shell(self):
        snapshot = {"status": {"pids": [], "aumid": "Package!App"}, "processes": {"processes": []},
                    "runtime": {"name": "OpenAI.CodexPrimaryRuntime.fixture", "publisher_id": "3k8sg7r9htsxt",
                                "status": "NotAvailable", "install_location": r"C:\WindowsApps\fixture"}}
        with mock.patch("codex_switcher.desktop.shutil.which", return_value=None), mock.patch(
            "codex_switcher.desktop._powershell", return_value=json.dumps(snapshot)) as runner:
            client = DesktopClient()
            self.assertTrue(client._script_display_snapshot()["ok"])
            self.assertTrue(client._script_display_snapshot()["ok"])
            self.assertNotIn("executable", runner.call_args.kwargs)
            self.assertEqual(client._display_shell_source, "system")

    def test_display_system_shell_uses_known_path_when_path_lookup_fails(self):
        snapshot = {"status": {"pids": [], "aumid": "Package!App"}, "processes": {"processes": []}}
        def run(script, **kwargs):
            if "executable" not in kwargs:
                raise FileNotFoundError("fixture system shell absent from PATH")
            return json.dumps(snapshot)
        with mock.patch("codex_switcher.desktop._powershell", side_effect=run) as runner, mock.patch(
            "codex_switcher.desktop._find_display_powershell", return_value=None):
            client = DesktopClient()
            self.assertTrue(client._script_display_snapshot()["ok"])
            expected = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")
            self.assertEqual(runner.call_args.kwargs["executable"], expected)
            self.assertTrue(client._script_display_snapshot()["ok"])
            self.assertEqual(runner.call_args.kwargs["executable"], expected)
            with self.assertRaises(FileNotFoundError):
                client._runner(STATUS_SCRIPT)
            self.assertNotIn("executable", runner.call_args.kwargs)

    def test_display_package_cache_expires_and_failed_probe_clears_it(self):
        clock = _Clock()
        payload = {"status": {"pids": [], "aumid": "Package_family!App"}, "processes": {"processes": []}}
        runner = mock.Mock(return_value=json.dumps(payload))
        client = _client(runner, clock=clock)
        self.assertTrue(client.display_snapshot()["ok"])
        self.assertIn("Get-AppxPackage", runner.call_args.args[0])
        clock.sleep(15)
        self.assertTrue(client.display_snapshot()["ok"])
        self.assertNotIn("Get-AppxPackage", runner.call_args.args[0])
        clock.sleep(285)
        self.assertTrue(client.display_snapshot()["ok"])
        self.assertIn("Get-AppxPackage", runner.call_args.args[0])
        runner.side_effect = RuntimeError("fixture unavailable")
        self.assertFalse(client.display_snapshot()["ok"])
        runner.side_effect = None
        self.assertTrue(client.display_snapshot()["ok"])
        self.assertIn("Get-AppxPackage", runner.call_args.args[0])

    def test_missing_package_is_rechecked_next_display_round_and_action_invalidates_metadata(self):
        clock = _Clock()
        payload = {"status": {"pids": [], "aumid": None}, "processes": {"processes": []}}
        runner = mock.Mock(return_value=json.dumps(payload))
        client = _client(runner, clock=clock)
        client.display_snapshot()
        clock.sleep(14)
        client.display_snapshot()
        self.assertNotIn("Get-AppxPackage", runner.call_args.args[0])
        clock.sleep(1)
        payload["status"]["aumid"] = "Package_family!App"
        runner.return_value = json.dumps(payload)
        client.display_snapshot()
        self.assertIn("Get-AppxPackage", runner.call_args.args[0])
        client._notify_display_change()
        client.display_snapshot()
        self.assertIn("Get-AppxPackage", runner.call_args.args[0])

    @unittest.skipUnless(os.name == "nt", "Win32 process-handle display filter")
    def test_display_excludes_only_signaled_process_handles(self):
        payload = {"status": {"pids": [1, 2], "all_pids": [1, 2, 3],
                              "process_details": [{"pid": pid} for pid in (1, 2, 3)]},
                   "processes": {"processes": []}}
        client = _client(mock.Mock(return_value=json.dumps(payload)))
        with mock.patch("codex_switcher.desktop._display_exit_observations", return_value={
            1: {"wait_status": 0, "probe_error": 0}, 2: {"wait_status": 258, "probe_error": 0},
            3: {"wait_status": -1, "probe_error": 5},
        }):
            state = client.display_snapshot()["status"]
        self.assertEqual(state["pids"], [2])
        self.assertEqual(state["all_pids"], [2, 3])
        self.assertEqual(state["exited_pids"], [1])
        self.assertEqual(state["process_details"][-1]["probe_error"], 5)

    def test_display_exit_probe_closes_every_opened_handle_and_preserves_access_errors(self):
        kernel = mock.Mock()
        kernel.OpenProcess.side_effect = [101, None, 103]
        kernel.WaitForSingleObject.side_effect = [0, 258]
        with mock.patch("codex_switcher.desktop.ctypes.WinDLL", return_value=kernel, create=True), mock.patch(
            "codex_switcher.desktop.ctypes.get_last_error", return_value=5, create=True):
            result = _display_exit_observations([1, 2, 3])
        self.assertEqual(result, {1: {"wait_status": 0, "probe_error": 0},
                                  2: {"wait_status": -1, "probe_error": 5},
                                  3: {"wait_status": 258, "probe_error": 0}})
        self.assertEqual(kernel.CloseHandle.call_args_list, [mock.call(101), mock.call(103)])

    def test_restart_guard_is_reported_without_waiting_for_or_launching_a_new_app(self):
        runner = _Runner([{"running": True, "pids": [11], "aumid": "Pkg!App"}], restart={"skipped": True, "reason": "service_in_desktop_tree"})
        result = _client(runner).restart(force=True)
        self.assertTrue(result["skipped"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "service_in_desktop_tree")
        self.assertEqual(result["killed"], [])
        self.assertEqual(runner.status_calls, 1)
        self.assertEqual(runner.launch_calls, 0)

    def test_close_guard_keeps_the_service_console_alive(self):
        runner = _Runner([{"running": True, "pids": [11], "aumid": "Pkg!App"}], close={"skipped": True, "reason": "service_in_desktop_tree"})
        result = _client(runner).close_app(force=True)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "service_in_desktop_tree")
        self.assertEqual(result["pids"], [11])
        self.assertEqual(runner.status_calls, 1)

    def test_helper_has_a_separate_hidden_console_and_knows_the_service_pid(self):
        with mock.patch("codex_switcher.desktop.subprocess.run", return_value=mock.Mock(returncode=0, stdout="{}")) as run:
            _powershell("Write-Output '{}'")
        self.assertEqual(run.call_args.kwargs["env"]["CODEX_SWITCHER_SERVICE_PID"], str(os.getpid()))
        self.assertEqual(run.call_args.kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_status_parses_processes_and_aumid(self):
        runner = _Runner([{"running": True, "pids": [11, 12], "aumid": "Pkg!App"}])
        status = _client(runner).status()
        self.assertTrue(status["ok"])
        self.assertTrue(status["running"])
        self.assertEqual(status["pids"], [11, 12])
        self.assertEqual(status["aumid"], "Pkg!App")
        self.assertTrue(status["enabled"])

    def test_status_failure_is_data(self):
        def boom(script):
            raise RuntimeError("powershell missing")

        status = _client(boom).status()
        self.assertFalse(status["ok"])
        self.assertFalse(status["available"])
        self.assertIn("powershell missing", status["error"])

    def test_disabled_skips_without_touching_processes(self):
        runner = _Runner([{"running": True, "pids": [1], "aumid": "Pkg!App"}])
        result = _client(runner, enabled=False).restart()
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "desktop_restart_disabled")
        self.assertEqual(runner.status_calls, 0)
        self.assertEqual(runner.restart_calls, 0)

    def test_force_bypasses_disabled_flag(self):
        runner = _Runner([{"running": True, "pids": [1], "aumid": "Pkg!App"},
                          {"running": True, "pids": [2], "aumid": "Pkg!App"}])
        result = _client(runner, enabled=False).restart(force=True)
        self.assertTrue(result["ok"])
        self.assertEqual(runner.restart_calls, 1)

    def test_not_running_is_started_instead_of_skipped(self):
        runner = _Runner(
            [
                {"running": False, "pids": [], "aumid": "Pkg!App"},
                {"running": True, "pids": [333], "aumid": "Pkg!App"},
            ], restart={"previous_pids": [], "killed": [], "launched": True, "aumid": "Pkg!App"}
        )
        result = _client(runner).restart()
        self.assertTrue(result["ok"])
        self.assertFalse(result["skipped"])
        self.assertTrue(result["launched"])
        self.assertIsNone(result["reason"])
        self.assertEqual(result["previous_pids"], [])
        self.assertEqual(result["pids"], [333])
        self.assertEqual(result["killed"], [])
        self.assertIn("已直接启动", result["message"])
        self.assertEqual(runner.restart_calls, 1)
        self.assertEqual(runner.launch_calls, 0)

    def test_negative_probe_still_restarts_the_process_found_by_the_helper(self):
        runner = _Runner([
            {"running": False, "pids": []},
            {"running": True, "pids": [222]},
        ])
        result = _client(runner).restart()
        self.assertTrue(result["ok"])
        self.assertEqual(result["previous_pids"], [111])
        self.assertTrue(result["restart_confirmed"])
        self.assertEqual(runner.launch_calls, 0)

    def test_unchanged_process_is_a_failed_restart_and_blocks_recovery(self):
        runner = _Runner([{"running": True, "pids": [111]}])
        desktop = _client(runner, timeout=1)
        result = desktop.restart()
        self.assertFalse(result["ok"])
        self.assertFalse(result["restart_confirmed"])
        self.assertEqual(result["reason"], "restart_not_confirmed")
        self.assertFalse(desktop.ready_for_resume())
        runner.statuses = [{"running": True, "pids": [222]}]
        self.assertTrue(desktop.ready_for_resume())

    def test_surviving_small_old_process_prevents_a_successful_restart(self):
        runner = _Runner([
            {"running": True, "pids": [111], "all_pids": [111, 112]},
            {"running": True, "pids": [222], "all_pids": [112, 222]},
        ])
        result = _client(runner, timeout=1).restart()
        self.assertFalse(result["ok"])
        self.assertEqual(result["remaining"], [112])

    def test_exited_residual_does_not_block_confirmation_or_later_recovery(self):
        before = {"running": True, "pids": [111], "all_pids": [111, 112]}
        after = {"running": True, "pids": [222], "all_pids": [222], "exited_pids": [112]}
        runner = _Runner([before, after], restart={
            "previous_pids": [111, 112], "killed": [111, 112], "remaining": [],
            "exited_pids": [111, 112], "launched": True,
        })
        desktop = _client(runner)
        result = desktop.restart()
        self.assertTrue(result["ok"])
        self.assertEqual(result["remaining"], [])
        self.assertEqual(result["exited_pids"], [111, 112])
        self.assertTrue(desktop.ready_for_resume())

        # A service still carrying a failed restart can recover after the user
        # opens a new app, even while Windows retains the old process object.
        desktop._restart_failed = True
        desktop._restart_old_pids = {111, 112}
        self.assertTrue(desktop.ready_for_resume())

    def test_start_not_detected_reports_failure(self):
        clock = _Clock()
        runner = _Runner([{"running": False, "pids": [], "aumid": "Pkg!App"}])
        with mock.patch("codex_switcher.desktop._stale_windows_pids", return_value=[]):
            result = _client(runner, clock=clock, timeout=2).start()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "launch_not_detected")
        self.assertTrue(result["launched"])
        self.assertIn("手动打开", result["message"])
        self.assertLessEqual(clock.value, 1005)

    def test_start_names_a_process_stuck_at_startup(self):
        """A suspended MSIX stub must be reported, not called "running"."""
        runner = _Runner([{"running": False, "pids": [], "aumid": "Pkg!App"}])
        with mock.patch(
            "codex_switcher.desktop._stale_windows_pids", return_value=[24804]
        ):
            result = _client(runner, timeout=1).start()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "launch_stuck")
        self.assertEqual(result["stale_pids"], [24804])
        self.assertIn("挂起", result["message"])

    def test_start_script_failure_is_data(self):
        def runner(script):
            if script == STATUS_SCRIPT:
                return json.dumps({"running": False, "pids": [], "aumid": "Pkg!App"})
            raise RuntimeError("launch denied")

        result = _client(runner).start()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "launch_failed")
        self.assertIn("launch denied", result["error"])

    def test_processes_names_the_occupants(self):
        runner = _Runner(
            [{"running": True, "pids": [11], "aumid": "Pkg!App"}],
            processes={
                "processes": [
                    {"pid": 11, "ppid": 1, "name": "ChatGPT.exe", "kind": "desktop"},
                    {
                        "pid": 22,
                        "ppid": os.getpid(),
                        "name": "codex.exe",
                        "kind": "app-server",
                        "command": "codex app-server",
                    },
                    {"pid": 33, "ppid": 1, "name": "codex.exe", "kind": "cli"},
                ]
            },
        )
        payload = _client(runner).processes()
        self.assertTrue(payload["ok"])
        self.assertEqual(
            payload["counts"], {"desktop": 1, "app-server": 1, "cli": 1}
        )
        self.assertEqual(payload["service_pid"], os.getpid())
        # Only our own child app-server is labelled; a desktop one is not.
        self.assertEqual(payload["processes"][1]["owner"], "codex-switcher")
        self.assertNotIn("owner", payload["processes"][0])

    def test_processes_accepts_a_single_object(self):
        # ConvertTo-Json unwraps single-element arrays on older PowerShell.
        runner = _Runner(
            [{"running": True, "pids": [1], "aumid": "Pkg!App"}],
            processes={"processes": {"pid": 7, "kind": "desktop", "name": "ChatGPT.exe"}},
        )
        payload = _client(runner).processes()
        self.assertTrue(payload["ok"])
        self.assertEqual([item["pid"] for item in payload["processes"]], [7])
        self.assertEqual(payload["counts"]["desktop"], 1)

    def test_processes_failure_is_data(self):
        def runner(script):
            raise RuntimeError("wmi denied")

        payload = _client(runner).processes()
        self.assertFalse(payload["ok"])
        self.assertIn("wmi denied", payload["error"])
        self.assertEqual(payload["processes"], [])

    def test_restart_closes_and_relaunches(self):
        runner = _Runner(
            [
                {"running": True, "pids": [111], "aumid": "Pkg!App"},
                {"running": True, "pids": [222], "aumid": "Pkg!App"},
            ]
        )
        result = _client(runner).restart()
        self.assertTrue(result["ok"])
        self.assertFalse(result["skipped"])
        self.assertEqual(result["previous_pids"], [111])
        self.assertEqual(result["pids"], [222])
        self.assertEqual(result["killed"], [111, 112])
        self.assertEqual(runner.restart_calls, 1)

    def test_relaunch_not_detected_reports_failure(self):
        clock = _Clock()
        runner = _Runner(
            [
                {"running": True, "pids": [111], "aumid": "Pkg!App"},
                {"running": False, "pids": [], "aumid": "Pkg!App"},
            ]
        )
        result = _client(runner, clock=clock, timeout=2).restart()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "relaunch_not_detected")
        self.assertIn("手动打开", result["message"])
        self.assertLessEqual(clock.value, 1005)

    def test_close_app_kills_the_tree_and_stays_closed(self):
        runner = _Runner(
            [
                {"running": True, "pids": [111], "aumid": "Pkg!App"},
                {"running": False, "pids": [], "aumid": "Pkg!App"},
            ]
        )
        result = _client(runner).close_app(force=True)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["reason"])
        self.assertEqual(result["previous_pids"], [111])
        self.assertEqual(result["pids"], [])
        self.assertEqual(result["killed"], [111, 112])
        self.assertIn("桌面端已关闭", result["message"])
        self.assertEqual(runner.close_calls, 1)
        # Closing must never launch the app: the resume job does that at the end.
        self.assertEqual(runner.launch_calls, 0)

    def test_close_app_when_nothing_runs_is_a_no_op(self):
        runner = _Runner([{"running": False, "pids": [], "aumid": "Pkg!App"}])
        result = _client(runner).close_app(force=True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["already_closed"])
        self.assertEqual(runner.close_calls, 0)
        self.assertEqual(runner.launch_calls, 0)

    def test_close_app_respects_the_restart_opt_in(self):
        runner = _Runner([{"running": True, "pids": [111], "aumid": "Pkg!App"}])
        result = _client(runner, enabled=False).close_app()
        self.assertFalse(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "desktop_restart_disabled")
        self.assertEqual(runner.close_calls, 0)

    def test_close_app_failure_is_data(self):
        def runner(script):
            if script == STATUS_SCRIPT:
                return json.dumps({"running": True, "pids": [9], "aumid": "Pkg!App"})
            raise RuntimeError("kill denied")

        result = _client(runner).close_app(force=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "close_failed")
        self.assertEqual(result["previous_pids"], [9])
        self.assertIn("kill denied", result["error"])

    def test_close_app_still_running_reports_failure(self):
        clock = _Clock()
        runner = _Runner(
            [
                {"running": True, "pids": [111], "aumid": "Pkg!App"},
                {"running": True, "pids": [999], "aumid": "Pkg!App"},
            ],
            close={"previous_pids": [111], "killed": [111], "remaining": [999]},
        )
        result = _client(runner, clock=clock, timeout=2).close_app(force=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "still_running")
        self.assertEqual(result["pids"], [999])
        self.assertIn("手动关闭", result["message"])
        self.assertLessEqual(clock.value, 1005)

    def test_restart_script_failure_keeps_previous_pids(self):
        calls = {"restart": 0}

        def runner(script):
            if script == STATUS_SCRIPT:
                return json.dumps({"running": True, "pids": [9], "aumid": "Pkg!App"})
            calls["restart"] += 1
            raise RuntimeError("kill denied")

        result = _client(runner).restart()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "restart_failed")
        self.assertEqual(result["previous_pids"], [9])
        self.assertEqual(calls["restart"], 1)

    def test_wake_thread_sends_the_app_deep_link(self):
        thread = "01a17a9b-88bf-7620-a979-c2ecbf5dcc26"
        runner = _Runner([{"running": True, "pids": [11], "aumid": "Pkg!App"}])
        client = _client(runner)
        opened = []
        with mock.patch.object(DesktopClient, "_open_url", opened.append):
            with mock.patch(
                "codex_switcher.desktop._windows_pids", return_value=[11]
            ):
                result = client.wake_thread(thread)

        self.assertTrue(result["ok"])
        self.assertFalse(result["skipped"])
        self.assertEqual(opened, [f"codex://threads/wake/{thread}"])
        self.assertEqual(result["url"], f"codex://threads/wake/{thread}")
        self.assertEqual(result["pids"], [11])
        # The fast probe answers, so the slow MSIX status script is not run.
        self.assertEqual(runner.status_calls, 0)

    def test_wake_thread_never_launches_a_closed_app(self):
        thread = "01a17a9b-88bf-7620-a979-c2ecbf5dcc26"
        runner = _Runner([{"running": False, "pids": [], "aumid": "Pkg!App"}])
        client = _client(runner)
        opened = []
        with mock.patch.object(DesktopClient, "_open_url", opened.append):
            with mock.patch("codex_switcher.desktop._windows_pids", return_value=[]):
                with mock.patch(
                    "codex_switcher.desktop._stale_windows_pids", return_value=[]
                ):
                    result = client.wake_thread(thread)

        self.assertFalse(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "desktop_not_running")
        self.assertEqual(opened, [])
        self.assertEqual(runner.launch_calls, 0)

    def test_wake_thread_reports_a_stuck_app_without_launching(self):
        """Suspended activation stubs look alive to tasklist but drain nothing."""
        thread = "01a17a9b-88bf-7620-a979-c2ecbf5dcc26"
        runner = _Runner([{"running": False, "pids": [], "aumid": "Pkg!App"}])
        client = _client(runner)
        opened = []
        with mock.patch.object(DesktopClient, "_open_url", opened.append):
            with mock.patch("codex_switcher.desktop._windows_pids", return_value=[]):
                with mock.patch(
                    "codex_switcher.desktop._stale_windows_pids", return_value=[24804]
                ):
                    result = client.wake_thread(thread)

        self.assertFalse(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "desktop_not_ready")
        self.assertEqual(result["stale_pids"], [24804])
        self.assertEqual(opened, [])
        self.assertEqual(runner.launch_calls, 0)

    def test_open_url_is_fire_and_forget(self):
        """A blocking ShellExecute used to be reported as a failed wake."""
        client = _client(_Runner([{"running": True, "pids": [11], "aumid": "Pkg!App"}]))
        url = "codex://threads/wake/01a17a9b-88bf-7620-a979-c2ecbf5dcc26"
        with mock.patch("codex_switcher.desktop.os.name", "nt"):
            with mock.patch("codex_switcher.desktop.subprocess.Popen") as popen:
                client._open_url(url)

        popen.assert_not_called()
        client._native_backend.open_uri.assert_called_once_with(url)

    def test_wake_thread_rejects_a_non_uuid(self):
        runner = _Runner([{"running": True, "pids": [11], "aumid": "Pkg!App"}])
        with self.assertRaises(ValueError):
            _client(runner).wake_thread("../../evil")
        self.assertEqual(runner.status_calls, 0)

    def test_protocol_activation_uses_native_dispatch_without_a_helper_process(self):
        """The native activation owns its STA; no PowerShell handoff exists."""
        client = _client(_Runner([]))
        with mock.patch("codex_switcher.desktop.os.name", "nt"):
            with mock.patch.multiple(
                "codex_switcher.desktop.subprocess",
                CREATE_NO_WINDOW=0x08000000,
                DETACHED_PROCESS=0x00000008,
                CREATE_NEW_PROCESS_GROUP=0x00000200,
                create=True,
            ):
                with mock.patch("codex_switcher.desktop.subprocess.Popen") as popen:
                    client._open_url("codex://threads/wake/01a17a9b-88bf-7620-a979-c2ecbf5dcc26")
        popen.assert_not_called()
        client._native_backend.open_uri.assert_called_once()

    def test_wake_thread_falls_back_to_status_when_the_probe_fails(self):
        """Unknown app state must never turn into a launch."""
        opened = []
        runner = _Runner([{"running": False, "pids": [], "aumid": "Pkg!App"}])
        client = _client(runner)
        with mock.patch.object(DesktopClient, "_open_url", opened.append):
            with mock.patch(
                "codex_switcher.desktop._windows_pids",
                side_effect=RuntimeError("tasklist unavailable"),
            ):
                result = client.wake_thread("01a17a9b-88bf-7620-a979-c2ecbf5dcc26")

        self.assertFalse(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "desktop_not_running")
        self.assertEqual(opened, [])
        self.assertEqual(runner.status_calls, 1)


class WindowsProbeTests(unittest.TestCase):
    """``tasklist`` also lists MSIX stubs that never actually started."""

    STUB = '"ChatGPT.exe","24804","Console","1","2,180 K"'
    LIVE = '"ChatGPT.exe","42112","Console","1","612,448 K"'

    def _probe(self, stdout: str):
        completed = mock.Mock(returncode=0, stdout=stdout, stderr="")
        with mock.patch(
            "codex_switcher.desktop.subprocess.run", return_value=completed
        ) as run:
            return _windows_processes(), _windows_pids(), run

    def test_suspended_stub_is_not_a_running_app(self):
        rows, pids, run = self._probe(f"{self.STUB}\r\n")
        self.assertEqual(rows, [{"pid": 24804, "mem_kb": 2180}])
        self.assertEqual(pids, [])
        self.assertIn("tasklist", run.call_args.args[0][0])

    def test_live_instance_survives_the_filter(self):
        rows, pids, _ = self._probe(f"{self.STUB}\r\n{self.LIVE}\r\n")
        self.assertEqual([row["pid"] for row in rows], [24804, 42112])
        self.assertEqual(pids, [42112])

    def test_unreadable_working_set_keeps_the_process(self):
        _, pids, _ = self._probe('"ChatGPT.exe","7","Console","1","N/A"\r\n')
        self.assertEqual(pids, [7])


@unittest.skipUnless(os.name == "nt", "PowerShell process-tree model")
class ProtectedServiceTreeTests(unittest.TestCase):
    def test_combined_display_reuses_inventory_without_compilation_or_process_mutation(self):
        source = r'''
$script:inventories = 0
function Get-CimInstance { param($ClassName,$Filter)
    $script:inventories++
    foreach ($id in 1,2,3) { [pscustomobject]@{ProcessId=$id;ParentProcessId=99;Name='ChatGPT.exe';CreationDate=[datetime]'2026-01-01'} }
    [pscustomobject]@{ProcessId=4;ParentProcessId=99;Name='codex.exe';CommandLine='codex app-server';CreationDate=[datetime]'2026-01-01'}
}
function Get-AppxPackage { [pscustomobject]@{PackageFamilyName='Fixture_family'} }
function Get-Process { param($Id)
    switch ($Id) {
        1 { [pscustomobject]@{Id=1;WorkingSet64=2MB;PrivateMemorySize64=300MB;Threads=@(1);MainWindowHandle=0} }
        2 { [pscustomobject]@{Id=2;WorkingSet64=2MB;PrivateMemorySize64=2MB;Threads=@(1,2);MainWindowHandle=0} }
        3 { [pscustomobject]@{Id=3;WorkingSet64=2MB;PrivateMemorySize64=2MB;Threads=@(1);MainWindowHandle=0} }
    }
}
function Add-Type { throw 'Display must not compile a helper' }
function Start-Process { throw 'Display must not start an application' }
function Stop-Process { throw 'Display must not stop an application' }
$result = & {
''' + DISPLAY_SCRIPT + r'''
}
[pscustomobject]@{payload=($result | ConvertFrom-Json);inventories=$script:inventories} | ConvertTo-Json -Compress -Depth 8
'''
        result = json.loads(_powershell(source))
        self.assertEqual(result["inventories"], 1)
        self.assertEqual(result["payload"]["status"]["pids"], [1, 2])
        self.assertEqual(result["payload"]["status"]["all_pids"], [1, 2, 3])
        self.assertEqual(result["payload"]["processes"]["processes"][-1]["kind"], "app-server")

    def test_native_termination_signal_handles_exit_code_259(self):
        # Keep this child process's handle open after it exits so Windows
        # retains the process object, reproducing the observed MSIX remnant.
        process = subprocess.Popen(
            [sys.executable, "-c", "raise SystemExit(259)"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            self.assertEqual(process.wait(timeout=10), 259)
            source = PROCESS_EXIT_SCRIPT + r'''
[pscustomobject]@{
    terminated = (Test-CsProcessExited __CHILD__)
    running = (Test-CsProcessExited $PID)
    unknown = (Test-CsProcessExited 2147483647)
} | ConvertTo-Json -Compress
'''.replace("__CHILD__", str(process.pid))
            state = json.loads(_powershell(source))
            self.assertTrue(state["terminated"])
            self.assertFalse(state["running"])
            self.assertFalse(state["unknown"])
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)

    def test_status_excludes_confirmed_exited_objects_but_keeps_live_stubs(self):
        source = r'''
Add-Type -TypeDefinition 'namespace CodexSwitcher { public static class ProcessExitProbe { public static bool HasExited(int id) { return id == 4; } } }'
function Get-AppxPackage { [pscustomobject]@{PackageFamilyName='Fake!App'} }
function Get-Process {
    [pscustomobject]@{Id=1;WorkingSet64=2MB;PrivateMemorySize64=300MB;Threads=@(1);MainWindowHandle=0}
    [pscustomobject]@{Id=3;WorkingSet64=80KB;PrivateMemorySize64=136KB;Threads=@(1);MainWindowHandle=0}
    [pscustomobject]@{Id=4;WorkingSet64=80KB;PrivateMemorySize64=136KB;Threads=@();MainWindowHandle=0}
}
''' + STATUS_SCRIPT
        state = json.loads(_powershell(source))
        self.assertEqual(state["pids"], [1])
        self.assertEqual(state["all_pids"], [1, 3])
        self.assertEqual(state["exited_pids"], [4])

    def test_remaining_exited_object_allows_launch_but_a_live_stub_still_blocks(self):
        prelude = r'''
Add-Type -TypeDefinition @'
namespace CodexSwitcher {
    public static class ProcessExitProbe {
        public static System.Collections.Generic.HashSet<int> Exited = new System.Collections.Generic.HashSet<int>();
        public static bool HasExited(int id) { return Exited.Contains(id); }
    }
}
'@
$env:CODEX_SWITCHER_SERVICE_PID = '102'
$script:stops = New-Object System.Collections.Generic.List[int]
$script:launches = 0
$app = [pscustomobject]@{Id=500}
$residual = [pscustomobject]@{Id=600}
$app | Add-Member ScriptMethod CloseMainWindow { return $true }
$residual | Add-Member ScriptMethod CloseMainWindow { return $false }
function Get-Process { param($Name) $app; $residual }
function Get-CimInstance {
    [pscustomobject]@{ProcessId=500;ParentProcessId=1}
    [pscustomobject]@{ProcessId=501;ParentProcessId=500}
    [pscustomobject]@{ProcessId=600;ParentProcessId=1}
    [pscustomobject]@{ProcessId=101;ParentProcessId=1}
    [pscustomobject]@{ProcessId=102;ParentProcessId=101}
}
function Get-AppxPackage { [pscustomobject]@{PackageFamilyName='Fake!App'} }
function Start-Sleep { param($Seconds,$Milliseconds) }
function Stop-Process {
    param($Id,[switch]$Force)
    $script:stops.Add([int]$Id)
    if ($Id -ne 600) { $null = [CodexSwitcher.ProcessExitProbe]::Exited.Add([int]$Id) }
}
function Start-Process { $script:launches++ }
'''
        for exited in (True, False):
            with self.subTest(residual_exited=exited):
                initial = "$null = [CodexSwitcher.ProcessExitProbe]::Exited.Add(600)" if exited else ""
                source = prelude + initial + "\n$result = & {\n" + RESTART_SCRIPT + r'''
}
[pscustomobject]@{payload=($result | ConvertFrom-Json);stops=@($script:stops);launches=$script:launches} | ConvertTo-Json -Depth 6 -Compress
'''
                result = json.loads(_powershell(source))
                self.assertIn(500, result["stops"])
                self.assertIn(500, result["payload"]["exited_pids"])
                self.assertNotIn(101, result["stops"])
                self.assertNotIn(102, result["stops"])
                self.assertEqual(result["payload"]["launched"], exited)
                self.assertEqual(result["launches"], 1 if exited else 0)
                self.assertEqual(result["payload"]["remaining"], [] if exited else [600])
                if exited:
                    self.assertNotIn(600, result["payload"]["previous_pids"])
                    self.assertNotIn(600, result["stops"])

    def test_status_recognizes_trimmed_apps_and_excludes_one_thread_stubs(self):
        source = r'''
function Get-AppxPackage { [pscustomobject]@{PackageFamilyName='Fake!App'} }
function Get-Process {
    [pscustomobject]@{Id=1;WorkingSet64=2MB;PrivateMemorySize64=300MB;Threads=@(1);MainWindowHandle=0}
    [pscustomobject]@{Id=2;WorkingSet64=2MB;PrivateMemorySize64=2MB;Threads=@(1,2);MainWindowHandle=0}
    [pscustomobject]@{Id=3;WorkingSet64=2MB;PrivateMemorySize64=2MB;Threads=@(1);MainWindowHandle=0}
}
''' + STATUS_SCRIPT
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", source],
                           capture_output=True, text=True, timeout=20,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(r.returncode, 0, r.stderr)
        state = json.loads(r.stdout.lstrip("\ufeff").strip())
        self.assertEqual(state["pids"], [1, 2])
        self.assertEqual(state["all_pids"], [1, 2, 3])

    def test_restart_and_close_never_kill_their_service_or_its_ancestors(self):
        # Every process/window command is replaced by an in-memory model. No
        # real application can be enumerated, closed, stopped or started here.
        prelude = r'''
$ErrorActionPreference = 'Stop'
$script:stops = New-Object System.Collections.Generic.List[int]
$script:windowsClosed = 0
$script:launches = 0
$env:CODEX_SWITCHER_SERVICE_PID = '102'
$app = [pscustomobject]@{Id=500}
$app | Add-Member ScriptMethod CloseMainWindow { $script:windowsClosed++; return $true }
function Get-Process { param($Name) if (-not $script:stops.Contains(500)) { return $app } }
function Get-CimInstance { param($ClassName)
    [pscustomobject]@{ProcessId=500;ParentProcessId=1}
    [pscustomobject]@{ProcessId=101;ParentProcessId=__PARENT__}
    [pscustomobject]@{ProcessId=102;ParentProcessId=101}
    [pscustomobject]@{ProcessId=501;ParentProcessId=500}
}
function Get-AppxPackage { [pscustomobject]@{PackageFamilyName='Fake!App'} }
function Start-Sleep { param($Seconds,$Milliseconds) }
function Stop-Process { param($Id,[switch]$Force) $script:stops.Add([int]$Id) }
function Start-Process { $script:launches++ }
'''
        for script in (RESTART_SCRIPT, CLOSE_SCRIPT):
            for parent, protected in ((500, True), (1, False)):
                with self.subTest(restart=script == RESTART_SCRIPT, nested=protected):
                    source = prelude.replace("__PARENT__", str(parent)) + "\n$result = & {\n" + script + r'''
}
[pscustomobject]@{payload=($result | ConvertFrom-Json);stops=@($script:stops);closed=$script:windowsClosed;launches=$script:launches} | ConvertTo-Json -Depth 6 -Compress
'''
                    r = subprocess.run(
                        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", source],
                        capture_output=True, text=True, timeout=20,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    self.assertEqual(r.returncode, 0, r.stderr)
                    result = json.loads(r.stdout.lstrip("\ufeff").strip())
                    self.assertNotIn(101, result["stops"])
                    self.assertNotIn(102, result["stops"])
                    if protected:
                        self.assertEqual(result["stops"], [])
                        self.assertEqual(result["closed"], 0)
                        self.assertEqual(result["launches"], 0)
                        self.assertEqual(result["payload"]["reason"], "service_in_desktop_tree")
                    else:
                        self.assertEqual(set(result["stops"]), {500, 501})
                        self.assertEqual(result["closed"], 1)


if __name__ == "__main__":
    unittest.main()
