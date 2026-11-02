"""Tests for reset-credit listing, redemption and the auto-reset policy."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from codex_switcher import quota
from codex_switcher.auto_switch import AutoSwitchMonitor


def _auth(access_token: str = "secret-token") -> bytes:
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "tokens": {"access_token": access_token, "account_id": "acct-1"},
        }
    ).encode("utf-8")


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _Capture:
    """Stand-in for urlopen that records the outgoing request."""

    def __init__(self, payload: dict, status: int = 200):
        self.payload = payload
        self.status = status
        self.requests: list = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        return _FakeResponse(self.payload, self.status)

    @property
    def last(self):
        return self.requests[-1]


def _credits_payload(*statuses: str) -> dict:
    credits = []
    for index, status in enumerate(statuses):
        credits.append(
            {
                "id": f"RateLimitResetCredit_{index}",
                "reset_type": "codex_rate_limits",
                "status": status,
                "granted_at": "2026-10-31T19:40:30Z",
                "expires_at": f"2026-11-{10 + index:02d}T19:40:30Z",
                "title": "Full reset (Weekly + 5 hr)",
            }
        )
    return {
        "credits": credits,
        "available_count": sum(1 for status in statuses if status == "available"),
        "total_earned_count": 0,
        "history_enabled": True,
    }


class SanitizeResetCreditsTests(unittest.TestCase):
    def test_usage_summary_carries_reset_credit_counts(self):
        summary = quota.sanitize_usage(
            {
                "rate_limit": {"limit_reached": True, "allowed": False},
                "rate_limit_reset_credits": {
                    "available_count": 2,
                    "applicable_available_count": 1,
                },
            }
        )
        self.assertEqual(
            summary["reset_credits"],
            {"known": True, "available_count": 2, "applicable_available_count": 1},
        )

    def test_missing_field_is_flagged_unknown(self):
        summary = quota.sanitize_usage({"rate_limit": {"limit_reached": False}})
        self.assertFalse(summary["reset_credits"]["known"])
        self.assertEqual(summary["reset_credits"]["applicable_available_count"], 0)

    def test_bad_values_fall_back_to_zero(self):
        summary = quota.sanitize_usage(
            {
                "rate_limit": {"limit_reached": True},
                "rate_limit_reset_credits": {
                    "available_count": "nope",
                    "applicable_available_count": None,
                },
            }
        )
        self.assertTrue(summary["reset_credits"]["known"])
        self.assertEqual(summary["reset_credits"]["available_count"], 0)


class FetchResetCreditsTests(unittest.TestCase):
    def test_lists_only_available_credits_and_earliest_expiry(self):
        capture = _Capture(_credits_payload("redeemed", "available", "available"))
        with mock.patch.object(quota.urllib.request, "urlopen", capture):
            result = quota.fetch_reset_credits(_auth())
        self.assertTrue(result["ok"])
        self.assertEqual(result["available_count"], 2)
        self.assertEqual(result["next_expires_at"], "2026-11-11T19:40:30Z")
        self.assertEqual(len(result["credits"]), 3)
        self.assertNotIn("secret-token", json.dumps(result))

    def test_auth_error_is_reported_as_data(self):
        def boom(request, timeout=None):
            raise HTTPError(request.full_url, 401, "Unauthorized", {}, None)

        with mock.patch.object(quota.urllib.request, "urlopen", boom):
            result = quota.fetch_reset_credits(_auth())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "auth_error")


class ConsumeResetCreditTests(unittest.TestCase):
    def test_posts_credit_id_and_request_id(self):
        capture = _Capture(
            {
                "code": "reset",
                "credit": {"id": "RateLimitResetCredit_0", "status": "redeemed"},
                "windows_reset": 2,
            }
        )
        with mock.patch.object(quota.urllib.request, "urlopen", capture):
            result = quota.consume_reset_credit(_auth(), "RateLimitResetCredit_0", "req-1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["windows_reset"], 2)
        request = capture.last
        self.assertEqual(request.get_method(), "POST")
        self.assertTrue(request.full_url.endswith("/rate-limit-reset-credits/consume"))
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(
            body, {"credit_id": "RateLimitResetCredit_0", "redeem_request_id": "req-1"}
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")

    def test_upstream_error_codes_are_mapped(self):
        for code in ("nothing_to_reset", "no_credit", "already_redeemed"):
            capture = _Capture({"code": code})
            with mock.patch.object(quota.urllib.request, "urlopen", capture):
                result = quota.consume_reset_credit(_auth(), "credit", "req")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], code)
            self.assertEqual(result["upstream_code"], code)

    def test_unknown_error_code_is_generic(self):
        capture = _Capture({"code": "something_new"})
        with mock.patch.object(quota.urllib.request, "urlopen", capture):
            result = quota.consume_reset_credit(_auth(), "credit", "req")
        self.assertEqual(result["error"], "consume_failed")

    def test_missing_arguments_are_rejected_before_any_request(self):
        capture = _Capture({})
        with mock.patch.object(quota.urllib.request, "urlopen", capture):
            self.assertEqual(
                quota.consume_reset_credit(_auth(), "", "req")["error"], "credit_required"
            )
            self.assertEqual(
                quota.consume_reset_credit(_auth(), "c", "")["error"],
                "request_id_required",
            )
        self.assertEqual(capture.requests, [])


class _StoreWithSnapshot:
    def __init__(self, snapshot: bytes | None):
        self.snapshot = snapshot

    def get_auth_snapshot(self, email: str):
        return self.snapshot


class QuotaServiceConsumeTests(unittest.TestCase):
    def _service(self, tmp: Path, snapshot: bytes | None = None):
        return quota.QuotaService(
            _StoreWithSnapshot(snapshot),
            tmp / "auth.json",
            auth_reader=lambda path: None,
        )

    def test_consume_picks_the_soonest_expiring_credit(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        service = self._service(Path(tmp.name), _auth())
        listing = _credits_payload("redeemed", "available", "available")
        consumed: list[dict] = []

        def fake_consume(auth_bytes, credit_id, request_id, **kwargs):
            consumed.append({"credit_id": credit_id, "request_id": request_id})
            return {"ok": True, "code": "reset", "credit_id": credit_id, "windows_reset": 2}

        with mock.patch.object(quota, "fetch_reset_credits", return_value={
            "ok": True,
            "available_count": 2,
            "credits": quota._credit_list(listing),
        }), mock.patch.object(quota, "consume_reset_credit", fake_consume):
            result = service.consume_reset(email="user@example.com")

        self.assertTrue(result["ok"])
        self.assertEqual(consumed[0]["credit_id"], "RateLimitResetCredit_1")
        self.assertTrue(consumed[0]["request_id"])

    def test_no_available_credit_short_circuits(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        service = self._service(Path(tmp.name), _auth())
        with mock.patch.object(
            quota,
            "fetch_reset_credits",
            return_value={"ok": True, "credits": quota._credit_list(_credits_payload("redeemed"))},
        ), mock.patch.object(quota, "consume_reset_credit") as consume:
            result = service.consume_reset(email="user@example.com")
        self.assertEqual(result["error"], "no_credit")
        consume.assert_not_called()

    def test_missing_auth_is_reported(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        service = self._service(Path(tmp.name), None)
        self.assertEqual(service.reset_credits()["error"], "no_auth_file")


class _Switch:
    def __init__(self, email: str = "a@example.com"):
        self.email = email

    def enabled(self) -> bool:
        return True

    def current(self) -> dict:
        return {"logged_in": True, "email": self.email}

    def switch_to(self, email: str, confirm: bool = False):
        self.email = email
        return type(
            "R", (), {"to_public": lambda self: {"target_email": email}}
        )()


class _Quota:
    def __init__(self, current: dict, pool: dict | None = None, consume: dict | None = None):
        self.current_quota = current
        self.pool = pool or {}
        self.consume_result = consume or {
            "ok": True,
            "code": "reset",
            "credit_id": "c1",
            "windows_reset": 2,
        }
        self.consumed: list[dict] = []
        self.invalidated = 0

    def current(self, force: bool = False) -> dict:
        return dict(self.current_quota)

    def for_email(self, email: str, force: bool = False, **kwargs) -> dict:
        return dict(self.pool.get(email, {"ok": False, "error": "snapshot_not_found"}))

    def consume_reset(self, **kwargs) -> dict:
        self.consumed.append(kwargs)
        return dict(self.consume_result)

    def invalidate(self, email: str | None = None) -> None:
        self.invalidated += 1


class _Store:
    def __init__(self, snapshots=None):
        self.snapshots = snapshots or []

    def list_auth_snapshots(self):
        return [dict(item) for item in self.snapshots]


def _limited_quota(secondary: float = 99, reset_after: float = 6 * 86400, credits: int = 1):
    return {
        "ok": True,
        "email": "a@example.com",
        "allowed": False,
        "limit_reached": True,
        "primary_window": {"used_percent": 100, "reset_after_seconds": 3600},
        "secondary_window": {
            "used_percent": secondary,
            "reset_after_seconds": reset_after,
        },
        "reset_credits": {
            "known": True,
            "available_count": credits,
            "applicable_available_count": credits,
        },
    }


class AutoResetPolicyTests(unittest.TestCase):
    def _monitor(self, quota_service, *, enabled=True, before_switch=False, max_per_week=1):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return AutoSwitchMonitor(
            _Store(),
            _Switch(),
            quota_service,
            enabled=lambda: True,
            interval_seconds=90,
            cooldown_seconds=900,
            log_path=Path(tmp.name) / "auto-switch.log",
            clock=lambda: 1000.0,
            reset_enabled=lambda: enabled,
            reset_threshold=98,
            reset_min_wait_hours=72,
            reset_max_per_week=max_per_week,
            reset_before_switch=lambda: before_switch,
        )

    def test_disabled_by_default_does_not_consume(self):
        quota_service = _Quota(_limited_quota())
        monitor = self._monitor(quota_service, enabled=False)
        result = monitor.tick_once()
        self.assertEqual(result["action"], "no_candidate")
        self.assertEqual(quota_service.consumed, [])
        self.assertEqual(result["reset_gate"], "disabled")

    def test_threshold_blocks_low_weekly_usage(self):
        quota_service = _Quota(_limited_quota(secondary=50))
        monitor = self._monitor(quota_service)
        result = monitor.tick_once()
        self.assertEqual(result["action"], "no_candidate")
        self.assertEqual(result["reset_gate"], "below_threshold")
        self.assertEqual(quota_service.consumed, [])

    def test_window_resets_soon_blocks_spending(self):
        quota_service = _Quota(_limited_quota(reset_after=48 * 3600))
        monitor = self._monitor(quota_service)
        result = monitor.tick_once()
        self.assertEqual(result["reset_gate"], "window_resets_soon")
        self.assertEqual(quota_service.consumed, [])

    def test_no_credit_blocks_spending(self):
        quota_service = _Quota(_limited_quota(credits=0))
        monitor = self._monitor(quota_service)
        result = monitor.tick_once()
        self.assertEqual(result["reset_gate"], "no_credit")

    def test_owned_but_not_applicable_credits_are_reported_separately(self):
        payload = _limited_quota(credits=1)
        payload["reset_credits"]["applicable_available_count"] = 0
        quota_service = _Quota(payload)
        monitor = self._monitor(quota_service)
        result = monitor.tick_once()
        self.assertEqual(result["reset_gate"], "not_applicable")
        self.assertEqual(quota_service.consumed, [])

    def test_unknown_credit_state_blocks_spending(self):
        payload = _limited_quota()
        payload.pop("reset_credits")
        quota_service = _Quota(payload)
        monitor = self._monitor(quota_service)
        result = monitor.tick_once()
        self.assertEqual(result["reset_gate"], "unknown")

    def test_consumes_credit_when_pool_is_dry(self):
        quota_service = _Quota(_limited_quota())
        monitor = self._monitor(quota_service)
        result = monitor.tick_once()
        self.assertEqual(result["action"], "reset_consumed")
        self.assertEqual(result["windows_reset"], 2)
        self.assertEqual(len(quota_service.consumed), 1)
        self.assertEqual(quota_service.consumed[0]["email"], "a@example.com")
        self.assertTrue(quota_service.consumed[0]["redeem_request_id"])
        self.assertEqual(quota_service.invalidated, 1)
        status = monitor.status()
        self.assertEqual(status["auto_reset"]["used_last_7d"], 1)
        self.assertTrue(status["auto_reset"]["last"]["ok"])

    def test_weekly_cap_limits_automatic_resets(self):
        quota_service = _Quota(_limited_quota())
        monitor = self._monitor(quota_service, max_per_week=1)
        self.assertEqual(monitor.tick_once()["action"], "reset_consumed")
        monitor._next_check_after = 0.0
        second = monitor.tick_once()
        self.assertEqual(second["action"], "no_candidate")
        self.assertEqual(second["reset_gate"], "weekly_cap")
        self.assertEqual(len(quota_service.consumed), 1)

    def test_failed_consume_falls_through_to_no_candidate(self):
        quota_service = _Quota(
            _limited_quota(),
            consume={"ok": False, "error": "no_credit", "message": "none"},
        )
        monitor = self._monitor(quota_service)
        result = monitor.tick_once()
        self.assertEqual(result["action"], "no_candidate")
        self.assertEqual(result["reset_gate"], "ok")
        status = monitor.status()
        self.assertFalse(status["auto_reset"]["last"]["ok"])
        self.assertEqual(status["auto_reset"]["last"]["error"], "no_credit")

    def test_prefers_switching_over_spending_by_default(self):
        quota_service = _Quota(
            _limited_quota(),
            pool={"b@example.com": {"ok": True, "allowed": True, "limit_reached": False,
                                    "primary_window": {"used_percent": 1},
                                    "secondary_window": {"used_percent": 1}}},
        )
        monitor = AutoSwitchMonitor(
            _Store([{"email": "b@example.com", "disabled": False, "expired": False}]),
            _Switch(),
            quota_service,
            enabled=lambda: True,
            interval_seconds=90,
            log_path=Path(tempfile.mkdtemp()) / "auto-switch.log",
            clock=lambda: 1000.0,
            reset_enabled=lambda: True,
            reset_threshold=98,
            reset_min_wait_hours=72,
            reset_max_per_week=1,
        )
        result = monitor.tick_once()
        self.assertEqual(result["action"], "switched")
        self.assertEqual(quota_service.consumed, [])

    def test_before_switch_consumes_first_when_configured(self):
        quota_service = _Quota(
            _limited_quota(),
            pool={"b@example.com": {"ok": True, "allowed": True, "limit_reached": False,
                                    "primary_window": {"used_percent": 1},
                                    "secondary_window": {"used_percent": 1}}},
        )
        monitor = AutoSwitchMonitor(
            _Store([{"email": "b@example.com", "disabled": False, "expired": False}]),
            _Switch(),
            quota_service,
            enabled=lambda: True,
            interval_seconds=90,
            log_path=Path(tempfile.mkdtemp()) / "auto-switch.log",
            clock=lambda: 1000.0,
            reset_enabled=lambda: True,
            reset_threshold=98,
            reset_min_wait_hours=72,
            reset_max_per_week=1,
            reset_before_switch=lambda: True,
        )
        result = monitor.tick_once()
        self.assertEqual(result["action"], "reset_consumed")
        self.assertEqual(len(quota_service.consumed), 1)

    def test_status_reports_a_live_gate_not_the_last_tick_reason(self):
        # Turning the feature on used to leave gate="disabled" until the monitor
        # reached the reset check again — which a long cooldown can postpone for
        # a while, so the panel lied about why nothing happened.
        quota_service = _Quota(_limited_quota(secondary=50))
        monitor = self._monitor(quota_service, enabled=False)
        monitor.tick_once()
        self.assertEqual(monitor.status()["auto_reset"]["gate"], "disabled")
        monitor._reset_enabled_fn = lambda: True
        status = monitor.status()["auto_reset"]
        self.assertTrue(status["enabled"])
        self.assertEqual(status["gate"], "below_threshold")

    def test_status_reports_ok_gate_when_every_condition_matches(self):
        quota_service = _Quota(_limited_quota())
        monitor = self._monitor(quota_service, enabled=False)
        monitor.tick_once()
        monitor._reset_enabled_fn = lambda: True
        self.assertEqual(monitor.status()["auto_reset"]["gate"], "ok")
        # ... and it is only a report: nothing was spent by asking.
        self.assertEqual(quota_service.consumed, [])


class ConfigDefaultsTests(unittest.TestCase):
    """Defaults matter: they are what ships when a user flips the flag on."""

    def test_defaults(self):
        from codex_switcher import config

        settings_values = {
            "enable_auto_reset": None,
            "auto_reset_secondary_threshold": None,
            "auto_reset_min_wait_hours": None,
            "auto_reset_max_per_week": None,
            "auto_reset_before_switch": None,
        }
        with mock.patch.object(
            config, "_settings_get", lambda key: settings_values.get(key)
        ), mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(config.auto_reset_enabled())
            self.assertEqual(config.auto_reset_secondary_threshold(), 98)
            self.assertEqual(config.auto_reset_min_wait_hours(), 72)
            self.assertEqual(config.auto_reset_max_per_week(), 1)
            self.assertFalse(config.auto_reset_before_switch())

    def test_settings_json_overrides_env(self):
        from codex_switcher import config

        values = {"auto_reset_secondary_threshold": "95", "enable_auto_reset": "1"}
        with mock.patch.object(config, "_settings_get", lambda key: values.get(key)):
            self.assertTrue(config.auto_reset_enabled())
            self.assertEqual(config.auto_reset_secondary_threshold(), 95)

    def test_settings_spec_exposes_the_knobs(self):
        from codex_switcher import settings

        for key in (
            "enable_auto_reset",
            "auto_reset_secondary_threshold",
            "auto_reset_min_wait_hours",
            "auto_reset_max_per_week",
            "auto_reset_before_switch",
        ):
            self.assertIn(key, settings.SPEC)


if __name__ == "__main__":
    unittest.main()
