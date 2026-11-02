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

import csv
import io
import json
import os
import re
import subprocess
import sys
import time
from typing import Callable

from . import config

#: App-server thread ids look like UUIDv7; only this shape may enter a deep link.
_THREAD_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

STATUS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$pkg = Get-AppxPackage -Name OpenAI.Codex
if (-not $pkg) { $pkg = Get-AppxPackage | Where-Object { $_.Name -like '*Codex*' } | Select-Object -First 1 }
# Suspended MSIX activation stubs sit at ~2 MB; the real app is far larger.
$procs = @(Get-Process -Name ChatGPT | Where-Object { $_.WorkingSet64 -gt 20MB } | ForEach-Object { [int]$_.Id })
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

#: A real desktop app holds hundreds of megabytes; a process the MSIX runtime
#: created but never resumed sits at ~2 MB with a single suspended thread.
_LIVE_WORKING_SET_KB = 20 * 1024


def _mem_kb(field: str) -> int | None:
    """``tasklist`` prints the working set as ``\"2,180 K\"``."""
    cleaned = (field or "").strip().strip('"').replace(",", "").replace("\u00a0", " ")
    if cleaned.upper().endswith(" K"):
        cleaned = cleaned[:-2].strip()
    return int(cleaned) if cleaned.isdigit() else None


def _windows_processes(image_name: str = "ChatGPT.exe") -> list[dict]:
    completed = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/NH", "/FO", "CSV"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or "tasklist failed").strip()[:200])
    rows: list[dict] = []
    # ``tasklist`` quotes the working set (``"2,180 K"``), so a plain split on
    # commas would cut the number in half.
    for parts in csv.reader(io.StringIO(completed.stdout or "")):
        fields = [field.strip() for field in parts]
        if len(fields) >= 2 and fields[0].lower() == image_name.lower() and fields[1].isdigit():
            rows.append({"pid": int(fields[1]), "mem_kb": _mem_kb(fields[-1])})
    return rows


#: Cheap "is the app running?" probe. ``STATUS_SCRIPT`` also resolves the MSIX
#: package (multiple seconds on a cold PowerShell); a wake only needs the pid
#: list, which ``tasklist`` returns in well under a second.
#:
#: Only *live* processes count. When a packaged app cannot resolve its framework
#: dependency, Windows leaves a suspended one-thread stub behind (``tasklist``
#: still lists it at ~2 MB). Counting those as "running" made a wake — and the
#: restart button — report success while nothing could ever drain the queue.
def _windows_pids(image_name: str = "ChatGPT.exe") -> list[int]:
    pids: list[int] = []
    for row in _windows_processes(image_name):
        mem = row.get("mem_kb")
        if mem is None or mem >= _LIVE_WORKING_SET_KB:
            pids.append(int(row["pid"]))
    return pids


