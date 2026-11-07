"""Tests for the read-only quota probe and cache."""

from __future__ import annotations

import base64
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
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


class QuotaBackgroundTests(unittest.TestCase):
    def setUp(self):
        self.live = auth_bytes()
        self.now = 100.0
        self.releases = []
        self.services = []

    def tearDown(self):
        for event in self.releases:
            event.set()
        for service in self.services:
            service.close()

    def event(self):
        event = threading.Event()
        self.releases.append(event)
        return event

    def service(self, fetch, **options):
        service = quota.QuotaService(options.pop("store", _FakeStore()), Path("unused-auth.json"),
                                     fetch_func=fetch, auth_reader=lambda _: self.live,
                                     clock=lambda: self.now, **options)
        self.services.append(service)
        return service

    @staticmethod
    def payload(used=10):
        return {"ok": True, "primary_window": {"used_percent": used}, "fetched_at": "2026-11-07T00:00:00+00:00"}

    def settled(self, service, **options):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            value = service.current(background=True, **options)
            if not value.get("pending"):
                return value
            time.sleep(.005)
        self.fail("background refresh did not finish")

    def test_cold_background_returns_before_blocked_fetch_and_force_coalesces(self):
        entered, release = threading.Event(), self.event()
        calls = []
        def fetch(raw):
            calls.append(raw)
            entered.set()
            release.wait(5)
            return self.payload()
        service = self.service(fetch, ttl_seconds=60)
        with ThreadPoolExecutor(max_workers=1) as client:
            response = client.submit(service.current, background=True)
            try:
                first = response.result(timeout=2)
                self.assertTrue(entered.wait(2))
                self.assertEqual(first["error"], "quota_pending")
                self.assertIsNone(first["fetched_at"])
                self.assertTrue(first["pending"])
                self.assertTrue(service.current(force=True, background=True)["refreshing"])
                self.assertTrue(service.for_email("user@example.com", scope="pool", ttl=60, force=True, background=True)["pending"])
                self.assertEqual(len(calls), 1)
            finally:
                release.set()
        fresh = self.settled(service)
        self.assertTrue(fresh["ok"])
        self.assertFalse(fresh["stale"])
        self.assertEqual(fresh["fetched_at"], self.payload()["fetched_at"])

    def test_stale_display_returns_old_value_but_decision_waits_for_fresh_result(self):
        entered, release, decision_finished = threading.Event(), self.event(), threading.Event()
        calls = []
        def fetch(raw):
            calls.append(raw)
            if len(calls) == 2:
                entered.set()
                release.wait(5)
            return self.payload(len(calls))
        service = self.service(fetch, ttl_seconds=5)
        self.assertEqual(service.current()["primary_window"]["used_percent"], 1)
        self.now += 6
        shown = service.current(background=True)
        self.assertEqual(shown["primary_window"]["used_percent"], 1)
        self.assertTrue(shown["stale"])
        self.assertTrue(entered.wait(2))
        def decision():
            result = service.current()
            decision_finished.set()
            return result
        with ThreadPoolExecutor(max_workers=1) as client:
            waiting = client.submit(decision)
            try:
                self.assertFalse(decision_finished.wait(.05))
            finally:
                release.set()
            self.assertEqual(waiting.result(timeout=2)["primary_window"]["used_percent"], 2)
        self.assertEqual(len(calls), 2)

    def test_current_and_pool_share_fresh_data_with_independent_max_ages(self):
        calls = []
        service = self.service(lambda raw: (calls.append(raw) or self.payload(len(calls))), ttl_seconds=5)
        service.current()
        pool = service.for_email("user@example.com", scope="pool", ttl=60)
        self.assertEqual(len(calls), 1)
        self.assertEqual(pool["primary_window"]["used_percent"], 1)
        self.now += 6
        service.for_email("user@example.com", scope="pool", ttl=60)
        self.assertEqual(len(calls), 1)
        service.current()
        self.assertEqual(len(calls), 2)
        self.assertEqual(service.for_email("user@example.com", scope="pool", ttl=60)["primary_window"]["used_percent"], 2)

    def test_different_workspaces_with_same_email_never_share_slow_fetch(self):
        other = json.loads(self.live)
        other["tokens"]["account_id"] = "workspace-other"
        other = json.dumps(other).encode()
        current_ref = quota.read_auth_state_bytes(self.live)["profile_id"]
        other_state = quota.read_auth_state_bytes(other)
        class Store:
            def resolve_auth_profile(self, ref):
                return other_state if ref == other_state["profile_id"] else None
            def get_auth_snapshot(self, ref):
                return other if ref == other_state["profile_id"] else None
        entered, release = threading.Event(), self.event()
        def fetch(raw):
            if raw == self.live:
                entered.set()
                release.wait(5)
                return self.payload(10)
            return self.payload(70)
        service = self.service(fetch, store=Store(), ttl_seconds=60, max_workers=2)
        self.assertTrue(service.current(background=True)["pending"])
        self.assertTrue(entered.wait(2))
        try:
            fast = service.for_email(other_state["profile_id"], scope="pool", ttl=60)
            self.assertEqual(fast["profile_id"], other_state["profile_id"])
            self.assertNotEqual(fast["profile_id"], current_ref)
            self.assertEqual(fast["primary_window"]["used_percent"], 70)
        finally:
            release.set()

    def test_token_change_discards_late_old_response(self):
        entered, release = threading.Event(), self.event()
        original = self.live
        def fetch(raw):
            if raw == original:
                entered.set()
                release.wait(5)
                return self.payload(1)
            return self.payload(99)
        service = self.service(fetch, ttl_seconds=60)
        service.current(background=True)
        self.assertTrue(entered.wait(2))
        self.live = auth_bytes(access_token="rotated-test-token")
        self.assertEqual(service.current()["primary_window"]["used_percent"], 99)
        release.set()
        service.close()
        # The completed old request cannot replace the new-credential entry.
        self.assertEqual(next(iter(service._cache.values()))["payload"]["primary_window"]["used_percent"], 99)

    def test_invalidation_during_request_does_not_refill_cache_with_old_quota(self):
        entered, release = threading.Event(), self.event()
        calls = []
        def fetch(raw):
            calls.append(raw)
            ordinal = len(calls)
            if ordinal == 1:
                entered.set()
                release.wait(5)
            return self.payload(ordinal)
        service = self.service(fetch, ttl_seconds=60)
        service.current(background=True)
        self.assertTrue(entered.wait(2))
        # Targeted invalidation also works before the first result has supplied
        # an email in its payload.
        service.invalidate("user@example.com")
        self.assertEqual(service.current()["primary_window"]["used_percent"], 2)
        release.set()
        self.assertEqual(service.current()["primary_window"]["used_percent"], 2)

    def test_refresh_failure_preserves_display_but_never_fools_decision(self):
        calls = []
        def fetch(raw):
            calls.append(raw)
            if len(calls) == 2:
                raise TimeoutError("fixture failure")
            return self.payload(len(calls))
        service = self.service(fetch, ttl_seconds=60)
        original = service.current()
        service.current(force=True, background=True)
        shown = self.settled(service)
        self.assertTrue(shown["ok"])
        self.assertTrue(shown["stale"])
        self.assertEqual(shown["fetched_at"], original["fetched_at"])
        self.assertEqual(shown["primary_window"], original["primary_window"])
        self.assertEqual(shown["refresh_error"]["error"], "request_failed")
        self.assertFalse(service.current()["ok"])
        self.assertTrue(service.current(force=True)["ok"])
        self.assertNotIn("refresh_error", self.settled(service))

    def test_default_ttl_changes_apply_without_recreating_service(self):
        calls = []
        with mock.patch("codex_switcher.quota.config.quota_cache_ttl_seconds", return_value=60) as ttl:
            service = self.service(lambda raw: (calls.append(raw) or self.payload()))
            service.current()
            self.now += 10
            service.current()
            self.assertEqual(len(calls), 1)
            ttl.return_value = 5
            service.current()
            self.assertEqual(len(calls), 2)

    def test_bounded_queue_keeps_deferred_manual_refresh_intent(self):
        other = auth_bytes("snap@example.com", "snapshot-test-token")
        entered, release = threading.Event(), self.event()
        counts = {"live": 0, "snapshot": 0}
        def fetch(raw):
            kind = "live" if raw == self.live else "snapshot"
            counts[kind] += 1
            if kind == "live":
                entered.set()
                release.wait(5)
            return self.payload(counts[kind])
        service = self.service(fetch, store=_FakeStore(other), ttl_seconds=60, max_workers=1, max_pending=1)
        service.for_email("snap@example.com", ttl=60, scope="pool")
        service.current(background=True)
        self.assertTrue(entered.wait(2))
        deferred = service.for_email("snap@example.com", force=True, ttl=60, scope="pool", background=True)
        self.assertTrue(deferred["pending"])
        self.assertFalse(deferred["refreshing"])
        self.assertEqual(deferred["primary_window"]["used_percent"], 1)
        self.assertEqual(len(service._futures), 1)
        release.set()
        self.settled(service)
        # No force on the poll: the unscheduled click remains pending.
        service.for_email("snap@example.com", ttl=60, scope="pool", background=True)
        fresh = service.for_email("snap@example.com", ttl=60, scope="pool")
        self.assertEqual(fresh["primary_window"]["used_percent"], 2)

    def test_close_cancels_queued_work_and_rejects_new_work(self):
        entered, release = threading.Event(), self.event()
        other = auth_bytes("snap@example.com", "snapshot-test-token")
        calls = []
        def fetch(raw):
            calls.append(raw)
            entered.set()
            release.wait(5)
            return self.payload()
        service = self.service(fetch, store=_FakeStore(other), ttl_seconds=60, max_workers=1, max_pending=2)
        service.current(background=True)
        self.assertTrue(entered.wait(2))
        service.for_email("snap@example.com", background=True)
        with ThreadPoolExecutor(max_workers=1) as client:
            closing = client.submit(service.close)
            deadline = time.monotonic() + 2
            while not service._closed and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertEqual(service.current(background=True)["error"], "service_closed")
            release.set()
            closing.result(timeout=2)
        self.assertEqual(len(calls), 1)
        self.assertFalse(service._futures)

    def test_deferred_pool_refreshes_are_fifo_without_polling_again(self):
        snapshots = {f"{name}@example.com": auth_bytes(f"{name}@example.com", name) for name in ("first", "last")}
        class Store:
            def get_auth_snapshot(self, ref):
                return snapshots.get(ref)
        entered = {name: threading.Event() for name in ("live", "first", "last")}
        release = {name: self.event() for name in entered}
        calls = []
        def fetch(raw):
            name = "live" if raw == self.live else json.loads(raw)["tokens"]["access_token"]
            calls.append(name)
            entered[name].set()
            release[name].wait(5)
            return self.payload()
        service = self.service(fetch, store=Store(), ttl_seconds=1, max_workers=1, max_pending=1)
        service.current(background=True)
        self.assertTrue(entered["live"].wait(2))
        for name in ("first", "last"):
            self.assertTrue(service.for_email(f"{name}@example.com", background=True)["pending"])
        release["live"].set()
        self.assertTrue(entered["first"].wait(2))
        # The static first row becoming stale again must go behind the tail.
        self.now += 2
        service.current(background=True)
        release["first"].set()
        self.assertTrue(entered["last"].wait(2))
        self.assertEqual(calls[:3], ["live", "first", "last"])
        release["last"].set()

    def test_synchronous_reader_gets_next_free_slot_before_deferred_display(self):
        snapshots = {f"{name}@example.com": auth_bytes(f"{name}@example.com", name) for name in ("display", "decision")}
        class Store:
            def get_auth_snapshot(self, ref):
                return snapshots.get(ref)
        entered, release = threading.Event(), self.event()
        calls = []
        def fetch(raw):
            name = "live" if raw == self.live else json.loads(raw)["tokens"]["access_token"]
            calls.append(name)
            if name == "live":
                entered.set()
                release.wait(5)
            return self.payload()
        service = self.service(fetch, store=Store(), max_workers=1, max_pending=1)
        service.current(background=True)
        self.assertTrue(entered.wait(2))
        service.for_email("display@example.com", background=True)
        with ThreadPoolExecutor(max_workers=1) as client:
            decision = client.submit(service.for_email, "decision@example.com")
            deadline = time.monotonic() + 2
            while not service._sync_waiters and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertEqual(service._sync_waiters, 1)
            release.set()
            self.assertTrue(decision.result(timeout=2)["ok"])
        self.assertEqual(calls[:2], ["live", "decision"])

    def test_synchronous_wait_is_bounded_and_never_returns_old_success(self):
        entered, release = threading.Event(), self.event()
        calls = []
        def fetch(raw):
            calls.append(raw)
            if len(calls) > 1:
                entered.set()
                release.wait(5)
            return self.payload()
        service = self.service(fetch, wait_timeout=.05, ttl_seconds=1)
        self.assertTrue(service.current()["ok"])
        self.now += 2
        service.current(background=True)
        self.assertTrue(entered.wait(2))
        result = service.current()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "refresh_timeout")
        self.assertNotIn("primary_window", result)


if __name__ == "__main__":
    unittest.main()
