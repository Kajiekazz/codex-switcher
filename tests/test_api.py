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
from unittest import mock

from codex_switcher import settings, sms
from codex_switcher.api import _existing_instance, create_server
from codex_switcher.goal_resume import GoalResumeService
from codex_switcher.store import AccountStore


def b64url(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def fake_auth_file(path: Path) -> None:
    header = b64url({"alg": "none", "typ": "JWT"})
    payload = b64url(
        {
            "email": "test@example.com",
            "name": "Test",
            "exp": 4102444800,
            "https://api.openai.com/auth": {
                "chatgpt_plan_type": "plus",
                "chatgpt_account_id": "acct-test",
                "chatgpt_user_id": "user-test",
            },
        }
    )
    token = f"{header}.{payload}.sig"
    path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "id_token": token,
                    "access_token": token,
                    "account_id": "acct-test",
                },
                "last_refresh": "2026-10-30T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _FakeDesktop:
    def __init__(self):
        self.restart_calls = []
        self.wake_calls = []
        self.status_payload = {
            "ok": True,
            "available": True,
            "enabled": False,
            "running": True,
            "pids": [4321],
            "aumid": "OpenAI.Codex_test!App",
        }

    def status(self) -> dict:
        return dict(self.status_payload)

    def wake_thread(self, thread_id: str) -> dict:
        self.wake_calls.append(thread_id)
        if not self.status_payload.get("running"):
            return {"ok": False, "skipped": True, "reason": "desktop_not_running"}
        return {
            "ok": True,
            "skipped": False,
            "url": f"codex://threads/wake/{thread_id}",
        }

    def processes(self) -> dict:
        return {
            "ok": True,
            "processes": [
                {"pid": 4321, "kind": "desktop", "name": "ChatGPT.exe"},
                {
                    "pid": 7001,
                    "kind": "app-server",
                    "name": "codex.exe",
                    "owner": "codex-switcher",
                },
            ],
            "counts": {"desktop": 1, "app-server": 1, "cli": 0},
            "service_pid": 4321,
        }

    def restart(self, force: bool = False) -> dict:
        self.restart_calls.append(force)
        return {
            "ok": True,
            "skipped": False,
            "previous_pids": [4321],
            "pids": [555],
            "message": "桌面端已重启",
        }


