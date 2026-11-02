"""Bounded, read-only turn evidence. Goal flags and locks are not turn status."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

from . import config

_cache: dict[str, tuple[int, int, dict]] = {}
_lock = threading.Lock()
MAX_TAIL_BYTES = 8 * 1024 * 1024


def _rollout_state(path: Path) -> dict:
    try:
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        with _lock:
            cached = _cache.get(str(path))
            if cached and cached[:2] == signature:
                return dict(cached[2])
        state = {"state": "unknown"}
        found_start = False
        usage_limited = False
        with path.open("rb") as stream:
            offset = stat.st_size
            scanned = 0
            suffix = b""
            while offset > 0 and scanned < MAX_TAIL_BYTES:
                count = min(offset, 128 * 1024)
                offset -= count
                scanned += count
                stream.seek(offset)
                lines = (stream.read(count) + suffix).split(b"\n")
                suffix = lines.pop(0) if offset else b""
                for line in reversed(lines):
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(record, dict) or record.get("type") != "event_msg":
                        continue
                    payload = record.get("payload") or {}
                    if not isinstance(payload, dict):
                        continue
                    kind = payload.get("type")
                    if kind == "error":
                        detail = str(payload.get("message") or "") + str(payload.get("error_code") or "")
                        usage_limited = usage_limited or any(value in detail.lower() for value in (
                            "usage_limit", "usage limit", "quota exceeded", "额度已用尽", "额度不足",
                        ))
                    if kind not in {"task_started", "task_complete", "task_completed", "turn_aborted"}:
                        continue
                    if state["state"] == "unknown":
                        attribution = payload.get("turn_attribution") or {}
                        state = {
                            "state": "running" if kind == "task_started" else
                            "interrupted" if kind == "turn_aborted" else "ended",
                            "turn_id": payload.get("turn_id"),
                            "at": record.get("timestamp"),
                            "trigger": attribution.get("turn_trigger") if isinstance(attribution, dict) else None,
                            "evidence": "rollout",
                        }
                    if kind == "task_started":
                        found_start = True
                        break
                if found_start:
                    break
        state["usage_limited"] = usage_limited
        with _lock:
            if len(_cache) >= 256:
                _cache.pop(next(iter(_cache)))
            _cache[str(path)] = (*signature, state)
        return dict(state)
    except (OSError, ValueError):
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
