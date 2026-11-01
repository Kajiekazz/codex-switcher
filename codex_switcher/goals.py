"""Read-only view of Codex thread goals.

The Codex app-server owns ``goals_1.sqlite``; this module never writes it.
All mutations (resume/pause/complete) go through the app-server JSON-RPC API
so the live runtime and the on-disk state stay consistent.

Display metadata (thread name/title/cwd) lives in ``state_5.sqlite``; it is
joined here so the dashboard can offer a human-readable session picker.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config

# DB status -> app-server ThreadGoalStatus enum value.
DB_TO_RPC_STATUS = {
    "active": "active",
    "paused": "paused",
    "blocked": "blocked",
    "usage_limited": "usageLimited",
    "budget_limited": "budgetLimited",
    "complete": "complete",
}

RPC_TO_DB_STATUS = {value: key for key, value in DB_TO_RPC_STATUS.items()}

#: Goal states a resume/switch may wake up. ``usage_limited`` is the classic
#: one; ``paused`` means the user stopped it, and ``active`` with no live turn is
#: what a desktop restart leaves behind. Completed/blocked goals stay untouched.
ARMABLE_STATUSES = {"usage_limited", "paused", "active"}


def _connect(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(str(path), timeout=3.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=3000")
        conn.execute("PRAGMA query_only=ON")
        return conn
    except sqlite3.Error:
        return None


def _iso_from_ms(value) -> str | None:
    try:
        if value is None:
            return None
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).isoformat(
            timespec="seconds"
        )
    except (TypeError, ValueError, OSError):
        return None


def _thread_rows() -> dict[str, dict]:
    conn = _connect(config.state_db_path())
    if conn is None:
        return {}
    try:
        rows = conn.execute(
            """
            SELECT id, name, title, preview, cwd, model, archived,
                   updated_at_ms
              FROM threads
            """
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    result = {}
    for row in rows:
        result[row["id"]] = {
            "name": row["name"] or "",
            "title": row["title"] or "",
            "preview": row["preview"] or "",
            "cwd": row["cwd"] or "",
            "model": row["model"] or "",
            "archived": bool(row["archived"]),
            "thread_updated_at": _iso_from_ms(row["updated_at_ms"]),
        }
    return result


def list_goals() -> list[dict]:
    """Return all goals newest-first, joined with thread display metadata."""
    conn = _connect(config.goals_db_path())
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            SELECT thread_id, goal_id, objective, status, token_budget,
                   tokens_used, time_used_seconds, created_at_ms, updated_at_ms
              FROM thread_goals
             ORDER BY updated_at_ms DESC
            """
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    threads = _thread_rows()
    now = int(datetime.now(timezone.utc).timestamp())
    goals = []
    for row in rows:
        thread = threads.get(row["thread_id"], {})
        status = row["status"] or "active"
        goals.append(
            {
                "thread_id": row["thread_id"],
                "goal_id": row["goal_id"],
                "objective": row["objective"] or "",
                "status": status,
                "status_rpc": DB_TO_RPC_STATUS.get(status, status),
                "resumable": status == "usage_limited",
                # Also resumable in practice: a paused goal or one that still
                # says active with no turn running (e.g. after a desktop restart
                # triggered by a switch) just needs to be woken up again.
                "armable": status in ARMABLE_STATUSES,
                "token_budget": row["token_budget"],
                "tokens_used": row["tokens_used"],
                "time_used_seconds": row["time_used_seconds"],
                "created_at": _iso_from_ms(row["created_at_ms"]),
                "updated_at": _iso_from_ms(row["updated_at_ms"]),
                "thread_name": thread.get("name", ""),
                "thread_title": thread.get("title", ""),
                "thread_preview": thread.get("preview", ""),
                "cwd": thread.get("cwd", ""),
                "model": thread.get("model", ""),
                "archived": thread.get("archived", False),
                "thread_updated_at": thread.get("thread_updated_at"),
                "age_seconds": max(
                    0, now - int((row["updated_at_ms"] or 0) / 1000)
                )
                if row["updated_at_ms"]
                else None,
            }
        )
    return goals


def get_goal(thread_id: str) -> dict | None:
    for goal in list_goals():
        if goal["thread_id"] == thread_id:
            return goal
    return None


SESSION_LIMIT = 500


def _normalise_path(value: str) -> str:
    text = (value or "").strip()
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return text.replace("/", "\\").rstrip("\\").lower()


