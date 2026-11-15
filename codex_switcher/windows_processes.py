"""Read-only Windows process observations without a shell or WMI provider.

Only the process snapshot and query/synchronize handles are opened. No process
is started, suspended or terminated. Unknown observations remain unknown;
particularly, an access failure is never evidence that a process exited.
"""
from __future__ import annotations

import ctypes
import ntpath
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache


DWORD = ctypes.c_uint32
LONG = ctypes.c_int32
BOOL = ctypes.c_int32
HANDLE = ctypes.c_void_p
SIZE_T = ctypes.c_size_t
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF
ERROR_NO_MORE_FILES = 18
ERROR_INSUFFICIENT_BUFFER = 122
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x100000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_MAX_COMMAND_BYTES = 1024 * 1024


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", DWORD), ("cntUsage", DWORD), ("th32ProcessID", DWORD),
                ("th32DefaultHeapID", SIZE_T), ("th32ModuleID", DWORD),
                ("cntThreads", DWORD), ("th32ParentProcessID", DWORD),
                ("pcPriClassBase", LONG), ("dwFlags", DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [("cb", DWORD), ("PageFaultCount", DWORD)] + [
        (name, SIZE_T) for name in (
            "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
            "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
            "PagefileUsage", "PeakPagefileUsage", "PrivateUsage")]


class FILETIME(ctypes.Structure):
    _fields_ = [("low", DWORD), ("high", DWORD)]


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_uint16), ("MaximumLength", ctypes.c_uint16),
                ("Buffer", ctypes.c_void_p)]


def _decode_command_buffer(buffer, returned):
    """Validate the native local-buffer pointer before reading any characters."""
    size = ctypes.sizeof(buffer)
    header_size = ctypes.sizeof(UNICODE_STRING)
    if not header_size <= returned <= size:
        raise ValueError("invalid process command buffer size")
    header = UNICODE_STRING.from_buffer_copy(buffer)
    length, maximum = int(header.Length), int(header.MaximumLength)
    if length % 2 or maximum % 2 or length > maximum:
        raise ValueError("invalid process command string length")
    if length == 0 and maximum == 0:
        return ""
    start = ctypes.addressof(buffer)
    pointer = int(header.Buffer or 0)
    if pointer < start + header_size or pointer % 2 or pointer + maximum > start + returned:
        raise ValueError("process command string is outside its buffer")
    return ctypes.string_at(pointer, length).decode("utf-16-le")


def _started_at(value):
    ticks = (int(value.high) << 32) | int(value.low)
    if not ticks:
        return None
    try:
        instant = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ticks // 10)
        return instant.isoformat(timespec="microseconds")
    except OverflowError:
        return None


