"""Native Windows activation and guarded desktop lifecycle; no command shell."""
from __future__ import annotations

import ctypes as c
from dataclasses import dataclass
import ntpath
import os
import re
import threading
import time
from concurrent.futures import Future, TimeoutError

from .windows_processes import BOOL, DWORD, FILETIME, HANDLE, _api


class SHELLEXECUTEINFOW(c.Structure):
    _fields_ = [("cbSize", DWORD), ("fMask", DWORD), ("hwnd", HANDLE),
                ("lpVerb", c.c_wchar_p), ("lpFile", c.c_wchar_p),
                ("lpParameters", c.c_wchar_p), ("lpDirectory", c.c_wchar_p),
                ("nShow", c.c_int32), ("hInstApp", HANDLE), ("lpIDList", HANDLE),
                ("lpClass", c.c_wchar_p), ("hkeyClass", HANDLE), ("dwHotKey", DWORD),
                ("hIcon", HANDLE), ("hProcess", HANDLE)]


def _shell_execute(target):
    # Each activation gets its own STA, independent of HTTP/background threads.
    ole = c.WinDLL("ole32", use_last_error=True)
    shell = c.WinDLL("shell32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    ole.CoInitializeEx.argtypes, ole.CoInitializeEx.restype = [HANDLE, DWORD], c.c_int32
    ole.CoUninitialize.argtypes, ole.CoUninitialize.restype = [], None
    shell.ShellExecuteExW.argtypes, shell.ShellExecuteExW.restype = [c.POINTER(SHELLEXECUTEINFOW)], BOOL
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [HANDLE], BOOL
    hr = ole.CoInitializeEx(None, 0x2 | 0x4)  # STA | DISABLE_OLE1DDE
    if hr < 0:
        raise OSError(f"Cannot initialize Windows activation (0x{hr & 0xffffffff:08x})")
    info = SHELLEXECUTEINFOW(cbSize=c.sizeof(SHELLEXECUTEINFOW),
                            fMask=0x40 | 0x100 | 0x400 | 0x8000,
                            lpVerb="open", lpFile=target, nShow=1)
    # NOCLOSEPROCESS | NOASYNC | FLAG_NO_UI | NO_CONSOLE. Keep the STA alive
    # until ShellExecute returns; never detach or kill an ongoing activation.
    try:
        if not shell.ShellExecuteExW(c.byref(info)):
            raise c.WinError(c.get_last_error())
    finally:
        if info.hProcess:
            kernel.CloseHandle(info.hProcess)
        ole.CoUninitialize()


class NativeActivation:
    def __init__(self, execute=_shell_execute):
        self.execute = execute
        self._lock = threading.Lock()
        self._pending = {}

    def submit(self, target):
        with self._lock:
            future = self._pending.get(target)
            if future is None:
                if len(self._pending) >= 16:
                    raise OSError("Windows activation is busy")
                future = Future()
                self._pending[target] = future
                def activate():
                    try:
                        self.execute(target)
                        future.set_result(True)
                    except Exception as exc:
                        future.set_exception(exc)
                    finally:
                        with self._lock:
                            if self._pending.get(target) is future:
                                del self._pending[target]
                try:
                    threading.Thread(target=activate, name="desktop-activation", daemon=True).start()
                except Exception:
                    del self._pending[target]
                    raise
        try:
            future.result(timeout=0.2)
            return {"activation_pending": False}
        except TimeoutError:
            if future.done():
                future.result()  # Propagate an activation's own timeout.
                return {"activation_pending": False}
            return {"activation_pending": True}


@dataclass
class OwnedProcess:
    pid: int
    handle: object
    name: str
    can_terminate: bool


class LifecycleApi:
    def __init__(self):
        self.probe = _api()
        self.kernel, self.user = self.probe.kernel, self.probe.user
        self.kernel.TerminateProcess.argtypes, self.kernel.TerminateProcess.restype = [HANDLE, DWORD], BOOL
        self.user.PostMessageW.argtypes = [HANDLE, DWORD, c.c_size_t, c.c_ssize_t]
        self.user.PostMessageW.restype = BOOL

    def inventory(self):
        return self.probe.entries()

    def open(self, row, captured_at):
        rights = 0x1000 | 0x100000
        handle = self.kernel.OpenProcess(rights | 1, False, row["pid"])
        can_terminate = bool(handle)
        if not handle:
            handle = self.kernel.OpenProcess(rights, False, row["pid"])
        if not handle:
            code = c.get_last_error()
            if code == 87:  # It disappeared; no handle exists to mutate.
                return None
            raise c.WinError(code)
        accepted = False
        try:
            if self.kernel.WaitForSingleObject(handle, 0) == 0:
                return None
            image = c.create_unicode_buffer(32768)
            length = DWORD(len(image))
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, image, c.byref(length)):
                raise c.WinError(c.get_last_error())
            if ntpath.basename(image.value).casefold() != row["name"].casefold():
                raise OSError("Process identity changed during desktop close")
            created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
            if not self.kernel.GetProcessTimes(handle, c.byref(created), c.byref(exited), c.byref(kernel), c.byref(user)):
                raise c.WinError(c.get_last_error())
            started = ((created.high << 32) | created.low) / 10_000_000 - 11644473600
            if started > captured_at:
                raise OSError("Process started after desktop close snapshot")
            accepted = True
            return OwnedProcess(row["pid"], handle, row["name"], can_terminate)
        finally:
            if not accepted:
                self.kernel.CloseHandle(handle)

    def wait(self, process):
        return int(self.kernel.WaitForSingleObject(process.handle, 0))

    def terminate(self, process):
        if not process.can_terminate:
            return False
        return bool(self.kernel.TerminateProcess(process.handle, 1))

    def close(self, process):
        self.kernel.CloseHandle(process.handle)

    def request_close(self, pids):
        sent = set()
        @self.probe._window_callback
        def visit(hwnd, _):
            pid = DWORD()
            self.user.GetWindowThreadProcessId(hwnd, c.byref(pid))
            if (pid.value in pids and pid.value not in sent and self.user.IsWindowVisible(hwnd)
                    and not self.user.GetWindow(hwnd, 4)):
                if self.user.PostMessageW(hwnd, 0x0010, 0, 0):  # WM_CLOSE
                    sent.add(pid.value)
            return True
        if not self.user.EnumWindows(visit, 0):
            raise c.WinError(c.get_last_error())


