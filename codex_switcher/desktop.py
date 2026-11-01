"""Restart the Codex desktop client so a switch becomes visible.

The MSIX desktop app (package ``OpenAI.Codex``, process ``ChatGPT``) reads
``~/.codex/auth.json`` at startup and keeps the account in memory. Replacing
the file while it runs does not hot-switch, so a restart is required after a
manual or automatic switch.

Only the ``ChatGPT`` process tree is restarted; standalone ``codex`` CLI
sessions in a terminal are left untouched. The restart is opt-in through
``CODEX_SWITCHER_RESTART_DESKTOP=1`` and can be forced per request by the
dashboard button / ``restart_desktop=true`` on a switch.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Callable

from . import config

STATUS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$pkg = Get-AppxPackage -Name OpenAI.Codex
if (-not $pkg) { $pkg = Get-AppxPackage | Where-Object { $_.Name -like '*Codex*' } | Select-Object -First 1 }
$procs = @(Get-Process -Name ChatGPT | ForEach-Object { [int]$_.Id })
$payload = [pscustomobject]@{
    running = ($procs.Count -gt 0)
    pids    = $procs
    aumid   = $(if ($pkg) { $pkg.PackageFamilyName + '!App' } else { $null })
}
$payload | ConvertTo-Json -Compress -Depth 4
"""

