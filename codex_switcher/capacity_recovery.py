"""Retry opted-in chats after model overload, without account or model changes."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import threading
import time


RETRY_SECONDS = 10


def _timestamp(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0


class CapacityRecovery:
    def __init__(self, snapshot, dispatch, *, path, clock=time.time, interval=10):
        self.snapshot, self.dispatch = snapshot, dispatch
        self.path, self.clock, self.interval = Path(path), clock, interval
        self._lock, self._tick_lock = threading.RLock(), threading.Lock()
        self._stop, self._thread = threading.Event(), None
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            self._entries = {
                tid: row for tid, row in saved.items()
                if isinstance(tid, str) and isinstance(row, dict)
                and isinstance(row.get("failure"), str)
                and type(row.get("attempts")) is int
                and row["attempts"] >= 0
                and type(row.get("after")) in (int, float)
            }
        except (OSError, ValueError, AttributeError):
            self._entries = {}
        self._last_error = None

    def status(self):
        with self._lock:
            return {"running": bool(self._thread and self._thread.is_alive()),
                    "entries": {tid: dict(row) for tid, row in self._entries.items()},
                    "error": self._last_error}

    def _save(self):
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix(".tmp")
                temporary.write_text(json.dumps(self._entries), encoding="utf-8")
                temporary.replace(self.path)
            except OSError:
                pass

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="capacity-recovery", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _loop(self):
        delay = self.interval
        while not self._stop.wait(delay):
            try:
                self.tick_once()
            except Exception:
                with self._lock:
                    self._last_error = "capacity_recovery_unavailable"
            with self._lock:
                now = self.clock()
                deadlines = [row["after"] - now for row in self._entries.values()
                             if not row.get("job_id") and row["after"] > now]
            delay = min([self.interval] + deadlines)

    def tick_once(self):
        with self._tick_lock:
            return self._tick()

    def _tick(self):
        snapshot = self.snapshot()
        targets, now = snapshot.get("targets", {}), self.clock()
        active = set(snapshot.get("active", []))
        with self._lock:
            self._last_error = None
            self._entries = {tid: row for tid, row in self._entries.items() if tid in active}
            due = {}
            for tid, target in targets.items():
                failure = target.get("turn_id") or target.get("at")
                if not failure:
                    continue
                row = self._entries.get(tid)
                if not row or row["failure"] != failure:
                    attempts = row["attempts"] if row else 0
                    # A persisted deadline also covers restart during the same
                    # failed turn. A future timestamp cannot postpone forever.
                    failed_at = min(now, _timestamp(target.get("at")) or now)
                    row = {"failure": failure, "attempts": attempts,
                           "after": max(row["after"] if row else 0, failed_at + RETRY_SECONDS),
                           "state": "waiting_retry"}
                    self._entries[tid] = row
                if (row.get("job_id") and
                        snapshot.get("unsubmitted", {}).get(tid) == row["job_id"]):
                    row.pop("job_id", None)
                    row["state"] = "waiting_retry"
                if not row.get("job_id") and now >= row["after"]:
                    due[tid] = failure
            # Save deadlines before dispatch so restart retains the same wait.
            self._save()
        if not due or self._stop.is_set():
            return {"ok": True, "skipped": "waiting_retry"}
        selected = dict(list(due.items())[:10])
        outcome = self.dispatch(selected)
        job = outcome.get("job") or {}
        if job.get("id"):
            submitted = set(outcome.get("thread_ids", job.get("thread_ids", [])))
            with self._lock:
                for tid in selected:
                    if tid not in submitted:
                        continue
                    row = self._entries[tid]
                    row["attempts"] += 1
                    row.update(job_id=job["id"], state="submitted",
                               after=now + RETRY_SECONDS)
                self._save()
        return outcome
