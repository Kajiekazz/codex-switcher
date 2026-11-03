"""One row per Switcher-managed chat; transport and execution stay distinct."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime

from . import config


def _time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0


def thread_labels(ids):
    """Read display metadata for historical chats outside the current list."""
    if not ids:
        return {}
    try:
        with closing(sqlite3.connect(config.state_db_path().resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
            conn.row_factory = sqlite3.Row
            columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
            fields = [key for key in ("id", "name", "title", "cwd", "archived") if key in columns]
            if "id" not in fields:
                return {}
            rows = conn.execute("SELECT " + ",".join(fields) + " FROM threads WHERE id IN ("
                                + ",".join("?" for _ in ids) + ")", list(ids)).fetchall()
        return {row["id"]: {**dict(row), "title": dict(row).get("name") or dict(row).get("title") or row["id"][:8]}
                for row in rows}
    except (sqlite3.Error, OSError):
        return {}


def _classify(result, goal, current, pause, dispatching):
    receipt = result.get("execution") or {}
    state = receipt.get("state")
    matched = bool(receipt.get("turn_id"))
    after_start = _time(current.get("at")) >= _time(receipt.get("started_at")) > 0
    # Only goal-triggered successors belong to this continuation. A later
    # manual turn in the same chat is not a Switcher execution receipt.
    successor = (matched and after_start and current.get("trigger") in {"goal", "goal_continuation"}
                 and goal and current.get("turn_id") != receipt.get("turn_id"))
    own_running = state == "running" or (successor and current.get("state") == "running")
    if pause:
        if current.get("state") == "running":
            return "running", "running", "暂停未完成 · 当前回合仍在运行" if pause.get("error") else "后续续跑已暂停 · 当前回合待停止"
        if dispatching:
            return "waiting", "cancelling", "正在取消投递"
        if result.get("pending") and not result.get("owned_queue"):
            return "attention", "external_queue", "后续恢复已暂停 · 仍有外部排队消息"
        stopped = current.get("state") in {"ended", "interrupted"} and _time(current.get("at")) >= _time(pause.get("at"))
        uncertain = state in {"confirming", "started", "unconfirmed"} and not result.get("cancelled")
        if (pause.get("needs_manual_stop") or uncertain) and not stopped:
            return "attention", "stop_unconfirmed", "后续续跑已暂停 · 停止待确认"
        if pause.get("error"):
            return "attention", "pause_failed", "暂停未完成"
        return "recent", "paused", "已暂停"
    if result.get("cancelled") or result.get("skipped_by_user"):
        return "recent", "cancelled", "已取消本次续跑"
    if result.get("queue_abandoned") or current.get("archived") or (goal or {}).get("archived"):
        return "attention", "archived", "会话已归档"
    if result.get("queue_expired"):
        return "attention", "unconfirmed", "桌面端未取走消息"
    if dispatching:
        return "waiting", "sending", "等待投递" if not result.get("queued_id") else "正在投递"
    if result.get("pending"):
        return "waiting", "queued", "已排队"
    if own_running:
        return "running", "running", "正在续跑"
    goal_status = (goal or {}).get("status")
    if matched and goal_status in {"paused", "complete", "budget_limited", "blocked", "usage_limited"}:
        if state in {"completed", "interrupted"} or successor:
            if goal_status == "complete":
                return "recent", "completed", "长期目标已完成"
            if goal_status == "paused":
                return "recent", "paused", "长期目标已暂停"
            return "attention", "blocked", "额度受限" if goal_status == "usage_limited" else "长期目标受限"
    later_manual = after_start and current.get("turn_id") != receipt.get("turn_id") and not successor
    if matched and goal_status == "active" and not later_manual and (state == "completed" or successor):
        if current.get("state") == "unknown" and current.get("reason") == "writer_not_confirmed":
            return "attention", "started", "运行状态待确认"
        return "waiting", "waiting_goal", "等待目标继续"
    if state == "completed":
        return "recent", "completed", "本次续跑已结束"
    if state == "interrupted":
        return "recent", "interrupted", "本次续跑已中断"
    if state == "failed":
        return "attention", "failed", "本次续跑失败"
    if state in {"started", "unconfirmed"}:
        return "attention", state, "运行状态待确认" if state == "started" else "未确认启动"
    if state == "confirming":
        return "waiting", "confirming", "启动确认中"
    if not result.get("ok"):
        return "attention", "failed", "投递失败"
    # No receipt means no claim of successful execution, even for legacy jobs.
    return "attention", "unconfirmed", "未确认启动"


def build_queue(*, history, latest, pending, job, goals, items, execution, pauses):
    attempts = {}
    for entry in history:  # newest first
        if entry.get("dry_run"):
            continue
        results = {row.get("thread_id"): row for row in entry.get("results", [])}
        for tid in dict.fromkeys(entry.get("thread_ids", []) + list(results)):
            if not tid:
                continue
            result = results.get(tid) or {}
            attempts.setdefault(tid, []).append({
                **result, "job_id": entry.get("id"), "requested_at": entry.get("started_at"),
                "finished_at": entry.get("finished_at"), "dispatch_state": entry.get("state"),
                "continue_once": entry.get("continue_once", True),
                "started_via": result.get("started_via") or entry.get("started_via"),
            })
    outstanding = set()
    if job and not job.get("dry_run") and job.get("state") == "running":
        finished = {row.get("thread_id") for row in job.get("results", [])}
        outstanding = set(job.get("thread_ids", [])) - finished - set(job.get("skipped", []))
        for tid in outstanding:
            attempts.setdefault(tid, [{"requested_at": job.get("started_at"), "job_id": job.get("id"),
                                       "started_via": job.get("started_via"), "continue_once": job.get("continue_once", True)}])
    labels = thread_labels([tid for tid in attempts if tid not in items])
    goal_index = {row["thread_id"]: row for row in goals}
    rows = []
    for tid, records in attempts.items():
        pause = pauses.get(tid)
        if pause and pause.get("removed_job_id") and pause["removed_job_id"] == records[0].get("job_id"):
            continue
        previous = latest.get(tid) or {}
        result = {**records[0], **(previous if previous.get("job_id") == records[0].get("job_id") else {})}
        if tid in pending:
            # A newer failed request must not conceal an older owned message.
            result.update(pending[tid])
        info, goal = items.get(tid) or labels.get(tid) or {}, goal_index.get(tid)
        current, pause = execution.get(tid) or {}, pauses.get(tid)
        group, state, label = _classify(result, goal, current, pause, tid in outstanding)
        receipt = result.get("execution") or {}
        automatic = str(result.get("started_via") or "").startswith(("switch", "quota_recovery:"))
        recovered_quota = str(result.get("started_via") or "").startswith("quota_recovery:")
        rows.append({
            "thread_id": tid, "title": info.get("title") or (goal or {}).get("thread_name")
            or (goal or {}).get("thread_title") or tid[:8],
            "cwd": info.get("cwd") or (goal or {}).get("cwd"), "project_name": info.get("project_name"),
            "group": group, "state": state, "label": label,
            "source": "automatic" if automatic else "manual", "continue_once": result.get("continue_once", True),
            "source_label": "额度恢复续跑" if recovered_quota else "切号自动恢复" if automatic else "手动续跑",
            "requested_at": result.get("queued_at") or result.get("requested_at"),
            "started_at": receipt.get("started_at"), "finished_at": receipt.get("completed_at"),
            "activity_at": current.get("at") or receipt.get("completed_at") or receipt.get("started_at")
            or result.get("requested_at"),
            "goal_status": (goal or {}).get("status"), "objective": (goal or {}).get("objective"),
            "paused": bool(pause), "pause": pause,
            "needs_manual_stop": bool(pause and (current.get("state") == "running" or state == "stop_unconfirmed")),
            "can_cancel": bool(not pause and ((result.get("owned_queue") and result.get("pending")) or tid in outstanding)),
            "can_pause": bool((not pause and (group in {"waiting", "running"} or (goal or {}).get("status") == "active")) or (pause or {}).get("error")),
            "can_retry": bool(group == "attention" and not result.get("pending") and current.get("state") != "running"
                              and not current.get("archived") and state != "archived" and not pause),
            "can_resume": bool(group == "recent" and current.get("state") != "running" and not current.get("archived")),
            "can_remove": True,
            "error": (pause or {}).get("error") or result.get("error") or receipt.get("error_code"),
            "attempts": records[:10], "result": result,
        })
    rows.sort(key=lambda row: _time(row.get("requested_at")), reverse=True)
    counts = {key: sum(row["group"] == key for row in rows) for key in ("waiting", "running", "attention", "recent")}
    # Keep every actionable chat; only the historical tail is bounded.
    recent = [row for row in rows if row["group"] == "recent"][:20]
    return {"items": [row for row in rows if row["group"] != "recent"] + recent, "counts": counts,
            "active": counts["waiting"] + counts["running"], "parallel": True}