class _FakeQuotaService:
    def __init__(self):
        self.invalidated = 0

    def current(self, force: bool = False) -> dict:
        return {
            "ok": True,
            "source": "live",
            "email": "test@example.com",
            "plan": "plus",
            "allowed": True,
            "limit_reached": False,
            "primary_window": {"used_percent": 12, "reset_at": 1900000000},
            "secondary_window": {"used_percent": 3},
            "fetched_at": "2026-01-01T00:00:00+00:00",
        }

    def for_email(
        self, email: str, force: bool = False, ttl=None, scope: str = "live"
    ) -> dict:
        if email.lower() == "test@example.com":
            return self.current(force=force)
        return {
            "ok": False,
            "error": "snapshot_not_found",
            "message": "missing",
        }

    def invalidate(self, email: str | None = None) -> None:
        self.invalidated += 1


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.auth_path = self.tmp_path / "auth.json"
        fake_auth_file(self.auth_path)
        self.auth_hash_before = hashlib.sha256(self.auth_path.read_bytes()).hexdigest()

        os.environ["CODEX_SWITCHER_DATA"] = str(self.tmp_path / "data")
        os.environ["CODEX_SWITCHER_AUTH"] = str(self.auth_path)
        os.environ["CODEX_SWITCHER_ENABLE_LOGIN"] = "0"
        os.environ["CODEX_SWITCHER_ENABLE_SWITCH"] = "0"
        os.environ["CODEX_SWITCHER_QUEUE_DB"] = str(self.tmp_path / "queue_1.sqlite")
        settings.reset_cache()

        self.store = AccountStore(self.tmp_path / "accounts.sqlite3")
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.quota_service = _FakeQuotaService()
        self.desktop = _FakeDesktop()
        self.goal_session_factory = mock.Mock(
            side_effect=AssertionError("API validation tests must not start a real session")
        )
        self.addCleanup(self.goal_session_factory.assert_not_called)
        self.goal_resume = GoalResumeService(
            session_factory=self.goal_session_factory,
            goals_provider=lambda: [],
            execution_provider=lambda ids: {},
            sessions_provider=lambda **kwargs: {
                "total": 0, "pinned": [], "projects": [], "recent": [], "counts": {},
            },
            history_path=self.tmp_path / "goal-resume-history.jsonl",
            desktop_client=self.desktop,
        )
        self.server = create_server(
            self.store,
            self.auth_path,
            "127.0.0.1",
            self.port,
            quota_service=self.quota_service,
            desktop_client=self.desktop,
            goal_resume=self.goal_resume,
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
        settings.reset_cache()

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
                raw = resp.read().decode("utf-8")
                return resp.status, dict(resp.headers), json.loads(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            headers = dict(exc.headers)
            code = exc.code
            exc.close()
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {"raw": raw}
            return code, headers, payload

    def test_health(self):
        status, _, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["switching_enabled"])
        self.assertTrue(body["switching_implemented"])
        self.assertEqual(body["session_api_version"], 5)

    def test_accounts_quotas_skips_accounts_without_snapshot(self):
        self.store.upsert({"email": "nopass@example.com", "password": "x"})
        status, _, body = self.request("GET", "/api/accounts/quotas")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["quotas"], {})
        self.assertEqual(body["skipped"], ["nopass@example.com"])

    def test_accounts_quotas_uses_snapshot_token(self):
        fake_auth_file(self.auth_path)
        snapshot_bytes = self.auth_path.read_bytes()
        self.store.upsert({"email": "test@example.com", "password": "x"})
        self.store.save_auth_snapshot("test@example.com", snapshot_bytes)
        status, _, body = self.request("GET", "/api/accounts/quotas")
        self.assertEqual(status, 200)
        quota = body["quotas"]["test@example.com"]
        self.assertTrue(quota["ok"])
        self.assertEqual(quota["plan"], "plus")
        self.assertEqual(quota["primary_window"]["used_percent"], 12)

    def test_settings_get_never_leaks_secrets(self):
        settings.save({"sms_api_key": "leak-me-1234"})
        status, _, body = self.request("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["groups"])
        keys = {item["key"] for item in body["items"]}
        self.assertIn("sms_provider", keys)
        self.assertIn("login_driver", keys)
        item = next(entry for entry in body["items"] if entry["key"] == "sms_api_key")
        self.assertEqual(item["value"], "****1234")
        self.assertNotIn("leak-me-1234", json.dumps(body))

    def test_settings_post_requires_confirm(self):
        status, _, body = self.request(
            "POST", "/api/settings", {"values": {"sms_provider": "grizzly"}}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "confirmation_required")

    def test_settings_post_saves_and_applies(self):
        status, _, body = self.request(
            "POST",
            "/api/settings",
            {
                "confirm": True,
                "values": {"sms_provider": "grizzly", "sms_api_key": "abc12345"},
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("sms_provider", body["changed"])
        self.assertEqual(body["restart_required"], [])
        _, _, health = self.request("GET", "/api/health")
        self.assertTrue(health["sms_enabled"])
        self.assertEqual(health["sms_provider"], "grizzly")

    def test_settings_post_rejects_unknown_key(self):
        status, _, body = self.request(
            "POST", "/api/settings", {"confirm": True, "values": {"nope": "x"}}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_settings")

    def test_switching_toggle_requires_confirm_and_valid_key(self):
        status, _, body = self.request(
            "POST", "/api/switching/toggle", {"key": "enable_switch", "value": True}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "confirmation_required")
        status, _, body = self.request(
            "POST",
            "/api/switching/toggle",
            {"key": "nope", "value": True, "confirm": True},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad_key")

    def test_manual_switching_has_no_disable_toggle(self):
        status, _, body = self.request(
            "POST",
            "/api/switching/toggle",
            {"key": "enable_switch", "value": True, "confirm": True},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad_key")
        status, _, body = self.request(
            "POST",
            "/api/switching/toggle",
            {"key": "enable_switch", "value": False, "confirm": True},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad_key")
        _, _, health = self.request("GET", "/api/health")
        self.assertTrue(health["switching_enabled"])

    def test_switching_toggle_auto_switch_is_hot_applied(self):
        status, _, body = self.request(
            "POST",
            "/api/switching/toggle",
            {"key": "enable_auto_switch", "value": True, "confirm": True},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["auto_switch"]["enabled"])
        self.assertTrue(body["auto_switch"]["running"])
        status, _, body = self.request(
            "POST",
            "/api/switching/toggle",
            {"key": "enable_auto_switch", "value": False, "confirm": True},
        )
        self.assertEqual(status, 200)
        self.assertFalse(body["auto_switch"]["enabled"])

    def test_accounts_reorder_sets_priority(self):
        for email in ("a@example.com", "b@example.com", "c@example.com"):
            self.store.upsert({"email": email, "password": "x"})
        status, _, body = self.request(
            "POST",
            "/api/accounts/reorder",
            {"emails": ["c@example.com", "a@example.com", "b@example.com"]},
        )
        self.assertEqual(status, 200)
        order = [account["email"] for account in body["accounts"]]
        self.assertEqual(order, ["c@example.com", "a@example.com", "b@example.com"])
        priorities = [account["priority"] for account in body["accounts"]]
        self.assertEqual(priorities, [100, 99, 98])

    def test_accounts_reorder_rejects_unknown_and_duplicates(self):
        self.store.upsert({"email": "a@example.com", "password": "x"})
        status, _, body = self.request(
            "POST", "/api/accounts/reorder", {"emails": ["a@example.com", "zz@example.com"]}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "unknown_email")
        status, _, body = self.request(
            "POST",
            "/api/accounts/reorder",
            {"emails": ["a@example.com", "A@example.com"]},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "duplicate_email")

    def test_sms_status_endpoint(self):
        with mock.patch.object(sms, "enabled", return_value=True), mock.patch.object(
            sms, "provider", return_value="grizzly"
        ), mock.patch.object(
            sms,
            "balance",
            return_value={"ok": True, "balance": 2.45, "provider": "grizzly"},
        ), mock.patch.object(
            sms,
            "activations",
            return_value=[
                {
                    "activation_id": "777",
                    "phone": "447700900123",
                    "age_seconds": 3,
                    "provider": "grizzly",
                }
            ],
        ), mock.patch.object(
            sms,
            "last_code",
            return_value={"code": "123456", "activation_id": "777", "at": None},
        ), mock.patch.object(
            sms,
            "recent_events",
            return_value=[{"ts": None, "level": "info", "message": "取号成功"}],
        ):
            status, _, body = self.request("GET", "/api/sms/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["enabled"])
        self.assertEqual(body["balance"]["balance"], 2.45)
        self.assertEqual(body["activations"][0]["phone"], "447700900123")
        self.assertEqual(body["last_code"]["code"], "123456")
        self.assertEqual(len(body["events"]), 1)

    def test_sms_test_and_cancel_endpoints(self):
        with mock.patch.object(
            sms, "acquire_number", return_value=("777", "447700900123")
        ) as acquired, mock.patch.object(sms, "cancel") as cancel, mock.patch.object(
            sms, "provider", return_value="grizzly"
        ), mock.patch.object(
            sms, "activations", return_value=[{"activation_id": "777"}]
        ):
            status, _, body = self.request("POST", "/api/sms/test", {"confirm": True})
            self.assertEqual(status, 200)
            self.assertEqual(body["phone"], "447700900123")
            acquired.assert_called_once()
            cancel.assert_called_with("777")
            status, _, body = self.request("POST", "/api/sms/test", {})
            self.assertEqual(status, 400)
            self.assertEqual(body["error"], "confirmation_required")
            status, _, body = self.request("POST", "/api/sms/cancel", {"confirm": True})
            self.assertEqual(status, 200)
            self.assertEqual(body["cancelled"], ["777"])
            status, _, body = self.request("POST", "/api/sms/cancel", {})
            self.assertEqual(status, 400)
            self.assertEqual(body["error"], "confirmation_required")

    def test_settings_post_can_clear(self):
        settings.save({"sms_provider": "grizzly"})
        status, _, body = self.request(
            "POST", "/api/settings", {"confirm": True, "clear": ["sms_provider"]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["cleared"], ["sms_provider"])
        self.assertIsNone(settings.get("sms_provider"))

    def test_status_reads_auth(self):
        status, _, body = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["auth"]["logged_in"])
        self.assertEqual(body["auth"]["email"], "test@example.com")
        self.assertEqual(body["auth"]["plan"], "plus")
        self.assertTrue(body["switching"]["enabled"])

    def test_quota_and_auto_switch_endpoints(self):
        status, _, body = self.request("GET", "/api/quota")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["quota"]["ok"])
        self.assertEqual(body["quota"]["plan"], "plus")
        self.assertEqual(body["quota"]["primary_window"]["used_percent"], 12)
        serialized = json.dumps(body)
        self.assertNotIn("access_token", serialized)
        self.assertNotIn("refresh_token", serialized)

        status, _, body = self.request("GET", "/api/auto-switch/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("enabled", body["auto_switch"])
        self.assertIn("switches_last_hour", body["auto_switch"])

        status, _, body = self.request(
            "GET", "/api/quota?email=missing@example.com"
        )
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])
        self.assertEqual(body["quota"]["error"], "snapshot_not_found")

    def test_desktop_status_and_restart(self):
        status, _, body = self.request("GET", "/api/desktop/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["desktop"]["running"])
        self.assertEqual(body["desktop"]["pids"], [4321])
        self.assertFalse(body["desktop"]["enabled"])

        status, _, body = self.request("POST", "/api/desktop/restart", {})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "confirmation_required")
        self.assertEqual(self.desktop.restart_calls, [])

        status, _, body = self.request(
            "POST", "/api/desktop/restart", {"confirm": True}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["desktop"]["pids"], [555])
        self.assertEqual(self.desktop.restart_calls, [True])

        status, _, body = self.request("GET", "/api/desktop/processes")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["counts"], {"desktop": 1, "app-server": 1, "cli": 0})
        self.assertEqual(body["processes"][1]["owner"], "codex-switcher")

    def test_second_instance_is_detected(self):
        # SO_REUSEADDR on Windows lets a second process bind the same port;
        # run() refuses to start when another service already answers.
        live = _existing_instance("127.0.0.1", self.port)
        self.assertIsNotNone(live)
        self.assertTrue(live["ok"])
        self.assertIn("version", live)
        self.assertIsNone(_existing_instance("127.0.0.1", free_port()))

    def test_accounts_and_snapshots_expose_display_name(self):
        self.store.upsert({"email": "named@example.com", "password": "p"})
        self.store.save_auth_snapshot(
            "named@example.com",
            b'{"tokens": {}}',
            {"email": "named@example.com", "name": "Ada Lovelace", "plan": "plus"},
        )

        status, _, body = self.request("GET", "/api/accounts")
        self.assertEqual(status, 200)
        row = next(a for a in body["accounts"] if a["email"] == "named@example.com")
        self.assertEqual(row["name"], "Ada Lovelace")
        self.assertNotIn("password", row)

        status, _, body = self.request("GET", "/api/snapshots")
        self.assertEqual(status, 200)
        snap = next(s for s in body["snapshots"] if s["email"] == "named@example.com")
        self.assertEqual(snap["name"], "Ada Lovelace")

    def test_dashboard(self):
        req = urllib.request.Request(self.base + "/")
        with urllib.request.urlopen(req, timeout=5) as resp:
            html = resp.read().decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn("Codex Switcher", html)
        self.assertIn("侧边栏扩展", html)
        self.assertIn("登录助手", html)

    def test_userscript_endpoint(self):
        # Retired surface: the URL is kept as an archive of the old panel.
        req = urllib.request.Request(
            self.base + "/userscript/codex-switcher.user.js"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
        self.assertEqual(resp.status, 200)
        body = raw.decode("utf-8")
        self.assertIn("==UserScript==", body)
        self.assertIn("Codex 切号助手", body)
        # The route must prefer the archived copy under legacy/ after retirement.
        archived = (
            Path(__file__).resolve().parent.parent
            / "legacy"
            / "codex-switcher.user.js"
        )
        if archived.exists():
            self.assertEqual(raw, archived.read_bytes())

    def test_extension_status_describes_shipped_manifest(self):
        status, _, body = self.request("GET", "/api/extension/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        ext = body["extension"]
        self.assertTrue(ext["present"], "extension/manifest.json must ship with the repo")
        self.assertEqual(ext["version"], "0.6.0")
        self.assertEqual(ext["min_chrome"], "114")
        # Chrome ids are 32 chars in the a-p range.
        self.assertEqual(len(ext["id"]), 32)
        self.assertTrue(all("a" <= ch <= "p" for ch in ext["id"]))
        self.assertIn("installed", body)
        self.assertIn("browsers", body)
        # Chrome 156 keeps extension state in "Secure Preferences"; reading only
        # "Preferences" is why the dashboard used to say "未安装" for a loaded
        # extension. The scan must look at both files.
        self.assertEqual(body["preference_files"], ["Secure Preferences", "Preferences"])

    def test_extension_detection_reads_secure_preferences(self):
        import tempfile

        from codex_switcher import extension_install as ext_mod

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name) / "User Data"
        profile = base / "Default"
        profile.mkdir(parents=True)
        ext_dir = Path(tmp.name) / "ext"
        ext_dir.mkdir()
        (ext_dir / "manifest.json").write_text(
            json.dumps({"name": "Codex 切号助手", "version": "0.5.0"}),
            encoding="utf-8",
        )
        ext_id = ext_mod.unpacked_id(ext_dir)
        # Preferences: empty settings (that is what Chrome 156 really writes).
        (profile / "Preferences").write_text(
            json.dumps({"extensions": {"settings": {}}}), encoding="utf-8"
        )
        (profile / "Secure Preferences").write_text(
            json.dumps(
                {
                    "extensions": {
                        "settings": {
                            ext_id: {
                                "location": 4,
                                "path": str(ext_dir),
                                "disable_reasons": [],
                                "service_worker_registration_info": {"version": "0.5.0"},
                            }
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        original = ext_mod.user_data_dirs
        ext_mod.user_data_dirs = lambda: [("Chrome", base)]
        try:
            hits = ext_mod.scan_profiles(ext_id)
        finally:
            ext_mod.user_data_dirs = original
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["browser"], "Chrome")
        self.assertEqual(hits[0]["profile"], "Default")
        self.assertEqual(hits[0]["version"], "0.5.0")
        self.assertTrue(hits[0]["unpacked"])
        self.assertTrue(hits[0]["enabled"])

    def test_extension_detection_flags_a_copy_loaded_from_another_path(self):
        import tempfile

        from codex_switcher import extension_install as ext_mod

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name) / "User Data"
        profile = base / "Profile 1"
        profile.mkdir(parents=True)
        other = Path(tmp.name) / "copy-of-extension"
        other.mkdir()
        (other / "manifest.json").write_text(
            json.dumps({"name": "Codex 切号助手", "version": "0.5.0"}),
            encoding="utf-8",
        )
        other_id = ext_mod.unpacked_id(other)
        (profile / "Secure Preferences").write_text(
            json.dumps(
                {
                    "extensions": {
                        "settings": {
                            other_id: {"location": 4, "path": str(other)}
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        original = ext_mod.user_data_dirs
        ext_mod.user_data_dirs = lambda: [("Chrome", base)]
        try:
            hits = ext_mod.scan_profiles(
                "a" * 32, {"name": "Codex 切号助手", "version": "0.5.0"}
            )
        finally:
            ext_mod.user_data_dirs = original
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0]["id_mismatch"])
        self.assertEqual(hits[0]["expected_id"], "a" * 32)

    def test_theme_roundtrip(self):
        status, _, body = self.request("GET", "/api/theme")
        self.assertEqual(status, 200)
        self.assertIn(body["theme"], ("light", "dark", "auto"))
        original = body["theme"]
        status, _, saved = self.request("POST", "/api/theme", {"theme": "dark"})
        self.assertEqual(status, 200)
        self.assertEqual(saved["theme"], "dark")
        status, _, again = self.request("GET", "/api/theme")
        self.assertEqual(again["theme"], "dark")
        # restore so the developer's own dashboard is not repainted by a test
        self.request("POST", "/api/theme", {"theme": original})

    def test_theme_rejects_unknown_value(self):
        status, _, body = self.request("POST", "/api/theme", {"theme": "neon"})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "invalid_theme")

    def test_goals_create_requires_confirm_and_thread_ids(self):
        status, _, body = self.request(
            "POST", "/api/goals/create", {"thread_ids": ["x"], "objective": "y"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "confirmation_required")
        status, _, body = self.request(
            "POST", "/api/goals/create", {"confirm": True, "objective": "y"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "thread_ids_required")

    def test_pause_requires_confirm_and_valid_explicit_targets(self):
        for payload, expected in (
            ({"thread_ids": ["chat"]}, "confirmation_required"),
            ({"confirm": True}, "thread_ids_required"),
            ({"confirm": True, "thread_ids": []}, "thread_required"),
            ({"confirm": True, "thread_ids": [str(i) for i in range(51)]}, "too_many_threads"),
        ):
            status, _, body = self.request("POST", "/api/goals/pause", payload)
            self.assertEqual(status, 400)
            self.assertEqual(body["error"], expected)

    def test_pause_returns_partial_results_and_manual_stop_requirement(self):
        outcome = {"ok": False, "results": [{"thread_id": "chat", "goal_paused": True,
                   "needs_manual_stop": True, "state": "needs_manual_stop", "error": "fixture_partial"}]}
        with mock.patch.object(self.goal_resume, "pause", return_value=outcome) as pause:
            status, _, body = self.request("POST", "/api/goals/pause", {"confirm": True, "thread_ids": ["chat"]})
        self.assertEqual(status, 207)
        self.assertTrue(body["results"][0]["needs_manual_stop"])
        pause.assert_called_once_with(["chat"])

    def test_remove_validates_confirmation_and_managed_targets(self):
        for payload, error in (({"thread_ids": ["chat"]}, "confirmation_required"),
                               ({"confirm": True}, "thread_ids_required"),
                               ({"confirm": True, "thread_ids": []}, "thread_required"),
                               ({"confirm": True, "thread_ids": ["unmanaged"]}, "not_managed")):
            status, _, body = self.request("POST", "/api/goals/remove", payload)
            self.assertEqual(status, 400)
            self.assertEqual(body["error"], error)

    def test_extension_install_dry_run_has_no_side_effects(self):
        status, _, body = self.request(
            "POST", "/api/extension/install", {"copy": False}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["clipboard"])
        # Chrome drops chrome:// URLs from the command line, so there is no
        # browser launch at all — the endpoint must not claim otherwise.
        self.assertFalse(body["browser_opened"])
        self.assertTrue(body["browser_blocked"])
        self.assertEqual(body["copy_target"], "path")
        self.assertEqual(body["clipboard_text"], body["extension"]["path"])
        self.assertEqual(len(body["steps"]), 4)
        self.assertIn("🧩", " ".join(body["steps"]))

    def test_extension_install_can_target_the_extensions_url(self):
        status, _, body = self.request(
            "POST", "/api/extension/install", {"copy": False, "target": "url"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["copy_target"], "url")
        self.assertEqual(body["clipboard_text"], "chrome://extensions")
        self.assertEqual(len(body["steps"]), 3)

    def test_import_and_list(self):
        text = (
            "a@example.com-----pass-a---JBSWY3DPEHPK3PXP\n"
            "b@example.com-----pass-b\n"
            "not-an-email-----x---JBSWY3DPEHPK3PXP\n"
        )
        status, _, body = self.request(
            "POST", "/api/accounts/import", {"text": text}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["inserted"], 2)
        self.assertEqual(body["updated"], 0)
        self.assertEqual(len(body["errors"]), 1)

        status, _, body = self.request("GET", "/api/accounts")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["accounts"]), 2)
        for account in body["accounts"]:
            self.assertNotIn("password", account)
            self.assertNotIn("totp", account)
            self.assertNotIn("secret_blob", account)
            self.assertIn("***@", account["email_masked"])

        status, _, body = self.request("GET", "/api/status")
        self.assertEqual(body["accounts"]["total"], 2)

    def test_manual_switch_is_available_and_still_requires_confirmation(self):
        status, _, body = self.request("POST", "/api/switch", {})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "confirmation_required")
        self.assertEqual(hashlib.sha256(self.auth_path.read_bytes()).hexdigest(), self.auth_hash_before)

    def test_manual_reset_is_independent_of_switch_permission(self):
        with mock.patch("codex_switcher.api.config.switching_enabled", return_value=False), mock.patch.object(
            self.quota_service, "consume_reset", create=True,
            return_value={"ok": True, "windows_reset": 2},
        ) as consume:
            status, _, body = self.request("POST", "/api/quota/reset", {"email": "test@example.com", "confirm": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        consume.assert_called_once_with(email="test@example.com", credit_id=None)

    def test_manual_reset_still_requires_confirmation(self):
        with mock.patch.object(self.quota_service, "consume_reset", create=True) as consume:
            status, _, body = self.request("POST", "/api/quota/reset", {})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "confirmation_required")
        consume.assert_not_called()

    def test_manual_reset_returns_recovery_separately_from_redemption_success(self):
        with mock.patch.object(self.quota_service, "consume_reset", create=True,
                               return_value={"ok": True, "windows_reset": 2}), mock.patch.object(
            self.server.auto_switch, "on_manual_reset", return_value={"ok": False, "error": "resume_failed"}
        ) as recover:
            status, _, body = self.request("POST", "/api/quota/reset", {"confirm": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["auto_resume"]["error"], "resume_failed")
        self.assertEqual(recover.call_count, 1)

    def test_login_disabled_by_default(self):
        status, _, body = self.request(
            "POST", "/api/login/start", {"email": "a@example.com"}
        )
        self.assertEqual(status, 423)
        self.assertEqual(body["error"], "login_disabled")
        self.assertFalse(body["login_enabled"])

        status, _, body = self.request("GET", "/api/login/status")
        self.assertEqual(status, 200)
        self.assertFalse(body["login_enabled"])
        self.assertEqual(body["sessions"], [])

    def test_resume_validation_never_restarts_desktop(self):
        cases = [
            (False, ["thread"], True, "goal_resume_disabled", 403),
            (True, ["thread"], False, "confirmation_required", 400),
            (True, [], True, "thread_required", 400),
            (True, [f"thread-{i}" for i in range(11)], True, "too_many_threads", 400),
        ]
        for enabled, ids, confirm, error, expected_status in cases:
            with self.subTest(error=error), mock.patch(
                "codex_switcher.config.goal_resume_enabled", return_value=enabled
            ):
                status, _, body = self.request(
                    "POST", "/api/goals/resume",
                    {
                        "thread_ids": ids,
                        "dry_run": False,
                        "confirm": confirm,
                        "restart_desktop": True,
                        "takeover_desktop": True,
                    },
                )
                self.assertEqual(status, expected_status)
                self.assertEqual(body["error"], error)
                self.assertEqual(self.desktop.restart_calls, [])

    def test_busy_resume_does_not_restart_desktop(self):
        with mock.patch("codex_switcher.config.goal_resume_enabled", return_value=True), mock.patch.object(
            self.server.goal_resume, "_thread", mock.Mock(is_alive=lambda: True)
        ):
            status, _, body = self.request(
                "POST", "/api/goals/resume",
                {"thread_ids": ["thread"], "dry_run": False, "confirm": True, "restart_desktop": True},
            )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "job_running")
        self.assertEqual(self.desktop.restart_calls, [])

    def test_resume_rejects_non_boolean_options_before_start(self):
        for key in ("dry_run", "confirm", "restart_desktop", "takeover_desktop", "continue_once"):
            with self.subTest(key=key), mock.patch.object(
                self.server.goal_resume, "start", return_value={"id": "fake"}
            ) as start:
                status, _, body = self.request(
                    "POST", "/api/goals/resume", {"thread_ids": ["thread"], key: "false"}
                )
                self.assertEqual(status, 400)
                self.assertEqual(body["error"], "invalid_option")
                start.assert_not_called()
                self.assertEqual(self.desktop.restart_calls, [])

    def test_resume_passes_desktop_options_to_validated_job(self):
        for takeover in (None, False, True):
            with self.subTest(takeover=takeover), mock.patch.object(
                self.server.goal_resume, "start", return_value={"id": "fake"}
            ) as start:
                payload = {
                    "thread_ids": ["thread"], "dry_run": False, "confirm": True,
                    "restart_desktop": True,
                }
                if takeover is not None:
                    payload["takeover_desktop"] = takeover
                status, _, body = self.request("POST", "/api/goals/resume", payload)
                self.assertEqual(status, 200)
                self.assertEqual(body["job"]["id"], "fake")
                start.assert_called_once_with(
                    ["thread"], dry_run=False, confirm=True,
                    restart_desktop=True, takeover_desktop=takeover,
                    continue_once=False, skip_running=True,
                )
                self.assertEqual(self.desktop.restart_calls, [])

    def test_resume_running_chat_returns_a_normal_noop_without_changing_history(self):
        self.server.goal_resume._execution_provider = lambda ids: {tid: {"state": "running"} for tid in ids}
        history = list(self.server.goal_resume._history)
        status, _, body = self.request("POST", "/api/goals/resume", {
            "thread_ids": ["busy"], "dry_run": False, "confirm": True, "continue_once": False})
        self.assertEqual(status, 200)
        self.assertTrue(body["job"]["noop"])
        self.assertEqual(body["job"]["state"], "already_running")
        self.assertEqual(body["job"]["skipped_running"], ["busy"])
        self.assertEqual(self.server.goal_resume._history, history)

    def test_create_goal_disabled_is_read_only(self):
        with mock.patch("codex_switcher.config.goal_resume_enabled", return_value=False), mock.patch.object(
            self.server.goal_resume, "_session_factory"
        ) as factory:
            status, _, body = self.request(
                "POST", "/api/goals/create",
                {"thread_ids": ["thread"], "objective": "test", "confirm": True},
            )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "goal_resume_disabled")
        factory.assert_not_called()

    def test_options_cors(self):
        req = urllib.request.Request(self.base + "/api/health", method="OPTIONS")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 204)
            self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")

    def test_account_crud_and_metadata(self):
        status, _, body = self.request(
            "POST",
            "/api/accounts/add",
            {
                "email": "a@example.com",
                "password": "secret-pass",
                "totp": "JBSWY3DPEHPK3PXP",
                "tags": ["work", "backup"],
                "priority": 250,
                "note": "主号",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["result"], "inserted")
        self.assertEqual(body["account"]["tags"], "work,backup")
        self.assertEqual(body["account"]["priority"], 250)
        self.assertNotIn("password", body["account"])
        self.assertNotIn("totp", body["account"])
        self.assertTrue(body["account"]["has_password"])

        status, _, body = self.request(
            "POST",
            "/api/accounts/update",
            {"email": "a@example.com", "tags": "x, y", "priority": 5, "disabled": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["account"]["tags"], "x,y")
        self.assertEqual(body["account"]["priority"], 5)
        self.assertTrue(body["account"]["disabled"])
        self.assertNotIn("password", body["account"])

        status, _, body = self.request(
            "POST",
            "/api/accounts/update",
            {"email": "a@example.com", "password": "rotated"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["account"]["has_password"])
        self.assertNotIn("rotated", json.dumps(body))

        status, _, body = self.request("GET", "/api/accounts")
        self.assertEqual(len(body["accounts"]), 1)
        self.assertEqual(body["accounts"][0]["tags"], "x,y")

    def test_account_validation_errors(self):
        status, _, body = self.request(
            "POST", "/api/accounts/add", {"email": "not-an-email"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_email")

        status, _, body = self.request(
            "POST",
            "/api/accounts/add",
            {"email": "a@example.com", "totp": "not base32!"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_totp")

        self.request("POST", "/api/accounts/add", {"email": "a@example.com"})
        status, _, body = self.request(
            "POST", "/api/accounts/update", {"email": "a@example.com", "priority": 5000}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad_request")

        status, _, body = self.request(
            "POST", "/api/accounts/update", {"email": "missing@example.com", "tags": "x"}
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "account_not_found")

    def test_account_delete_requires_confirm(self):
        self.request("POST", "/api/accounts/add", {"email": "a@example.com"})

        status, _, body = self.request(
            "POST", "/api/accounts/delete", {"email": "a@example.com"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "confirmation_required")

        status, _, body = self.request(
            "POST",
            "/api/accounts/delete",
            {"email": "a@example.com", "confirm": True},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["deleted"]["account"])

        status, _, body = self.request("GET", "/api/accounts")
        self.assertEqual(body["accounts"], [])

        status, _, body = self.request(
            "POST",
            "/api/accounts/delete",
            {"email": "a@example.com", "confirm": True},
        )
        self.assertEqual(status, 404)

    def test_quota_history_endpoint(self):
        status, _, body = self.request("GET", "/api/quota/history?hours=6")
        self.assertEqual(status, 200)
        self.assertEqual(body["email"], "test@example.com")
        self.assertEqual(body["samples"], [])
        self.assertIn("prediction", body)
        self.assertFalse(body["prediction"]["primary"]["available"])
        serialized = json.dumps(body)
        self.assertNotIn("access_token", serialized)

        status, _, body = self.request(
            "GET", "/api/quota/history?email=missing@example.com"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["samples"], [])

    def test_auth_file_unchanged(self):
        self.request("GET", "/api/status")
        self.request(
            "POST",
            "/api/accounts/import",
            {"text": "a@example.com-----p---JBSWY3DPEHPK3PXP"},
        )
        self.request("POST", "/api/switch", {})
        after = hashlib.sha256(self.auth_path.read_bytes()).hexdigest()
        self.assertEqual(after, self.auth_hash_before)


if __name__ == "__main__":
    unittest.main()
