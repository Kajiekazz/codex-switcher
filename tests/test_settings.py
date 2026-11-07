import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_switcher import config, settings


class SettingsStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._saved_env = {
            key: os.environ.get(key)
            for key in (
                "CODEX_SWITCHER_DATA",
                "SMS_PROVIDER",
                "SMS_API_KEY",
                "CODEX_SWITCHER_ENABLE_SWITCH",
                "CODEX_SWITCHER_AUTO_SWITCH_INTERVAL",
            )
        }
        os.environ["CODEX_SWITCHER_DATA"] = str(self.root / "data")
        for key in ("SMS_PROVIDER", "SMS_API_KEY", "CODEX_SWITCHER_ENABLE_SWITCH"):
            os.environ.pop(key, None)
        settings.reset_cache()

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        settings.reset_cache()
        self.tmp.cleanup()

    def test_save_and_read_roundtrip(self):
        changed = settings.save(
            {"sms_provider": "grizzly", "sms_api_key": "a64e3725c82e0133fafb8eff7590d12c"}
        )
        self.assertIn("sms_provider", changed)
        self.assertEqual(settings.get("sms_provider"), "grizzly")
        self.assertEqual(
            settings.get("sms_api_key"), "a64e3725c82e0133fafb8eff7590d12c"
        )
        self.assertEqual(config.sms_provider(), "grizzly")
        self.assertEqual(
            config.sms_api_key(), "a64e3725c82e0133fafb8eff7590d12c"
        )

    def test_secret_is_encrypted_at_rest(self):
        settings.save({"sms_api_key": "super-secret-key-9999"})
        raw = (self.root / "data" / "settings.json").read_text(encoding="utf-8")
        self.assertNotIn("super-secret-key-9999", raw)
        self.assertIn("dpapi:", raw)
        payload = json.loads(raw)
        self.assertTrue(payload["values"]["sms_api_key"].startswith("dpapi:"))

    def test_public_masks_secrets(self):
        settings.save({"sms_api_key": "abcdef123456"})
        data = settings.public()
        item = next(entry for entry in data["items"] if entry["key"] == "sms_api_key")
        self.assertEqual(item["value"], "****3456")
        self.assertTrue(item["is_set"])
        self.assertTrue(item["has_value"])
        self.assertNotIn("abcdef123456", json.dumps(data))

    def test_settings_beat_environment(self):
        os.environ["SMS_PROVIDER"] = "h"
        settings.save({"sms_provider": "grizzly"})
        self.assertEqual(config.sms_provider(), "grizzly")
        settings.clear(["sms_provider"])
        self.assertEqual(config.sms_provider(), "h")

    def test_environment_used_when_unset(self):
        os.environ["SMS_API_KEY"] = "env-key-123"
        self.assertEqual(config.sms_api_key(), "env-key-123")
        self.assertEqual(settings.source("sms_api_key"), "env")

    def test_typed_values_drive_config(self):
        settings.save({"enable_auto_switch": "1", "auto_switch_interval": "120"})
        self.assertTrue(config.auto_switch_enabled())
        self.assertEqual(config.auto_switch_interval_seconds(), 120)

    def test_pool_quota_default_is_fresh_without_replacing_explicit_values(self):
        with mock.patch.dict(os.environ, {"CODEX_SWITCHER_POOL_QUOTA_TTL": ""}):
            self.assertEqual(settings.SPEC["pool_quota_ttl"]["default"], 15)
            self.assertEqual(config.pool_quota_ttl_seconds(), 15)
            os.environ["CODEX_SWITCHER_POOL_QUOTA_TTL"] = "45"
            self.assertEqual(config.pool_quota_ttl_seconds(), 45)
            settings.save({"pool_quota_ttl": "60"})
            self.assertEqual(config.pool_quota_ttl_seconds(), 60)

    def test_manual_switching_ignores_legacy_disabled_values(self):
        os.environ["CODEX_SWITCHER_ENABLE_SWITCH"] = "0"
        path = self.root / "data" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "values": {"enable_switch": "0"}}))
        settings.reset_cache()
        self.assertTrue(config.switching_enabled())
        self.assertNotIn("enable_switch", {item["key"] for item in settings.public()["items"]})

    def test_validation_errors(self):
        with self.assertRaises(ValueError):
            settings.save({"not_a_key": "x"})
        with self.assertRaises(ValueError):
            settings.save({"enable_switch": "maybe"})
        with self.assertRaises(ValueError):
            settings.save({"auto_switch_interval": "5"})
        with self.assertRaises(ValueError):
            settings.save({"sms_provider": "unknown-provider"})

    def test_clear_removes_keys(self):
        settings.save({"sms_provider": "grizzly"})
        self.assertEqual(settings.clear(["sms_provider"]), ["sms_provider"])
        self.assertIsNone(settings.get("sms_provider"))
        self.assertEqual(settings.clear(["sms_provider"]), [])

    def test_restart_required_flags(self):
        changed = settings.save({"port": "8899", "sms_provider": "grizzly"})
        self.assertEqual(settings.restart_required(changed), ["port"])


if __name__ == "__main__":
    unittest.main()