def _projects() -> dict[str, dict]:
    """Project id -> {name, position, roots} from ``state_5.sqlite``."""
    conn = _connect(config.state_db_path())
    if conn is None:
        return {}
    try:
        rows = conn.execute(
            "SELECT id, name, position FROM projects"
        ).fetchall()
        roots = conn.execute(
            "SELECT project_id, path FROM project_roots ORDER BY position"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    result = {}
    for row in rows:
        result[row["id"]] = {
            "name": row["name"] or "",
            "position": row["position"] or 0,
            "roots": [],
            "pinned": False,
        }
    for row in roots:
        project = result.get(row["project_id"])
        if project is not None and row["path"]:
            project["roots"].append(row["path"])
    return result


def _pinned_section_ids() -> set[str]:
    conn = _connect(config.state_db_path())
    if conn is None:
        return set()
    try:
        rows = conn.execute("SELECT id, name FROM thread_sections").fetchall()
    except sqlite3.Error:
        return set()
    finally:
        conn.close()
    return {
        row["id"]
        for row in rows
        if (row["name"] or "").strip().lower() in ("pinned", "置顶")
    }


def _match_project(cwd: str, projects: dict[str, dict]) -> str | None:
    """Longest project root that contains ``cwd``."""
    path = _normalise_path(cwd)
    if not path:
        return None
    best: tuple[int, str] | None = None
    for project_id, project in projects.items():
        for root in project["roots"]:
            normalized = _normalise_path(root)
            if not normalized:
                continue
            if path == normalized or path.startswith(normalized + "\\"):
                score = len(normalized)
                if best is None or score > best[0]:
                    best = (score, project_id)
    return best[1] if best else None


def _empty_sessions() -> dict:
    return {
        "total": 0,
        "shown": 0,
        "truncated": False,
        "pinned": [],
        "projects": [],
        "recent": [],
        "counts": {
            "pinned": 0,
            "projects": 0,
            "recent": 0,
            "goals": 0,
            "subagents": 0,
        },
    }


#: Thread origins that are always the user's own sessions, even when Codex
#: wrote ``thread_source='subagent'`` by mistake (seen on VS Code threads).
USER_FACING_ORIGINS = {"exec", "vscode", "cli", "app", "desktop", "tui", "web", "api"}


def _is_internal_thread(source: str, thread_source: str) -> bool:
    """True for agent-spawned / guardian threads that belong to a parent run.

    ``thread_source`` alone is not trustworthy: real data has VS Code sessions
    (``source='vscode'``, user-written prompts, no spawn metadata) tagged
    ``subagent``. A plain origin string therefore wins over the column, while
    the JSON ``{"subagent": {...}}`` markers stay authoritative for spawning
    and guardian reviews.
    """
    origin = (source or "").strip()
    if origin and not origin.startswith("{"):
        return origin.lower() not in USER_FACING_ORIGINS
    if '"subagent"' in origin or '"guardian"' in origin:
        return True
    return (thread_source or "").strip() in ("subagent", "guardian_review")


def _lock_dir() -> Path:
    return Path(config.goals_db_path()).parent / "thread-writer-locks"


def held_thread_ids() -> set[str]:
    """Thread ids whose writer lock is held by a live process right now.

    Codex guards each thread's rollout with ``<thread-id>.lock`` under
    ``~/.codex/thread-writer-locks``. The holder keeps the file open without
    sharing, so opening it the same way fails with ERROR_SHARING_VIOLATION (32).
    That is how the dashboard can say "the desktop has this conversation open"
    *before* a resume runs into ``already has an active writer``.

    Read-only: ``OPEN_EXISTING`` never creates a file, and the handle is closed
    immediately when the probe wins.
    """
    if os.name != "nt":
        return set()
    directory = _lock_dir()
    try:
        paths = list(directory.glob("*.lock"))
    except OSError:
        return set()
    if not paths:
        return set()
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    invalid = wintypes.HANDLE(-1).value
    held: set[str] = set()
    for path in paths:
        name = path.name
        if not name.endswith(".lock") or name.startswith("."):
            continue
        handle = kernel32.CreateFileW(
            str(path), 0x80000000 | 0x40000000, 0, None, 3, 0, None
        )
        if handle == invalid:
            if ctypes.get_last_error() == 32:  # ERROR_SHARING_VIOLATION
                held.add(name[: -len(".lock")])
            continue
        kernel32.CloseHandle(handle)
    return held


def list_sessions(
    include_subagents: bool = False, limit: int = SESSION_LIMIT
) -> dict:
    """Codex-style thread browser: pinned / projects / recent.

    Threads (not goals) drive the sections, so sessions without a goal are
    listed too; ``has_goal`` / ``goal_status`` mark the ones that have one.
    Agent-spawned and guardian-review threads are hidden by default (they are
    the bulk of ``threads``), but a thread with a goal or one the user pinned
    is always listed.
    """
    conn = _connect(config.state_db_path())
    if conn is None:
        return _empty_sessions()
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(threads)").fetchall()
        }

        def expr(name: str, default: str) -> str:
            return name if name in columns else f"{default} AS {name}"

        if "updated_at_ms" in columns and "updated_at" in columns:
            updated_expr = "COALESCE(updated_at_ms, updated_at * 1000)"
        elif "updated_at_ms" in columns:
            updated_expr = "updated_at_ms"
        elif "updated_at" in columns:
            updated_expr = "updated_at * 1000"
        else:
            updated_expr = "0"

        selected = ", ".join(
            [
                "id",
                expr("name", "''"),
                expr("title", "''"),
                expr("preview", "''"),
                expr("first_user_message", "''"),
                expr("cwd", "''"),
                expr("git_branch", "''"),
                expr("model", "''"),
                expr("tokens_used", "0"),
                expr("is_pinned", "0"),
                expr("thread_section_id", "NULL"),
                expr("thread_source", "NULL"),
                expr("source", "''"),
                expr("agent_nickname", "NULL"),
                expr("recency_at_ms", "0"),
                expr("attached_thread_parent_id", "NULL"),
                f"{updated_expr} AS updated_ms",
            ]
        )
        where = "WHERE COALESCE(archived, 0) = 0" if "archived" in columns else ""
        rows = conn.execute(
            f"SELECT {selected} FROM threads {where} "
            f"ORDER BY updated_ms DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM threads {where}"
        ).fetchone()[0]
    except sqlite3.Error:
        return _empty_sessions()
    finally:
        conn.close()

    projects = _projects()
    pinned_sections = _pinned_section_ids()
    goals_by_thread = {goal["thread_id"]: goal for goal in list_goals()}
    held_threads = held_thread_ids()
    now = int(datetime.now(timezone.utc).timestamp())

    pinned: list[dict] = []
    recent: list[dict] = []
    grouped: dict[str, list[dict]] = {}
    hidden_subagents = 0
    for row in rows:
        goal = goals_by_thread.get(row["id"]) or {}
        source = (row["thread_source"] or "").strip()
        pinned_flag = bool(row["is_pinned"]) or (
            row["thread_section_id"] in pinned_sections
        )
        is_internal = _is_internal_thread(row["source"], source)
        if is_internal and not goal and not pinned_flag and not include_subagents:
            hidden_subagents += 1
            continue
        updated_ms = row["updated_ms"] or 0
        title = (
            row["name"]
            or row["title"]
            or row["first_user_message"]
            or row["preview"]
            or row["agent_nickname"]
            or ""
        )
        item = {
            "thread_id": row["id"],
            "title": title,
            "preview": (row["preview"] or row["first_user_message"] or "")[:160],
            "cwd": row["cwd"] or "",
            "git_branch": row["git_branch"] or "",
            "model": row["model"] or "",
            "tokens_used": row["tokens_used"] or 0,
            "updated_at": _iso_from_ms(updated_ms) if updated_ms else None,
            "age_seconds": max(0, now - int(updated_ms / 1000))
            if updated_ms
            else None,
            "recency_seconds": max(
                0, now - int((row["recency_at_ms"] or 0) / 1000)
            )
            if row["recency_at_ms"]
            else None,
            "thread_source": source,
            "agent_nickname": row["agent_nickname"] or "",
            "pinned": pinned_flag,
            "parent_thread_id": row["attached_thread_parent_id"] or "",
            # Some process (usually the desktop app showing this conversation)
            # holds the thread writer lock right now.
            "writer_busy": row["id"] in held_threads,
            "internal": is_internal,
            "thread_origin": row["source"] or "",
            "project_id": "",
            "project_name": "",
            "has_goal": bool(goal),
            "goal_status": goal.get("status"),
            "goal_status_rpc": goal.get("status_rpc"),
            "goal_objective": goal.get("objective") or "",
            "resumable": bool(goal.get("resumable")),
            "armable": bool(goal.get("armable")),
        }
        project_id = _match_project(row["cwd"] or "", projects)
        if project_id:
            item["project_id"] = project_id
            item["project_name"] = projects[project_id]["name"] or ""
        if item["pinned"]:
            pinned.append(item)
            # Codex shows a pinned conversation twice: once under 置顶 and once
            # inside its project. Mirroring that matters here because a forked
            # thread often reuses the parent's title — if it silently vanished
            # from 项目 the user would think a different session went missing.
            if project_id:
                grouped.setdefault(project_id, []).append(item)
        elif project_id:
            grouped.setdefault(project_id, []).append(item)
        else:
            recent.append(item)

    recent.sort(
        key=lambda item: (
            item["recency_seconds"]
            if item["recency_seconds"] is not None
            else 10**9
        )
    )

    project_list = []
    for project_id in sorted(
        grouped, key=lambda pid: (projects[pid]["position"], pid)
    ):
        meta = projects[project_id]
        name = meta["name"] or (
            Path(meta["roots"][0]).name if meta["roots"] else project_id
        )
        project_list.append(
            {
                "id": project_id,
                "name": name,
                "path": meta["roots"][0] if meta["roots"] else "",
                "count": len(grouped[project_id]),
                "items": grouped[project_id],
            }
        )

    shown_ids = {item["thread_id"] for item in pinned}
    shown_ids.update(item["thread_id"] for items in grouped.values() for item in items)
    shown_ids.update(item["thread_id"] for item in recent)

    return {
        "total": total,
        "shown": len(shown_ids),
        "truncated": total > len(rows),
        "include_subagents": bool(include_subagents),
        "pinned": pinned,
        "projects": project_list,
        "recent": recent,
        "counts": {
            "pinned": len(pinned),
            "projects": len(project_list),
            "recent": len(recent),
            "goals": len(goals_by_thread),
            "subagents": hidden_subagents,
        },
    }
