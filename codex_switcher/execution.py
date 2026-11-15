"""Bounded, read-only turn evidence. Goal flags and locks are not turn status."""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from . import config

@dataclass(frozen=True)
class _RolloutCache:
    signature: tuple
    state: dict
    fingerprint: bytes
    complete_line: bool


_cache: dict[str, _RolloutCache] = {}
_lock = threading.Lock()
# A bounded set of locks merges simultaneous reads of the same active file.
# Holding the global cache lock during disk reads would serialize all chats.
_scan_locks = tuple(threading.Lock() for _ in range(16))
MAX_TAIL_BYTES = 8 * 1024 * 1024


def _usage_limit_error(value) -> bool:
    if isinstance(value, dict):
        return any(_usage_limit_error(value.get(key)) for key in (
            "message", "error_code", "code", "codex_error_info", "error",
        ))
    if not isinstance(value, str):
        return False
    return any(marker in value.casefold() for marker in (
        "usage_limit", "usage limit", "quota exceeded", "额度已用尽", "额度不足",
    ))


def is_capacity_error(value) -> bool:
    if isinstance(value, dict):
        return any(key in value for key in ("server_overloaded", "model_at_capacity", "model_capacity_exceeded")) or any(
            is_capacity_error(value.get(key)) for key in ("message", "error_code", "code", "codex_error_info", "error"))
    return isinstance(value, str) and any(marker in value.casefold() for marker in (
        "server_overloaded", "model_at_capacity", "model_capacity_exceeded",
        "model is at capacity", "model is currently at capacity",
    ))


def _signature(stat) -> tuple:
    return (stat.st_mtime_ns, stat.st_size, stat.st_dev, stat.st_ino,
            getattr(stat, "st_birthtime_ns", None))


def _boundary(stream, size: int) -> tuple[bytes, bool]:
    """Keep hashes, never log contents, for append-only boundary validation."""
    count = min(size, 256)
    stream.seek(0)
    prefix = stream.read(count)
    stream.seek(size - count)
    suffix = stream.read(count)
    return hashlib.blake2b(prefix + suffix, digest_size=16).digest(), not size or suffix.endswith(b"\n")


def _event(line):
    try:
        record = json.loads(line)
    except (ValueError, UnicodeError):
        return None
    if not isinstance(record, dict) or record.get("type") != "event_msg":
        return None
    payload = record.get("payload") or {}
    return (record, payload) if isinstance(payload, dict) else None


def _turn_state(record, payload):
    kind = payload.get("type")
    attribution = payload.get("turn_attribution") or {}
    return {
        "state": "running" if kind == "task_started" else "interrupted" if kind == "turn_aborted" else "ended",
        "turn_id": payload.get("turn_id"), "at": record.get("timestamp"),
        "trigger": attribution.get("turn_trigger") if isinstance(attribution, dict) else None,
        "evidence": "rollout",
    }


def _tail_state(stream, size: int) -> dict:
    state = {"state": "unknown"}
    usage_limited = capacity_limited = False
    offset, scanned, suffix = size, 0, b""
    found_start = False
    while offset > 0 and scanned < MAX_TAIL_BYTES:
        count = min(offset, 128 * 1024)
        offset -= count
        scanned += count
        stream.seek(offset)
        lines = (stream.read(count) + suffix).split(b"\n")
        suffix = lines.pop(0) if offset else b""
        for line in reversed(lines):
            event = _event(line)
            if event is None:
                continue
            record, payload = event
            kind = payload.get("type")
            if kind == "error":
                usage_limited = usage_limited or _usage_limit_error(payload)
                capacity_limited = capacity_limited or is_capacity_error(payload)
            if kind not in {"task_started", "task_complete", "task_completed", "turn_aborted"}:
                continue
            if kind != "task_started":
                usage_limited = usage_limited or _usage_limit_error(payload.get("error"))
                capacity_limited = capacity_limited or is_capacity_error(payload.get("error"))
            if state["state"] == "unknown":
                state = _turn_state(record, payload)
            if kind == "task_started":
                found_start = True
                break
        if found_start:
            break
    return {**state, "usage_limited": usage_limited, "capacity_limited": capacity_limited}


def _appended_state(stream, offset: int, size: int, previous: dict) -> dict | None:
    stream.seek(offset)
    data = stream.read(size - offset)
    if not data.endswith(b"\n"):
        # A partial last line must be reconsidered in full on the next read.
        return None
    state = dict(previous)
    for line in data.split(b"\n"):
        event = _event(line)
        if event is None:
            continue
        record, payload = event
        kind = payload.get("type")
        if kind == "error":
            state["usage_limited"] |= _usage_limit_error(payload)
            state["capacity_limited"] |= is_capacity_error(payload)
        elif kind in {"task_started", "task_complete", "task_completed", "turn_aborted"}:
            usage = kind != "task_started" and (state["usage_limited"] or _usage_limit_error(payload.get("error")))
            capacity = kind != "task_started" and (state["capacity_limited"] or is_capacity_error(payload.get("error")))
            state = {**_turn_state(record, payload), "usage_limited": usage, "capacity_limited": capacity}
    return state


def _rollout_state(path: Path) -> dict:
    key = str(path)
    with _scan_locks[hash(key) % len(_scan_locks)]:
        try:
            signature = _signature(path.stat())
            with _lock:
                cached = _cache.get(key)
                if cached and cached.signature == signature:
                    return dict(cached.state)
            with path.open("rb") as stream:
                signature = _signature(os.fstat(stream.fileno()))
                size = signature[1]
                state = None
                if (cached and cached.complete_line and signature[2:] == cached.signature[2:]
                        and 0 < size - cached.signature[1] <= MAX_TAIL_BYTES
                        and _boundary(stream, cached.signature[1])[0] == cached.fingerprint):
                    state = _appended_state(stream, cached.signature[1], size, cached.state)
                if state is None:
                    state = _tail_state(stream, size)
                fingerprint, complete = _boundary(stream, size)
                # A concurrent write will be reconsidered on the next poll.
                # Do not attach a mixed read to a newer file signature.
                stable = _signature(os.fstat(stream.fileno())) == signature
            if stable:
                with _lock:
                    if key not in _cache and len(_cache) >= 256:
                        _cache.pop(next(iter(_cache)))
                    _cache[key] = _RolloutCache(signature, state, fingerprint, complete)
            return dict(state)
        except (OSError, ValueError):
            with _lock:
                _cache.pop(key, None)
            return {"state": "unknown"}


def snapshot_threads(thread_ids: list[str]) -> dict[str, dict]:
    ids = list(dict.fromkeys(value for value in thread_ids if isinstance(value, str) and value))
    if not ids:
        return {}
    path = config.state_db_path()
    if not path.exists():
        return {}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
            if "rollout_path" not in columns:
                return {}
            archived = "archived" if "archived" in columns else "0"
            rows = conn.execute(
                f"SELECT id, rollout_path, {archived} FROM threads WHERE id IN ("
                + ",".join("?" for _ in ids) + ")", ids,
            ).fetchall()
    except sqlite3.Error:
        return {}
    from .goals import held_thread_ids
    held = held_thread_ids()
    result = {
        tid: {**(_rollout_state(Path(rollout)) if rollout else {"state": "unknown"}),
              "archived": bool(archived)}
        for tid, rollout, archived in rows
    }
    for tid, state in result.items():
        if state["state"] == "running" and tid not in held:
            state.update(state="unknown", reason="writer_not_confirmed")
    return result
