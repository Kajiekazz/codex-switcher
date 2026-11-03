import json
import base64
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

    def test_code_submission_requires_current_account_step_and_origin(self):
        from codex_switcher.login import LoginSession
        session = LoginSession("fixture-login", "one@example.com", self.root, driver="playwright")
        session.waiting_for_input("等待验证码")
        session.offer_code("email", 6, "fixture-document")
        self.server.login_manager._sessions[session.id] = session
        body = {"confirm": True, "session_id": session.id, "challenge_id": session.challenge["id"],
                "email": session.email, "code": "123456"}
        self.assertEqual(self.request("/api/login/code", body, {"Origin": "https://external.example"})[0], 403)
        self.assertEqual(self.request("/api/login/code", {**body, "email": "other@example.com"})[0], 409)
        self.assertEqual(self.request("/api/login/code", {**body, "challenge_id": "stale"})[0], 409)
        self.assertEqual(self.request("/api/login/code", {**body, "code": "123"})[0], 400)
        code, reply = self.request("/api/login/code", body)
        self.assertEqual(code, 200)
        self.assertTrue(reply["session"]["challenge"]["submitting"])
        self.assertNotIn("123456", json.dumps(reply))
        self.assertEqual(self.request("/api/login/code", body)[0], 409)
        session.clear_challenge()
        session.status = "cancelled"

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

    def local_auth(self):
        claims = base64.urlsafe_b64encode(json.dumps({"email": "fixture@example.com", "exp": 4102444800}).encode()).decode().rstrip("=")
        token = "e30." + claims + ".fixture"
        path = self.root / "auth.json"
        path.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": token, "id_token": token, "account_id": "fixture-account"}}), encoding="utf-8")
        return path.read_bytes()

    def test_local_import_is_confirmed_deduplicated_and_never_replaces_live_auth(self):
        before = self.local_auth()
        state = self.request("/api/setup/status")[1]
        body = {"confirm": True, "import_local": True, "local_fingerprint": state["local_account"]["fingerprint"]}
        code, result = self.request("/api/setup/import", body)
        self.assertEqual((code, result["inserted"]), (200, 1))
        self.assertEqual(result["setup"]["accounts"]["ready"], 1)
        self.assertEqual(self.request("/api/setup/import", body)[1]["local_import"]["result"], "unchanged")
        self.assertEqual((self.root / "auth.json").read_bytes(), before)
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.request("/api/setup/import", {**body, "local_fingerprint": "stale"})[0], 400)

    def test_components_precede_automatic_import_and_manual_mode_can_skip_them(self):
        text = "fixture@example.com|fixture-password"
        body = {"confirm": True, "text": text}
        self.assertEqual(self.request("/api/setup/import", body)[0], 400)
        self.assertEqual(self.store.count(), 0)
        self.setup.progress(browser_mode="manual", step=2)
        settings.save({"enable_login": True, "login_driver": "manual"})
        with mock.patch.object(self.server.login_manager, "enqueue", return_value={"email": "fixture@example.com", "state": "queued"}) as queued:
            self.assertEqual(self.request("/api/setup/import", body)[0], 200)
        queued.assert_called_once_with("fixture@example.com", source="import")
        self.components.start.assert_not_called()

    def test_progress_persists_and_security_actions_reject_external_origins(self):
        self.assertEqual(self.request("/api/setup/progress", {"step": 2})[0], 400)
        self.assertEqual(self.request("/api/setup/progress", {"confirm": True, "step": 2, "browser_mode": "manual", "local_selected": False})[0], 200)
        state = self.request("/api/setup/status")[1]
        self.assertEqual((state["step"], state["browser_mode"], state["local_selected"]), (2, "manual", False))
        for path in ("/api/setup/progress", "/api/setup/import", "/api/accounts/totp/setup", "/api/accounts/totp/import"):
            self.assertEqual(self.request(path, {"confirm": True}, {"Origin": "https://external.example"})[0], 403)

    def test_manual_totp_accepts_a_seed_or_qr_and_never_returns_the_secret(self):
        from test_totp_setup import SEED, URI, FIXTURE
        self.store.upsert({"email": "fixture@example.com", "password": "keep"})
        self.assertEqual(self.request("/api/accounts/totp/import", {"email": "fixture@example.com", "totp": URI})[0], 400)
        body = {"confirm": True, "email": "fixture@example.com"}
        self.assertEqual(self.request("/api/accounts/totp/import", {**body, "totp": "234567"})[0], 400)
        for value in ({"totp": URI}, {"image": base64.b64encode(FIXTURE.read_bytes()).decode()}):
            code, result = self.request("/api/accounts/totp/import", {**body, **value})
            self.assertEqual(code, 200)
            self.assertNotIn(SEED, json.dumps(result))
            self.assertTrue(result["account"]["has_totp"])
            self.assertEqual(self.store.get_secret("fixture@example.com"), {"password": "keep", "totp": SEED})


if __name__ == "__main__":
    unittest.main()