class _WindowsApi:
    def __init__(self):
        if os.name != "nt":
            raise OSError("native process observations require Windows")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        self._window_callback = ctypes.WINFUNCTYPE(BOOL, HANDLE, ctypes.c_ssize_t)
        bindings = {
            "CreateToolhelp32Snapshot": ([DWORD, DWORD], HANDLE),
            "Process32FirstW": ([HANDLE, ctypes.POINTER(PROCESSENTRY32W)], BOOL),
            "Process32NextW": ([HANDLE, ctypes.POINTER(PROCESSENTRY32W)], BOOL),
            "OpenProcess": ([DWORD, BOOL, DWORD], HANDLE),
            "CloseHandle": ([HANDLE], BOOL),
            "WaitForSingleObject": ([HANDLE, DWORD], DWORD),
            "QueryFullProcessImageNameW": ([HANDLE, DWORD, ctypes.c_wchar_p, ctypes.POINTER(DWORD)], BOOL),
            "K32GetProcessMemoryInfo": ([HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX), DWORD], BOOL),
            "GetProcessHandleCount": ([HANDLE, ctypes.POINTER(DWORD)], BOOL),
            "GetProcessTimes": ([HANDLE] + [ctypes.POINTER(FILETIME)] * 4, BOOL),
            "GetPackageFamilyName": ([HANDLE, ctypes.POINTER(DWORD), ctypes.c_wchar_p], LONG),
        }
        for name, (arguments, result) in bindings.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
        self.user.EnumWindows.argtypes = [self._window_callback, ctypes.c_ssize_t]
        self.user.EnumWindows.restype = BOOL
        self.user.GetWindowThreadProcessId.argtypes = [HANDLE, ctypes.POINTER(DWORD)]
        self.user.GetWindowThreadProcessId.restype = DWORD
        self.user.IsWindowVisible.argtypes = [HANDLE]
        self.user.IsWindowVisible.restype = BOOL
        self.user.GetWindow.argtypes = [HANDLE, DWORD]
        self.user.GetWindow.restype = HANDLE
        self.ntdll.NtQueryInformationProcess.argtypes = [HANDLE, DWORD, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD)]
        self.ntdll.NtQueryInformationProcess.restype = LONG

    def entries(self, image_names):
        snapshot = self.kernel.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
        if snapshot in (None, INVALID_HANDLE_VALUE):
            raise ctypes.WinError(ctypes.get_last_error())
        rows = []
        try:
            entry = PROCESSENTRY32W(dwSize=ctypes.sizeof(PROCESSENTRY32W))
            present = self.kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            while present:
                if entry.szExeFile.casefold() in image_names:
                    rows.append({"pid": int(entry.th32ProcessID), "ppid": int(entry.th32ParentProcessID),
                                 "name": entry.szExeFile, "threads": int(entry.cntThreads)})
                entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                present = self.kernel.Process32NextW(snapshot, ctypes.byref(entry))
            code = ctypes.get_last_error()
            if code != ERROR_NO_MORE_FILES:
                raise ctypes.WinError(code)
            return rows
        finally:
            self.kernel.CloseHandle(snapshot)

    def window_pids(self, pids):
        visible = set()

        @self._window_callback
        def visit(window, _):
            process_id = DWORD()
            self.user.GetWindowThreadProcessId(window, ctypes.byref(process_id))
            if (process_id.value in pids and self.user.IsWindowVisible(window)
                    and not self.user.GetWindow(window, 4)):  # GW_OWNER
                visible.add(process_id.value)
            return True

        if not self.user.EnumWindows(visit, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        return visible

    def _command(self, handle):
        # ProcessCommandLineInformation (60) returns a UNICODE_STRING whose
        # buffer must lie inside this caller-owned allocation. This optional
        # Windows information class is isolated from all essential probes.
        size = 4096
        for _ in range(3):
            buffer = ctypes.create_string_buffer(size)
            returned = DWORD()
            status = self.ntdll.NtQueryInformationProcess(handle, 60, buffer, size, ctypes.byref(returned))
            unsigned = int(status) & 0xFFFFFFFF
            if unsigned == 0:
                return _decode_command_buffer(buffer, returned.value)
            if unsigned not in (0xC0000004, 0xC0000023, 0x80000005):
                return None
            required = int(returned.value)
            if required <= size or required > _MAX_COMMAND_BYTES:
                return None
            size = required
        return None

    def _package_family(self, handle):
        length = DWORD()
        code = self.kernel.GetPackageFamilyName(handle, ctypes.byref(length), None)
        if code != ERROR_INSUFFICIENT_BUFFER or not 1 <= length.value <= 1024:
            return None
        buffer = ctypes.create_unicode_buffer(length.value)
        if self.kernel.GetPackageFamilyName(handle, ctypes.byref(length), buffer) != 0:
            return None
        return buffer.value or None

    def inspect(self, entry):
        row = {**entry, "handles": None, "working_set": None, "private_memory": None,
               "wait_status": -1, "probe_error": 0, "started": None, "command": "",
               "command_available": False, "package_family": None, "has_window": None,
               "identity_confirmed": False, "exited": False, "detail_errors": {}}
        handle = self.kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, row["pid"])
        if not handle:
            row["probe_error"] = ctypes.get_last_error()
            return row
        try:
            status = int(self.kernel.WaitForSingleObject(handle, 0))
            row.update(wait_status=status, probe_error=ctypes.get_last_error() if status == WAIT_FAILED else 0,
                       exited=status == WAIT_OBJECT_0)
            if row["exited"]:
                return row
            image = ctypes.create_unicode_buffer(32768)
            length = DWORD(len(image))
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)):
                row["detail_errors"]["image"] = ctypes.get_last_error()
                return row
            if ntpath.basename(image.value).casefold() != row["name"].casefold():
                return None  # PID was reused for another executable after the snapshot.
            row["identity_confirmed"] = True
            memory = PROCESS_MEMORY_COUNTERS_EX(cb=ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX))
            if self.kernel.K32GetProcessMemoryInfo(handle, ctypes.byref(memory), ctypes.sizeof(memory)):
                row.update(working_set=int(memory.WorkingSetSize), private_memory=int(memory.PrivateUsage))
            else:
                row["detail_errors"]["memory"] = ctypes.get_last_error()
            handles = DWORD()
            if self.kernel.GetProcessHandleCount(handle, ctypes.byref(handles)):
                row["handles"] = int(handles.value)
            else:
                row["detail_errors"]["handles"] = ctypes.get_last_error()
            created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
            if self.kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                           ctypes.byref(kernel), ctypes.byref(user)):
                row["started"] = _started_at(created)
            else:
                row["detail_errors"]["times"] = ctypes.get_last_error()
            if row["name"].casefold() == "codex.exe":
                try:
                    command = self._command(handle)
                except (OSError, ValueError):
                    command = None
                if command is not None:
                    row.update(command=command, command_available=True)
            if row["name"].casefold() == "chatgpt.exe":
                row["package_family"] = self._package_family(handle)
            # The process can exit during the queries. Only this same handle
            # may establish that fact, rather than opening the PID again.
            status = int(self.kernel.WaitForSingleObject(handle, 0))
            row.update(wait_status=status, probe_error=ctypes.get_last_error() if status == WAIT_FAILED else 0,
                       exited=status == WAIT_OBJECT_0)
            return row
        finally:
            self.kernel.CloseHandle(handle)


@lru_cache(maxsize=1)
def _api():
    return _WindowsApi()


def snapshot_processes(image_names=("ChatGPT.exe", "codex.exe"), *, api=None):
    """Return one fresh observation per matching process, with bounded handles.

    Snapshot/enumeration errors raise OSError so the caller can use its fallback.
    Optional per-process fields are None when unavailable. ``exited`` is true
    only for a signaled process handle; ``identity_confirmed`` means the handle's
    image matches the snapshot. Command lines are queried only for codex.exe.
    """
    native = api if api is not None else _api()
    entries = native.entries({str(name).casefold() for name in image_names})
    rows = []
    for entry in entries:
        row = native.inspect(entry)
        if row is not None:
            rows.append(row)
    # A main window only changes the live/stub decision for low-memory,
    # single-threaded candidates. Do not enumerate windows in ordinary rounds.
    candidates = {row["pid"] for row in rows if row["name"].casefold() == "chatgpt.exe"
                  and not row["exited"] and row["threads"] <= 1
                  and (row["working_set"] or 0) < 20 * 1024 * 1024
                  and (row["private_memory"] or 0) < 20 * 1024 * 1024}
    if candidates:
        try:
            visible = native.window_pids(candidates)
        except OSError:
            pass
        else:
            for row in rows:
                if row["pid"] in candidates:
                    row["has_window"] = row["pid"] in visible
    return rows
