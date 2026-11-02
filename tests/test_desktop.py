"""Tests for the ChatGPT desktop restart helper (no real processes)."""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from unittest import mock

from codex_switcher.desktop import (
    CLOSE_SCRIPT,
    LAUNCH_SCRIPT,
    PROCESS_SCRIPT,
    RESTART_SCRIPT,
    STATUS_SCRIPT,
    _powershell,
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
    )


class DesktopClientTests(unittest.TestCase):
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
        runner = _Runner([{"running": True, "pids": [1], "aumid": "Pkg!App"}])
        result = _client(runner, enabled=False).restart(force=True)
        self.assertTrue(result["ok"])
        self.assertEqual(runner.restart_calls, 1)

    def test_not_running_is_started_instead_of_skipped(self):
        runner = _Runner(
            [
                {"running": False, "pids": [], "aumid": "Pkg!App"},
                {"running": True, "pids": [333], "aumid": "Pkg!App"},
            ]
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
        self.assertEqual(runner.restart_calls, 0)
        self.assertEqual(runner.launch_calls, 1)

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
        self.assertIn("续跑结束后会自动打开", result["message"])
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

        popen.assert_called_once()
        argv = popen.call_args.args[0]
        self.assertEqual(argv[0], "powershell.exe")
        self.assertIn(f"Start-Process '{url}'", argv[-1])
        kwargs = popen.call_args.kwargs
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        self.assertNotIn("timeout", kwargs)

    def test_wake_thread_rejects_a_non_uuid(self):
        runner = _Runner([{"running": True, "pids": [11], "aumid": "Pkg!App"}])
        with self.assertRaises(ValueError):
            _client(runner).wake_thread("../../evil")
        self.assertEqual(runner.status_calls, 0)

    def test_protocol_activation_keeps_a_hidden_console_instead_of_detaching(self):
        """Detached PowerShell can exit zero without delivering an MSIX URI."""
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
        flags = popen.call_args.kwargs["creationflags"]
        self.assertEqual(flags, 0x08000000)
        self.assertFalse(flags & 0x00000008)
        self.assertFalse(flags & 0x00000200)
        popen.return_value.wait.assert_not_called()

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
