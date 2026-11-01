import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from codex_switcher import config, settings, sms
from codex_switcher.login import (
    code_page_uses_whatsapp,
    is_phone_prompt,
    is_sms_unavailable,
    mask_phone,
    phone_e164,
)


SMS_ENV_KEYS = (
    "CODEX_SWITCHER_SMS_PROVIDER",
    "SMS_PROVIDER",
    "CODEX_SWITCHER_SMS_SERVICE",
    "SMS_SERVICE",
    "CODEX_SWITCHER_SMS_COUNTRY",
    "SMS_COUNTRY",
    "CODEX_SWITCHER_SMS_MAX_PRICE",
    "SMS_MAX_PRICE",
    "CODEX_SWITCHER_SMS_CODE_WAIT",
    "SMS_CODE_WAIT",
    "CODEX_SWITCHER_SMS_POLL_INTERVAL",
    "SMS_POLL_INTERVAL",
    "CODEX_SWITCHER_SMS_MAX_RETRIES",
    "SMS_MAX_RETRIES",
    "CODEX_SWITCHER_SMS_REQUEST_TIMEOUT",
    "SMS_REQUEST_TIMEOUT",
    "CODEX_SWITCHER_SMS_API_BASE",
    "SMS_API_BASE",
    "CODEX_SWITCHER_SMS_API_KEY",
    "SMS_API_KEY",
    "CODEX_SWITCHER_SMSBOWER_API_BASE",
    "SMSBOWER_API_BASE",
    "CODEX_SWITCHER_SMSBOWER_API_KEY",
    "SMSBOWER_API_KEY",
    "CODEX_SWITCHER_SMSBOWER_USE_V2",
    "SMSBOWER_USE_V2",
    "CODEX_SWITCHER_L_API_BASE",
    "L_API_BASE",
    "CODEX_SWITCHER_L_ADMIN_AUTH_CODE",
    "L_ADMIN_AUTH_CODE",
    "CODEX_SWITCHER_L_PHONE_PREFIX",
    "L_PHONE_PREFIX",
    "CODEX_SWITCHER_H_API_BASE",
    "H_API_BASE",
    "CODEX_SWITCHER_H_ADMIN_AUTH_CODE",
    "H_ADMIN_AUTH_CODE",
    "CODEX_SWITCHER_H_PHONE_PREFIX",
    "H_PHONE_PREFIX",
    "CODEX_SWITCHER_H_PHONE_ACQUIRE_MODE",
    "H_PHONE_ACQUIRE_MODE",
)


class FakeProviderHandler(BaseHTTPRequestHandler):
    state: dict = {}

    def log_message(self, *args):  # noqa: D401 - silence the test server
        return

    def _send(self, text: str, status: int = 200) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 - http.server API
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.state["last_ua"] = self.headers.get("User-Agent") or ""
        self.state.setdefault("calls", []).append(("GET", parsed.path, query))
        action = query.get("action")
        if action == "getNumber":
            self._send(self.state.get("get_number", "ACCESS_NUMBER:777:447700900123"))
        elif action == "getStatus":
            self._send(self.state.get("get_status", "STATUS_OK:123456"))
        elif action == "getBalance":
            self._send(self.state.get("balance", "ACCESS_BALANCE:2.45"))
        elif action == "setStatus":
            self._send("ACCESS_READY")
        else:
            self._send("BAD_ACTION")

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {"raw": raw.decode("utf-8", "replace")}
        parsed = urlparse(self.path)
        self.state.setdefault("calls", []).append(
            ("POST", parsed.path, body, self.headers.get("Authorization"))
        )
        if parsed.path.endswith("/take-phone") or parsed.path.endswith(
            "/take-reusable-phone"
        ):
            self._send(json.dumps({"item": {"id": "abc123", "phone": "447700900123"}}))
        elif parsed.path.endswith("/fetch-code"):
            self._send(json.dumps({"code": "654321"}))
        elif parsed.path.endswith("/release"):
            self._send(json.dumps({"ok": True}))
        else:
            self._send(json.dumps({"error": "unknown path"}), 404)


