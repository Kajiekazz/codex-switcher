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
import copy
import ctypes
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Callable

from . import config

#: App-server thread ids look like UUIDv7; only this shape may enter a deep link.
_THREAD_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# A terminated MSIX process may remain in Get-Process with zero threads while
# Windows retains its process object. The process handle becomes signaled even
# when GetExitCodeProcess still returns STILL_ACTIVE (259). Only that explicit
# termination evidence may remove an old PID from the restart barrier.
PROCESS_EXIT_SCRIPT = r"""
if (-not ('CodexSwitcher.ProcessExitProbe' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace CodexSwitcher {
    public static class ProcessExitProbe {
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr OpenProcess(uint access, bool inherit, int processId);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
        [DllImport("kernel32.dll")]
        private static extern bool CloseHandle(IntPtr handle);
        public static bool HasExited(int processId) {
            IntPtr handle = OpenProcess(0x00100000, false, processId);
            if (handle == IntPtr.Zero) return false;
            try { return WaitForSingleObject(handle, 0) == 0; }
            finally { CloseHandle(handle); }
        }
        public static long[] Inspect(int processId) {
            IntPtr handle = OpenProcess(0x00100000, false, processId);
            if (handle == IntPtr.Zero) return new long[] { -1, Marshal.GetLastWin32Error() };
            try {
                uint status = WaitForSingleObject(handle, 0);
                return new long[] { status, status == 0xffffffff ? Marshal.GetLastWin32Error() : 0 };
            }
            finally { CloseHandle(handle); }
        }
    }
}
'@
}
function Test-CsProcessExited([int]$procId) {
    try { return [CodexSwitcher.ProcessExitProbe]::HasExited($procId) }
    catch { return $false }
}
"""

