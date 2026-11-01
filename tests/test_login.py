import hashlib
import json
import os
import socket
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from codex_switcher.api import create_server
from codex_switcher.login import (
    LoginError,
    LoginManager,
    PlaywrightDriver,
    parse_device_code_output,
)
from codex_switcher.store import AccountStore


FAKE_CLI = textwrap.dedent(
    r'''
    import base64
    import json
    import os
    import sys
    import time
    from pathlib import Path

    home = Path(os.environ["CODEX_HOME"])
    print("Open this URL: https://auth.openai.com/codex/device", flush=True)
    print("Enter this code: PBAH-TBU2Y", flush=True)

    flag = home / "done.flag"
    deadline = time.time() + 20
    while time.time() < deadline:
        if flag.exists():
            break
        time.sleep(0.05)
    else:
        print("timed out waiting for done.flag", flush=True)
        sys.exit(2)

    def b64(data):
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    header = b64({"alg": "none", "typ": "JWT"})
    payload = b64(
        {
            "email": "a@example.com",
            "name": "Fake User",
            "exp": 4102444800,
            "https://api.openai.com/auth": {
                "chatgpt_plan_type": "plus",
                "chatgpt_account_id": "acct-fake",
                "chatgpt_user_id": "user-fake",
            },
        }
    )
    token = f"{header}.{payload}.sig"
    auth = {
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": token,
            "access_token": token,
            "account_id": "acct-fake",
        },
        "last_refresh": "2026-10-30T00:00:00Z",
    }
    (home / "auth.json").write_text(json.dumps(auth), encoding="utf-8")
    print("Successfully logged in", flush=True)
    '''
).strip()


class FakeDriver:
    """Simulates the browser side by writing the CLI's done flag."""

    def complete_login(self, *, email, password, totp, device_url, device_code, session):
        (session.codex_home / "done.flag").write_text(
            "ok", encoding="utf-8"
        )


