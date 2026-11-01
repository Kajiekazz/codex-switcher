"""Read-only burn-rate math for quota samples.

The dashboard and the optional predictive auto-switch both use this module.
It works on the sanitised sample rows produced by
:meth:`codex_switcher.store.AccountStore.quota_history` (percentages and reset
timestamps only) and never touches credentials.
"""

from __future__ import annotations

import time

PRIMARY_DEFAULT_WINDOW = 18000      # 5 hours
SECONDARY_DEFAULT_WINDOW = 604800   # 7 days


def _points(samples: list[dict], window: str) -> list[tuple[int, float, int | None, int | None]]:
    used_key = f"{window}_used"
    reset_key = f"{window}_reset_at"
    span_key = f"{window}_window_seconds"
    points: list[tuple[int, float, int | None, int | None]] = []
    for row in samples or []:
        used = row.get(used_key)
        if used is None:
            continue
        try:
            epoch = int(row.get("epoch") or 0)
        except (TypeError, ValueError):
            continue
        if epoch <= 0:
            continue
        reset_at = row.get(reset_key)
        try:
            reset_at = int(reset_at) if reset_at else None
        except (TypeError, ValueError):
            reset_at = None
        try:
            window_seconds = int(row.get(span_key)) if row.get(span_key) else None
        except (TypeError, ValueError):
            window_seconds = None
        points.append((epoch, float(used), reset_at, window_seconds))
    return points


def _last_segment(points: list[tuple[int, float, int | None, int | None]]):
    """Split on quota resets (a visible usage drop) and keep the newest run."""
    if not points:
        return []
    segment = [points[0]]
    for point in points[1:]:
        previous = segment[-1][1]
        if point[1] + 2 < previous:  # usage went down: new window
            segment = [point]
        else:
            segment.append(point)
    return segment


def _slope(points: list[tuple[int, float, int | None, int | None]]) -> float | None:
    n = len(points)
    if n < 2:
        return None
    mean_x = sum(p[0] for p in points) / n
    mean_y = sum(p[1] for p in points) / n
    denominator = sum((p[0] - mean_x) ** 2 for p in points)
    if denominator <= 0:
        return None
    numerator = sum((p[0] - mean_x) * (p[1] - mean_y) for p in points)
    return numerator / denominator


def predict_window(
    samples: list[dict],
    window: str = "primary",
    *,
    now: float | None = None,
    default_window_seconds: int | None = None,
) -> dict:
    """Predict when ``window`` reaches 100% based on the current window only."""
    now = float(now if now is not None else time.time())
    fallback = default_window_seconds or (
        PRIMARY_DEFAULT_WINDOW if window == "primary" else SECONDARY_DEFAULT_WINDOW
    )
    points = _points(samples, window)
    if not points:
        return {
            "available": False,
            "reason": "no_samples",
            "used_percent": None,
            "rate_per_hour": None,
            "eta_seconds": None,
            "projected_at": None,
            "sample_count": 0,
            "span_seconds": 0,
            "confidence": "none",
        }

    latest = points[-1]
    reset_at = latest[2]
    window_seconds = latest[3] or fallback

    # Use ``reset_at`` as a window marker only when it is stable. An idle
    # rolling window returns ``now + window`` on every call, so ``reset_at``
    # drifts one second per second; filtering on it would throw away every
    # earlier sample. When most recent samples share the same reset time the
    # window is active and old-window samples can be dropped safely.
    if reset_at is not None:
        same_window = [
            p for p in points if p[2] is not None and abs(p[2] - reset_at) <= 120
        ]
        if len(same_window) >= 3 and len(same_window) * 2 >= len(points):
            points = same_window

    segment = _last_segment(points)
    if len(segment) < 3:
        return {
            "available": False,
            "reason": "insufficient_samples",
            "used_percent": latest[1],
            "rate_per_hour": None,
            "eta_seconds": None,
            "projected_at": None,
            "sample_count": len(segment),
            "span_seconds": 0,
            "confidence": "low",
            "reset_at": reset_at,
            "window_seconds": window_seconds,
        }

    span = segment[-1][0] - segment[0][0]
    if span < 120:
        return {
            "available": False,
            "reason": "insufficient_span",
            "used_percent": segment[-1][1],
            "rate_per_hour": None,
            "eta_seconds": None,
            "projected_at": None,
            "sample_count": len(segment),
            "span_seconds": span,
            "confidence": "low",
            "reset_at": reset_at,
            "window_seconds": window_seconds,
        }

    slope = _slope(segment) or 0.0
    rate_per_hour = max(0.0, slope * 3600.0)
    used = segment[-1][1]
    if used >= 100:
        eta = 0.0
    elif slope <= 0:
        eta = None
    else:
        eta = (100.0 - used) / slope

    if len(segment) >= 8 and span >= 1800:
        confidence = "high"
    elif len(segment) >= 5 and span >= 600:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "available": True,
        "reason": None,
        "used_percent": round(used, 2),
        "rate_per_hour": round(rate_per_hour, 2),
        "eta_seconds": None if eta is None else int(round(eta)),
        "projected_at": None if eta is None else int(round(segment[-1][0] + eta)),
        "sample_count": len(segment),
        "span_seconds": int(span),
        "confidence": confidence,
        "reset_at": reset_at,
        "window_seconds": window_seconds,
        "updated_at": int(segment[-1][0]),
    }


def predict_quota(
    quota: dict | None,
    samples: list[dict],
    *,
    now: float | None = None,
) -> dict:
    """Combine both windows plus a short, human-oriented summary."""
    now = float(now if now is not None else time.time())
    primary = predict_window(samples, "primary", now=now)
    secondary = predict_window(samples, "secondary", now=now)
    soonest = None
    for name, prediction in (("primary", primary), ("secondary", secondary)):
        eta = prediction.get("eta_seconds")
        if eta is None:
            continue
        if soonest is None or eta < soonest[1]:
            soonest = (name, eta)
    return {
        "primary": primary,
        "secondary": secondary,
        "soonest_window": soonest[0] if soonest else None,
        "soonest_eta_seconds": soonest[1] if soonest else None,
        "limit_reached": bool((quota or {}).get("limit_reached")),
        "now": int(now),
    }
