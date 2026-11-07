"""Isolated Team OAuth routing, callback ownership and workspace validation."""
import json
import threading
import time
import unittest
from unittest import mock

import test_login as fixtures
from codex_switcher.login import (
    LoginError, LoginSession, ProcessOutput, BrowserAuthProcess,
    ManualDriver, _BROWSER_AUTH_LOCK, start_browser_auth, start_device_auth,
)


class TeamLoginTests(unittest.TestCase):
    setUp = fixtures.LoginTests.setUp
    tearDown = fixtures.LoginTests.tearDown
    manager = fixtures.LoginTests.manager

    def finish(self, manager, info):
        session = manager.get(info["session"]["id"])
        self.assertTrue(session.done_event.wait(12), session.to_public())
        return session

    def browser_start(self, codex_home, **kwargs):
        return start_device_auth([fixtures.sys.executable, str(self.fake_cli)], codex_home,
                                 cancel_event=kwargs.get("cancel_event"))

    def team_fixture(self):
        self.fake_cli.write_text(fixtures.FAKE_CLI.replace('"plus"', '"team"'), encoding="utf-8")

    def test_personal_default_keeps_device_flow(self):
        manager = self.manager(lambda session: fixtures.FakeDriver())
        with mock.patch("codex_switcher.login.start_browser_auth") as browser:
            result = self.finish(manager, manager.enqueue("a@example.com"))
        browser.assert_not_called()
        self.assertEqual((result.status, result.identity_type, result.login_method), ("success", "personal", "device"))

    def test_team_uses_browser_and_saves_actual_workspace(self):
        self.team_fixture()
        manager = self.manager(lambda session: fixtures.FakeDriver())
        with mock.patch("codex_switcher.login.start_browser_auth", side_effect=self.browser_start) as browser:
            session = self.finish(manager, manager.enqueue("a@example.com", identity_type="team", workspace_id="acct-fake"))
        self.assertEqual(session.status, "success", session.message)
        self.assertEqual(session.workspace_id, "acct-fake")
        self.assertEqual(session.login_method, "browser")
        self.assertEqual(browser.call_args.kwargs["workspace_id"], "acct-fake")
        self.assertNotIn("verification_url", json.loads(manager.history_path.read_text(encoding="utf-8"))[0])
        self.assertFalse(session.auth_path.exists())
        self.assertFalse(_BROWSER_AUTH_LOCK.locked())
        self.assertEqual(manager.active_map(), {})

    def test_team_rejects_personal_or_wrong_workspace_without_saving(self):
        manager = self.manager(lambda session: fixtures.FakeDriver())
        with mock.patch("codex_switcher.login.start_browser_auth", side_effect=self.browser_start):
            personal = self.finish(manager, manager.enqueue("a@example.com", identity_type="team"))
            self.assertEqual(personal.error, "workspace_mismatch")
            self.assertFalse(self.store.has_auth_snapshot("a@example.com"))
            self.team_fixture()
            wrong = self.finish(manager, manager.enqueue("a@example.com", identity_type="team", workspace_id="another-team"))
            self.assertEqual(wrong.error, "workspace_mismatch")
            self.assertFalse(self.store.has_auth_snapshot("a@example.com"))

    def test_no_password_uses_manual_handoff_instead_of_rejecting(self):
        self.team_fixture()
        self.store.update_account("a@example.com", password="", clear_totp=True)
        manager = self.manager(lambda session: fixtures.FakeDriver())
        manager.driver_name = "playwright"
        with mock.patch("codex_switcher.login.start_browser_auth", side_effect=self.browser_start):
            session = self.finish(manager, manager.enqueue("a@example.com", identity_type="team"))
        self.assertEqual(session.driver, "manual")
        self.assertEqual(session.status, "success", session.message)

    def test_cancel_while_waiting_callback_does_not_release_other_owner(self):
        manager = self.manager(lambda session: fixtures.FakeDriver())
        self.assertTrue(_BROWSER_AUTH_LOCK.acquire(timeout=1))
        try:
            with mock.patch("codex_switcher.login.start_browser_auth") as browser:
                info = manager.enqueue("a@example.com", identity_type="team")
                deadline = time.monotonic() + 4
                session = manager.get(info["session"]["id"])
                while session.phase != "waiting_for_callback_slot" and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertEqual(session.phase, "waiting_for_callback_slot")
                self.assertTrue(manager.cancel(session.id))
                self.finish(manager, info)
                browser.assert_not_called()
                self.assertTrue(_BROWSER_AUTH_LOCK.locked())
        finally:
            _BROWSER_AUTH_LOCK.release()

    def test_waiting_user_keeps_oauth_serial_and_distinguishes_workspaces(self):
        self.team_fixture()
        entered, release = threading.Event(), threading.Event()
        class Held(fixtures.FakeDriver):
            def complete_login(self, **kwargs):
                session = kwargs["session"]
                if session.workspace_id == "acct-fake":
                    session.waiting_for_input("fixture workspace chooser")
                    entered.set()
                    release.wait(8)
                super().complete_login(**kwargs)
        manager = self.manager(lambda session: Held())
        manager._concurrency = 2
        with mock.patch("codex_switcher.login.start_browser_auth", side_effect=self.browser_start) as browser:
            first = manager.enqueue("a@example.com", identity_type="team", workspace_id="acct-fake")
            try:
                self.assertTrue(entered.wait(5))
                duplicate = manager.enqueue("a@example.com", identity_type="team", workspace_id="acct-fake")
                second = manager.enqueue("a@example.com", identity_type="team", workspace_id="team-two")
                self.assertEqual(first["session"]["id"], duplicate["session"]["id"])
                self.assertNotEqual(first["session"]["id"], second["session"]["id"])
                time.sleep(.3)
                self.assertEqual(browser.call_count, 1)
            finally:
                release.set()
            self.assertEqual(self.finish(manager, first).status, "success")
            self.assertEqual(self.finish(manager, second).error, "workspace_mismatch")
        self.assertEqual(browser.call_count, 2)
        self.assertFalse(_BROWSER_AUTH_LOCK.locked())

    def test_startup_failure_releases_callback_and_remains_visible(self):
        manager = self.manager(lambda session: fixtures.FakeDriver())
        with mock.patch("codex_switcher.login.start_browser_auth", side_effect=LoginError("fixture", "unavailable")):
            session = self.finish(manager, manager.enqueue("a@example.com", identity_type="team"))
        self.assertEqual(session.error, "fixture")
        self.assertFalse(_BROWSER_AUTH_LOCK.locked())
        self.assertEqual(manager.active_map(), {})

    def test_reauthorize_team_profile_selects_browser_and_remembers_workspace(self):
        self.team_fixture()
        manager = self.manager(lambda session: fixtures.FakeDriver())
        with mock.patch("codex_switcher.login.start_browser_auth", side_effect=self.browser_start):
            first = self.finish(manager, manager.enqueue("a@example.com", identity_type="team"))
            self.assertTrue(first.profile_id)
            second = self.finish(manager, manager.enqueue("a@example.com", profile_id=first.profile_id))
        self.assertEqual((second.status, second.identity_type, second.login_method, second.workspace_id),
                         ("success", "team", "browser", "acct-fake"))
        self.assertEqual(first.profile_id, second.profile_id)

    def test_unsupported_rpc_falls_back_to_native_oauth_without_device_auth(self):
        self.team_fixture()
        manager = self.manager(lambda session: fixtures.FakeDriver())
        self.fake_cli.write_text(self.fake_cli.read_text(encoding="utf-8").replace(
            "https://auth.openai.com/codex/device", "https://auth.openai.com/oauth/authorize?state=private"), encoding="utf-8")
        with mock.patch("codex_switcher.login.start_browser_auth", side_effect=LoginError("browser_auth_unsupported", "unsupported")):
            session = self.finish(manager, manager.enqueue("a@example.com", identity_type="team"))
        self.assertEqual(session.status, "success", session.message)
        self.assertTrue(session.browser_opened_by_cli)
        self.assertEqual(session.driver, "manual")
        self.assertIsNone(session.device_code)
        self.assertNotIn("state=private", (session.codex_home / "cli.log").read_text(encoding="utf-8"))
        with mock.patch("webbrowser.open") as opened:
            ManualDriver().complete_login(email=session.email, password="", totp="", device_url=session.verification_url, device_code="", session=session)
        opened.assert_not_called()

    def test_automation_failure_reuses_authorization_in_manual_browser(self):
        self.team_fixture()
        class Broken:
            def complete_login(self, **kwargs):
                raise RuntimeError("fixture browser unavailable")
        manager = self.manager(lambda session: Broken())
        def manual(**kwargs):
            kwargs["session"].auth_path.parent.joinpath("done.flag").write_text("ok")
        with mock.patch("codex_switcher.login.start_browser_auth", side_effect=self.browser_start) as browser, mock.patch.object(ManualDriver, "complete_login", side_effect=manual) as handoff:
            session = self.finish(manager, manager.enqueue("a@example.com", identity_type="team"))
        self.assertEqual(session.status, "success", session.message)
        self.assertEqual(session.driver, "manual")
        self.assertEqual(browser.call_count, 1)
        self.assertEqual(handoff.call_count, 1)

    def test_oauth_metadata_and_logs_do_not_persist_auth_url_or_code(self):
        manager = self.manager(lambda session: fixtures.FakeDriver())
        session = LoginSession("test", "a@example.com", self.tmp_path / "oauth", login_method="browser", identity_type="team")
        session.verification_url = "https://auth.openai.com/oauth/authorize?state=secret-state&code=secret-code"
        session.output = ProcessOutput(session.codex_home / "cli.log")
        session.output.add(session.verification_url + "\nPBAH-TBU2Y\n")
        manager._sessions[session.id] = session
        manager._persist()
        session.status = "failed"
        for text in (manager.history_path.read_text(encoding="utf-8"), session.output.path.read_text(encoding="utf-8"), json.dumps(session.to_public())):
            self.assertNotIn("secret-state", text)
            self.assertNotIn("secret-code", text)
            self.assertNotIn("PBAH-TBU2Y", text)

    def test_rpc_uses_isolated_file_store_and_official_url(self):
        server = mock.Mock()
        server.request.return_value = {"type": "chatgpt", "loginId": "own-id", "authUrl": "https://auth.openai.com/oauth/authorize?state=private"}
        server.proc.poll.return_value = None
        server.wait_notification.return_value = None
        home = self.tmp_path / "rpc"
        factory = mock.Mock(return_value=server)
        process, code, url, output = start_browser_auth(home, workspace_id="team-1", server_factory=factory)
        self.assertEqual(factory.call_args.kwargs["env"]["CODEX_HOME"], str(home))
        text = (home / "config.toml").read_text(encoding="utf-8")
        self.assertIn('cli_auth_credentials_store = "file"', text)
        self.assertIn('forced_chatgpt_workspace_id = "team-1"', text)
        server.request.assert_called_once_with("account/login/start", {"type": "chatgpt"}, timeout=30)
        self.assertIsNone(code)
        self.assertNotIn("private", output.text())
        process.terminate()
        server.request.assert_called_with("account/login/cancel", {"loginId": "own-id"}, timeout=2)
        server.close.assert_called_once()

    def test_rpc_rejects_untrusted_url_and_closes_own_process(self):
        server = mock.Mock()
        server.request.return_value = {"loginId": "own-id", "authUrl": "https://auth.openai.com.evil.example/authorize"}
        with self.assertRaises(LoginError) as error:
            start_browser_auth(self.tmp_path / "bad-rpc", server_factory=lambda **kwargs: server)
        self.assertEqual(error.exception.code, "invalid_authorization_url")
        server.close.assert_called_once()

    def test_existing_callback_is_not_cancelled_or_replaced(self):
        probe = mock.MagicMock()
        probe.__enter__.return_value.connect_ex.return_value = 0
        with mock.patch("codex_switcher.login.socket.socket", return_value=probe), mock.patch("codex_switcher.appserver.AppServerSession") as server:
            with self.assertRaises(LoginError) as error:
                start_browser_auth(self.tmp_path / "busy")
        self.assertEqual(error.exception.code, "callback_port_busy")
        server.assert_not_called()

    def test_login_completion_does_not_wait_for_helper_to_close_stdout(self):
        # A launcher can leave a helper holding its inherited output pipe.
        # Auth is already saved and the login child has exited, but closing
        # stdout from the manager would block on the reading thread's lock.
        holder = (
            "import sys,time; from pathlib import Path; h=Path(sys.argv[1]); "
            "(h/'holder.started').write_text('ready'); deadline=time.monotonic()+12\n"
            "while not (h/'release.holder').exists() and time.monotonic()<deadline: time.sleep(.02)\n"
        )
        spawn = (
            "import subprocess\n"
            f"subprocess.Popen([sys.executable, '-c', {holder!r}, str(home)])\n"
            "while not (home / 'holder.started').exists(): time.sleep(.02)\n"
        )
        self.fake_cli.write_text(fixtures.FAKE_CLI.replace(
            '(home / "auth.json").write_text', spawn + '(home / "auth.json").write_text'), encoding="utf-8")
        manager = self.manager(lambda session: fixtures.FakeDriver())
        info = manager.enqueue("a@example.com")
        session = manager.get(info["session"]["id"])
        try:
            deadline = time.monotonic() + 8
            while session.status != "success" and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(session.status, "success", session.to_public())
            self.assertTrue(session.done_event.wait(1), "completion blocked on a helper-owned stdout pipe")
            self.assertFalse(session.output.done.is_set(), "fixture must still own the pipe")
            self.assertFalse(session.auth_path.exists())
            self.assertEqual(manager.active_map(), {})
        finally:
            (session.codex_home / "release.holder").write_text("stop")
            if session.output:
                self.assertTrue(session.output.done.wait(5))
            self.assertTrue(session.done_event.wait(5))

    def test_personal_selection_does_not_save_team_authorization(self):
        self.team_fixture()
        manager = self.manager(lambda session: fixtures.FakeDriver())
        session = self.finish(manager, manager.enqueue("a@example.com"))
        self.assertEqual(session.error, "workspace_mismatch")
        self.assertFalse(self.store.has_auth_snapshot("a@example.com"))


if __name__ == "__main__":
    unittest.main()