RESTART_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$procs = @(Get-Process -Name ChatGPT)
$ids = @($procs | ForEach-Object { [int]$_.Id })
$pkg = Get-AppxPackage -Name OpenAI.Codex
if (-not $pkg) { $pkg = Get-AppxPackage | Where-Object { $_.Name -like '*Codex*' } | Select-Object -First 1 }
$aumid = $(if ($pkg) { $pkg.PackageFamilyName + '!App' } else { $null })
foreach ($p in $procs) { $null = $p.CloseMainWindow() }
Start-Sleep -Seconds 2
$all = @(Get-CimInstance Win32_Process)
$kill = New-Object System.Collections.Generic.HashSet[int]
function Add-Tree([int]$procId) {
    if ($kill.Add($procId)) {
        foreach ($child in $all | Where-Object { $_.ParentProcessId -eq $procId }) {
            Add-Tree ([int]$child.ProcessId)
        }
    }
}
foreach ($procId in $ids) { Add-Tree $procId }
foreach ($procId in $kill) { Stop-Process -Id $procId -Force }
for ($i = 0; $i -lt 20; $i++) {
    if (-not (Get-Process -Name ChatGPT)) { break }
    Start-Sleep -Milliseconds 250
}
if ($aumid) { Start-Process ('shell:AppsFolder\' + $aumid) }
$payload = [pscustomobject]@{
    previous_pids = $ids
    killed        = @($kill)
    aumid         = $aumid
}
$payload | ConvertTo-Json -Compress -Depth 4
"""

LAUNCH_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$pkg = Get-AppxPackage -Name OpenAI.Codex
if (-not $pkg) { $pkg = Get-AppxPackage | Where-Object { $_.Name -like '*Codex*' } | Select-Object -First 1 }
$aumid = $(if ($pkg) { $pkg.PackageFamilyName + '!App' } else { $null })
$launched = $false
if ($aumid) { Start-Process ('shell:AppsFolder\' + $aumid); $launched = $true }
$payload = [pscustomobject]@{
    aumid    = $aumid
    launched = $launched
}
$payload | ConvertTo-Json -Compress -Depth 4
"""

#: Same kill tree as ``RESTART_SCRIPT`` without the relaunch. A resume job that
#: has to take a thread away from the desktop closes the app, drives the turn,
#: and relaunches it when the job ends.
CLOSE_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$procs = @(Get-Process -Name ChatGPT)
$ids = @($procs | ForEach-Object { [int]$_.Id })
foreach ($p in $procs) { $null = $p.CloseMainWindow() }
Start-Sleep -Seconds 2
$all = @(Get-CimInstance Win32_Process)
$kill = New-Object System.Collections.Generic.HashSet[int]
function Add-Tree([int]$procId) {
    if ($kill.Add($procId)) {
        foreach ($child in $all | Where-Object { $_.ParentProcessId -eq $procId }) {
            Add-Tree ([int]$child.ProcessId)
        }
    }
}
foreach ($procId in $ids) { Add-Tree $procId }
foreach ($procId in $kill) { Stop-Process -Id $procId -Force }
for ($i = 0; $i -lt 20; $i++) {
    if (-not (Get-Process -Name ChatGPT)) { break }
    Start-Sleep -Milliseconds 250
}
$remaining = @(Get-Process -Name ChatGPT | ForEach-Object { [int]$_.Id })
$payload = [pscustomobject]@{
    previous_pids = $ids
    killed        = @($kill)
    remaining     = $remaining
}
$payload | ConvertTo-Json -Compress -Depth 4
"""

#: Who can hold a thread writer ("会话被占用"): the desktop app, our own
#: ``codex app-server`` (spawned by a resume job), or a ``codex`` CLI in a
#: terminal. The dashboard needs this to name the occupant instead of guessing.
PROCESS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$rows = New-Object System.Collections.ArrayList
foreach ($p in @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'ChatGPT.exe' -or $_.Name -eq 'codex.exe' })) {
    $cmd = $(if ($p.CommandLine) { [string]$p.CommandLine } else { '' })
    $kind = 'cli'
    if ($p.Name -eq 'ChatGPT.exe') {
        $kind = 'desktop'
    } elseif ($cmd -match 'app-server') {
        $kind = 'app-server'
    }
    $null = $rows.Add([pscustomobject]@{
        pid     = [int]$p.ProcessId
        ppid    = [int]$p.ParentProcessId
        name    = $p.Name
        kind    = $kind
        started = $(if ($p.CreationDate) { $p.CreationDate.ToString('o') } else { $null })
        command = $cmd
    })
}
[pscustomobject]@{ processes = @($rows) } | ConvertTo-Json -Compress -Depth 4
"""


def _powershell(script: str, timeout: float = 30.0) -> str:
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            (completed.stderr or completed.stdout or "powershell failed").strip()[:300]
        )
    return (completed.stdout or "").strip()


def _parse_json(raw: str) -> dict:
    if not raw:
        return {}
    # PowerShell may emit a leading BOM or stray blank lines when redirected.
    cleaned = raw.lstrip("\ufeff").strip()
    if not cleaned:
        return {}
    payload = json.loads(cleaned)
    return payload if isinstance(payload, dict) else {}


class DesktopClient:
    """Small wrapper around the ChatGPT process tree.

    ``runner`` and ``sleep`` are injectable so tests never launch or kill a
    real application.
    """

    def __init__(
        self,
        *,
        enabled: Callable[[], bool] | None = None,
        runner: Callable[[str], str] | None = None,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        timeout_seconds: int | None = None,
        poll_seconds: float = 0.5,
    ):
        self._enabled_fn = enabled or config.desktop_restart_enabled
        self._runner = runner or _powershell
        self._sleep = sleep or time.sleep
        self._clock = clock or time.time
        self.timeout_seconds = int(
            timeout_seconds
            if timeout_seconds is not None
            else config.desktop_restart_timeout_seconds()
        )
        self.poll_seconds = float(poll_seconds)

    def enabled(self) -> bool:
        try:
            return bool(self._enabled_fn())
        except Exception:
            return False

    def status(self) -> dict:
        try:
            payload = _parse_json(self._runner(STATUS_SCRIPT))
        except Exception as exc:  # noqa: BLE001 - status must never raise
            return {
                "ok": False,
                "available": False,
                "running": False,
                "pids": [],
                "aumid": None,
                "error": str(exc)[:300],
                "enabled": self.enabled(),
            }
        pids = [int(pid) for pid in (payload.get("pids") or []) if str(pid).isdigit()]
        return {
            "ok": True,
            "available": True,
            "enabled": self.enabled(),
            "running": bool(payload.get("running")) or bool(pids),
            "pids": pids,
            "aumid": payload.get("aumid") or None,
        }

    def processes(self) -> dict:
        """List processes that can hold a thread writer lock.

        The desktop app, an ``app-server`` spawned by one of our resume jobs,
        and a ``codex`` CLI in a terminal all compete for the same threads, so
        the dashboard can name the occupant instead of saying "被占用".
        """
        service_pid = os.getpid()
        try:
            payload = _parse_json(self._runner(PROCESS_SCRIPT))
        except Exception as exc:  # noqa: BLE001 - diagnostics must not raise
            return {
                "ok": False,
                "error": str(exc)[:300],
                "processes": [],
                "counts": {"desktop": 0, "app-server": 0, "cli": 0},
                "service_pid": service_pid,
            }
        rows = payload.get("processes")
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            rows = []
        counts = {"desktop": 0, "app-server": 0, "cli": 0}
        items: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                pid = int(row.get("pid"))
            except (TypeError, ValueError):
                continue
            kind = str(row.get("kind") or "cli")
            if kind not in counts:
                kind = "cli"
            counts[kind] += 1
            item = {
                "pid": pid,
                "ppid": row.get("ppid"),
                "name": row.get("name"),
                "kind": kind,
                "started": row.get("started"),
            }
            if kind == "app-server":
                item["owner"] = (
                    "codex-switcher"
                    if str(row.get("ppid")) == str(service_pid)
                    else None
                )
            items.append(item)
        return {
            "ok": True,
            "processes": items,
            "counts": counts,
            "service_pid": service_pid,
        }

    def restart(self, force: bool = False) -> dict:
        started = self._clock()
        if not force and not self.enabled():
            return {
                "ok": False,
                "skipped": True,
                "reason": "desktop_restart_disabled",
                "message": (
                    "桌面端自动重启未启用。设置 CODEX_SWITCHER_RESTART_DESKTOP=1 "
                    "或在本次切换中勾选「切换后重启桌面端」。"
                ),
                "duration_ms": 0,
            }

        before = self.status()
        if not before.get("running"):
            # "重新启动" must also work when the app is closed — launching it is
            # the whole point of the button after a switch (and after the app
            # got stuck on its startup-policy screen and the user quit it).
            return self.start(before=before)

        try:
            raw = self._runner(RESTART_SCRIPT)
            payload = _parse_json(raw)
        except Exception as exc:  # noqa: BLE001 - surface as data
            return {
                "ok": False,
                "skipped": False,
                "reason": "restart_failed",
                "error": str(exc)[:300],
                "previous_pids": before.get("pids") or [],
                "pids": [],
                "aumid": before.get("aumid"),
                "duration_ms": int((self._clock() - started) * 1000),
            }

        after = self._wait_until_running()
        return {
            "ok": bool(after.get("running")),
            "skipped": False,
            "reason": None if after.get("running") else "relaunch_not_detected",
            "previous_pids": before.get("pids") or [],
            "pids": after.get("pids") or [],
            "killed": payload.get("killed") or [],
            "aumid": payload.get("aumid") or before.get("aumid"),
            "message": (
                "桌面端已重启，切换后的账号会在新进程中生效。"
                if after.get("running")
                else "桌面端进程已关闭，但未检测到重新启动，请手动打开 ChatGPT。"
            ),
            "duration_ms": int((self._clock() - started) * 1000),
        }

    def close_app(self, force: bool = False) -> dict:
        """Close the ChatGPT process tree and leave it closed.

        ``restart`` is for "make the new account visible"; this is for
        "get the thread writer locks out of the way", used by the resume job
        when the desktop app has the target conversation open. The caller is
        responsible for launching the app again.

        ``force`` bypasses the ``restart_desktop`` opt-in: the resume takeover
        has its own switch (``goal_resume_takeover``) and decides for itself.
        """
        started = self._clock()
        if not force and not self.enabled():
            return {
                "ok": False,
                "skipped": True,
                "reason": "desktop_restart_disabled",
                "message": (
                    "桌面端自动重启未启用，无法关闭它来接管会话。"
                    "设置 CODEX_SWITCHER_RESTART_DESKTOP=1 后再试。"
                ),
                "previous_pids": [],
                "pids": [],
                "killed": [],
                "duration_ms": 0,
            }

        before = self.status()
        if not before.get("running"):
            return {
                "ok": True,
                "skipped": False,
                "already_closed": True,
                "reason": None,
                "previous_pids": [],
                "pids": [],
                "killed": [],
                "message": "桌面端原本没在运行。",
                "duration_ms": int((self._clock() - started) * 1000),
            }

        try:
            payload = _parse_json(self._runner(CLOSE_SCRIPT))
        except Exception as exc:  # noqa: BLE001 - surface as data
            return {
                "ok": False,
                "skipped": False,
                "reason": "close_failed",
                "error": str(exc)[:300],
                "previous_pids": before.get("pids") or [],
                "pids": before.get("pids") or [],
                "killed": [],
                "duration_ms": int((self._clock() - started) * 1000),
            }

        after = self._wait_until_stopped()
        running = bool(after.get("running"))
        remaining = [int(pid) for pid in (payload.get("remaining") or []) if str(pid).isdigit()]
        if not running:
            remaining = []
        return {
            "ok": not running,
            "skipped": False,
            "reason": None if not running else "still_running",
            "previous_pids": before.get("pids") or [],
            "pids": after.get("pids") or remaining,
            "killed": payload.get("killed") or [],
            "message": (
                "桌面端已关闭，续跑结束后会自动打开。"
                if not running
                else "桌面端仍在运行，请手动关闭 Codex 后再续跑。"
            ),
            "duration_ms": int((self._clock() - started) * 1000),
        }

    def start(self, before: dict | None = None) -> dict:
        """Launch the desktop app when it is closed, then wait for it.

        Used by ``restart`` when nothing is running: after a switch the app is
        often already closed (or the user quit it because of a startup error),
        and "重启" should then simply start it instead of doing nothing.
        """
        started = self._clock()
        before = before if before is not None else self.status()
        try:
            payload = _parse_json(self._runner(LAUNCH_SCRIPT))
        except Exception as exc:  # noqa: BLE001 - surface as data
            return {
                "ok": False,
                "skipped": False,
                "launched": False,
                "reason": "launch_failed",
                "error": str(exc)[:300],
                "previous_pids": before.get("pids") or [],
                "pids": [],
                "aumid": before.get("aumid"),
                "duration_ms": int((self._clock() - started) * 1000),
            }

        after = self._wait_until_running()
        running = bool(after.get("running"))
        return {
            "ok": running,
            "skipped": False,
            "launched": bool(payload.get("launched")),
            "reason": None if running else "launch_not_detected",
            "previous_pids": before.get("pids") or [],
            "pids": after.get("pids") or [],
            "killed": [],
            "aumid": payload.get("aumid") or after.get("aumid"),
            "message": (
                "桌面端原本没在运行，已直接启动。"
                if running
                else "已请求启动桌面端，但没有检测到进程，请手动打开 Codex。"
            ),
            "duration_ms": int((self._clock() - started) * 1000),
        }

    def _wait_until_running(self) -> dict:
        deadline = self._clock() + max(1, self.timeout_seconds)
        last = {"running": False, "pids": [], "aumid": None}
        while True:
            last = self.status()
            if last.get("running"):
                return last
            if self._clock() >= deadline:
                return last
            self._sleep(self.poll_seconds)

    def _wait_until_stopped(self) -> dict:
        deadline = self._clock() + max(1, self.timeout_seconds)
        last = {"running": True, "pids": [], "aumid": None}
        while True:
            last = self.status()
            if not last.get("running"):
                return last
            if self._clock() >= deadline:
                return last
            self._sleep(self.poll_seconds)