class NeverDriver:
    """Simulates a browser step that never completes (for cancel tests)."""

    def complete_login(self, *, email, password, totp, device_url, device_code, session):
        return None


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.auth_path = self.tmp_path / "real-auth.json"
        self.auth_path.write_text('{"real":"auth"}', encoding="utf-8")
        self.real_hash = hashlib.sha256(self.auth_path.read_bytes()).hexdigest()

        os.environ["CODEX_SWITCHER_ENABLE_LOGIN"] = "1"
        os.environ["CODEX_SWITCHER_DATA"] = str(self.tmp_path / "data")
        os.environ["CODEX_SWITCHER_AUTH"] = str(self.auth_path)
        os.environ["CODEX_SWITCHER_ENABLE_SWITCH"] = "0"

        self.fake_cli = self.tmp_path / "fake_codex_cli.py"
        self.fake_cli.write_text(FAKE_CLI, encoding="utf-8")

        self.store = AccountStore(self.tmp_path / "accounts.sqlite3")
        self.store.upsert(
            {
                "email": "a@example.com",
                "password": "pw",
                "totp": "JBSWY3DPEHPK3PXP",
                "totp_format": "base32",
            }
        )

    def tearDown(self):
        os.environ["CODEX_SWITCHER_ENABLE_LOGIN"] = "0"
        os.environ.pop("CODEX_SWITCHER_ENABLE_SWITCH", None)
        self.store.close()
        self.tmp.cleanup()

    def manager(self, driver_factory):
        return LoginManager(
            self.store,
            codex_cmd=[sys.executable, str(self.fake_cli)],
            driver_name="manual",
            sessions_dir=self.tmp_path / "sessions",
            profile_root=self.tmp_path / "profiles",
            driver_factory=driver_factory,
        )

    def wait_done(self, manager, session, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            current = manager.get(session.id)
            if current and current.status in ("success", "failed", "cancelled"):
                return current
            time.sleep(0.1)
        return manager.get(session.id)

    def test_parse_device_code_output(self):
        text = (
            "\x1b[32mOpen this URL: https://auth.openai.com/codex/device\x1b[0m\n"
            "Enter this code: PBAH-TBU2Y\n"
        )
        url, code = parse_device_code_output(text)
        self.assertEqual(url, "https://auth.openai.com/codex/device")
        self.assertEqual(code, "PBAH-TBU2Y")

    def test_parse_returns_default_url(self):
        url, code = parse_device_code_output("no code here")
        self.assertEqual(url, "https://auth.openai.com/codex/device")
        self.assertIsNone(code)

    def test_login_manager_saves_encrypted_snapshot(self):
        manager = self.manager(lambda session: FakeDriver())
        session = manager.start("a@example.com")
        self.assertEqual(session.device_code, "PBAH-TBU2Y")
        self.assertEqual(
            session.verification_url, "https://auth.openai.com/codex/device"
        )

        done = self.wait_done(manager, session)
        self.assertIsNotNone(done)
        self.assertEqual(done.status, "success", done.message)
        self.assertTrue(self.store.has_auth_snapshot("a@example.com"))

        snapshot = self.store.get_auth_snapshot("a@example.com")
        self.assertIsNotNone(snapshot)
        parsed = json.loads(snapshot.decode("utf-8"))
        self.assertEqual(parsed["auth_mode"], "chatgpt")
        self.assertIn("id_token", parsed["tokens"])

        listed = self.store.list()
        self.assertTrue(listed[0]["has_auth_snapshot"])
        self.assertNotIn("auth_blob", listed[0])

        # The real auth.json is never touched.
        self.assertEqual(
            hashlib.sha256(self.auth_path.read_bytes()).hexdigest(),
            self.real_hash,
        )

    def test_cancel_marks_session_cancelled(self):
        manager = self.manager(lambda session: NeverDriver())
        session = manager.start("a@example.com")
        time.sleep(0.3)
        self.assertTrue(manager.cancel(session.id))
        done = self.wait_done(manager, session)
        self.assertEqual(done.status, "cancelled")
        self.assertFalse(self.store.has_auth_snapshot("a@example.com"))

    def test_login_disabled(self):
        os.environ["CODEX_SWITCHER_ENABLE_LOGIN"] = "0"
        os.environ.pop("CODEX_SWITCHER_ENABLE_SWITCH", None)
        manager = self.manager(lambda session: FakeDriver())
        with self.assertRaises(LoginError) as ctx:
            manager.start("a@example.com")
        self.assertEqual(ctx.exception.code, "login_disabled")

    def test_enqueue_serial_dedupes_and_finishes(self):
        # The fake CLI always emits a JWT for a@example.com, so b is only used
        # to prove the queue attempts a second account serially; it will end
        # in account_mismatch rather than success.
        self.store.upsert(
            {"email": "b@example.com", "password": "pw", "totp": "", "totp_format": "none"}
        )
        manager = self.manager(lambda session: FakeDriver())
        first = manager.enqueue("a@example.com")
        self.assertEqual(first["state"], "queued")
        second = manager.enqueue("b@example.com")
        self.assertIn(second["state"], ("queued", "running"))
        duplicate = manager.enqueue("b@example.com")
        self.assertIn(duplicate["state"], ("queued", "running"))

        deadline = time.time() + 20
        sessions = []
        while time.time() < deadline:
            sessions = manager.list()
            if (
                len(sessions) >= 2
                and not manager.queue_info()
                and all(
                    item["status"] in ("success", "failed", "cancelled")
                    for item in sessions
                )
            ):
                break
            time.sleep(0.1)

        self.assertTrue(self.store.has_auth_snapshot("a@example.com"))
        by_email = {session["email"]: session["status"] for session in sessions}
        self.assertEqual(by_email.get("a@example.com"), "success")
        self.assertIn("b@example.com", by_email)
        # Duplicate enqueue must not create a third session.
        self.assertEqual(len(sessions), 2)
        self.assertEqual(manager.queue_info(), [])

    def test_enqueue_disabled(self):
        os.environ["CODEX_SWITCHER_ENABLE_LOGIN"] = "0"
        manager = self.manager(lambda session: FakeDriver())
        with self.assertRaises(LoginError) as ctx:
            manager.enqueue("a@example.com")
        self.assertEqual(ctx.exception.code, "login_disabled")

    # ------------------------------------------------------------- history
    def history_file(self):
        return self.tmp_path / "login-history.json"

    def test_history_survives_restart(self):
        manager = self.manager(lambda session: FakeDriver())
        session = manager.start("a@example.com")
        done = self.wait_done(manager, session)
        self.assertEqual(done.status, "success", done.message)
        self.assertTrue(self.history_file().exists())

        # A fresh manager on the same data directory must still see the login.
        restarted = self.manager(lambda session: FakeDriver())
        by_id = {item["id"]: item for item in restarted.list()}
        self.assertIn(session.id, by_id)
        self.assertEqual(by_id[session.id]["status"], "success")
        self.assertEqual(by_id[session.id]["email"], "a@example.com")

    def test_history_marks_interrupted_sessions_failed(self):
        self.history_file().write_text(
            json.dumps(
                [
                    {
                        "id": "deadbeef0001",
                        "email": "x@example.com",
                        "status": "waiting_for_browser",
                        "message": "设备码已生成，等待浏览器授权",
                        "driver": "playwright",
                        "started_at": 1.0,
                        "updated_at": 2.0,
                        "codex_home": str(self.tmp_path / "sessions" / "deadbeef0001"),
                    }
                ]
            ),
            encoding="utf-8",
        )
        manager = self.manager(lambda session: FakeDriver())
        by_id = {item["id"]: item for item in manager.list()}
        self.assertIn("deadbeef0001", by_id)
        self.assertEqual(by_id["deadbeef0001"]["status"], "failed")
        self.assertEqual(by_id["deadbeef0001"]["error"], "interrupted")
        # Interrupted history must not be queued or re-run automatically.
        self.assertEqual(manager.queue_info(), [])
        self.assertEqual(manager.active_map(), {})

    def test_history_never_stores_device_code(self):
        manager = self.manager(lambda session: FakeDriver())
        session = manager.start("a@example.com")
        self.assertEqual(session.device_code, "PBAH-TBU2Y")
        self.wait_done(manager, session)
        raw = self.history_file().read_text(encoding="utf-8")
        self.assertNotIn("PBAH-TBU2Y", raw)
        self.assertNotIn("device_code", raw)
        self.assertNotIn("log_tail", raw)

    def test_history_caps_at_50_records(self):
        from codex_switcher.login import LoginSession

        manager = self.manager(lambda session: FakeDriver())
        for index in range(60):
            session = LoginSession(
                id=f"hist{index:04d}",
                email="a@example.com",
                codex_home=self.tmp_path / "sessions" / f"hist{index:04d}",
                status="success",
                started_at=1000.0 + index,
                updated_at=1000.0 + index,
            )
            manager._sessions[session.id] = session
        manager._persist()

        records = json.loads(self.history_file().read_text(encoding="utf-8"))
        self.assertEqual(len(records), 50)
        # Newest first, oldest dropped from disk and memory alike.
        self.assertEqual(records[0]["id"], "hist0059")
        self.assertNotIn("hist0000", {item["id"] for item in manager.list()})

    def test_queue_runs_logins_in_parallel(self):
        self.store.upsert(
            {"email": "b@example.com", "password": "pw", "totp": "", "totp_format": "none"}
        )
        gate = threading.Event()
        started: list[str] = []
        started_lock = threading.Lock()

        class SlowDriver:
            def complete_login(
                self, *, email, password, totp, device_url, device_code, session
            ):
                with started_lock:
                    started.append(email)
                gate.wait(15)
                (session.codex_home / "done.flag").write_text("ok", encoding="utf-8")

        manager = LoginManager(
            self.store,
            codex_cmd=[sys.executable, str(self.fake_cli)],
            driver_name="manual",
            sessions_dir=self.tmp_path / "sessions",
            profile_root=self.tmp_path / "profiles",
            driver_factory=lambda session: SlowDriver(),
            concurrency=2,
        )
        self.assertEqual(manager.concurrency(), 2)
        manager.enqueue("a@example.com")
        manager.enqueue("b@example.com")

        # Both must enter the driver before either is allowed to finish.
        deadline = time.time() + 15
        while time.time() < deadline:
            with started_lock:
                if len(started) >= 2:
                    break
            time.sleep(0.05)
        with started_lock:
            self.assertEqual(sorted(started), ["a@example.com", "b@example.com"])

        gate.set()
        deadline = time.time() + 20
        sessions = []
        while time.time() < deadline:
            sessions = manager.list()
            if len(sessions) >= 2 and all(
                item["status"] in ("success", "failed", "cancelled") for item in sessions
            ):
                break
            time.sleep(0.1)
        by_email = {item["email"]: item["status"] for item in sessions}
        self.assertEqual(by_email.get("a@example.com"), "success")
        self.assertIn("b@example.com", by_email)

    def test_concurrency_one_stays_serial(self):
        self.store.upsert(
            {"email": "b@example.com", "password": "pw", "totp": "", "totp_format": "none"}
        )
        gate = threading.Event()
        started: list[str] = []
        started_lock = threading.Lock()

        class SlowDriver:
            def complete_login(
                self, *, email, password, totp, device_url, device_code, session
            ):
                with started_lock:
                    started.append(email)
                gate.wait(2)
                (session.codex_home / "done.flag").write_text("ok", encoding="utf-8")

        manager = LoginManager(
            self.store,
            codex_cmd=[sys.executable, str(self.fake_cli)],
            driver_name="manual",
            sessions_dir=self.tmp_path / "sessions",
            profile_root=self.tmp_path / "profiles",
            driver_factory=lambda session: SlowDriver(),
            concurrency=1,
        )
        manager.enqueue("a@example.com")
        manager.enqueue("b@example.com")
        time.sleep(0.8)
        with started_lock:
            self.assertEqual(started, ["a@example.com"])
        gate.set()
        deadline = time.time() + 20
        while time.time() < deadline and not all(
            item["status"] in ("success", "failed", "cancelled")
            for item in manager.list()
        ):
            time.sleep(0.1)


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class LoginApiTests(unittest.TestCase):
    """End-to-end API test with a fake CLI and fake browser driver."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.auth_path = self.tmp_path / "real-auth.json"
        self.auth_path.write_text('{"real":"auth"}', encoding="utf-8")
        self.real_hash = hashlib.sha256(self.auth_path.read_bytes()).hexdigest()

        os.environ["CODEX_SWITCHER_ENABLE_LOGIN"] = "1"
        os.environ["CODEX_SWITCHER_DATA"] = str(self.tmp_path / "data")
        os.environ["CODEX_SWITCHER_AUTH"] = str(self.auth_path)
        os.environ["CODEX_SWITCHER_ENABLE_SWITCH"] = "0"

        self.fake_cli = self.tmp_path / "fake_codex_cli.py"
        self.fake_cli.write_text(FAKE_CLI, encoding="utf-8")

        self.store = AccountStore(self.tmp_path / "accounts.sqlite3")
        self.store.upsert(
            {
                "email": "a@example.com",
                "password": "pw",
                "totp": "JBSWY3DPEHPK3PXP",
                "totp_format": "base32",
            }
        )

        self.manager = LoginManager(
            self.store,
            codex_cmd=[sys.executable, str(self.fake_cli)],
            driver_name="manual",
            sessions_dir=self.tmp_path / "sessions",
            profile_root=self.tmp_path / "profiles",
            driver_factory=lambda session: FakeDriver(),
        )
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.server = create_server(
            self.store,
            self.auth_path,
            "127.0.0.1",
            self.port,
            login_manager=self.manager,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        for _ in range(100):
            try:
                self.request("GET", "/api/health")
                break
            except Exception:
                time.sleep(0.05)

    def tearDown(self):
        os.environ["CODEX_SWITCHER_ENABLE_LOGIN"] = "0"
        os.environ.pop("CODEX_SWITCHER_ENABLE_SWITCH", None)
        os.environ.pop("CODEX_SWITCHER_AUTO_LOGIN_ON_IMPORT", None)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()
        self.tmp.cleanup()

    def request(self, method: str, path: str, body: dict | None = None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method)
        for key, value in headers.items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            code = exc.code
            exc.close()
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {"raw": raw}
            return code, payload

    def test_login_start_status_and_snapshot(self):
        status, body = self.request(
            "POST", "/api/login/start", {"email": "a@example.com"}
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        session = body["session"]
        self.assertIn("***@", session["email_masked"])
        self.assertEqual(session["email"], "a@example.com")

        deadline = time.time() + 10
        latest = None
        while time.time() < deadline:
            status, body = self.request("GET", "/api/login/status")
            self.assertEqual(status, 200)
            sessions = body["sessions"]
            if sessions:
                latest = sessions[0]
                if latest["status"] in ("success", "failed", "cancelled"):
                    break
            time.sleep(0.1)

        self.assertIsNotNone(latest)
        self.assertEqual(latest["status"], "success", latest.get("message"))

        status, body = self.request("GET", "/api/accounts")
        self.assertEqual(status, 200)
        self.assertTrue(body["accounts"][0]["has_auth_snapshot"])

        self.assertEqual(
            hashlib.sha256(self.auth_path.read_bytes()).hexdigest(),
            self.real_hash,
        )

    def test_accounts_login_endpoint_and_auto_import(self):
        status, body = self.request(
            "POST", "/api/accounts/login", {"email": "a@example.com"}
        )
        self.assertEqual(status, 200, body)
        self.assertIn(body["login"]["state"], ("queued", "running"))

        deadline = time.time() + 10
        while time.time() < deadline:
            status, body = self.request("GET", "/api/accounts")
            account = body["accounts"][0]
            if account["has_auth_snapshot"]:
                break
            time.sleep(0.1)
        self.assertTrue(body["accounts"][0]["has_auth_snapshot"])

        # An account that already has a valid snapshot must not be re-queued.
        status, body = self.request(
            "POST",
            "/api/accounts/import",
            {"text": "a@example.com|pw|JBSWY3DPEHPK3PXP"},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["login_queued"], [])

        os.environ["CODEX_SWITCHER_AUTO_LOGIN_ON_IMPORT"] = "1"
        status, body = self.request(
            "POST",
            "/api/accounts/import",
            {"text": "b@example.com|pw|JBSWY3DPEHPK3PXP"},
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(body["login_auto"])
        self.assertIn("b@example.com", body["login_queued"])

        # The fake CLI only issues a JWT for a@example.com, so b ends in
        # account_mismatch; this still proves the import queued it serially.
        deadline = time.time() + 20
        session = None
        while time.time() < deadline:
            status, body = self.request("GET", "/api/login/status")
            session = next(
                (
                    item
                    for item in body.get("sessions", [])
                    if item["email"] == "b@example.com"
                ),
                None,
            )
            if session and session["status"] in ("success", "failed", "cancelled"):
                break
            time.sleep(0.1)
        self.assertIsNotNone(session)
        self.assertIn(session["status"], ("success", "failed"))


class PlaywrightNavTests(unittest.TestCase):
    """A stalled first navigation must be retried, not turned into a failure."""

    def _driver(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return PlaywrightDriver(Path(tmp.name))

    def test_retries_transient_navigation_timeouts(self):
        driver = self._driver()
        calls = {"n": 0}

        class Page:
            url = "about:blank"

            def goto(self, url, wait_until=None, timeout=None):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise RuntimeError("Page.goto: Timeout 45000ms exceeded.")

            def wait_for_load_state(self, *args, **kwargs):
                pass

            def wait_for_timeout(self, ms):
                pass

        driver._goto(Page(), "https://chatgpt.com/auth/login")
        self.assertEqual(calls["n"], 3)

    def test_gives_up_with_actionable_message(self):
        driver = self._driver()

        class Page:
            url = "about:blank"

            def goto(self, *args, **kwargs):
                raise RuntimeError("Page.goto: Timeout 45000ms exceeded.")

            def wait_for_timeout(self, ms):
                pass

        with self.assertRaises(LoginError) as ctx:
            driver._goto(Page(), "https://chatgpt.com/auth/login")
        self.assertEqual(ctx.exception.code, "navigation_failed")
        self.assertIn("代理", ctx.exception.message)
        self.assertIn("chatgpt.com", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()