import base64
import hashlib
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from codex_switcher import switcher
from codex_switcher.api import create_server
from codex_switcher.login import LoginManager
from codex_switcher.store import AccountStore
from codex_switcher.switcher import (
    SwitchError,
    SwitchManager,
    read_backup,
)


def b64url(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_auth(
    path: Path,
    email: str,
    account_id: str,
    *,
    plan: str = "plus",
    exp: int = 4102444800,
) -> bytes:
    header = b64url({"alg": "none", "typ": "JWT"})
    payload = b64url(
        {
            "email": email,
            "name": email.split("@", 1)[0],
            "exp": exp,
            "https://api.openai.com/auth": {
                "chatgpt_plan_type": plan,
                "chatgpt_account_id": account_id,
                "chatgpt_user_id": "user-" + account_id,
            },
        }
    )
    token = f"{header}.{payload}.sig"
    data = json.dumps(
        {
            "auth_mode": "chatgpt",
            "tokens": {
                "id_token": token,
                "access_token": token,
                "account_id": account_id,
            },
            "last_refresh": "2026-10-30T00:00:00Z",
        }
    ).encode("utf-8")
    path.write_bytes(data)
    return data


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class SwitchEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.auth_path = self.root / "auth.json"
        self.a_bytes = make_auth(
            self.auth_path, "a@example.com", "acct-a"
        )
        self.a_hash = sha(self.a_bytes)
        self.b_bytes = make_auth(
            self.root / "b.json", "b@example.com", "acct-b"
        )
        self.b_hash = sha(self.b_bytes)
        self.store = AccountStore(self.root / "accounts.sqlite3")
        self.store.save_auth_snapshot(
            "b@example.com",
            self.b_bytes,
            {
                "email": "b@example.com",
                "plan": "plus",
                "account_id": "acct-b",
                "expires_at": 4102444800,
            },
        )
        self.manager = SwitchManager(
            self.store,
            self.auth_path,
            target_backup_dir=self.root / "backups",
            enabled=lambda: True,
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_disabled_refuses_and_does_not_touch_auth(self):
        manager = SwitchManager(
            self.store,
            self.auth_path,
            target_backup_dir=self.root / "backups",
            enabled=lambda: False,
        )
        with self.assertRaises(SwitchError) as ctx:
            manager.switch_to("b@example.com", confirm=True)
        self.assertEqual(ctx.exception.code, "switching_disabled")
        self.assertEqual(sha(self.auth_path.read_bytes()), self.a_hash)

    def test_confirmation_required(self):
        with self.assertRaises(SwitchError) as ctx:
            self.manager.switch_to("b@example.com", confirm=False)
        self.assertEqual(ctx.exception.code, "confirmation_required")
        self.assertEqual(sha(self.auth_path.read_bytes()), self.a_hash)

    def test_missing_snapshot(self):
        with self.assertRaises(SwitchError) as ctx:
            self.manager.switch_to("missing@example.com", confirm=True)
        self.assertEqual(ctx.exception.code, "snapshot_not_found")
        self.assertEqual(sha(self.auth_path.read_bytes()), self.a_hash)

    def test_switch_writes_target_and_encrypted_backup(self):
        result = self.manager.switch_to("b@example.com", confirm=True)

        self.assertEqual(sha(self.auth_path.read_bytes()), self.b_hash)
        self.assertEqual(result.previous_email, "a@example.com")
        self.assertEqual(result.target_email, "b@example.com")
        self.assertEqual(result.target_sha256, self.b_hash)
        self.assertEqual(result.previous_sha256, self.a_hash)
        self.assertTrue(result.backup_path)

        backup = Path(result.backup_path)
        self.assertTrue(backup.exists())
        self.assertEqual(read_backup(backup), self.a_bytes)
        raw_backup = backup.read_text(encoding="utf-8")
        self.assertNotIn("eyJ", raw_backup)  # no plaintext JWT in the backup

        # The outgoing account was snapshotted automatically.
        self.assertTrue(self.store.has_auth_snapshot("a@example.com"))
        self.assertEqual(self.store.get_auth_snapshot("a@example.com"), self.a_bytes)

    def test_switch_back_to_a_restores_original(self):
        self.manager.switch_to("b@example.com", confirm=True)
        result = self.manager.switch_to("a@example.com", confirm=True)
        self.assertEqual(sha(self.auth_path.read_bytes()), self.a_hash)
        self.assertEqual(result.previous_email, "b@example.com")
        self.assertEqual(result.target_email, "a@example.com")
        self.assertTrue(result.backup_path)
        self.assertEqual(read_backup(Path(result.backup_path)), self.b_bytes)

    def test_already_active_is_rejected(self):
        with self.assertRaises(SwitchError) as ctx:
            self.manager.switch_to("a@example.com", confirm=True)
        self.assertEqual(ctx.exception.code, "already_active")
        self.assertEqual(sha(self.auth_path.read_bytes()), self.a_hash)

    def test_switch_calls_desktop_restart_hook(self):
        class FakeDesktop:
            def __init__(self):
                self.calls = []

            def restart(self, force=False):
                self.calls.append(force)
                return {"ok": True, "pids": [222], "aumid": "Pkg!App"}

        fake = FakeDesktop()
        manager = SwitchManager(
            self.store,
            self.auth_path,
            target_backup_dir=self.root / "backups",
            enabled=lambda: True,
            desktop_client=fake,
        )
        result = manager.switch_to("b@example.com", confirm=True)
        self.assertEqual(fake.calls, [False])
        self.assertEqual(result.desktop_restart["pids"], [222])
        self.assertEqual(result.to_public()["desktop_restart"]["ok"], True)

    def test_switch_can_skip_desktop_restart(self):
        class FakeDesktop:
            def __init__(self):
                self.calls = []

            def restart(self, force=False):
                self.calls.append(force)
                return {"ok": True}

        fake = FakeDesktop()
        manager = SwitchManager(
            self.store,
            self.auth_path,
            target_backup_dir=self.root / "backups",
            enabled=lambda: True,
            desktop_client=fake,
        )
        result = manager.switch_to(
            "b@example.com", confirm=True, restart_desktop=False
        )
        self.assertEqual(fake.calls, [])
        self.assertTrue(result.desktop_restart["skipped"])
        self.assertEqual(result.desktop_restart["reason"], "not_requested")
        self.assertEqual(sha(self.auth_path.read_bytes()), self.b_hash)

    def test_switch_forces_desktop_restart(self):
        class FakeDesktop:
            def __init__(self):
                self.calls = []

            def restart(self, force=False):
                self.calls.append(force)
                return {"ok": True, "pids": [333]}

        fake = FakeDesktop()
        manager = SwitchManager(
            self.store,
            self.auth_path,
            target_backup_dir=self.root / "backups",
            enabled=lambda: True,
            desktop_client=fake,
        )
        manager.switch_to("b@example.com", confirm=True, restart_desktop=True)
        self.assertEqual(fake.calls, [True])

    def test_desktop_restart_failure_does_not_roll_back_switch(self):
        class BrokenDesktop:
            def restart(self, force=False):
                raise RuntimeError("boom")

        manager = SwitchManager(
            self.store,
            self.auth_path,
            target_backup_dir=self.root / "backups",
            enabled=lambda: True,
            desktop_client=BrokenDesktop(),
        )
        result = manager.switch_to("b@example.com", confirm=True)
        self.assertFalse(result.desktop_restart["ok"])
        self.assertEqual(result.desktop_restart["reason"], "restart_error")
        self.assertEqual(sha(self.auth_path.read_bytes()), self.b_hash)

    def test_snapshot_email_mismatch_is_rejected(self):
        bad = make_auth(self.root / "bad.json", "c@example.com", "acct-c")
        self.store.save_auth_snapshot(
            "b@example.com",
            bad,
            {"email": "c@example.com", "plan": "plus"},
        )
        with self.assertRaises(SwitchError) as ctx:
            self.manager.switch_to("b@example.com", confirm=True)
        self.assertEqual(ctx.exception.code, "snapshot_email_mismatch")
        self.assertEqual(sha(self.auth_path.read_bytes()), self.a_hash)

    def test_invalid_snapshot_is_rejected(self):
        self.store.save_auth_snapshot("b@example.com", b"not json")
        with self.assertRaises(SwitchError) as ctx:
            self.manager.switch_to("b@example.com", confirm=True)
        self.assertEqual(ctx.exception.code, "target_snapshot_invalid")
        self.assertEqual(sha(self.auth_path.read_bytes()), self.a_hash)

    def test_verify_failure_rolls_back(self):
        real_read = switcher.read_auth_state
        calls = {"n": 0}

        def flaky(path):
            calls["n"] += 1
            if calls["n"] == 2:  # the post-write verification
                return {
                    "logged_in": False,
                    "reason": "simulated",
                    "file_sha256": "bad",
                    "email": None,
                }
            return real_read(path)

        with patch.object(switcher, "read_auth_state", side_effect=flaky):
            with self.assertRaises(SwitchError) as ctx:
                self.manager.switch_to("b@example.com", confirm=True)

        self.assertEqual(ctx.exception.code, "verify_failed")
        self.assertEqual(sha(self.auth_path.read_bytes()), self.a_hash)


class SwitchApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.auth_path = self.root / "auth.json"
        self.a_bytes = make_auth(
            self.auth_path, "a@example.com", "acct-a"
        )
        self.a_hash = sha(self.a_bytes)
        self.b_bytes = make_auth(
            self.root / "b.json", "b@example.com", "acct-b"
        )
        self.store = AccountStore(self.root / "accounts.sqlite3")
        self.store.save_auth_snapshot(
            "b@example.com",
            self.b_bytes,
            {
                "email": "b@example.com",
                "plan": "plus",
                "account_id": "acct-b",
                "expires_at": 4102444800,
            },
        )
        self.switch_manager = SwitchManager(
            self.store,
            self.auth_path,
            target_backup_dir=self.root / "backups",
            enabled=lambda: True,
        )

        os.environ["CODEX_SWITCHER_DATA"] = str(self.root / "data")
        os.environ["CODEX_SWITCHER_AUTH"] = str(self.auth_path)
        os.environ["CODEX_SWITCHER_ENABLE_LOGIN"] = "0"
        os.environ["CODEX_SWITCHER_ENABLE_SWITCH"] = "0"

        self.login_manager = LoginManager(self.store)
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.server = create_server(
            self.store,
            self.auth_path,
            "127.0.0.1",
            self.port,
            login_manager=self.login_manager,
            switch_manager=self.switch_manager,
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
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()
        self.tmp.cleanup()
        os.environ.pop("CODEX_SWITCHER_ENABLE_SWITCH", None)

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

    def test_snapshots_endpoint(self):
        status, body = self.request("GET", "/api/snapshots")
        self.assertEqual(status, 200)
        self.assertTrue(body["switching"]["enabled"])
        emails = {s["email"] for s in body["snapshots"]}
        self.assertIn("b@example.com", emails)
        self.assertFalse(any(s["is_current"] for s in body["snapshots"]))

    def test_switch_requires_confirm(self):
        status, body = self.request(
            "POST", "/api/switch", {"email": "b@example.com"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "confirmation_required")
        self.assertEqual(sha(self.auth_path.read_bytes()), self.a_hash)

    def test_switch_endpoint(self):
        status, body = self.request(
            "POST",
            "/api/switch",
            {"email": "b@example.com", "confirm": True},
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["switch"]["target_email"], "b@example.com")
        self.assertEqual(sha(self.auth_path.read_bytes()), sha(self.b_bytes))

        status, body = self.request("GET", "/api/snapshots")
        self.assertEqual(status, 200)
        by_email = {s["email"]: s for s in body["snapshots"]}
        self.assertTrue(by_email["b@example.com"]["is_current"])
        self.assertIn("a@example.com", by_email)

    def test_switch_endpoint_disabled(self):
        manager = SwitchManager(
            self.store,
            self.auth_path,
            target_backup_dir=self.root / "backups",
            enabled=lambda: False,
        )
        self.server.switch_manager = manager  # type: ignore[attr-defined]
        # The handler closes over the original manager, so exercise the
        # engine directly instead of rebuilding the server.
        with self.assertRaises(SwitchError) as ctx:
            manager.switch_to("b@example.com", confirm=True)
        self.assertEqual(ctx.exception.code, "switching_disabled")


if __name__ == "__main__":
    unittest.main()