def _stale_windows_pids(image_name: str = "ChatGPT.exe") -> list[int]:
    """App processes that exist but never started (suspended activation stubs)."""
    try:
        rows = _windows_processes(image_name)
    except Exception:  # noqa: BLE001 - diagnostics only
        return []
    return [
        int(row["pid"])
        for row in rows
        if (row.get("mem_kb") or 0) < _LIVE_WORKING_SET_KB
    ]


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
        "close the app on request". Goal resume no longer uses it — a resume
        is queued and the app runs the turn itself — so only an explicit caller
        (or a future feature) should reach for it.

        ``force`` bypasses the ``restart_desktop`` opt-in.
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
        stale = [] if running else _stale_windows_pids()
        return {
            "ok": running,
            "skipped": False,
            "launched": bool(payload.get("launched")),
            "reason": None if running else ("launch_stuck" if stale else "launch_not_detected"),
            "previous_pids": before.get("pids") or [],
            "pids": after.get("pids") or [],
            "stale_pids": stale,
            "killed": [],
            "aumid": payload.get("aumid") or after.get("aumid"),
            "message": (
                "桌面端原本没在运行，已直接启动。"
                if running
                else (
                    "桌面端进程已创建，但一直停在被挂起的状态（通常是 MSIX 框架包"
                    "没就绪），请注销或重启系统后再试。"
                    if stale
                    else "已请求启动桌面端，但没有检测到进程，请手动打开 Codex。"
                )
            ),
            "duration_ms": int((self._clock() - started) * 1000),
        }

    #: The desktop app's own deep link for "this thread has queued work, load it".
    #: Handled by the main process: it lists ``thread/queue/list`` and only calls
    #: ``resumeThread`` when something is actually pending.
    WAKE_URL = "codex://threads/wake/{thread_id}"

    def wake_thread(self, thread_id: str) -> dict:
        """Ask the running desktop app to load ``thread_id`` and drain its queue.

        A queued resume message is only delivered once the app has the thread
        loaded, and the app ships a deep link for exactly that
        (``codex://threads/wake/<thread>``, MSIX ``windows.protocol`` →
        ``--single-argument <uri>`` → main-process wake handler). Waking only the
        *running* app is deliberate: opening the URI with no desktop process
        would launch it, and a resume must never start (or stop) the user's app.

        ``ok`` only means "the URI was handed to the registered app". Delivery is
        still asynchronous: the app drains the queue as capacity allows, so a
        wake sent while other threads are mid-turn can sit queued for a while.
        The caller confirms delivery through ``thread/queue/list``.
        """
        tid = (thread_id or "").strip()
        if not _THREAD_ID_RE.match(tid):
            raise ValueError(f"invalid thread id: {thread_id!r}")
        started = self._clock()
        state = self._running_snapshot()
        if not state["running"]:
            return {
                "ok": False,
                "skipped": True,
                "reason": (
                    "desktop_not_ready" if state.get("stale_pids") else "desktop_not_running"
                ),
                "url": None,
                "pids": state.get("pids") or [],
                "stale_pids": state.get("stale_pids") or [],
                "duration_ms": int((self._clock() - started) * 1000),
            }
        url = self.WAKE_URL.format(thread_id=tid)
        self._open_url(url)
        return {
            "ok": True,
            "skipped": False,
            "reason": None,
            "url": url,
            "pids": state.get("pids") or [],
            "duration_ms": int((self._clock() - started) * 1000),
        }

    def _running_snapshot(self) -> dict:
        """Whether the app is running, and its pids — without the MSIX lookup.

        ``stale_pids`` lists app processes Windows created but never resumed
        (a framework-activation stall leaves them suspended at ~2 MB). They look
        like a running app to ``tasklist`` but can never drain a queue.
        """
        if os.name == "nt":
            try:
                pids = _windows_pids()
            except Exception:  # noqa: BLE001 - fall back to the full status probe
                pids = None
            if pids is not None:
                return {
                    "running": bool(pids),
                    "pids": pids,
                    "stale_pids": [] if pids else _stale_windows_pids(),
                }
        state = self.status()
        return {
            "running": bool(state.get("running")),
            "pids": list(state.get("pids") or []),
            "stale_pids": [],
        }

    def process_snapshot(self) -> dict:
        """``{"running": bool, "pids": [...]}`` from the cheap ``tasklist`` probe.

        Used by the queue watchdog to notice a relaunch (a switch or an update
        restarts the app, which drops any wake that was already in flight).
        """
        return self._running_snapshot()

    def _open_url(self, url: str) -> None:
        """Hand a URI to the OS so the registered app receives it.

        The hand-off is fire-and-forget: ``ShellExecute`` can block for tens of
        seconds while the shell resolves a packaged app, and killing the helper
        at a timeout would abort an activation that was still in flight. The
        caller does not trust this call anyway — ``thread/queue/list`` tells
        whether the app actually picked the message up.
        """
        if os.name == "nt":
            # ``Start-Process <uri>`` goes through ShellExecute, which activates
            # the MSIX ``codex`` protocol handler; the handler forwards the URI
            # to the running instance as ``--single-argument``. The wake handler
            # itself is documented in work/codex-goal-resume-re/findings.md §5.3.
            subprocess.Popen(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    f"Start-Process '{url}'",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=(
                    getattr(subprocess, "DETACHED_PROCESS", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                ),
                close_fds=True,
            )
            return
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen(
            [opener, url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

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