STATUS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$pkg = Get-AppxPackage -Name OpenAI.Codex
if (-not $pkg) { $pkg = Get-AppxPackage | Where-Object { $_.Name -like '*Codex*' } | Select-Object -First 1 }
# Windows may trim a live app's working set while it is in the background.
# A startup stub has one thread, no window and very little private memory.
__PROCESS_EXIT_PROBE__
$listedProcs = @(Get-Process -Name ChatGPT)
$exitedPids = @($listedProcs | Where-Object { Test-CsProcessExited ([int]$_.Id) } | ForEach-Object { [int]$_.Id })
$allProcs = @($listedProcs | Where-Object { $exitedPids -notcontains [int]$_.Id })
$procs = @($allProcs | Where-Object {
    $_.WorkingSet64 -ge 20MB -or $_.PrivateMemorySize64 -ge 20MB -or
    $_.Threads.Count -gt 1 -or $_.MainWindowHandle -ne 0
} | ForEach-Object { [int]$_.Id })
$payload = [pscustomobject]@{
    running = ($procs.Count -gt 0)
    pids    = $procs
    all_pids = @($allProcs | ForEach-Object { [int]$_.Id })
    exited_pids = $exitedPids
    process_details = @($allProcs | ForEach-Object {
        $probe = @(-1, -1)
        try { $probe = [CodexSwitcher.ProcessExitProbe]::Inspect([int]$_.Id) } catch {}
        [pscustomobject]@{
            pid = [int]$_.Id; threads = $_.Threads.Count; handles = $_.HandleCount
            working_set = $_.WorkingSet64; private_memory = $_.PrivateMemorySize64
            wait_status = $probe[0]; probe_error = $probe[1]
        }
    })
    aumid   = $(if ($pkg) { $pkg.PackageFamilyName + '!App' } else { $null })
}
$payload | ConvertTo-Json -Compress -Depth 4
""".replace('__PROCESS_EXIT_PROBE__', PROCESS_EXIT_SCRIPT)

SERVICE_TREE_GUARD = r"""
# Protect the service and every ancestor, including its launch terminal.
# Capture the tree before closing windows so exited PIDs cannot change roots.
$all = @(Get-CimInstance Win32_Process)
$childrenByParent = @{}
foreach ($process in $all) {
    $parentId = [int]$process.ParentProcessId
    if (-not $childrenByParent.ContainsKey($parentId)) {
        $childrenByParent[$parentId] = New-Object System.Collections.Generic.List[int]
    }
    $childrenByParent[$parentId].Add([int]$process.ProcessId)
}
$protected = New-Object System.Collections.Generic.HashSet[int]
$guardId = [int]$env:CODEX_SWITCHER_SERVICE_PID
while ($guardId -gt 0 -and $protected.Add($guardId)) {
    $parent = $all | Where-Object { $_.ProcessId -eq $guardId } | Select-Object -First 1
    if (-not $parent) { break }
    $guardId = [int]$parent.ParentProcessId
}
if (@($ids | Where-Object { $protected.Contains($_) }).Count -gt 0) {
    [pscustomobject]@{
        skipped = $true
        reason = 'service_in_desktop_tree'
        previous_pids = $ids
        killed = @()
    } | ConvertTo-Json -Compress -Depth 4
    return
}
"""

RESTART_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
__PROCESS_EXIT_PROBE__
$listedProcs = @(Get-Process -Name ChatGPT)
$exited = New-Object System.Collections.Generic.HashSet[int]
foreach ($p in $listedProcs) {
    if (Test-CsProcessExited ([int]$p.Id)) { $null = $exited.Add([int]$p.Id) }
}
$procs = @($listedProcs | Where-Object { -not $exited.Contains([int]$_.Id) })
$ids = @($procs | ForEach-Object { [int]$_.Id })
__SERVICE_TREE_GUARD__
$pkg = Get-AppxPackage -Name OpenAI.Codex
if (-not $pkg) { $pkg = Get-AppxPackage | Where-Object { $_.Name -like '*Codex*' } | Select-Object -First 1 }
$aumid = $(if ($pkg) { $pkg.PackageFamilyName + '!App' } else { $null })
foreach ($p in $procs) { $null = $p.CloseMainWindow() }
Start-Sleep -Seconds 2
$kill = New-Object System.Collections.Generic.HashSet[int]
function Add-Tree([int]$procId) {
    if ($protected.Contains($procId)) { return }
    if ($kill.Add($procId)) {
        foreach ($childId in $childrenByParent[$procId]) {
            Add-Tree $childId
        }
    }
}
foreach ($procId in $ids) { Add-Tree $procId }
foreach ($procId in $kill) { Stop-Process -Id $procId -Force }
for ($i = 0; $i -lt 20; $i++) {
    $remaining = @(Get-Process -Name ChatGPT | Where-Object {
        $ids -contains [int]$_.Id -and -not (Test-CsProcessExited ([int]$_.Id))
    } | ForEach-Object { [int]$_.Id })
    if ($remaining.Count -eq 0) { break }
    Start-Sleep -Milliseconds 250
}
$remaining = @(Get-Process -Name ChatGPT | Where-Object {
    $ids -contains [int]$_.Id -and -not (Test-CsProcessExited ([int]$_.Id))
} | ForEach-Object { [int]$_.Id })
foreach ($p in @(Get-Process -Name ChatGPT)) {
    if (Test-CsProcessExited ([int]$p.Id)) { $null = $exited.Add([int]$p.Id) }
}
$launched = $false
if ($remaining.Count -eq 0 -and $aumid) {
    Start-Process ('shell:AppsFolder\' + $aumid)
    $launched = $true
}
$payload = [pscustomobject]@{
    previous_pids = $ids
    killed        = @($kill)
    remaining     = $remaining
    exited_pids   = @($exited)
    launched      = $launched
    aumid         = $aumid
}
$payload | ConvertTo-Json -Compress -Depth 4
""".replace('__SERVICE_TREE_GUARD__', SERVICE_TREE_GUARD).replace('__PROCESS_EXIT_PROBE__', PROCESS_EXIT_SCRIPT)

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
__SERVICE_TREE_GUARD__
foreach ($p in $procs) { $null = $p.CloseMainWindow() }
Start-Sleep -Seconds 2
$kill = New-Object System.Collections.Generic.HashSet[int]
function Add-Tree([int]$procId) {
    if ($protected.Contains($procId)) { return }
    if ($kill.Add($procId)) {
        foreach ($childId in $childrenByParent[$procId]) {
            Add-Tree $childId
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
""".replace('__SERVICE_TREE_GUARD__', SERVICE_TREE_GUARD)

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
foreach ($p in @(Get-CimInstance Win32_Process -Filter "Name='ChatGPT.exe' OR Name='codex.exe'")) {
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

# Display both panels from one helper and one process inventory. The Python
# caller performs the same process-handle exit checks without compiling C# in
# every helper. Actions still use their independent, original live probes.
_PACKAGE_QUERY = """$pkg = Get-AppxPackage -Name OpenAI.Codex
if (-not $pkg) { $pkg = Get-AppxPackage | Where-Object { $_.Name -like '*Codex*' } | Select-Object -First 1 }"""
_DISPLAY_PACKAGE_QUERY = _PACKAGE_QUERY + r"""
# WindowsApps may deny directory enumeration even though a registered
# package's known files are readable. Resolve the runtime through Appx.
$csRuntime = $null
foreach ($runtime in @(Get-AppxPackage -Name 'OpenAI.CodexPrimaryRuntime*' | Where-Object {
    $_.Name -match '^OpenAI\.CodexPrimaryRuntime(?:\.|$)' -and
    $_.PublisherId -eq '3k8sg7r9htsxt' -and [string]$_.Status -eq 'Ok'
} | Sort-Object Version -Descending)) {
    if (-not $runtime.InstallLocation) { continue }
    $shellPath = Join-Path $runtime.InstallLocation 'dependencies\native\powershell\pwsh.exe'
    if (Test-Path -LiteralPath $shellPath -PathType Leaf) {
        $csRuntime = [pscustomobject]@{
            name = $runtime.Name
            publisher_id = $runtime.PublisherId
            status = [string]$runtime.Status
            install_location = $runtime.InstallLocation
        }
        break
    }
}
"""
DISPLAY_SCRIPT = (
    "# Codex Switcher display snapshot\n$ErrorActionPreference = 'Stop'\n"
    "$csProcessRows = @(Get-CimInstance Win32_Process -Filter \"Name='ChatGPT.exe' OR Name='codex.exe'\")\n"
    + STATUS_SCRIPT.replace(_PACKAGE_QUERY, _DISPLAY_PACKAGE_QUERY).replace(PROCESS_EXIT_SCRIPT, "").replace(
        "$exitedPids = @($listedProcs | Where-Object { Test-CsProcessExited ([int]$_.Id) } | ForEach-Object { [int]$_.Id })",
        "$exitedPids = @()",
    ).replace(
        "        try { $probe = [CodexSwitcher.ProcessExitProbe]::Inspect([int]$_.Id) } catch {}", "",
    ).replace(
        "$listedProcs = @(Get-Process -Name ChatGPT)",
        "$listedProcs = @($csProcessRows | Where-Object { $_.Name -eq 'ChatGPT.exe' } | "
        "ForEach-Object { Get-Process -Id ([int]$_.ProcessId) })",
    ).replace("$payload | ConvertTo-Json -Compress -Depth 4", "$statusPayload = $payload")
    + PROCESS_SCRIPT.replace(
        "foreach ($p in @(Get-CimInstance Win32_Process -Filter \"Name='ChatGPT.exe' OR Name='codex.exe'\"))",
        "foreach ($p in $csProcessRows)",
    ).replace(
        "[pscustomobject]@{ processes = @($rows) } | ConvertTo-Json -Compress -Depth 4",
        "[pscustomobject]@{ status = $statusPayload; processes = [pscustomobject]@{ processes = @($rows) }; runtime = $csRuntime } "
        "| ConvertTo-Json -Compress -Depth 6",
    )
)


def _native_display_processes():
    from .windows_processes import snapshot_processes
    return snapshot_processes()


def _native_display_aumid(families=()):
    from .windows_packages import CODEX_PACKAGE_FAMILIES, codex_aumid
    return codex_aumid(families=tuple(dict.fromkeys((*families, *CODEX_PACKAGE_FAMILIES))))


def _display_exit_observations(pids):
    """Read process-handle state; an access failure never means 'exited'."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    observations = {}
    for pid in pids:
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            observations[pid] = {"wait_status": -1, "probe_error": ctypes.get_last_error()}
            continue
        try:
            status = kernel.WaitForSingleObject(handle, 0)
            observations[pid] = {
                "wait_status": status,
                "probe_error": ctypes.get_last_error() if status == 0xffffffff else 0,
            }
        finally:
            kernel.CloseHandle(handle)
    return observations


def _find_display_powershell():
    """Use an existing modern shell; never install a display dependency."""
    found = shutil.which("pwsh.exe")
    if found:
        return found, "path"
    return None


def _registered_display_powershell(runtime):
    """Accept only the fixed shell path inside a healthy official Appx package."""
    if not isinstance(runtime, dict):
        return None
    if (not re.fullmatch(r"OpenAI\.CodexPrimaryRuntime(?:\.[A-Za-z0-9_.-]+)?", str(runtime.get("name") or ""))
            or runtime.get("publisher_id") != "3k8sg7r9htsxt" or runtime.get("status") != "Ok"):
        return None
    root = Path(str(runtime.get("install_location") or ""))
    if not root.is_absolute():
        return None
    shell = root / "dependencies/native/powershell/pwsh.exe"
    try:
        if shell.is_file():
            return str(shell), "codex_runtime"
    except OSError:
        pass
    return None


def _powershell(script: str, timeout: float = 30.0, *, executable="powershell.exe") -> str:
    completed = subprocess.run(
        [
            executable,
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
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env={**os.environ, "CODEX_SWITCHER_SERVICE_PID": str(os.getpid())},
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


def _display_action(method):
    """Invalidate display observations around actions from every caller."""
    @wraps(method)
    def action(self, *args, **kwargs):
        self._notify_display_change()
        try:
            return method(self, *args, **kwargs)
        finally:
            self._notify_display_change()
    return action


class DesktopDisplayCache:
    """Bounded background snapshots for display, never for desktop actions.

    The real client provides both panels in one probe/worker. Alternate clients
    without that API keep independent bounded probes. A cold read has no value
    rather than pretending the desktop is closed; failures retain the last
    successful snapshot and retry after a short backoff.
    """

    def __init__(self, client, *, ttl=15.0, retry_seconds=3.0,
                 clock=time.monotonic, wall_clock=time.time):
        self._client = client
        self._ttl = float(ttl)
        self._retry_seconds = float(retry_seconds)
        self._clock, self._wall_clock = clock, wall_clock
        self._lock = threading.Lock()
        self._closed = False
        self._combined = callable(getattr(type(client), "display_snapshot", None))
        self._entries = {
            key: {"value": None, "sampled": None, "fetched_at": None,
                  "retry_at": 0.0, "pending": False, "error": None,
                  "generation": 0, "invalidated": False}
            for key in (("snapshot",) if self._combined else ("status", "processes"))
        }
        register = getattr(type(client), "add_display_listener", None)
        self._unsubscribe = client.add_display_listener(self.invalidate) if callable(register) else None

    def get(self, key, *, force=False):
        if key not in ("status", "processes"):
            raise ValueError("unknown desktop display probe")
        entry_key = "snapshot" if self._combined else key
        with self._lock:
            row = self._entries[entry_key]
            now = self._clock()
            if force and not self._closed:
                row["generation"] += 1
                row["invalidated"] = True
            expired = row["invalidated"] or row["sampled"] is None or now - row["sampled"] >= self._ttl
            if (not self._closed and not row["pending"] and (force or expired)
                    and (force or now >= row["retry_at"])):
                row["pending"] = True
                try:
                    # Starting under the lock makes close() a strict boundary:
                    # it cannot race with a request about to spawn a worker.
                    threading.Thread(target=self._refresh, args=(entry_key, row["generation"]),
                                     name="desktop-display-" + entry_key, daemon=True).start()
                except RuntimeError:
                    row["pending"] = False
                    row["retry_at"] = now + self._retry_seconds
                    row["error"] = {"error": "probe_unavailable", "message": "桌面状态刷新暂未启动"}
            return {
                "value": copy.deepcopy(row["value"].get(key) if self._combined and row["value"] is not None else row["value"]),
                "pending": row["pending"],
                "fetched_at": row["fetched_at"],
                "cache_age_seconds": max(0.0, now - row["sampled"]) if row["sampled"] is not None else None,
                "refresh_error": copy.deepcopy(row["error"]),
            }

    def _refresh(self, key, generation):
        while True:
            with self._lock:
                if self._closed:
                    self._entries[key]["pending"] = False
                    return
            try:
                value = getattr(self._client, "display_snapshot" if self._combined else key)()
                if not isinstance(value, dict) or not value.get("ok"):
                    error = (value.get("error") or value.get("message")) if isinstance(value, dict) else None
                    failure = {"error": "probe_failed", "message": str(error or "无法读取桌面状态")[:300]}
                else:
                    failure = None
            except Exception as exc:  # display must remain available
                value = None
                failure = {"error": "probe_failed", "message": str(exc)[:300] or "无法读取桌面状态"}
            with self._lock:
                row = self._entries[key]
                if self._closed:
                    row["pending"] = False
                    return
                if generation != row["generation"]:
                    # A mutation/explicit refresh happened during the probe.
                    # Discard its result and repeat in this same worker. Many
                    # refresh requests coalesce into one follow-up, not a queue.
                    generation = row["generation"]
                    continue
                row["pending"] = False
                now = self._clock()
                if failure is None:
                    row.update(value=copy.deepcopy(value), sampled=now,
                               fetched_at=datetime.fromtimestamp(self._wall_clock(), timezone.utc).isoformat(timespec="seconds"),
                               retry_at=0.0, error=None, invalidated=False)
                else:
                    row.update(error=failure, retry_at=now + self._retry_seconds)
                return

    def invalidate(self):
        with self._lock:
            if self._closed:
                return
            for row in self._entries.values():
                row["generation"] += 1
                row["invalidated"] = True
                row["retry_at"] = 0.0

    def close(self):
        with self._lock:
            self._closed = True
            # Existing read-only probes can finish, but cannot publish results
            # or schedule follow-up work after the HTTP server has closed.
            for row in self._entries.values():
                row["pending"] = False
            unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()


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
        native_backend=None,
    ):
        self._enabled_fn = enabled or config.desktop_restart_enabled
        self._runner = runner or _powershell  # Explicit legacy fixtures only.
        self._native_backend = native_backend
        self._display_custom_runner = runner is not None
        self._sleep = sleep or time.sleep
        self._clock = clock or time.time
        self.timeout_seconds = int(
            timeout_seconds
            if timeout_seconds is not None
            else config.desktop_restart_timeout_seconds()
        )
        self.poll_seconds = float(poll_seconds)
        self._restart_failed = False
        self._restart_old_pids: set[int] = set()
        self._display_listeners_lock = threading.Lock()
        self._display_listeners: set[Callable[[], None]] = set()
        self._display_package_lock = threading.Lock()
        self._display_package = None
        self._display_package_generation = 0
        self._display_shell = None
        self._display_registered_shell = None
        self._display_shell_retry_at = 0.0
        self._display_shell_source = "system"
        self._display_native_retry_at = 0.0
        self._display_probe_source = None

    def add_display_listener(self, callback):
        """Observe actions without changing their live status checks."""
        with self._display_listeners_lock:
            self._display_listeners.add(callback)
        def unsubscribe():
            with self._display_listeners_lock:
                self._display_listeners.discard(callback)
        return unsubscribe

    def _notify_display_change(self):
        with self._display_package_lock:
            self._display_package = None
            self._display_package_generation += 1
        with self._display_listeners_lock:
            callbacks = tuple(self._display_listeners)
        for callback in callbacks:
            try:
                callback()
            except Exception:
                # Display failures must never interfere with closing/starting
                # the app or with the caller's real-time result.
                pass

    def enabled(self) -> bool:
        try:
            return bool(self._enabled_fn())
        except Exception:
            return False

    def status(self) -> dict:
        try:
            payload = self._action_payload("status")
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
        return self._status_result(payload)

    def _status_result(self, payload):
        pids = [int(pid) for pid in (payload.get("pids") or []) if str(pid).isdigit()]
        return {
            "ok": True,
            "available": True,
            "enabled": self.enabled(),
            "running": bool(payload.get("running")) or bool(pids),
            "pids": pids,
            "all_pids": [int(pid) for pid in payload.get("all_pids", pids) if str(pid).isdigit()],
            "exited_pids": [int(pid) for pid in payload.get("exited_pids", []) if str(pid).isdigit()],
            "process_details": payload.get("process_details") or [],
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
            payload = self._action_payload("processes")
        except Exception as exc:  # noqa: BLE001 - diagnostics must not raise
            return {
                "ok": False,
                "error": str(exc)[:300],
                "processes": [],
                "counts": {"desktop": 0, "app-server": 0, "cli": 0},
                "service_pid": service_pid,
            }
        return self._processes_result(payload)

    def _processes_result(self, payload):
        service_pid = os.getpid()
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
            if kind == "unknown":
                counts.setdefault("unknown", 0)
            elif kind not in counts:
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

    def _run_system_display_script(self, script):
        try:
            return self._runner(script)
        except FileNotFoundError:
            # Explorer/service environments can omit both pwsh and the
            # WindowsPowerShell directory from PATH. Actions keep their runner.
            executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            return _powershell(script, executable=str(executable))

    def _run_display_script(self, script):
        # Appx commands remain in Windows PowerShell for compatibility. Only
        # cached, read-only process display probes may use an existing pwsh.
        if self._display_custom_runner:
            self._display_shell_source = "custom"
            return self._runner(script)
        if "Get-AppxPackage" in script:
            self._display_shell_source = "system"
            return self._run_system_display_script(script)
        now = self._clock()
        if now >= self._display_shell_retry_at:
            try:
                if self._display_shell is None:
                    self._display_shell = self._display_registered_shell or _find_display_powershell()
                if self._display_shell is not None:
                    executable, source = self._display_shell
                    result = _powershell(script, executable=executable)
                    payload = _parse_json(result)
                    if not all(isinstance(payload.get(key), dict) for key in ("status", "processes")):
                        raise ValueError("incomplete display response")
                    self._display_shell_source = source
                    return result
            except Exception:
                # A removed package, unsupported shell, or failed CIM call
                # must not break display or cause two helpers on every poll.
                self._display_shell = None
            self._display_shell_retry_at = now + 300.0
        self._display_shell_source = "system"
        return self._run_system_display_script(script)

    def _native(self):
        with self._display_package_lock:
            if self._native_backend is None:
                from .windows_desktop import NativeDesktop
                self._native_backend = NativeDesktop()
            return self._native_backend

    def _action_payload(self, action):
        # Script runners exist solely for explicit legacy behavior fixtures.
        # Production never chooses a shell, including on native probe failure.
        if self._display_custom_runner:
            script = {"status": STATUS_SCRIPT, "processes": PROCESS_SCRIPT,
                      "restart": RESTART_SCRIPT, "close": CLOSE_SCRIPT, "launch": LAUNCH_SCRIPT}[action]
            return _parse_json(self._runner(script))
        if action in {"status", "processes"}:
            return self._native_display_snapshot(fresh_package=True)[action]
        if action in {"restart", "close"}:
            return self._native().close_tree()
        if action == "launch":
            return self._native().launch(_native_display_aumid())
        raise ValueError("Unknown desktop action")

    def dispose(self):
        if self._native_backend is not None:
            self._native_backend.dispose()

    def _native_display_snapshot(self, *, fresh_package=False):
        rows = _native_display_processes()
        desktop_rows = [row for row in rows if row["name"].lower() == "chatgpt.exe"]
        # Permission errors and PID identity races must not turn into a false
        # "desktop closed" observation. The existing helper is the fallback.
        if any(not row.get("identity_confirmed") and not row.get("exited") for row in desktop_rows):
            raise OSError("Cannot confirm desktop process identity")
        with self._display_package_lock:
            package = self._display_package
            generation = self._display_package_generation
        ttl = 300.0 if package and package["aumid"] else 15.0
        refresh_package = fresh_package or not package or not 0 <= self._clock() - package["sampled"] < ttl
        families = tuple(dict.fromkeys(row["package_family"] for row in desktop_rows if (
            row.get("identity_confirmed") and row.get("package_family"))))
        aumid = _native_display_aumid(families) if refresh_package else package["aumid"]
        if aumid is not None and (not isinstance(aumid, str) or "!" not in aumid):
            raise ValueError("Invalid native application identity")

        exited = [row["pid"] for row in desktop_rows if row.get("exited")]
        live = [row for row in desktop_rows if not row.get("exited")]
        ready = []
        for row in live:
            running = (row.get("working_set") or 0) >= 20 * 1024 * 1024 or (
                row.get("private_memory") or 0) >= 20 * 1024 * 1024 or (
                row.get("threads") or 0) > 1 or row.get("has_window") is True
            if running:
                ready.append(row["pid"])
            elif any(row.get(key) is None for key in ("working_set", "private_memory", "threads", "has_window")):
                raise OSError("Cannot distinguish desktop activation stub from running process")
        processes = []
        for row in rows:
            if row.get("exited"):
                continue
            if not row.get("identity_confirmed"):
                kind = "unknown"
            elif row["name"].lower() == "chatgpt.exe":
                kind = "desktop"
            elif not row.get("command_available"):
                kind = "unknown"
            else:
                kind = "app-server" if re.search(r"\bapp-server\b", row.get("command") or "", re.I) else "cli"
            processes.append({**row, "kind": kind})
        state = self._status_result({
            "running": bool(ready), "pids": ready, "all_pids": [row["pid"] for row in live],
            "exited_pids": exited, "aumid": aumid,
            "process_details": [{key: row.get(key) for key in (
                "pid", "threads", "handles", "working_set", "private_memory", "wait_status", "probe_error"
            )} for row in live],
        })
        if self._native_backend is not None:
            barrier = self._native_backend.barrier()
            state["all_pids"] = sorted(set(state["all_pids"]) | set(barrier["remaining"]))
            state["exited_pids"] = sorted(set(state["exited_pids"]) | set(barrier["exited_pids"]))
            known = {item["pid"] for item in state["process_details"]}
            state["process_details"].extend(item for item in barrier["process_details"] if item["pid"] not in known)
        if refresh_package:
            with self._display_package_lock:
                if generation == self._display_package_generation:
                    self._display_package = {"aumid": aumid, "sampled": self._clock()}
        return {"ok": True, "status": state, "processes": self._processes_result({"processes": processes})}

    def display_snapshot(self) -> dict:
        """One native read supplies both panels; the cache retains last data."""
        if self._display_custom_runner:
            self._display_probe_source = "custom"
            return self._script_display_snapshot()
        self._display_probe_source = "native"
        try:
            return self._native_display_snapshot()
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:300]}

    def _script_display_snapshot(self) -> dict:
        """Compatibility path, also used by explicitly injected runners."""
        with self._display_package_lock:
            package = self._display_package
            generation = self._display_package_generation
        ttl = 300.0 if package and package["aumid"] else 15.0
        refresh_package = not package or not 0 <= self._clock() - package["sampled"] < ttl
        script = DISPLAY_SCRIPT
        if not refresh_package:
            aumid = package["aumid"]
            replacement = ("$pkg = [pscustomobject]@{ PackageFamilyName = '" + aumid[:-4].replace("'", "''") + "' }"
                           if aumid else "$pkg = $null")
            script = script.replace(_DISPLAY_PACKAGE_QUERY, replacement + "\n$csRuntime = $null")
        try:
            payload = _parse_json(self._run_display_script(script))
            if not all(isinstance(payload.get(key), dict) for key in ("status", "processes")):
                raise ValueError("桌面状态快照不完整")
            state = self._status_result(payload["status"])
            observations = _display_exit_observations(state["all_pids"]) if os.name == "nt" else {}
            exited = {pid for pid, probe in observations.items() if probe["wait_status"] == 0}
            state["pids"] = [pid for pid in state["pids"] if pid not in exited]
            state["all_pids"] = [pid for pid in state["all_pids"] if pid not in exited]
            state["exited_pids"] = sorted(exited)
            state["running"] = bool(state["pids"])
            state["process_details"] = [
                {**row, **observations.get(row.get("pid"), {})}
                for row in state["process_details"] if row.get("pid") not in exited
            ]
            aumid = state["aumid"]
            if refresh_package and (aumid is None or aumid.endswith("!App")):
                registered_shell = _registered_display_powershell(payload.get("runtime"))
                with self._display_package_lock:
                    if generation == self._display_package_generation:
                        self._display_package = {"aumid": aumid, "sampled": self._clock()}
                        if registered_shell != self._display_registered_shell:
                            self._display_registered_shell = registered_shell
                            self._display_shell = None
                            self._display_shell_retry_at = 0.0
            return {
                "ok": True,
                "status": state,
                "processes": self._processes_result(payload["processes"]),
            }
        except Exception as exc:
            with self._display_package_lock:
                if generation == self._display_package_generation:
                    self._display_package = None
            return {"ok": False, "error": str(exc)[:300]}

    @_display_action
    def restart(self, force: bool = False) -> dict:
        started = self._clock()
        if not force and not self.enabled():
            return {
                "ok": False,
                "skipped": True,
                "reason": "desktop_restart_disabled",
                "message": (
                    "未开启切号后自动重启。可在设置中开启「切号后重启桌面端」，"
                    "或在本次切换中勾选「切换后重启桌面端」。"
                ),
                "duration_ms": 0,
            }

        before = self.status()
        # A negative status probe can be transient. Always enumerate and close
        # the process tree inside the restart operation itself; activating an
        # existing instance would leave its old account cached in memory.
        self._restart_failed = True
        self._restart_old_pids = set(before.get("all_pids", before.get("pids")) or [])
        try:
            payload = self._action_payload("restart")
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

        if payload.get("skipped"):
            return self._protected_service_skip(before, started)
        previous = sorted({int(pid) for pid in (
            list(before.get("all_pids", before.get("pids")) or [])
            + list(payload.get("previous_pids") or [])
        ) if str(pid).isdigit()})
        self._restart_old_pids = set(previous)
        # The helper's remaining list is only a snapshot. A process can finish
        # exiting while the next PowerShell probe starts; recheck it and issue
        # the missing launch instead of permanently trusting the stale list.
        result = self.complete_restart(previous, launched=bool(payload.get("launched")))
        result.update(
            killed=payload.get("killed") or [],
            aumid=result.get("aumid") or payload.get("aumid") or before.get("aumid"),
            exited_pids=sorted(set(payload.get("exited_pids") or []) | set(result.get("exited_pids") or [])),
            duration_ms=int((self._clock() - started) * 1000),
        )
        return result

    @_display_action
    def complete_restart(self, previous_pids, *, launched=False, wait=True) -> dict:
        """Finish an already requested restart without closing the app again."""
        started = self._clock()
        previous = {int(pid) for pid in previous_pids if str(pid).isdigit()}
        self._restart_old_pids = previous
        self._restart_failed = True
        deadline = started + max(1, self.timeout_seconds)
        exited = set()
        error = None
        while True:
            after = self.status()
            exited.update(after.get("exited_pids") or [])
            remaining = sorted(previous.intersection(after.get("all_pids", after.get("pids")) or []))
            if not after.get("ok") or not remaining or not wait or self._clock() >= deadline:
                break
            self._sleep(self.poll_seconds)
        if after.get("ok") and not remaining and not after.get("running"):
            if not launched:
                try:
                    launch = self._action_payload("launch")
                    launched = bool(launch.get("launched"))
                except Exception as exc:
                    error = str(exc)[:300]
            if launched:
                after = self._wait_until_running(exclude_pids=previous)
                exited.update(after.get("exited_pids") or [])
                remaining = sorted(previous.intersection(after.get("all_pids", after.get("pids")) or []))
        confirmed = bool(after.get("ok") and after.get("running") and not remaining)
        self._restart_failed = not confirmed
        reason = None if confirmed else (
            "restart_status_unknown" if not after.get("ok") else
            "restart_not_confirmed" if remaining else
            "launch_failed" if error else
            "launch_stuck" if after.get("all_pids") else "relaunch_not_detected"
        )
        return {
            "ok": confirmed, "skipped": False, "reason": reason,
            "restart_confirmed": confirmed, "launched": launched,
            "previous_pids": sorted(previous), "pids": after.get("pids") or [],
            "remaining": remaining, "exited_pids": sorted(exited),
            "remaining_processes": [p for p in after.get("process_details", []) if p.get("pid") in remaining],
            "aumid": after.get("aumid"), "error": error or after.get("error"),
            "message": (
                ("桌面端已重启，切换后的账号会在新进程中生效。" if previous else
                 "桌面端原本没在运行，已直接启动。") if confirmed else
                "正在等待桌面端旧进程退出，尚未启动新实例。" if remaining else
                "桌面端未完成重新启动，可手动打开 Codex，或等待自动恢复。"
            ),
            "duration_ms": int((self._clock() - started) * 1000),
        }

    def ready_for_resume(self) -> bool:
        """Do not retry against a process that failed to load the new account."""
        if not self._restart_failed:
            return True
        state = self.status()
        current = set(state.get("all_pids", state.get("pids")) or [])
        if (not self._restart_old_pids or not state.get("running") or
                self._restart_old_pids.intersection(current)):
            return False
        self._restart_failed = False
        return True

    @_display_action
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
                    "桌面端关闭操作未启用。请在设置中开启「切号后重启桌面端」后再试。"
                ),
                "previous_pids": [],
                "pids": [],
                "killed": [],
                "duration_ms": 0,
            }

        before = self.status()
        if not before.get("ok"):
            return {"ok": False, "skipped": False, "reason": "desktop_status_unknown",
                    "error": before.get("error"), "pids": [], "killed": [], "duration_ms": 0}
        if not before.get("all_pids", before.get("pids")):
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
            payload = self._action_payload("close")
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

        if payload.get("skipped"):
            return self._protected_service_skip(before, started)
        after = self._wait_until_stopped()
        running = bool(after.get("running"))
        remaining = after.get("all_pids", after.get("pids")) or []
        confirmed = bool(after.get("ok") and not remaining and not running)
        return {
            "ok": confirmed,
            "skipped": False,
            "reason": None if confirmed else "still_running" if after.get("ok") else "desktop_status_unknown",
            "error": after.get("error"),
            "previous_pids": before.get("pids") or [],
            "pids": after.get("pids") or remaining,
            "killed": payload.get("killed") or [],
            "message": (
                "桌面端已关闭。"
                if confirmed
                else "桌面端仍在运行，请手动关闭 Codex 后再续跑。"
            ),
            "duration_ms": int((self._clock() - started) * 1000),
        }

    @_display_action
    def start(self, before: dict | None = None) -> dict:
        """Launch the desktop app when it is closed, then wait for it.

        Used by ``restart`` when nothing is running: after a switch the app is
        often already closed (or the user quit it because of a startup error),
        and "重启" should then simply start it instead of doing nothing.
        """
        started = self._clock()
        before = before if before is not None else self.status()
        if not before.get("ok"):
            return {"ok": False, "skipped": False, "launched": False,
                    "reason": "desktop_status_unknown", "error": before.get("error"),
                    "previous_pids": [], "pids": [], "duration_ms": 0}
        try:
            payload = self._action_payload("launch")
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
        stale = [] if running else (after.get("all_pids", []) if not self._display_custom_runner else _stale_windows_pids())
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
        The caller observes removal through ``thread/queue/list``; removal does
        not prove execution because editing or deletion also removes a row.
        """
        tid = (thread_id or "").strip()
        if not _THREAD_ID_RE.match(tid):
            raise ValueError(f"invalid thread id: {thread_id!r}")
        started = self._clock()
        state = self._running_snapshot()
        if not state.get("ok", True):
            return {"ok": False, "skipped": True, "reason": "desktop_status_unknown",
                    "error": state.get("error"), "url": None,
                    "duration_ms": int((self._clock() - started) * 1000)}
        if not self._display_custom_runner and not self.ready_for_resume():
            return {"ok": False, "skipped": True, "reason": "desktop_not_ready", "url": None,
                    "pids": state.get("pids") or [], "duration_ms": int((self._clock() - started) * 1000)}
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
        activation = self._open_url(url)
        return {
            "ok": True,
            "skipped": False,
            "reason": None,
            "url": url,
            "pids": state.get("pids") or [],
            "duration_ms": int((self._clock() - started) * 1000),
            **(activation if isinstance(activation, dict) else {}),
        }

    def _protected_service_skip(self, before: dict, started: float) -> dict:
        return {
            "ok": False,
            "skipped": True,
            "reason": "service_in_desktop_tree",
            "previous_pids": before.get("pids") or [],
            "pids": before.get("pids") or [],
            "killed": [],
            "message": "已跳过桌面端关闭：它的进程树包含 Switcher 或启动程序。请从桌面图标独立打开 Switcher 后再重启桌面端。",
            "duration_ms": int((self._clock() - started) * 1000),
        }

    def _running_snapshot(self) -> dict:
        """Whether the app is running, and its pids — without the MSIX lookup.

        ``stale_pids`` lists app processes Windows created but never resumed
        (a framework-activation stall leaves them suspended at ~2 MB). They look
        like a running app to ``tasklist`` but can never drain a queue.
        """
        if os.name == "nt" and self._display_custom_runner:
            try:
                pids = _windows_pids()
            except Exception:  # noqa: BLE001 - fall back to the full status probe
                pids = None
            if pids is not None:
                if not pids:
                    # The cheap working-set-only probe can miss a trimmed app.
                    state = self.status()
                    if state.get("running"):
                        return {"running": True, "pids": list(state.get("pids") or []), "stale_pids": []}
                return {
                    "running": bool(pids),
                    "pids": pids,
                    "stale_pids": [] if pids else _stale_windows_pids(),
                }
        state = self.status()
        return {
            "ok": bool(state.get("ok")),
            "error": state.get("error"),
            "running": bool(state.get("running")),
            "pids": list(state.get("pids") or []),
            "stale_pids": [] if state.get("running") else list(state.get("all_pids") or []),
        }

    def process_snapshot(self) -> dict:
        """Fresh native process state for the queue watchdog.

        Used by the queue watchdog to notice a relaunch (a switch or an update
        restarts the app, which drops any wake that was already in flight).
        """
        return self._running_snapshot()

    def _open_url(self, url: str) -> None:
        """Hand a URI to the OS so the registered app receives it.

        The hand-off is fire-and-forget: ``ShellExecute`` can block for tens of
        seconds while the shell resolves a packaged app, and killing the helper
        at a timeout would abort an activation that was still in flight. The
        caller does not trust this call anyway — ``thread/queue/list`` only
        tells whether the row is still queued, not whether a turn started.
        """
        if os.name == "nt":
            return self._native().open_uri(url)
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen(
            [opener, url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _wait_until_running(self, exclude_pids=()) -> dict:
        deadline = self._clock() + max(1, self.timeout_seconds)
        last = {"running": False, "pids": [], "aumid": None}
        while True:
            last = self.status()
            if last.get("running") and not set(exclude_pids).intersection(
                last.get("all_pids", last.get("pids")) or []
            ):
                return last
            if self._clock() >= deadline:
                return last
            self._sleep(self.poll_seconds)

    def _wait_until_stopped(self) -> dict:
        deadline = self._clock() + max(1, self.timeout_seconds)
        last = {"running": True, "pids": [], "aumid": None}
        while True:
            last = self.status()
            if not last.get("ok") or not last.get("all_pids", last.get("pids")):
                return last
            if self._clock() >= deadline:
                return last
            self._sleep(self.poll_seconds)