class PhoneHelpersTest(unittest.TestCase):
    def test_detects_phone_prompt_by_url(self):
        self.assertTrue(
            is_phone_prompt(
                "https://auth.openai.com/phone-verification", "任何内容"
            )
        )
        self.assertTrue(
            is_phone_prompt("https://auth.openai.com/add-phone?x=1", "")
        )

    def test_detects_phone_prompt_by_body(self):
        self.assertTrue(
            is_phone_prompt("https://auth.openai.com/x", "Verify your phone number")
        )
        self.assertTrue(is_phone_prompt("https://auth.openai.com/x", "请输入手机号"))
        self.assertFalse(
            is_phone_prompt(
                "https://auth.openai.com/codex/device", "使用你的设备代码"
            )
        )
        self.assertFalse(is_phone_prompt("https://chatgpt.com/", "今天想聊点什么"))

    def test_login_landing_page_is_not_a_phone_prompt(self):
        # The ChatGPT login page offers "使用手机号继续" as a login method; the
        # driver must not treat that as phone verification (it would buy a
        # number before typing the email/password).
        body = "登录或注册 使用 Google 继续 使用 Apple 继续 使用手机号继续 或 电子邮箱地址 继续"
        self.assertFalse(is_phone_prompt("https://chatgpt.com/auth/login", body))
        self.assertFalse(is_phone_prompt("https://auth.openai.com/log-in", body))
        # A real add-phone URL still wins.
        self.assertTrue(is_phone_prompt("https://auth.openai.com/add-phone", body))

    def test_detects_sms_unavailable(self):
        self.assertTrue(
            is_sms_unavailable("我们无法向该电话号码发送短信，因此已切换为 WhatsApp。")
        )
        self.assertTrue(
            is_sms_unavailable("We couldn't send a text message; switched to WhatsApp")
        )
        self.assertFalse(is_sms_unavailable("验证码已发送，请查看短信"))

    def test_detects_whatsapp_code_page(self):
        self.assertTrue(
            code_page_uses_whatsapp(
                "输入我们刚刚通过 WhatsApp 发送到 +44 7447 415211 的验证码。"
            )
        )
        self.assertTrue(
            code_page_uses_whatsapp("Enter the code we sent to your WhatsApp")
        )
        self.assertFalse(
            code_page_uses_whatsapp("输入我们刚刚通过短信发送到 +44 7447 415211 的验证码。")
        )
        self.assertFalse(code_page_uses_whatsapp(""))

    def test_phone_e164_and_mask(self):
        self.assertEqual(phone_e164("44 7700-900123"), "+447700900123")
        self.assertEqual(phone_e164(""), "")
        self.assertEqual(mask_phone("447700900123"), "+44****0123")
        self.assertEqual(mask_phone(""), "")


class SmsProviderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        FakeProviderHandler.state = {}
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeProviderHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeProviderHandler.state = {}
        self.tmp = tempfile.TemporaryDirectory()
        env = {key: "" for key in SMS_ENV_KEYS}
        env["CODEX_SWITCHER_DATA"] = str(Path(self.tmp.name) / "data")
        env.update(
            {
                "CODEX_SWITCHER_SMS_CODE_WAIT": "5",
                "CODEX_SWITCHER_SMS_POLL_INTERVAL": "1",
                "CODEX_SWITCHER_SMS_REQUEST_TIMEOUT": "5",
                "CODEX_SWITCHER_SMS_API_BASE": f"http://127.0.0.1:{self.port}/grizzly",
                "CODEX_SWITCHER_SMS_API_KEY": "test-key",
                "CODEX_SWITCHER_SMSBOWER_API_BASE": f"http://127.0.0.1:{self.port}/bower",
                "CODEX_SWITCHER_SMSBOWER_API_KEY": "bower-key",
                "CODEX_SWITCHER_L_API_BASE": f"http://127.0.0.1:{self.port}",
                "CODEX_SWITCHER_L_ADMIN_AUTH_CODE": "admin-token",
                "CODEX_SWITCHER_H_API_BASE": f"http://127.0.0.1:{self.port}",
                "CODEX_SWITCHER_H_ADMIN_AUTH_CODE": "admin-token",
            }
        )
        self.env = mock.patch.dict(os.environ, env, clear=False)
        self.env.start()
        self.delay = mock.patch.object(sms, "MIN_CANCEL_DELAY", 0)
        self.delay.start()

    def tearDown(self):
        self.delay.stop()
        self.env.stop()
        settings.reset_cache()
        self.tmp.cleanup()

    def test_disabled_without_provider(self):
        self.assertFalse(sms.enabled())
        with self.assertRaises(sms.SmsError) as ctx:
            sms.acquire_number()
        self.assertEqual(ctx.exception.code, "not_configured")

    def test_grizzly_acquire_wait_complete_cancel(self):
        with mock.patch.dict(
            os.environ, {"CODEX_SWITCHER_SMS_PROVIDER": "grizzly"}, clear=False
        ):
            activation_id, phone = sms.acquire_number()
            self.assertEqual(activation_id, "777")
            self.assertEqual(phone, "447700900123")
            self.assertEqual(sms.wait_for_code(activation_id, timeout=3), "123456")
            sms.complete(activation_id)
            statuses = [
                call[2].get("status")
                for call in FakeProviderHandler.state["calls"]
                if call[0] == "GET" and call[2].get("action") == "setStatus"
            ]
            self.assertIn("6", statuses)

            activation_id, _ = sms.acquire_number()
            sms.cancel(activation_id, background=False)
            statuses = [
                call[2].get("status")
                for call in FakeProviderHandler.state["calls"]
                if call[0] == "GET" and call[2].get("action") == "setStatus"
            ]
            self.assertIn("8", statuses)

    def test_browser_user_agent_is_sent(self):
        # Cloudflare in front of GrizzlySMS 403s the urllib default UA.
        with mock.patch.dict(
            os.environ, {"CODEX_SWITCHER_SMS_PROVIDER": "grizzly"}, clear=False
        ):
            sms.acquire_number()
        self.assertIn("Mozilla/5.0", FakeProviderHandler.state["last_ua"])

    def test_grizzly_balance_is_reported(self):
        with mock.patch.dict(
            os.environ, {"CODEX_SWITCHER_SMS_PROVIDER": "grizzly"}, clear=False
        ):
            FakeProviderHandler.state["balance"] = "ACCESS_BALANCE:2.45"
            balance = sms.balance(force=True)
            self.assertTrue(balance["ok"])
            self.assertEqual(balance["balance"], 2.45)
            self.assertEqual(balance["provider"], "grizzly")
            FakeProviderHandler.state["balance"] = "BAD_KEY"
            failed = sms.balance(force=True)
            self.assertFalse(failed["ok"])
            self.assertEqual(failed["error"], "bad_key")
            FakeProviderHandler.state["balance"] = "ACCESS_BALANCE:2.45"

    def test_activations_and_last_code_are_visible(self):
        with mock.patch.dict(
            os.environ, {"CODEX_SWITCHER_SMS_PROVIDER": "grizzly"}, clear=False
        ):
            activation_id, phone = sms.acquire_number()
            items = sms.activations()
            entry = next(
                item for item in items if item["activation_id"] == activation_id
            )
            self.assertEqual(entry["phone"], phone)
            self.assertEqual(entry["provider"], "grizzly")
            self.assertGreaterEqual(entry["age_seconds"], 0)
            self.assertEqual(sms.wait_for_code(activation_id, timeout=3), "123456")
            last = sms.last_code()
            self.assertEqual(last["code"], "123456")
            self.assertEqual(last["activation_id"], activation_id)
            messages = [event["message"] for event in sms.recent_events(50)]
            self.assertTrue(any("验证码" in message for message in messages))

    def test_balance_without_provider(self):
        with mock.patch.dict(os.environ, {"CODEX_SWITCHER_SMS_PROVIDER": ""}, clear=False):
            payload = sms.balance(force=True)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "not_configured")

    def test_grizzly_error_mapping(self):
        with mock.patch.dict(
            os.environ, {"CODEX_SWITCHER_SMS_PROVIDER": "grizzly"}, clear=False
        ):
            FakeProviderHandler.state["get_number"] = "NO_BALANCE"
            with self.assertRaises(sms.SmsNoBalance):
                sms.acquire_number()
            FakeProviderHandler.state["get_number"] = "NO_NUMBERS"
            with self.assertRaises(sms.SmsNoNumbers):
                sms.acquire_number()

    def test_grizzly_code_timeout(self):
        with mock.patch.dict(
            os.environ, {"CODEX_SWITCHER_SMS_PROVIDER": "grizzly"}, clear=False
        ):
            FakeProviderHandler.state["get_status"] = "STATUS_WAIT_CODE"
            with self.assertRaises(sms.SmsTimeout):
                sms.wait_for_code("777", timeout=1, poll_interval=1)

    def test_grizzly_wait_honours_stop_event(self):
        with mock.patch.dict(
            os.environ, {"CODEX_SWITCHER_SMS_PROVIDER": "grizzly"}, clear=False
        ):
            stop = threading.Event()
            stop.set()
            with self.assertRaises(sms.SmsError) as ctx:
                sms.wait_for_code("777", timeout=3, poll_interval=1, stop_event=stop)
            self.assertEqual(ctx.exception.code, "cancelled")

    def test_l_provider_flow(self):
        with mock.patch.dict(
            os.environ, {"CODEX_SWITCHER_SMS_PROVIDER": "l"}, clear=False
        ):
            activation_id, phone = sms.acquire_number()
            self.assertEqual(activation_id, "abc123")
            self.assertEqual(phone, "447700900123")
            self.assertEqual(sms.wait_for_code(activation_id, timeout=3), "654321")
            sms.cancel(activation_id, background=False)

            calls = FakeProviderHandler.state["calls"]
            paths = [call[1] for call in calls if call[0] == "POST"]
            self.assertIn("/api/admin/l/take-phone", paths)
            self.assertIn("/api/admin/l/fetch-code", paths)
            self.assertIn("/api/admin/l/release", paths)
            for call in calls:
                if call[0] == "POST":
                    self.assertEqual(call[3], "Bearer admin-token")

    def test_l_provider_requires_token(self):
        with mock.patch.dict(
            os.environ,
            {
                "CODEX_SWITCHER_SMS_PROVIDER": "l",
                "CODEX_SWITCHER_L_ADMIN_AUTH_CODE": "",
                "L_ADMIN_AUTH_CODE": "",
            },
            clear=False,
        ):
            with self.assertRaises(sms.SmsError) as ctx:
                sms.acquire_number()
            self.assertEqual(ctx.exception.code, "not_configured")

    def test_h_provider_uses_reusable_endpoint_by_default(self):
        with mock.patch.dict(
            os.environ, {"CODEX_SWITCHER_SMS_PROVIDER": "h"}, clear=False
        ):
            activation_id, _ = sms.acquire_number()
            self.assertEqual(activation_id, "abc123")
            paths = [
                call[1]
                for call in FakeProviderHandler.state["calls"]
                if call[0] == "POST"
            ]
            self.assertIn("/api/admin/h/take-reusable-phone", paths)

    def test_smsbower_flow(self):
        with mock.patch.dict(
            os.environ, {"CODEX_SWITCHER_SMS_PROVIDER": "smsbower"}, clear=False
        ):
            activation_id, phone = sms.acquire_number()
            self.assertEqual(activation_id, "777")
            self.assertEqual(phone, "447700900123")
            self.assertEqual(sms.wait_for_code(activation_id, timeout=3), "123456")

    def test_public_config_hides_secrets(self):
        with mock.patch.dict(
            os.environ, {"CODEX_SWITCHER_SMS_PROVIDER": "grizzly"}, clear=False
        ):
            payload = config.public_config()
        self.assertTrue(payload["sms_enabled"])
        self.assertEqual(payload["sms_provider"], "grizzly")
        self.assertNotIn("test-key", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
