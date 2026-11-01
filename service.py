#!/usr/bin/env python3
"""Start the Codex Switcher local service.

v0.4 adds read-only quota display, opt-in auto-switching, a tray helper and a
Chrome extension. Manual switching is still opt-in: set
CODEX_SWITCHER_ENABLE_SWITCH=1 (or use start.ps1 -EnableSwitch). Every switch
backs up the live auth.json, writes atomically, verifies, and rolls back on
failure. Auto-switching additionally requires
CODEX_SWITCHER_ENABLE_AUTO_SWITCH=1 and uses cooldown/cap/circuit-breaker
guards. The login assistant remains opt-in and uses an isolated CODEX_HOME.
"""

from codex_switcher.api import run

if __name__ == "__main__":
    run()
