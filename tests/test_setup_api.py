import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from codex_switcher import settings
from codex_switcher.api import create_server
from codex_switcher.onboarding import Onboarding
from codex_switcher.startup import StartupRegistration
from codex_switcher.store import AccountStore
from test_onboarding import FakeRegistry


class SetupApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"CODEX_SWITCHER_DATA": str(self.root / "data"),
                                               "CODEX_SWITCHER_AUTH": str(self.root / "auth.json"),
                                               "CODEX_SWITCHER_ENABLE_AUTO_SWITCH": "0"})
        self.env.start()
        settings.reset_cache()
        self.store = AccountStore(self.root / "accounts.sqlite3")
        self.components = mock.Mock()
        self.components.status.return_value = {"ready": False, "state": "missing"}
        self.components.start.return_value = {"ready": False, "state": "installing"}
        self.setup = Onboarding(self.store, self.components, StartupRegistration(FakeRegistry(), ["app.exe"]))
        self.runtime = mock.Mock(token="test-instance-token")
        self.server = create_server(self.store, self.root / "auth.json", "127.0.0.1", 0,
                                    onboarding=self.setup, app_runtime=self.runtime)
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.store.close()
        self.env.stop()
        settings.reset_cache()
        self.tmp.cleanup()

    def request(self, path, body=None, headers=None):
        request = urllib.request.Request(self.base + path,
                                         data=None if body is None else json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", **(headers or {})})
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    def test_setup_read_does_not_download_or_enable_startup(self):
        code, data = self.request("/api/setup/status")
        self.assertEqual(code, 200)
        self.assertTrue(data["show_guide"])
        self.assertFalse(data["startup"]["enabled"])
        self.components.start.assert_not_called()

    def test_startup_requires_boolean_and_is_immediately_shared(self):
        self.assertEqual(self.request("/api/app/startup", {"enabled": True})[0], 400)
        self.assertEqual(self.request("/api/app/startup", {"confirm": True, "enabled": "false"})[0], 400)
        self.assertEqual(self.request("/api/app/startup", {"confirm": True, "enabled": True})[0], 200)
        self.assertTrue(self.setup.startup.status()["enabled"])
        self.setup.startup.set_enabled(False)
        self.assertFalse(self.request("/api/app/startup")[1]["enabled"])

    def test_browser_download_is_explicit_and_later_persists(self):
        code, data = self.request("/api/setup/browser", {"confirm": True})
        self.assertEqual((code, data["browser"]["state"]), (200, "installing"))
        self.components.start.assert_called_once()
        self.assertEqual(self.request("/api/setup/finish", {"confirm": True, "action": "complete"})[0], 400)
        self.assertEqual(self.request("/api/setup/finish", {"confirm": True, "action": "later"})[0], 200)
        self.assertFalse(self.request("/api/setup/status")[1]["show_guide"])

    def test_external_origins_cannot_change_windows_startup_or_open_a_picker(self):
        for path, body in (("/api/app/startup", {"enabled": True}), ("/api/setup/codex", {})):
            code, _ = self.request(path, {"confirm": True, **body}, {"Origin": "https://external.example"})
            self.assertEqual(code, 403)
        self.assertFalse(self.setup.startup.status()["enabled"])

    def test_exit_requires_the_current_instance_token(self):
        self.assertEqual(self.request("/api/app/exit", {"confirm": True})[0], 403)
        self.runtime.stop.assert_not_called()
        self.assertEqual(self.request("/api/app/exit", {"confirm": True}, {"X-Cs-Instance": self.runtime.token})[0], 200)
        self.runtime.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