class NativeDesktop:
    def __init__(self, *, api=None, activation=None, service_pid=None, clock=time.monotonic,
                 sleep=time.sleep, wall_clock=time.time):
        self.api = api if api is not None else LifecycleApi()
        self.activation = activation if activation is not None else NativeActivation()
        self.service_pid = os.getpid() if service_pid is None else service_pid
        self.clock, self.sleep, self.wall_clock = clock, sleep, wall_clock
        self._action_lock = threading.Lock()
        self._lock = threading.RLock()
        self._processes = {}
        self._previous = []
        self._exited = set()
        self._closed = False

    def barrier(self):
        remaining, details = [], []
        with self._lock:
            for pid, process in list(self._processes.items()):
                status = self.api.wait(process)
                if status == 0:
                    self.api.close(process)
                    del self._processes[pid]
                    self._exited.add(pid)
                else:
                    remaining.append(pid)
                    details.append({"pid": pid, "name": process.name, "wait_status": status})
            return {"remaining": remaining, "exited_pids": sorted(self._exited), "process_details": details}

    def close_tree(self):
        with self._action_lock:
            if self._closed:
                raise OSError("Native desktop controller is closed")
            pending = self.barrier()
            if pending["remaining"]:
                return {"previous_pids": self._previous, "killed": [], "launched": False}
            captured_at = self.wall_clock()
            rows = self.api.inventory()
            parents = {row["pid"]: row["ppid"] for row in rows}
            roots = {row["pid"] for row in rows if row["name"].casefold() == "chatgpt.exe"}
            protected = set()
            pid = self.service_pid
            while pid and pid not in protected:
                protected.add(pid)
                pid = parents.get(pid, 0)
            if roots & protected:
                return {"skipped": True, "reason": "service_in_desktop_tree", "killed": [],
                        "previous_pids": sorted(roots)}
            tree = set(roots)
            while True:
                children = {pid for pid, parent in parents.items() if parent in tree} - tree
                if not children:
                    break
                tree.update(children)
                if len(tree) > 2048:
                    raise OSError("Desktop process tree is too large")
            opened = {}
            try:
                for row in rows:
                    if row["pid"] in tree:
                        process = self.api.open(row, captured_at)
                        if process is not None:
                            opened[process.pid] = process
            except Exception:
                for process in opened.values():
                    self.api.close(process)
                raise
            with self._lock:
                self._processes.update(opened)
                self._previous = sorted(tree)
                self._exited = set()
            if not opened:
                return {"previous_pids": sorted(tree), "killed": [], "launched": False}
            # Hold identities before asking windows to close. Every later wait
            # and termination uses that same handle, never a recycled PID.
            try:
                self.api.request_close(roots.intersection(opened))
                self.sleep(2)
                def depth(pid):
                    seen = set()
                    while pid in tree and pid not in seen:
                        seen.add(pid)
                        pid = parents.get(pid)
                    return len(seen)
                killed = []
                with self._lock:
                    for pid in sorted(opened, key=depth, reverse=True):
                        process = self._processes.get(pid)
                        if process is not None and self.api.wait(process) == 258 and self.api.terminate(process):
                            killed.append(pid)
                deadline = self.clock() + 5
                pending = self.barrier()
                while pending["remaining"] and self.clock() < deadline:
                    self.sleep(0.25)
                    pending = self.barrier()
                return {"previous_pids": self._previous, "killed": killed, "launched": False,
                        "remaining": pending["remaining"], "exited_pids": pending["exited_pids"]}
            except Exception:
                # Keep unconfirmed identities alive for the restart barrier;
                # releasing a handle is never proof that its process exited.
                self.barrier()
                raise

    def launch(self, aumid):
        with self._action_lock:
            if self._closed:
                raise OSError("Native desktop controller is closed")
            if self.barrier()["remaining"]:
                raise OSError("Waiting for old desktop processes to exit")
            if not aumid:
                return {"launched": False, "aumid": None}
            if not re.fullmatch(r"OpenAI\.Codex_[a-z0-9]{13}!App", aumid):
                raise ValueError("Invalid Codex application identity")
            return {"launched": True, "aumid": aumid,
                    **self.activation.submit("shell:AppsFolder\\" + aumid)}

    def open_uri(self, uri):
        if self._closed:
            raise OSError("Native desktop controller is closed")
        if not re.fullmatch(r"codex://threads/(?:wake/)?[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", uri):
            raise ValueError("Invalid Codex URI")
        return self.activation.submit(uri)

    def dispose(self):
        with self._action_lock, self._lock:
            self._closed = True
            for process in self._processes.values():
                self.api.close(process)
            self._processes.clear()
