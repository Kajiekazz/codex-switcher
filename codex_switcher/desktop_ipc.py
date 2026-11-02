"""Targeted desktop interruption through its local coordination pipe.

The desktop forwards follower requests to the actual conversation owner. This
does not start a second app-server or terminate any application process.
"""
from __future__ import annotations

import json
import os
import struct
import time
import uuid


class DesktopIpcError(RuntimeError):
    pass


class WindowsPipe:
    def __init__(self, path=r"\\.\pipe\codex-ipc", timeout=5):
        if os.name != "nt":
            raise DesktopIpcError("desktop_stop_unsupported_platform")
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.timeout = ctypes, timeout
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self.kernel.CreateFileW.restype = wintypes.HANDLE
        self.kernel.PeekNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                            wintypes.LPDWORD, wintypes.LPDWORD, wintypes.LPDWORD]
        self.kernel.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.LPDWORD, wintypes.LPVOID]
        self.kernel.WriteFile.argtypes = self.kernel.ReadFile.argtypes
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateFileW(path, 0xC0000000, 0, None, 3, 0, None)
        if self.handle == ctypes.c_void_p(-1).value:
            self.handle = None
            raise DesktopIpcError("desktop_ipc_unavailable")

    def write(self, data):
        written = self.ctypes.c_ulong()
        if not self.kernel.WriteFile(self.handle, data, len(data), self.ctypes.byref(written), None) or written.value != len(data):
            raise DesktopIpcError("desktop_ipc_write_failed")

    def read(self, count, deadline):
        data = bytearray()
        while len(data) < count:
            if time.monotonic() >= deadline:
                raise DesktopIpcError("desktop_ipc_timeout")
            available = self.ctypes.c_ulong()
            if not self.kernel.PeekNamedPipe(self.handle, None, 0, None, self.ctypes.byref(available), None):
                raise DesktopIpcError("desktop_ipc_disconnected")
            size = min(count - len(data), available.value)
            if not size:
                time.sleep(.01)
                continue
            buffer = self.ctypes.create_string_buffer(size)
            read = self.ctypes.c_ulong()
            if not self.kernel.ReadFile(self.handle, buffer, size, self.ctypes.byref(read), None) or not read.value:
                raise DesktopIpcError("desktop_ipc_read_failed")
            data.extend(buffer.raw[:read.value])
        return bytes(data)

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class DesktopControl:
    def __init__(self, pipe_factory=WindowsPipe, timeout=12):
        self.pipe_factory, self.timeout = pipe_factory, timeout

    def interrupt(self, thread_id: str, turn_id: str) -> dict:
        if not thread_id or not turn_id:
            raise DesktopIpcError("confirmed_turn_required")
        pipe = self.pipe_factory()
        deadline = time.monotonic() + self.timeout
        client = "initializing-client"

        def send(message):
            data = json.dumps(message, ensure_ascii=False).encode("utf-8")
            pipe.write(struct.pack("<I", len(data)) + data)

        def request(method, params, version, target=None):
            rid = str(uuid.uuid4())
            message = dict(type="request", requestId=rid, sourceClientId=client, method=method,
                           params=params, version=version, timeoutMs=max(1, int((deadline - time.monotonic()) * 1000)))
            if target:
                message["targetClientId"] = target
            send(message)
            while True:
                size = struct.unpack("<I", pipe.read(4, deadline))[0]
                if not 0 < size <= 8 * 1024 * 1024:
                    raise DesktopIpcError("desktop_ipc_invalid_frame")
                try:
                    reply = json.loads(pipe.read(size, deadline))
                except (ValueError, UnicodeError) as exc:
                    raise DesktopIpcError("desktop_ipc_invalid_json") from exc
                if reply.get("type") == "client-discovery-request":
                    send(dict(type="client-discovery-response", requestId=reply.get("requestId"), response=dict(canHandle=False)))
                if reply.get("type") != "response" or reply.get("requestId") != rid:
                    continue
                if reply.get("resultType") != "success":
                    raise DesktopIpcError(str(reply.get("error") or "desktop_ipc_request_failed")[:300])
                return reply

        try:
            initialized = request("initialize", {"clientType": "codex-switcher"}, 0)
            client = (initialized.get("result") or {}).get("clientId")
            if not isinstance(client, str) or not client:
                raise DesktopIpcError("desktop_ipc_not_initialized")
            owner = request("thread-owner-discovery", {"hostId": "local", "conversationId": thread_id}, 1)
            target = owner.get("handledByClientId")
            if not isinstance(target, str) or not target:
                raise DesktopIpcError("desktop_thread_owner_not_found")
            response = request("thread-follower-interrupt-turn", {
                "conversationId": thread_id, "mode": "user-stop", "expectedTurnId": turn_id,
            }, 4, target)
            result = response.get("result") or {}
            if result.get("ok") is not True or result.get("interruptedTurnId") != turn_id:
                raise DesktopIpcError("desktop_stop_not_confirmed")
            if result.get("goalPauseError"):
                raise DesktopIpcError(str(result["goalPauseError"])[:300])
            return {"stopped": True, "turn_id": turn_id, "evidence": "desktop_ipc"}
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise DesktopIpcError("desktop_ipc_invalid_response") from exc
        finally:
            pipe.close()
