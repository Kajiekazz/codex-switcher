#!/usr/bin/env python3
"""Start the local API and management page for foreground debugging.

Use switcher.pyw for the normal tray launcher. Feature settings are shared
with the installed app and can be changed from the management page.
"""

from codex_switcher.api import run

if __name__ == "__main__":
    run()
