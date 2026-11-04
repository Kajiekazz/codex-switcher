import io
import base64
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from codex_switcher import config, settings
from codex_switcher.components import BrowserComponents, ComponentError
from codex_switcher.onboarding import Onboarding
from codex_switcher.startup import RUN_KEY, StartupRegistration
from codex_switcher.store import AccountStore


class FakeRegistry:
    HKEY_CURRENT_USER, KEY_SET_VALUE, REG_SZ = 1, 2, 3

    def __init__(self):
        self.values = {"AnotherApp": "unchanged"}

    def OpenKey(self, root, path):
        assert root == self.HKEY_CURRENT_USER and path == RUN_KEY
        return self

    def CreateKeyEx(self, root, path, reserved, flags):
        return self.OpenKey(root, path)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = value

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


class StartupTests(unittest.TestCase):
    def test_registration_quotes_the_executable_and_defaults_to_background(self):
        registry = FakeRegistry()
        startup = StartupRegistration(registry, [r"C:\Program Files\Codex Switcher\CodexSwitcher.exe"])
        self.assertFalse(startup.status()["enabled"])
        self.assertTrue(startup.set_enabled(True)["enabled"])
        self.assertEqual(registry.values["CodexSwitcher"], '"C:\\Program Files\\Codex Switcher\\CodexSwitcher.exe" --background --startup')
        # A change from the tray is read on the dashboard's next poll.
        other = StartupRegistration(registry, startup.command)
        other.set_enabled(False)
        self.assertFalse(startup.status()["enabled"])
        self.assertEqual(registry.values, {"AnotherApp": "unchanged"})

    def test_upgrades_only_the_existing_registration_for_this_installation(self):
        registry = FakeRegistry()
        startup = StartupRegistration(registry, ["app.exe"])
        startup.upgrade_command()
        self.assertNotIn("CodexSwitcher", registry.values)
        registry.values["CodexSwitcher"] = "other.exe --background"
        startup.upgrade_command()
        self.assertEqual(registry.values["CodexSwitcher"], "other.exe --background")
        registry.values["CodexSwitcher"] = "app.exe --background"
        startup.upgrade_command()
        self.assertEqual(registry.values["CodexSwitcher"], "app.exe --background --startup")

    def test_invalid_values_never_write_registry(self):
        registry = FakeRegistry()
        startup = StartupRegistration(registry, ["x" * 260])
        with self.assertRaises(ValueError):
            startup.set_enabled("false")
        with self.assertRaises(ValueError):
            startup.set_enabled(True)
        self.assertNotIn("CodexSwitcher", registry.values)


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"CODEX_SWITCHER_DATA": str(self.root / "data")})
        self.env.start()
        settings.reset_cache()
        self.store = AccountStore(self.root / "accounts.sqlite3")
        self.components = mock.Mock()
        self.components.status.return_value = {"state": "missing", "ready": False}
        self.auth_path = self.root / "auth.json"
        self.setup = Onboarding(self.store, self.components, StartupRegistration(FakeRegistry(), ["app.exe"]), auth_path=self.auth_path)
        self.detect = mock.patch("codex_switcher.config.find_codex_exe", return_value=None)
        self.detect.start()

    def tearDown(self):
        self.detect.stop()
        self.store.close()
        self.env.stop()
        settings.reset_cache()
        self.tmp.cleanup()

    def test_first_run_later_persists_without_marking_setup_complete(self):
        self.assertTrue(self.setup.status()["show_guide"])
        self.setup.finish("later")
        recreated = Onboarding(self.store, self.components, self.setup.startup)
        self.assertFalse(recreated.status()["show_guide"])
        self.assertFalse(recreated.status()["completed"])
        with self.assertRaises(ValueError):
            recreated.finish("complete")

    def test_pending_accounts_keep_the_guide_until_authorization(self):
        self.store.upsert({"email": "fixture@example.com", "password": "fixture-password", "totp": ""})
        self.assertTrue(self.setup.status()["show_guide"])
        self.assertEqual(self.setup.status()["accounts"], {"total": 1, "ready": 0})
        with self.assertRaises(ValueError):
            self.setup.finish("complete")
        self.store.save_auth_snapshot("fixture@example.com", b'{"fixture":true}', {})
        with mock.patch("codex_switcher.config.find_codex_exe", return_value="codex.exe"):
            self.assertTrue(self.setup.finish("complete")["completed"])

    def test_picker_cancellation_does_not_clear_the_previous_codex_path(self):
        exe = self.root / "codex.exe"
        exe.write_bytes(b"fixture")
        self.setup.select_codex(str(exe))
        with mock.patch("codex_switcher.windows_ui.select_executable", return_value=None):
            self.assertTrue(self.setup.select_codex()["cancelled"])
        self.assertEqual(Path(settings.get("codex_exe")).resolve(), exe.resolve())
        with self.assertRaises(ValueError):
            self.setup.select_codex(str(self.root))

    def local_auth(self, email="fixture@example.com", *, expired=False, refresh=True):
        payload = base64.urlsafe_b64encode(json.dumps({"email": email, "exp": 1 if expired else 4102444800}).encode()).decode().rstrip("=")
        token = "e30." + payload + ".fixture"
        tokens = {"id_token": token, "access_token": token, "account_id": "fixture-account"}
        if refresh:
            tokens["refresh_token"] = "fixture-refresh-token"
        self.auth_path.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": tokens}), encoding="utf-8")
        return self.setup.status()["local_account"]

    def test_local_detection_is_read_only_and_selected_by_default(self):
        local = self.local_auth()
        before = self.auth_path.read_bytes()
        self.assertTrue(local["available"])
        self.assertTrue(self.setup.status()["local_selected"])
        self.assertEqual(self.store.count(), 0)
        self.assertFalse(self.setup.path.exists())
        self.assertEqual(self.auth_path.read_bytes(), before)
        self.assertNotIn("fixture-refresh-token", json.dumps(self.setup.status()))

    def test_import_local_preserves_saved_secrets_metadata_and_email_case(self):
        self.store.upsert({"email": "Fixture@Example.com", "password": "saved-password", "totp": "JBSWY3DPEHPK3PXP", "tags": "keep", "priority": 333})
        local = self.local_auth()
        before = self.auth_path.read_bytes()
        self.assertEqual(self.setup.import_local(local["fingerprint"])["result"], "updated")
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.store.get_secret("fixture@example.com"), {"password": "saved-password", "totp": "JBSWY3DPEHPK3PXP"})
        self.assertEqual(self.store.get("fixture@example.com")["priority"], 333)
        self.assertEqual(self.store.get("fixture@example.com")["tags"], "keep")
        self.assertEqual(self.setup.import_local(local["fingerprint"])["result"], "unchanged")
        self.assertEqual(self.auth_path.read_bytes(), before)

    def test_changed_local_account_requires_a_fresh_detection(self):
        local = self.local_auth()
        self.local_auth("other@example.com")
        with self.assertRaisesRegex(ValueError, "已变化"):
            self.setup.import_local(local["fingerprint"])
        self.assertEqual(self.store.count(), 0)

    def test_invalid_api_key_and_unrefreshable_expired_auth_are_not_importable(self):
        for content in ('{}', '{"tokens":[]}', '{"OPENAI_API_KEY":"fixture"}', 'not json'):
            self.auth_path.write_text(content, encoding="utf-8")
            self.assertFalse(self.setup.status()["local_account"]["available"])
        self.assertFalse(self.local_auth(expired=True, refresh=False)["available"])
        local = self.local_auth(expired=True)
        self.assertTrue(local["available"])
        self.setup.import_local(local["fingerprint"])
        self.assertEqual(self.setup.status()["accounts"]["ready"], 0)

    def test_unfinished_guide_survives_import_and_reload_without_saving_credentials(self):
        local = self.local_auth()
        self.setup.progress(step=2, browser_mode="local", local_selected=True)
        self.setup.import_local(local["fingerprint"])
        recreated = Onboarding(self.store, self.components, self.setup.startup, auth_path=self.auth_path)
        state = recreated.status()
        self.assertEqual((state["step"], state["browser_mode"], state["accounts"]["ready"]), (2, "local", 1))
        self.assertTrue(state["show_guide"])
        self.assertNotIn("fixture-refresh-token", self.setup.path.read_text())
        with mock.patch("codex_switcher.config.find_codex_exe", return_value="codex.exe"):
            self.assertTrue(recreated.finish("complete")["completed"])


