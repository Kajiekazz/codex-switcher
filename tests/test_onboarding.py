import io
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
        self.assertEqual(registry.values["CodexSwitcher"], '"C:\\Program Files\\Codex Switcher\\CodexSwitcher.exe" --background')
        # A change from the tray is read on the dashboard's next poll.
        other = StartupRegistration(registry, startup.command)
        other.set_enabled(False)
        self.assertFalse(startup.status()["enabled"])
        self.assertEqual(registry.values, {"AnotherApp": "unchanged"})

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
        self.setup = Onboarding(self.store, self.components, StartupRegistration(FakeRegistry(), ["app.exe"]))
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

    def test_existing_accounts_skip_guide_but_complete_requires_authorization(self):
        self.store.upsert({"email": "fixture@example.com", "password": "fixture-password", "totp": ""})
        self.assertFalse(self.setup.status()["show_guide"])
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
        self.assertEqual(settings.get("codex_exe"), str(exe))
        with self.assertRaises(ValueError):
            self.setup.select_codex(str(self.root))


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
