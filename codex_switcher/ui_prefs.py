"""Shared UI preferences for the dashboard and the browser extension.

The dashboard used to keep its theme in ``localStorage`` only, and the side
panel lives on ``chrome-extension://`` — a completely different origin, so it
could never see that value. Storing the theme here (``ui-prefs.json`` in the
data dir) is what makes "the extension follows the dashboard" actually work.

Only presentation state lives in this file: no accounts, no tokens.
"""

from __future__ import annotations

import json
import os
import threading
import time

from . import config

THEMES = ("light", "dark", "auto")
DEFAULT_THEME = "light"

_LOCK = threading.Lock()


def _path():
    return config.data_dir() / "ui-prefs.json"


def _read() -> dict:
    try:
        with open(_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, path)


def theme() -> str:
    value = str(_read().get("theme") or "").strip().lower()
    return value if value in THEMES else DEFAULT_THEME


def set_theme(value: str) -> str:
    value = str(value or "").strip().lower()
    if value not in THEMES:
        raise ValueError("theme must be one of: " + ", ".join(THEMES))
    with _LOCK:
        data = _read()
        data["theme"] = value
        data["updated_at"] = int(time.time())
        _write(data)
    return value


def state() -> dict:
    data = _read()
    return {
        "theme": theme(),
        "updated_at": data.get("updated_at"),
        "themes": list(THEMES),
    }
