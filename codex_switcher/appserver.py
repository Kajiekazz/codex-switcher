"""Minimal JSON-RPC client for the local ``codex app-server``.

The Codex desktop app owns its own app-server over stdio; we cannot attach to
that pipe. Instead this module spawns a short-lived app-server child for goal
operations and speaks the same JSON-RPC protocol. The child reads the live
``~/.codex/auth.json`` at startup, so after an account switch the session must
be recreated.

Only display fields and goal mutations are handled here. Passwords and tokens
are never read, logged or returned by this module.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import Future, TimeoutError as FutureTimeout
from pathlib import Path

from . import __version__, config

SERVER_REQUEST_REJECTED = "codex-switcher/serverRequestRejected"


class AppServerError(RuntimeError):
    pass


class AppServerSession:
    """One app-server child process speaking JSON-RPC over stdio."""

    def __init__(
        self,
        exe: str | None = None,
        *,
        cwd: str | Path | None = None,
        env: dict | None = None,
        stderr_tail: int = 60,
    ):
        exe = exe or config.find_codex_exe()
        if not exe:
            raise AppServerError("codex executable not found")
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.exe = exe
        self._pending: dict[int, Future] = {}
        self._notifications: list[dict] = []
        self._cond = threading.Condition()
        self._write_lock = threading.Lock()
        self._closed = False
        self._next_id = 0
        self._stderr_tail = deque(maxlen=stderr_tail)
        self._server_requests = 0
        try:
            self.proc = subprocess.Popen(
                [exe, "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=str(cwd) if cwd else None,
                env=env,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise AppServerError(f"cannot start app-server: {exc}") from exc

        self._stdout_thread = threading.Thread(
            target=self._read_stdout, name="appserver-stdout", daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, name="appserver-stderr", daemon=True
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    # ------------------------------------------------------------- lifecycle
    @property
    def pid(self) -> int | None:
        return getattr(self.proc, "pid", None)

    @property
    def alive(self) -> bool:
        return (not self._closed) and self.proc.poll() is None

    def close(self) -> None:
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._cond.notify_all()
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        with self._cond:
            self._fail_pending_locked("app-server closed")
            self._notifications.clear()
            self._cond.notify_all()

    def __enter__(self) -> "AppServerSession":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ------------------------------------------------------------------ rpc
    def initialize(self, timeout: float = 30.0) -> dict:
        return self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex-switcher",
                    "title": "Codex Switcher",
                    "version": __version__,
                },
                # ``thread/queue/list`` (used to confirm the desktop app took a
                # queued resume message) is gated behind this capability.
                "capabilities": {"experimentalApi": True},
            },
            timeout=timeout,
        )

    def request(self, method: str, params: dict | None = None, timeout: float = 30.0):
        with self._cond:
            if self._closed:
                raise AppServerError("app-server is closed")
            self._next_id += 1
            req_id = self._next_id
            future: Future = Future()
            self._pending[req_id] = future
        payload: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._write(payload)
        except OSError as exc:
            with self._cond:
                self._pending.pop(req_id, None)
            raise AppServerError(f"cannot write {method}: {exc}") from exc
        try:
            message = future.result(timeout=timeout)
        except FutureTimeout:
            with self._cond:
                self._pending.pop(req_id, None)
            raise AppServerError(f"{method} timed out after {timeout:.0f}s") from None
        if not isinstance(message, dict):
            raise AppServerError(f"{method}: invalid response")
        error = message.get("error")
        if error:
            detail = error.get("message") if isinstance(error, dict) else str(error)
            raise AppServerError(f"{method}: {detail}")
        return message.get("result")

    def notify(self, method: str, params: dict | None = None) -> None:
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

    def wait_notification(self, predicate, timeout: float) -> dict | None:
        """Wait for the first notification matching *predicate*.

        Matching notifications are removed from the buffer; non-matching ones
        stay (bounded) so later waits can still see them.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while True:
                for index, notification in enumerate(self._notifications):
                    if predicate(notification):
                        return self._notifications.pop(index)
                if self._closed:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(min(remaining, 1.0))

    # -------------------------------------------------------------- internal
    def _write(self, payload: dict) -> None:
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with self._write_lock:
            if not self.proc.stdin:
                raise OSError("stdin is closed")
            self.proc.stdin.write(line)
            self.proc.stdin.flush()

    def _read_stdout(self) -> None:
        stream = self.proc.stdout
        if stream is None:
            return
        for raw in stream:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            has_id = "id" in message
            if has_id and "method" in message:
                # Server -> client request (approval, user input, ...). We do
                # not drive interactive turns, so refuse it instead of hanging.
                with self._cond:
                    self._server_requests += 1
                try:
                    self._write(
                        {
                            "jsonrpc": "2.0",
                            "id": message.get("id"),
                            "error": {
                                "code": -32601,
                                "message": "codex-switcher goal runner does not handle server requests",
                            },
                        }
                    )
                except OSError:
                    pass
                params = message.get("params") or {}
                if not isinstance(params, dict):
                    params = {}
                # Preserve only routing metadata. Tool arguments, approval
                # details and user-input prompts must not enter task logs.
                with self._cond:
                    self._notifications.append({
                        "method": SERVER_REQUEST_REJECTED,
                        "params": {
                            "threadId": params.get("threadId") or params.get("thread_id"),
                            "turnId": params.get("turnId") or params.get("turn_id"),
                            "requestMethod": str(message.get("method") or "")[:120],
                        },
                    })
                    self._cond.notify_all()
                continue
            if has_id and ("result" in message or "error" in message):
                with self._cond:
                    future = self._pending.pop(message["id"], None)
                if future is not None and not future.done():
                    future.set_result(message)
                continue
            with self._cond:
                self._notifications.append(message)
                if len(self._notifications) > 400:
                    del self._notifications[:200]
                self._cond.notify_all()
        with self._cond:
            self._fail_pending_locked("app-server stdout closed")
            self._cond.notify_all()

    def _read_stderr(self) -> None:
        stream = self.proc.stderr
        if stream is None:
            return
        for line in stream:
            text = line.rstrip()
            if text:
                self._stderr_tail.append(text)
                sys.stderr.write(f"[app-server] {text}\n")

    def _fail_pending_locked(self, message: str) -> None:
        self._closed = True
        for future in self._pending.values():
            if not future.done():
                detail = message
                if self._stderr_tail:
                    detail += ": " + self._stderr_tail[-1]
                future.set_exception(AppServerError(detail))
        self._pending.clear()

    def stderr_tail(self) -> list[str]:
        return list(self._stderr_tail)
