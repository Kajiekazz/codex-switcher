"""Shared quota decisions. Unknown fields never grant usable quota."""
import math


def number(value, *, minimum=0, maximum=None):
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result < minimum or (maximum is not None and result > maximum):
        return None
    return result


def usage_state(quota):
    if not isinstance(quota, dict) or not quota.get("ok"):
        return "unknown"
    if quota.get("limit_reached") is True or quota.get("allowed") is False:
        return "exhausted"
    if quota.get("allowed") is True:
        return "available"
    return "unknown"


def remaining(window):
    used = number(window.get("used_percent"), maximum=100) if isinstance(window, dict) else None
    return None if used is None else 100 - used


def comparison_group(quota):
    """Percentages are comparable only within the same plan/window shape."""
    return (str(quota.get("plan") or "unknown").casefold(), *(
        ((quota.get(key) or {}).get("window_seconds"), remaining(quota.get(key)) is not None)
        for key in ("primary_window", "secondary_window")
    ))