class BrowserComponentTests(unittest.TestCase):
    def test_one_shared_download_is_reused_and_reports_ready(self):
        started, release = threading.Event(), threading.Event()
        ready = []
        class Process:
            def __init__(self):
                started.set()
            @property
            def stdout(self):
                yield "downloading 42%\n"
                release.wait(2)
            def wait(self):
                ready.append(True)
                return 0
            def poll(self):
                return None
            def terminate(self):
                release.set()
        factory = mock.Mock(side_effect=lambda *a, **k: Process())
        manager = BrowserComponents(factory, lambda: Path("chrome.exe") if ready else None)
        with tempfile.TemporaryDirectory() as directory, mock.patch("codex_switcher.components.browser_root", return_value=Path(directory)):
            manager.start()
            self.assertTrue(started.wait(2))
            self.assertEqual(manager.start()["state"], "installing")
            release.set()
            self.assertTrue(manager._done.wait(2))
            self.assertTrue(manager.status()["ready"])
            manager.ensure_ready()
        self.assertEqual(factory.call_count, 1)
        command, kwargs = factory.call_args
        self.assertEqual(command[0][-3:], ["install", "chromium", "--no-shell"])
        self.assertEqual(kwargs["env"]["CI"], "1")

    def test_failed_download_waits_for_explicit_retry(self):
        process = mock.Mock(stdout=io.StringIO("network unavailable\n"))
        process.wait.return_value = 1
        factory = mock.Mock(return_value=process)
        manager = BrowserComponents(factory, lambda: None)
        with tempfile.TemporaryDirectory() as directory, mock.patch("codex_switcher.components.browser_root", return_value=Path(directory)):
            manager.start()
            self.assertTrue(manager._done.wait(2))
            self.assertEqual(manager.status()["state"], "failed")
            with self.assertRaisesRegex(ComponentError, "network unavailable"):
                manager.ensure_ready()
            self.assertEqual(factory.call_count, 1)
            manager.start()
            self.assertTrue(manager._done.wait(2))
        self.assertEqual(factory.call_count, 2)

    def test_existing_components_do_not_start_a_download(self):
        factory = mock.Mock(side_effect=AssertionError("must not download"))
        manager = BrowserComponents(factory, lambda: Path("chrome.exe"))
        manager.ensure_ready()
        self.assertTrue(manager.status()["ready"])
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
