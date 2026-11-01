"""Tests for the read-only quota probe and cache."""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from codex_switcher import quota


def _b64url(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _token(payload: dict) -> str:
    return "x." + _b64url(payload) + ".y"


def auth_bytes(email: str = "user@example.com", access_token: str = "secret-token") -> bytes:
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": access_token,
                "account_id": "acct-123",
                "id_token": _token({"email": email, "exp": 9999999999}),
            },
        }
    ).encode("utf-8")


class _FakeResponse:
    status = 200

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FetchQuotaTests(unittest.TestCase):
    def test_sanitizes_usage_and_never_returns_token(self):
        payload = {
            "email": "user@example.com",
            "account_id": "acct-123",
            "plan_type": "plus",
            "rate_limit": {
                "allowed": False,
                "limit_reached": True,
                "primary_window": {
                    "used_percent": 100,
                    "limit_window_seconds": 18000,
                    "reset_at": 1793371492,
                    "reset_after_seconds": 13862,
                },
                "secondary_window": {
                    "used_percent": 32,
                    "limit_window_seconds": 604800,
                    "reset_at": 1793847826,
                    "reset_after_seconds": 490196,
                },
            },
            "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
            "rate_limit_upsell": {
                "title": "You're out of Codex messages",
                "description": "reset",
                "reset_at": 1793371492,
                "ctas": [{"action": "open_pricing_dialog"}],
            },
            "rate_limit_reached_type": {"type": "rate_limit_reached"},
        }
        with mock.patch(
            "codex_switcher.quota.urllib.request.urlopen",
            return_value=_FakeResponse(payload),
        ):
            result = quota.fetch_quota(auth_bytes())

        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"], "plus")
        self.assertTrue(result["limit_reached"])
        self.assertEqual(result["primary_window"]["used_percent"], 100)
        self.assertEqual(result["secondary_window"]["used_percent"], 32)
        serialized = json.dumps(result)
        self.assertNotIn("secret-token", serialized)
        self.assertNotIn("access_token", serialized)
        self.assertNotIn("ctas", serialized)

    def test_maps_401_to_auth_error(self):
        error = HTTPError(
            "https://chatgpt.com/backend-api/wham/usage",
            401,
            "Unauthorized",
            {},
            None,
        )
        with mock.patch(
            "codex_switcher.quota.urllib.request.urlopen", side_effect=error
        ):
            result = quota.fetch_quota(auth_bytes())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "auth_error")
        self.assertNotIn("secret-token", json.dumps(result))

    def test_missing_token_is_reported_without_network(self):
        with mock.patch(
            "codex_switcher.quota.urllib.request.urlopen"
        ) as urlopen:
            result = quota.fetch_quota(
                json.dumps({"tokens": {"account_id": "acct"}}).encode("utf-8")
            )
        urlopen.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no_chatgpt_token")


class _FakeStore:
    def __init__(self, snapshot: bytes | None = None, email: str = "snap@example.com"):
        self.snapshot = snapshot
        self.email = email

    def get_auth_snapshot(self, email: str) -> bytes | None:
        if email.lower() == self.email.lower():
            return self.snapshot
        return None


class QuotaServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.auth_path = Path(self.temp.name) / "auth.json"
        self.auth_path.write_bytes(auth_bytes())
        self.calls = []

    def _fetch(self, data: bytes) -> dict:
        self.calls.append(data)
        return {
            "ok": True,
            "source": "live",
            "email": "user@example.com",
            "plan": "plus",
            "limit_reached": False,
            "allowed": True,
            "primary_window": {"used_percent": 10},
            "fetched_at": "now",
        }

    def test_current_is_cached_and_force_bypasses(self):
        store = _FakeStore()
        service = quota.QuotaService(
            store,
            self.auth_path,
            ttl_seconds=60,
            fetch_func=self._fetch,
        )
        first = service.current()
        second = service.current()
        forced = service.current(force=True)
        self.assertTrue(first["ok"])
        self.assertEqual(second["primary_window"]["used_percent"], 10)
        self.assertEqual(forced["primary_window"]["used_percent"], 10)
        self.assertEqual(len(self.calls), 2)

    def test_snapshot_lookup_and_missing(self):
        snapshot = auth_bytes("snap@example.com", "snap-token")
        store = _FakeStore(snapshot=snapshot)
        service = quota.QuotaService(
            store,
            self.auth_path,
            ttl_seconds=60,
            fetch_func=self._fetch,
        )
        found = service.for_email("snap@example.com")
        missing = service.for_email("missing@example.com")
        self.assertTrue(found["ok"])
        self.assertEqual(found["source"], "snapshot")
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["error"], "snapshot_not_found")

    def test_pool_scope_has_its_own_cache_and_ttl(self):
        snapshot = auth_bytes("snap@example.com", "snap-token")
        store = _FakeStore(snapshot=snapshot)
        service = quota.QuotaService(
            store,
            self.auth_path,
            ttl_seconds=5,
            fetch_func=self._fetch,
        )
        first = service.for_email("snap@example.com", ttl=600, scope="pool")
        second = service.for_email("snap@example.com", ttl=600, scope="pool")
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(len(self.calls), 1)
        # The live namespace must stay independent and short-lived.
        service.current()
        self.assertEqual(len(self.calls), 2)
        service.for_email("snap@example.com", force=True, ttl=600, scope="pool")
        self.assertEqual(len(self.calls), 3)

    def test_pool_scope_caches_even_the_current_account(self):
        service = quota.QuotaService(
            _FakeStore(),
            self.auth_path,
            ttl_seconds=0,
            fetch_func=self._fetch,
        )
        first = service.for_email("user@example.com", ttl=600, scope="pool")
        second = service.for_email("user@example.com", ttl=600, scope="pool")
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(len(self.calls), 1)
        service.for_email("user@example.com", force=True, ttl=600, scope="pool")
        self.assertEqual(len(self.calls), 2)

    def test_auth_hash_change_invalidates_cache(self):
        store = _FakeStore()
        service = quota.QuotaService(
            store,
            self.auth_path,
            ttl_seconds=60,
            fetch_func=self._fetch,
        )
        service.current()
        self.auth_path.write_bytes(auth_bytes(access_token="new-token"))
        service.current()
        self.assertEqual(len(self.calls), 2)


if __name__ == "__main__":
    unittest.main()
