"""Keep watching queued resume messages until the desktop app takes them.

A resume dispatch writes ``继续`` into ``~/.codex/queue_1.sqlite`` and wakes the
desktop app with ``codex://threads/wake/<thread>``. Two things can still leave
that row sitting there for hours:

* the wake is handed to the app during its *startup window* (a switch restarts
  it) and is silently dropped — the new process never sees the URI;
* every conversation of the app already runs a turn, so the queue is simply not
  drained yet.

The dispatch itself cannot wait for either: it finishes in seconds and the desk-
top app may take minutes. This module keeps the row "owned" after the job ends:
it polls the queue database read-only, records removal when the row disappears,
and re-sends the deep-link wake with an exponential backoff
while the row is still pending. Nothing here writes the queue, starts a process,
or closes a window — it only re-sends the same deep link the dispatch already
used, and only while a live desktop app exists.

Truth is *the queue row*, not "the dispatch finished":

* row gone  -> ``delivered`` (compatible field: no longer queued; execution unknown)
* row there -> still ``pending``; re-wake on the backoff schedule
* unreadable -> unknown; keep the entry and try again next tick
* TTL passed -> ``queue_ttl_expired`` (the app never came back for it)
* thread archived -> ``thread_archived``; stop waking, never claim delivery

A pending row whose conversation got archived (the user archived the task, or the
thread was cleaned up) can never be drained: the app does not resume archived
conversations. Without this check the dashboard would keep saying 「等桌面端取走」
for hours while every wake lands on a conversation nobody opens.

State is persisted next to the resume history so a service restart keeps
watching instead of forgetting the row.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

#: First retry is quick — a wake lost during the app's startup window is the
#: common failure — then it backs off so a busy app is not spammed. Presence is
#: still checked on every tick, whatever the backoff says.
RETRY_BACKOFF_SECONDS: tuple[float, ...] = (15, 30, 60, 120, 300, 600)

#: Six hours is generous: a queued resume that old will not be picked up by the
#: user either. The entry stays in the snapshot as ``queue_ttl_expired``.
DEFAULT_TTL_SECONDS = 6 * 3600.0

#: How often an entry is checked when nothing else is scheduled.
DEFAULT_INTERVAL_SECONDS = 15.0

#: Entries kept for the dashboard / history (pending ones are never trimmed).
MAX_ENTRIES = 50

#: Delivered/expired entries are only interesting for a while.
KEEP_FINISHED_SECONDS = 24 * 3600.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def queue_row_present(queue_db: Path | str | None, queued_id: str) -> bool | None:
    """Whether the queued item still exists.

    ``None`` means "cannot tell" (no database, locked, unexpected schema): the
    caller must not turn that into a delivery. ``mode=ro`` keeps this a pure
    read — the Codex app owns that database and we must never write it.
    """
    if not queued_id or queue_db is None:
        return None
    path = Path(queue_db)
    if not path.exists():
        return None
    try:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=1.0
        )
    except sqlite3.Error:
        return None
    try:
        row = connection.execute(
            "SELECT 1 FROM queued_items WHERE id = ?", (queued_id,)
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    return bool(row)


def thread_archived(
    state_db: Path | str | None, thread_id: str
) -> bool | None:
    """Whether the desktop app has this conversation archived.

    ``None`` means "cannot tell" (no database, old schema, unknown thread): the
    watchdog then keeps its normal behaviour instead of giving up. Archived
    conversations are never resumed, so a queued row for one is dead weight.
    """
    if state_db is None or not thread_id:
        return None
    path = Path(state_db)
    if not path.exists():
        return None
    try:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=1.0
        )
    except sqlite3.Error:
        return None
    try:
        row = connection.execute(
            "SELECT archived FROM threads WHERE id = ?", (thread_id,)
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    if not row:
        return None
    try:
        return bool(int(row[0] or 0))
    except (TypeError, ValueError):
        return None


def _payload_is_resume(payload_json: str, message: str) -> bool:
    try:
        payload = json.loads(payload_json or "")
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    # ``{"UserInput": {"content": [{"type": "text", "text": "继续"}]}}``
    body = payload.get("UserInput") if "UserInput" in payload else payload
    if not isinstance(body, dict):
        return False
    for item in body.get("content") or []:
        if (
            isinstance(item, dict)
            and item.get("type") == "text"
            and (item.get("text") or "").strip() == message
        ):
            return True
    return (body.get("text") or "").strip() == message


def resume_rows(
    queue_db: Path | str | None,
    thread_id: str,
    *,
    message: str,
    since_ms: int,
    until_ms: int,
) -> list[str]:
    """Rows that look like *our* resume message for this thread.

    Used only to adopt rows queued before the watchdog existed (a history entry
    with no ``queued_id``): the text must be exactly the resume message and the
    row must have been created inside the dispatch window, so an unrelated
    user-queued message is never hijacked.
    """
    if queue_db is None or not thread_id:
        return []
    path = Path(queue_db)
    if not path.exists():
        return []
    try:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=1.0
        )
    except sqlite3.Error:
        return []
    try:
        rows = connection.execute(
            "SELECT id, payload_json, created_at_ms FROM queued_items "
            "WHERE thread_id = ?",
            (thread_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    found: list[str] = []
    for queued_id, payload_json, created_at_ms in rows:
        try:
            created = int(created_at_ms)
        except (TypeError, ValueError):
            continue
        if not (since_ms <= created <= until_ms):
            continue
        if _payload_is_resume(payload_json, message):
            found.append(str(queued_id))
    return found


def first_resume_row(
    queue_db: Path | str | None, thread_id: str, *, message: str
) -> dict | None:
    """An already-queued resume row for this thread, if one is still waiting.

    ``thread/queue/add`` cannot tell us that a 「继续」 from a previous attempt
    is still in ``queue_1.sqlite``; queueing a second one runs a second turn.
    Only a row whose text is *exactly* the resume message qualifies, so a
    message the user typed is never hijacked.
    """
    if queue_db is None or not thread_id:
        return None
    path = Path(queue_db)
    if not path.exists():
        return None
    try:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=1.0
        )
    except sqlite3.Error:
        return None
    try:
        rows = connection.execute(
            "SELECT id, payload_json FROM queued_items WHERE thread_id = ? "
            "ORDER BY created_at_ms",
            (thread_id,),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    for queued_id, payload_json in rows:
        if not _payload_is_resume(payload_json, message):
            continue
        client_id = None
        try:
            payload = json.loads(payload_json or "")
            body = payload.get("UserInput") if "UserInput" in payload else payload
            if isinstance(body, dict):
                client_id = body.get("client_id")
        except (ValueError, AttributeError):
            pass
        return {"id": str(queued_id), "client_message_id": client_id}
    return None


class QueueWatchdog:
    """Background owner of queued resume messages that nobody took yet."""

    def __init__(
        self,
        *,
        wake=None,
        queue_db: Path | str | None = None,
        state_path: Path | str | None = None,
        clock=None,
        sleep=None,
        backoff: tuple[float, ...] = RETRY_BACKOFF_SECONDS,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        on_change=None,
        log=None,
        autostart: bool = True,
        probe=None,
        probe_interval: float = 60.0,
        archived=None,
        archived_interval: float = 60.0,
    ):
        self._wake = wake
        self._queue_db = Path(queue_db) if queue_db is not None else None
        self._state_path = Path(state_path) if state_path is not None else None
        self._clock = clock or time.time
        self._sleep = sleep or time.sleep
        self._backoff = tuple(float(x) for x in backoff) or RETRY_BACKOFF_SECONDS
        self._ttl = float(ttl_seconds)
        self._interval = max(1.0, float(interval))
        self._on_change = on_change
        self._log = log or (lambda level, message: None)
        #: Optional ``() -> {"running": bool, "pids": [...]}`` probe. When the
        #: process set turns over (a switch restarts the app, an update does
        #: too) the wake we already sent went to a dead window, so retry at once
        #: instead of waiting out the backoff. ``probe_interval`` throttles the
        #: (relatively slow) process lookup.
        self._probe = probe
        self._probe_interval = max(10.0, float(probe_interval))
        self._last_probe_at: float | None = None
        self._last_pids: set[int] | None = None
        #: Optional ``(thread_id) -> bool | None`` probe for "conversation
        #: archived". A row queued for an archived conversation will never be
        #: taken, so the entry is retired instead of woken forever. Results are
        #: cached for ``archived_interval`` — the check costs a database read.
        self._archived = archived
        self._archived_interval = max(10.0, float(archived_interval))
        self._archived_cache: dict[str, tuple[float, bool | None]] = {}

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._entries: list[dict] = self._load()
        if autostart and self.pending():
            self._ensure_thread()

    # ------------------------------------------------------------- lifecycle
    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._stop.is_set():
                return
            self._thread = threading.Thread(
                target=self._loop, name="goal-queue-watch", daemon=True
            )
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._stop.wait(self._interval):
                return
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - a watchdog must not die
                self._log("error", f"队列看护出错: {exc}")

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)

    # ---------------------------------------------------------------- public
    def cancel(self, queued_id: str) -> None:
        """Forget a row only after the queue API confirmed its deletion."""
        with self._lock:
            entry = next((item for item in self._entries if item.get("queued_id") == queued_id), None)
            if entry is None:
                return
            entry.update(pending=False, delivered=False, delivered_at=None,
                         cancelled=True, error="cancelled", next_wake_at=None)
            self._save()
        self._notify(entry)

    def track(
        self,
        thread_id: str,
        queued_id: str,
        *,
        job_id: str | None = None,
        wake: dict | None = None,
        observed: bool = True,
    ) -> dict:
        """Take ownership of a queued row; probe it once right away.

        ``observed`` says the dispatch saw the row in ``thread/queue/list`` a
        moment ago, which makes "not in the database" mean *removed* rather
        than "cannot tell". Without it (no queue-list support) an absent row is
        not evidence: a queue database we cannot read must never turn into a
        false removal report. Even an observed removal does not prove execution.
        """
        now = self._clock()
        with self._lock:
            existing = next(
                (item for item in self._entries if item.get("queued_id") == queued_id),
                None,
            )
            if existing is not None:
                existing.update(
                    thread_id=thread_id,
                    job_id=job_id or existing.get("job_id"),
                    pending=True,
                    delivered=False,
                    error=None,
                )
                entry = existing
            else:
                entry = {
                    "thread_id": thread_id,
                    "queued_id": queued_id,
                    "job_id": job_id,
                    "queued_at": _iso(now),
                    "attempts": 0,
                    "last_wake_at": None,
                    "last_wake": None,
                    "next_wake_at": now + self._backoff[0],
                    "expires_at": now + self._ttl,
                    "pending": True,
                    "delivered": False,
                    "delivered_at": None,
                    "error": None,
                }
                self._entries.append(entry)
            if wake:
                entry["last_wake"] = _compact(wake)
                entry["last_wake_at"] = _iso(now)
            entry["observed"] = bool(observed) or bool(entry.get("observed"))
            entry["next_wake_at"] = max(
                float(entry.get("next_wake_at") or 0), now + self._backoff[0]
            )
            self._save()
        if observed and queue_row_present(self._queue_db, queued_id) is False:
            # The row disappeared between the last poll and here; the reason
            # (execution, editing or deletion) is not observable in this database.
            self._finish(entry, delivered=True)
        elif self._entry_archived(entry, now):
            self._abandon(entry, now)
        elif self.pending():
            self._ensure_thread()
        return dict(entry)

    def tick(self, now: float | None = None) -> list[dict]:
        """One synchronous pass; returns the entries whose state changed."""
        now = self._clock() if now is None else float(now)
        self._probe_restart(now)
        changed: list[dict] = []
        with self._lock:
            entries = list(self._entries)
        for entry in entries:
            if not entry.get("pending"):
                continue
            if now >= float(entry.get("expires_at") or 0):
                entry["pending"] = False
                entry["error"] = entry.get("error") or "queue_ttl_expired"
                entry["next_wake_at"] = None
                changed.append(entry)
                self._log(
                    "warn",
                    f"{entry.get('thread_id', '')[:8]} 排队超过 "
                    f"{int(self._ttl // 3600)} 小时仍未被桌面端取走，停止重试",
                )
                continue
            present = queue_row_present(self._queue_db, entry.get("queued_id") or "")
            if entry.get("cancelled"):
                continue
            if present:
                entry["seen"] = True
            elif present is False and (entry.get("observed") or entry.get("seen")):
                entry["pending"] = False
                entry["delivered"] = True
                entry["delivered_at"] = _iso(now)
                entry["error"] = None
                entry["next_wake_at"] = None
                changed.append(entry)
                self._log(
                    "info",
                    f"{entry.get('thread_id', '')[:8]} 已出队，执行待确认（队列行已消失）",
                )
                continue
            # ``None`` (unreadable) and an absence we never observed both mean
            # "cannot tell": the row may still be queued. Keep retrying the wake
            # — it is a no-op when nothing is pending — but never claim delivery.
            if self._entry_archived(entry, now):
                # The conversation is archived, so no wake can ever make the app
                # take this row. Retire the entry instead of promising 「等桌面端
                # 取走」 forever; the row itself is left untouched and is *not*
                # reported as delivered.
                self._abandon(entry, now)
                changed.append(entry)
                continue
            if now < float(entry.get("next_wake_at") or 0):
                continue
            self._retry_wake(entry, now)
        if changed:
            self._save()
            for entry in changed:
                self._notify(entry)
        return changed

    def snapshot(self) -> dict:
        with self._lock:
            entries = [dict(item) for item in self._entries[-20:]]
            pending = sum(1 for item in self._entries if item.get("pending"))
        return {
            "enabled": True,
            "pending": pending,
            "entries": entries,
        }

    def pending(self, thread_id: str | None = None) -> list[dict]:
        with self._lock:
            return [
                dict(item)
                for item in self._entries
                if item.get("pending")
                and (thread_id is None or item.get("thread_id") == thread_id)
            ]

    def for_thread(self, thread_id: str) -> dict | None:
        with self._lock:
            for item in reversed(self._entries):
                if item.get("thread_id") == thread_id:
                    result = dict(item)
                    result["backoff_remaining"] = max(
                        0,
                        int(
                            float(item.get("next_wake_at") or 0) - self._clock()
                        ),
                    )
                    return result
        return None

    # --------------------------------------------------------------- private
    def _probe_restart(self, now: float) -> None:
        """Retry at once when the desktop app turned over.

        A wake sent while the app was restarting lands in a window that no
        longer exists. ``pids`` survive across turns (renderers come and go, the
        main process does not), so "no overlap with the previous set" means a
        real relaunch — not a helper process churn.
        """
        probe = self._probe
        if probe is None:
            return
        if self._last_probe_at is not None and now - self._last_probe_at < self._probe_interval:
            return
        self._last_probe_at = now
        try:
            state = probe() or {}
        except Exception:  # noqa: BLE001 - a probe must never break the tick
            return
        if not isinstance(state, dict):
            return
        pids: set[int] = set()
        for pid in state.get("pids") or []:
            try:
                pids.add(int(pid))
            except (TypeError, ValueError):
                continue
        if not pids:
            # The app is gone; the next launch is a restart too.
            self._last_pids = None
            return
        previous = self._last_pids
        self._last_pids = pids
        if not previous or (previous & pids):
            return
        with self._lock:
            if not any(entry.get("pending") for entry in self._entries):
                return
            for entry in self._entries:
                if entry.get("pending"):
                    entry["next_wake_at"] = now
                    entry["restarted_at"] = _iso(now)
            self._save()
        self._log("info", "桌面端已重新启动，立即重发排队消息的唤醒")

    def _entry_archived(self, entry: dict, now: float) -> bool:
        probe = self._archived
        if probe is None:
            return False
        thread_id = entry.get("thread_id") or ""
        if not thread_id:
            return False
        cached = self._archived_cache.get(thread_id)
        if cached is not None and now - cached[0] < self._archived_interval:
            return cached[1] is True
        try:
            state = probe(thread_id)
        except Exception:  # noqa: BLE001 - a probe must never break the tick
            state = None
        self._archived_cache[thread_id] = (now, state)
        return state is True

    def _retry_wake(self, entry: dict, now: float) -> None:
        thread_id = entry.get("thread_id") or ""
        attempts = int(entry.get("attempts") or 0)
        try:
            result = self._wake(thread_id) if self._wake else {
                "ok": False, "skipped": True, "reason": "unsupported"
            }
        except Exception as exc:  # noqa: BLE001 - retry again later
            result = {"ok": False, "error": str(exc)[:200]}
        if not isinstance(result, dict):
            result = {"ok": bool(result)}
        entry["attempts"] = attempts + 1
        entry["last_wake_at"] = _iso(now)
        entry["last_wake"] = _compact(result)
        delay = self._backoff[min(attempts, len(self._backoff) - 1)]
        entry["next_wake_at"] = now + delay
        if result.get("skipped"):
            self._log(
                "info",
                f"{thread_id[:8]} 第 {attempts + 1} 次唤醒跳过"
                f"（{result.get('reason') or 'desktop_not_running'}），"
                f"{int(delay)} 秒后再试",
            )
        elif result.get("ok"):
            self._log(
                "info",
                f"{thread_id[:8]} 第 {attempts + 1} 次唤醒已发出，"
                f"{int(delay)} 秒后复查队列",
            )
        else:
            self._log(
                "warn",
                f"{thread_id[:8]} 第 {attempts + 1} 次唤醒失败："
                f"{result.get('error') or '未知原因'}",
            )
        self._save()

    def _abandon(self, entry: dict, now: float) -> None:
        """Retire an entry whose conversation cannot take the row any more.

        Not a delivery: the row may still exist in ``queue_1.sqlite``. We only
        stop waking and say why.
        """
        entry["pending"] = False
        entry["delivered"] = False
        entry["delivered_at"] = None
        entry["error"] = "thread_archived"
        entry["abandoned_at"] = _iso(now)
        entry["next_wake_at"] = None
        self._save()
        self._log(
            "warn",
            f"{entry.get('thread_id', '')[:8]} 会话已归档，"
            "队列里的消息不会被执行，停止唤醒",
        )
        self._notify(entry)

    def _finish(self, entry: dict, *, delivered: bool) -> None:
        if entry.get("cancelled"):
            return
        now = self._clock()
        entry["pending"] = False
        entry["delivered"] = bool(delivered)
        entry["delivered_at"] = _iso(now) if delivered else None
        entry["next_wake_at"] = None
        if delivered:
            entry["error"] = None
        self._save()
        self._log(
            "info",
            f"{entry.get('thread_id', '')[:8]} 已出队，执行待确认（看护确认队列行已消失）",
        )
        self._notify(entry)

    def _notify(self, entry: dict) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(dict(entry))
        except Exception as exc:  # noqa: BLE001 - display bookkeeping only
            self._log("error", f"更新续跑历史失败: {exc}")

    # ------------------------------------------------------- persistence
    def _load(self) -> list[dict]:
        if self._state_path is None:
            return []
        try:
            raw = self._state_path.read_text(encoding="utf-8")
        except OSError:
            return []
        try:
            payload = json.loads(raw)
        except ValueError:
            return []
        items = payload.get("entries") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            return []
        now = self._clock()
        entries: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if not item.get("thread_id") or not item.get("queued_id"):
                continue
            finished_at = item.get("delivered_at") or item.get("abandoned_at")
            if not item.get("pending") and finished_at:
                try:
                    age = now - datetime.fromisoformat(finished_at).timestamp()
                except ValueError:
                    age = 0
                if age > KEEP_FINISHED_SECONDS:
                    continue
            entries.append(item)
        return entries[-MAX_ENTRIES:]

    def _save(self) -> None:
        if self._state_path is None:
            return
        with self._lock:
            entries = [dict(item) for item in self._entries[-MAX_ENTRIES:]]
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps({"entries": entries}, ensure_ascii=False, indent=2)
                + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, self._state_path)
        except OSError:
            pass


def _compact(wake: dict | None) -> dict | None:
    if not isinstance(wake, dict):
        return None
    keys = ("ok", "skipped", "reason", "error", "url", "duration_ms")
    return {key: wake[key] for key in keys if wake.get(key) is not None}
