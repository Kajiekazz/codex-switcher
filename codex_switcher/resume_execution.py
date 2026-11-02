"""Follow a queued continuation through its own desktop turn, read-only.

Queue removal is only transport evidence. A receipt needs a new queue-triggered
turn containing the submitted message (or its exact client id), after the
dispatch cursor. Only identifiers, timestamps and hashes are persisted here.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .goals import held_thread_ids
from .queue_watch import queue_row_present

READ_BYTES = 2 * 1024 * 1024
BACKFILL_BYTES = 8 * 1024 * 1024
CONFIRM_SECONDS = 90
TERMINAL = {"completed", "failed", "interrupted", "cancelled"}


def _iso(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds")


def _seconds(value) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0


def _digest(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def rollout_cursor(thread_id: str, *, backfill: bool = False) -> dict | None:
    """Capture the file boundary before queue/add, without reading messages."""
    try:
        path = config.state_db_path()
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
            row = conn.execute("SELECT rollout_path FROM threads WHERE id = ?", (thread_id,)).fetchone()
        if not row or not row[0]:
            return None
        path = Path(row[0])
        size = path.stat().st_size
        offset = max(0, size - BACKFILL_BYTES) if backfill else size
        return {"path": str(path), "offset": offset, "skip_line": backfill and offset > 0}
    except (OSError, sqlite3.Error):
        return None


def _ids(payload: dict) -> list[str]:
    metadata = payload.get("internal_chat_message_metadata_passthrough") or {}
    return list(dict.fromkeys(str(source[key]) for source in (payload, metadata)
                            if isinstance(source, dict)
                            for key in ("clientUserMessageId", "client_user_message_id", "client_id")
                            if source.get(key)))


def _consume(record: dict, event: dict) -> None:
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        return
    category, kind = event.get("type"), payload.get("type")
    metadata = payload.get("internal_chat_message_metadata_passthrough") or {}
    tid = payload.get("turn_id") or (metadata.get("turn_id") if isinstance(metadata, dict) else None)
    turns = record.setdefault("turns", {})
    if category == "turn_context" and tid:
        record["context_turn"] = tid
        return
    if category == "event_msg" and kind == "task_started" and tid:
        attribution = payload.get("turn_attribution") or {}
        turns.setdefault(tid, {}).update(
            turn_id=tid, started_at=event.get("timestamp"),
            trigger=attribution.get("turn_trigger") if isinstance(attribution, dict) else None,
        )
        record["context_turn"] = tid
        # Retain the matched turn even when a long-running goal starts others.
        for old in list(turns)[:-16]:
            if old != record.get("turn_id"):
                turns.pop(old, None)
        return
    tid = tid or record.get("context_turn")
    if not tid:
        return
    turn = turns.setdefault(tid, {"turn_id": tid})
    text = None
    if category == "response_item" and payload.get("role") == "user":
        content = payload.get("content") or []
        text = "".join(item.get("text", "") for item in content if isinstance(item, dict)
                       and item.get("type") in {"input_text", "text"})
    elif category == "event_msg" and kind == "user_message":
        text = payload.get("message")
    if isinstance(text, str):
        # Only the initial user input belongs to this turn. A later steer must
        # not make an unrelated turn look like our continuation.
        if not turn.get("message_digest"):
            turn.update(message_digest=_digest(text), message_at=event.get("timestamp"),
                        message_id=payload.get("id"), client_ids=_ids(payload))
    if category == "event_msg" and kind in {"task_complete", "task_completed", "turn_aborted"}:
        turn["completed_at"] = event.get("timestamp")
        error = payload.get("error")
        turn["state"] = "interrupted" if kind == "turn_aborted" else "failed" if error else "completed"
        if error:
            info = error.get("codex_error_info") if isinstance(error, dict) else None
            turn["error_code"] = next(iter(info), "turn_failed") if isinstance(info, dict) else "turn_failed"


def _scan(record: dict) -> None:
    cursor = record.get("cursor")
    if not cursor:
        cursor = rollout_cursor(record["thread_id"], backfill=True)
        record["cursor"] = cursor
    if not cursor:
        record["scan_error"] = "rollout_unavailable"
        return
    try:
        path = Path(cursor["path"])
        size = path.stat().st_size
        offset = int(cursor.get("offset") or 0)
        if size < offset:
            # A rewrite/truncation is not a new turn. Timestamps below still
            # enforce the request boundary after rescanning a bounded tail.
            offset = max(0, size - BACKFILL_BYTES)
            cursor.update(offset=offset, skip_line=offset > 0)
        with path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(READ_BYTES)
        if not data:
            record.pop("scan_error", None)
            return
        end = data.rfind(b"\n")
        if end < 0:
            if len(data) == READ_BYTES:
                cursor.update(offset=offset + len(data), skip_line=True)
            return
        lines = data[:end].split(b"\n")
        if cursor.pop("skip_line", False):
            lines = lines[1:]
        for line in lines:
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(event, dict):
                _consume(record, event)
        cursor["offset"] = offset + end + 1
        record.pop("scan_error", None)
    except (OSError, ValueError, TypeError):
        record["scan_error"] = "rollout_unavailable"


def _receipt(record: dict, now: float, held: set[str], queue_db) -> dict:
    present = queue_row_present(queue_db, record["queued_id"])
    if present is True:
        record["seen_queued"] = True
    elif present is False and (record.get("seen_queued") or record.get("removed_at")):
        record.setdefault("removed_at", _iso(now))
    _scan(record)
    turns = record.get("turns") or {}
    if not record.get("turn_id") and record.get("removed_at") and present is not True:
        candidates = []
        for turn in turns.values():
            start = _seconds(turn.get("started_at"))
            if start < _seconds(record.get("requested_at")) or not start:
                continue
            if start > _seconds(record["removed_at"]) + CONFIRM_SECONDS:
                continue
            # A manual turn, an older running turn and a goal wake are never
            # receipts for a queued continuation just because they are active.
            exact = record.get("client_message_id") in (turn.get("client_ids") or [])
            matching = turn.get("trigger") == "queue" and turn.get("message_digest") == record["message_digest"]
            if exact or matching:
                candidates.append((exact, turn))
        exact = [turn for match, turn in candidates if match]
        eligible = exact or [turn for _, turn in candidates]
        if len(eligible) == 1:
            record["turn_id"] = eligible[0]["turn_id"]
            record["correlation"] = "client_message_id" if exact else "queue_turn_message"
        elif len(eligible) > 1:
            record["scan_error"] = "ambiguous_queue_turns"
    turn = turns.get(record.get("turn_id"))
    if turn:
        later_turn = any(_seconds(other.get("started_at")) > _seconds(turn.get("started_at"))
                         for other in turns.values() if other.get("turn_id") != turn["turn_id"])
        state = turn.get("state") or ("running" if record["thread_id"] in held and not later_turn else "started")
        return {"state": state, "turn_id": turn["turn_id"], "message_id": turn.get("message_id"),
                "started_at": turn.get("started_at"), "completed_at": turn.get("completed_at"),
                "error_code": turn.get("error_code"), "evidence": "rollout",
                "correlation": record.get("correlation")}
    if present is True or not record.get("removed_at"):
        return {"state": "queued"}
    elapsed = now - _seconds(record["removed_at"])
    return {"state": "unconfirmed" if elapsed >= CONFIRM_SECONDS else "confirming",
            "reason": record.get("scan_error") or "no_matching_queue_turn",
            "since": record["removed_at"]}


class ResumeExecutionTracker:
    """Persist receipts independently of the short-lived dispatch worker."""

    def __init__(self, *, state_path=None, queue_db=None, on_change=None, interval=5,
                 clock=time.time, held=held_thread_ids, autostart=True):
        self.path = Path(state_path or config.data_dir() / "resume-execution.json")
        self.queue_db = queue_db if queue_db is not None else config.queue_db_path()
        self.on_change, self.interval, self.clock, self.held = on_change, interval, clock, held
        self.autostart = autostart
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.worker = None
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            self.records = {row["queued_id"]: row for row in loaded if isinstance(row, dict) and row.get("queued_id")}
        except (OSError, ValueError, TypeError):
            self.records = {}
        if self.active():
            self._start()

    def active(self) -> int:
        with self.lock:
            return sum(self._watching(row, self.clock()) for row in self.records.values())

    @staticmethod
    def _watching(row, now):
        result = row.get("result") or {}
        # The discovery timeout bounds unmatched messages, not the duration of
        # a confirmed long-running turn. Follow that turn until it ends.
        return result.get("state") not in TERMINAL and (
            bool(row.get("turn_id")) or now < float(row.get("expires_at") or 0)
        )

    def _start(self):
        with self.lock:
            if self.autostart and (self.worker is None or not self.worker.is_alive()) and not self.stop_event.is_set():
                self.worker = threading.Thread(target=self._loop, daemon=True, name="resume-execution")
                self.worker.start()

    def _loop(self):
        while not self.stop_event.wait(self.interval):
            try:
                self.reconcile()
            except Exception as exc:
                print(f"[resume-execution] 回合状态读取失败：{type(exc).__name__}", flush=True)

    def stop(self):
        self.stop_event.set()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(2)

    def track(self, thread_id, result, *, requested_at, cursor=None):
        qid = result.get("queued_id")
        if not qid or result.get("cancelled") or not _seconds(requested_at):
            return
        with self.lock:
            row = self.records.get(qid)
            if row is None:
                cursor = cursor or rollout_cursor(thread_id, backfill=True)
                if not cursor:
                    return
                row = {"thread_id": thread_id, "queued_id": qid,
                       "client_message_id": result.get("client_message_id"),
                       "requested_at": requested_at, "message_digest": _digest("继续"),
                       "cursor": dict(cursor), "turns": {},
                       "seen_queued": bool(result.get("pending")),
                       "expires_at": max(self.clock(), _seconds(requested_at)) + 24 * 3600,
                       "result": {"state": "queued"}}
                self.records[qid] = row
            if result.get("delivered"):
                row.setdefault("removed_at", result.get("delivered_at") or _iso(self.clock()))
            self._save()
        self.reconcile()
        self._start()

    def cancel(self, queued_id):
        with self.lock:
            row = self.records.get(queued_id)
            if row:
                row["result"] = {"state": "cancelled"}
                self._save()
        if row:
            self._notify(row)

    def for_queue(self, queued_id):
        with self.lock:
            row = self.records.get(queued_id)
            return dict(row["result"]) if row else None

    def reconcile(self):
        changed = []
        with self.lock:
            now = self.clock()
            rows = [row for row in self.records.values() if self._watching(row, now)]
            if not rows:
                return
            try:
                held = self.held()
            except OSError:
                held = set()
            for row in rows:
                receipt = _receipt(row, now, held, self.queue_db)
                if receipt != row.get("result"):
                    row["result"] = receipt
                    changed.append(row)
            self._save()
        for row in changed:
            self._notify(row)

    def _notify(self, row):
        if self.on_change:
            self.on_change(row["thread_id"], row["queued_id"], dict(row["result"]))

    def _save(self):
        # Retain receipts while active and keep a bounded recent terminal tail.
        if len(self.records) > 100:
            for key in list(self.records):
                if len(self.records) <= 100:
                    break
                if not self._watching(self.records[key], self.clock()):
                    self.records.pop(key)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(list(self.records.values()), ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass
