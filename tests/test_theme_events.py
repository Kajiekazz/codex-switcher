"""``GET /api/theme/events`` — the SSE stream that keeps the side panel in sync.

The panel used to poll ``/api/theme`` every 2 s, which is why a dashboard theme
click took up to two seconds to reach it ("慢一拍"). The service now pushes the
new state to every subscriber instead.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from codex_switcher import settings
from codex_switcher.api import create_server
from codex_switcher.store import AccountStore

from test_api import _FakeDesktop, fake_auth_file, free_port


class ThemeEventStreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.auth_path = self.root / "auth.json"
        fake_auth_file(self.auth_path)
        os.environ["CODEX_SWITCHER_DATA"] = str(self.root / "data")
        os.environ["CODEX_SWITCHER_AUTH"] = str(self.auth_path)
        os.environ["CODEX_SWITCHER_ENABLE_LOGIN"] = "0"
        os.environ["CODEX_SWITCHER_ENABLE_SWITCH"] = "0"
        settings.reset_cache()
        self.store = AccountStore(self.root / "accounts.sqlite3")
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.server = create_server(
            self.store,
            self.auth_path,
            "127.0.0.1",
            self.port,
            desktop_client=_FakeDesktop(),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        for _ in range(100):
            try:
                urllib.request.urlopen(self.base + "/api/health", timeout=2).read()
                break
            except Exception:
                time.sleep(0.05)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()
        self.tmp.cleanup()
        settings.reset_cache()

    # ------------------------------------------------------------------ utils
    def _post_theme(self, theme: str) -> dict:
        request = urllib.request.Request(
            self.base + "/api/theme",
            data=json.dumps({"theme": theme}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return json.loads(urllib.request.urlopen(request, timeout=10).read())

    def _read_data(self, stream, timeout: float = 5.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = stream.readline()
            if not line:
                self.fail("stream closed")
            if line.startswith(b"data: "):
                return json.loads(line[len(b"data: "):])
        self.fail("no data frame before the timeout")

    # ------------------------------------------------------------------ tests
    def test_stream_opens_with_the_current_theme(self):
        stream = urllib.request.urlopen(
            self.base + "/api/theme/events", timeout=10
        )
        self.addCleanup(stream.close)
        self.assertTrue(stream.readline().startswith(b"retry:"))
        self.assertEqual(stream.readline().strip(), b"")
        payload = self._read_data(stream)
        self.assertIn(payload["theme"], ("light", "dark", "auto"))
        self.assertEqual(payload["themes"], ["light", "dark", "auto"])

    def test_theme_change_is_pushed_without_polling(self):
        stream = urllib.request.urlopen(
            self.base + "/api/theme/events", timeout=10
        )
        self.addCleanup(stream.close)
        stream.readline()  # retry:
        stream.readline()  # blank
        self._read_data(stream)  # initial state

        health = json.loads(
            urllib.request.urlopen(self.base + "/api/health", timeout=10).read()
        )
        self.assertGreaterEqual(health.get("theme_subscribers", 0), 1)

        response = self._post_theme("dark")
        self.assertTrue(response["ok"])
        self.assertEqual(response["saved"], "dark")
        pushed = self._read_data(stream)
        self.assertEqual(pushed["theme"], "dark")

        self._post_theme("light")
        pushed = self._read_data(stream)
        self.assertEqual(pushed["theme"], "light")

    def test_publishing_without_subscribers_is_harmless(self):
        health = json.loads(
            urllib.request.urlopen(self.base + "/api/health", timeout=10).read()
        )
        self.assertEqual(health.get("theme_subscribers"), 0)
        self.assertTrue(self._post_theme("light")["ok"])


if __name__ == "__main__":
    unittest.main()
