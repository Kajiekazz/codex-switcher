"""Tests for the opt-in quota-driven auto-switch policy."""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_switcher.auto_switch import AutoSwitchMonitor
from codex_switcher.store import AccountStore
from codex_switcher.switcher import SwitchError, SwitchManager


def _b64url(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _make_auth(path: Path, email: str, account_id: str) -> bytes:
    header = _b64url({"alg": "none", "typ": "JWT"})
    payload = _b64url(
        {
            "email": email,
            "name": email.split("@", 1)[0],
            "exp": 4102444800,
            "https://api.openai.com/auth": {
                "chatgpt_plan_type": "plus",
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


class _Clock:
    def __init__(self, value: float = 1000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class _FakeSwitchManager:
    def __init__(
        self,
        email: str = "a@example.com",
        *,
        enabled: bool = True,
        fail: bool = False,
        desktop_restart: dict | None = None,
        auto_resume: dict | None = None,
    ):
        self.email = email
        self._enabled = enabled
        self.fail = fail
        self.desktop_restart = desktop_restart
        self.auto_resume = auto_resume
        self.switched: list[str] = []

    def enabled(self) -> bool:
        return self._enabled

    def current(self) -> dict:
        return {"logged_in": bool(self.email), "email": self.email}

    def switch_to(self, email: str, confirm: bool = False):
        if self.fail:
            raise SwitchError("switch_failed", "boom", "detail")
        previous = self.email
        self.email = email
        self.switched.append(email)
        return SimpleNamespace(
            to_public=lambda: {
                "previous_email": previous,
                "target_email": email,
                "switched_at": "2026-01-01T00:00:00+00:00",
                "backup_path": "backup.bin",
                "desktop_restart": self.desktop_restart,
                "auto_resume": self.auto_resume,
            }
        )


class _FakeQuotaService:
    def __init__(self, current: dict, by_email: dict[str, dict] | None = None):
        self.current_quota = current
        self.by_email = by_email or {}
        self.invalidated = 0
        self.calls: list[str] = []

    def current(self, force: bool = False) -> dict:
        return dict(self.current_quota)

    def for_email(self, email: str, force: bool = False) -> dict:
        self.calls.append(email)
        return dict(
            self.by_email.get(
                email,
                {"ok": False, "error": "snapshot_not_found"},
            )
        )

    def invalidate(self, email: str | None = None) -> None:
        self.invalidated += 1


class _FakeStore:
    def __init__(self, snapshots: list[dict] | None = None):
        self.snapshots = snapshots or []

    def list_auth_snapshots(self) -> list[dict]:
        return [dict(item) for item in self.snapshots]


class _FakeStoreWithHistory(_FakeStore):
    def __init__(self, snapshots: list[dict] | None = None, history: list[dict] | None = None):
        super().__init__(snapshots)
        self.history = history or []

    def quota_history(self, email: str, hours: int = 24) -> list[dict]:
        return [dict(item) for item in self.history]


def _limit_reached() -> dict:
    return {
        "ok": True,
        "email": "a@example.com",
        "plan": "plus",
        "allowed": False,
        "limit_reached": True,
        "primary_window": {
            "used_percent": 100,
            "reset_at": 2000,
            "reset_after_seconds": 1000,
        },
        "secondary_window": {"used_percent": 32},
        "fetched_at": "2026-01-01T00:00:00+00:00",
    }


def _quota_ok(email: str = "b@example.com") -> dict:
    return {
        "ok": True,
        "email": email,
        "plan": "plus",
        "allowed": True,
        "limit_reached": False,
        "primary_window": {"used_percent": 5},
        "secondary_window": {"used_percent": 1},
        "fetched_at": "2026-01-01T00:00:00+00:00",
    }


def _near_limit() -> dict:
    return {
        "ok": True,
        "email": "a@example.com",
        "plan": "plus",
        "allowed": True,
        "limit_reached": False,
        "primary_window": {"used_percent": 95, "reset_at": None},
        "secondary_window": {"used_percent": 40},
        "fetched_at": "2026-01-01T00:00:00+00:00",
    }


def _quota(
    email: str,
    *,
    primary: float = 0,
    secondary: float = 0,
    limit: bool = False,
    reset_at: float | None = None,
) -> dict:
    """Quota payload with configurable remaining headroom."""
    return {
        "ok": True,
        "email": email,
        "plan": "plus",
        "allowed": not limit,
        "limit_reached": limit,
        "primary_window": {"used_percent": primary, "reset_at": reset_at},
        "secondary_window": {"used_percent": secondary},
        "fetched_at": "2026-01-01T00:00:00+00:00",
    }


class AutoSwitchMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log_path = Path(self.temp.name) / "auto-switch.log"
        self.clock = _Clock()
        self.store = _FakeStore(
            [
                {
                    "email": "b@example.com",
                    "expired": False,
                    "saved_at": "2026-01-01T00:00:00+00:00",
                }
            ]
        )

    def _monitor(
        self,
        *,
        manager: _FakeSwitchManager | None = None,
        quota_service: _FakeQuotaService,
        enabled=lambda: True,
        cooldown_seconds: int = 900,
        max_switches_per_hour: int = 3,
        failure_threshold: int = 3,
        store: _FakeStore | None = None,
        predictive_enabled=None,
        predictive_threshold_seconds: int = 600,
        max_probes: int | None = None,
    ) -> AutoSwitchMonitor:
        return AutoSwitchMonitor(
            store or self.store,
            manager or _FakeSwitchManager(),
            quota_service,
            enabled=enabled,
            interval_seconds=90,
            cooldown_seconds=cooldown_seconds,
            max_switches_per_hour=max_switches_per_hour,
            failure_threshold=failure_threshold,
            circuit_open_seconds=1800,
            no_candidate_retry_seconds=300,
            predictive_enabled=predictive_enabled,
            predictive_threshold_seconds=predictive_threshold_seconds,
            log_path=self.log_path,
            clock=self.clock,
            max_probes=max_probes,
        )

    def test_switches_when_limit_reached_and_candidate_is_available(self):
        manager = _FakeSwitchManager()
        quota = _FakeQuotaService(
            _limit_reached(),
            {"b@example.com": _quota_ok()},
        )
        monitor = self._monitor(manager=manager, quota_service=quota)

        result = monitor.tick_once()

        self.assertEqual(result["action"], "switched")
        self.assertEqual(result["target_email"], "b@example.com")
        self.assertEqual(manager.switched, ["b@example.com"])
        self.assertEqual(quota.invalidated, 1)
        status = monitor.status()
        self.assertEqual(status["last_result"], "switched")
        self.assertEqual(status["switches_last_hour"], 1)
        self.assertIn("a@example.com", status["cooldowns"])
        self.assertIn("switched", self.log_path.read_text(encoding="utf-8"))
        self.assertNotIn("secret-token", json.dumps(status))

    def test_switch_logs_desktop_restart_failure(self):
        # Regression: a disabled desktop restart used to leave no trace at all,
        # so a switch looked fine while the desktop app kept the old account.
        manager = _FakeSwitchManager(
            desktop_restart={
                "ok": False,
                "skipped": True,
                "reason": "desktop_restart_disabled",
            }
        )
        quota = _FakeQuotaService(
            _limit_reached(),
            {"b@example.com": _quota_ok()},
        )
        monitor = self._monitor(manager=manager, quota_service=quota)

        monitor.tick_once()

        lines = [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        switched = [item for item in lines if item["event"] == "switched"]
        self.assertEqual(len(switched), 1)
        self.assertEqual(
            switched[0]["desktop_restart"],
            {"ok": False, "skipped": True, "reason": "desktop_restart_disabled"},
        )
        failed = [item for item in lines if item["event"] == "desktop_restart_failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["reason"], "desktop_restart_disabled")

    def test_switch_logs_successful_desktop_restart_without_failure_event(self):
        manager = _FakeSwitchManager(
            desktop_restart={"ok": True, "previous_pids": [1, 2], "pids": [3, 4]}
        )
        quota = _FakeQuotaService(
            _limit_reached(),
            {"b@example.com": _quota_ok()},
        )
        monitor = self._monitor(manager=manager, quota_service=quota)

        monitor.tick_once()

        lines = [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        switched = [item for item in lines if item["event"] == "switched"]
        self.assertEqual(len(switched), 1)
        self.assertEqual(switched[0]["desktop_restart"]["ok"], True)
        self.assertEqual(
            [item for item in lines if item["event"] == "desktop_restart_failed"], []
        )

    def test_quota_ok_does_not_switch(self):
        manager = _FakeSwitchManager()
        quota = _FakeQuotaService(_quota_ok("a@example.com"))
        monitor = self._monitor(manager=manager, quota_service=quota)

        result = monitor.tick_once()

        self.assertEqual(result["action"], "quota_ok")
        self.assertEqual(manager.switched, [])
        self.assertFalse(self.log_path.exists())

    def test_auto_resume_outcomes_are_visible_without_logging_goal_content(self):
        outcomes = [
            {"ok": True, "job": {"id": "resume-job", "events": ["private-goal-content"]},
             "thread_ids": ["goal-a", "goal-b"], "cancelled_stale_job": True},
            {"ok": True, "skipped": "no_marked_goal", "thread_ids": []},
            {"ok": False, "error": "private-error-details", "message": "private-goal-content"},
        ]
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                manager = _FakeSwitchManager(auto_resume=outcome)
                monitor = self._monitor(
                    manager=manager,
                    quota_service=_FakeQuotaService(
                        _limit_reached(), {"b@example.com": _quota_ok()},
                    ),
                )
                result = monitor.tick_once()
                summary = result["auto_resume"]
                self.assertEqual(result["action"], "switched")
                self.assertEqual(summary["ok"], outcome["ok"])
                self.assertEqual(summary["skipped"], outcome.get("skipped"))
                self.assertEqual(summary["job_id"], (outcome.get("job") or {}).get("id"))
                self.assertEqual(summary["thread_count"], len(outcome.get("thread_ids", [])))
                self.assertEqual(summary["failed"], not outcome["ok"])
                status = monitor.status()
                self.assertEqual(status["last_switch"]["auto_resume"], summary)
                switched = [event for event in status["recent_events"] if event["event"] == "switched"]
                self.assertEqual(switched[-1]["auto_resume"], summary)
                persisted = self.log_path.read_text(encoding="utf-8")
                self.assertNotIn("private-goal-content", persisted)
                self.assertNotIn("private-error-details", persisted)

    def test_priority_orders_candidates(self):
        store = _FakeStore(
            [
                {
                    "email": "low@example.com",
                    "expired": False,
                    "saved_at": "2026-01-03T00:00:00+00:00",
                    "priority": 10,
                },
                {
                    "email": "high@example.com",
                    "expired": True,
                    "saved_at": "2026-01-01T00:00:00+00:00",
                    "priority": 500,
                },
            ]
        )
        manager = _FakeSwitchManager()
        quota = _FakeQuotaService(
            _limit_reached(),
            {"low@example.com": _quota_ok(), "high@example.com": _quota_ok()},
        )
        monitor = self._monitor(manager=manager, quota_service=quota, store=store)

        result = monitor.tick_once()

        self.assertEqual(result["action"], "switched")
        self.assertEqual(result["target_email"], "high@example.com")

    def test_allowed_false_alone_triggers_a_switch(self):
        """The endpoint can refuse without setting limit_reached; still leave."""
        blocked = {
            "ok": True,
            "email": "a@example.com",
            "plan": "plus",
            "allowed": False,
            "limit_reached": False,
            "primary_window": {"used_percent": 100},
            "secondary_window": {"used_percent": 20},
        }
        manager = _FakeSwitchManager()
        quota = _FakeQuotaService(blocked, {"b@example.com": _quota_ok()})
        monitor = self._monitor(manager=manager, quota_service=quota)

        result = monitor.tick_once()

        self.assertEqual(result["action"], "switched")
        self.assertEqual(result["target_email"], "b@example.com")

    def test_max_probes_cap_is_opt_in(self):
        """max_probes=0 (default) probes the pool; a cap is honoured when set."""
        emails = [f"c{index}@example.com" for index in range(1, 7)]
        store = _FakeStore(
            [
                {"email": email, "expired": False, "priority": 100 - index}
                for index, email in enumerate(emails)
            ]
        )
        by_email = {
            email: _quota(email, primary=100, limit=True) for email in emails[:-1]
        }
        by_email[emails[-1]] = _quota(emails[-1])
        quota = _FakeQuotaService(_limit_reached(), by_email)
        monitor = self._monitor(
            quota_service=quota, store=store, max_probes=2
        )

        result = monitor.tick_once()

        self.assertEqual(result["action"], "no_candidate")
        self.assertEqual(len(quota.calls), 2)

    def test_probe_uses_pool_cache_scope(self):
        """Probes must reuse the pool quota cache, not a fresh live call each time."""
        seen = []

        class _KwargQuota(_FakeQuotaService):
            def for_email(self, email, force=False, ttl=None, scope="live"):
                seen.append((email, ttl, scope))
                return {"ok": True, "email": email, "allowed": True}

        quota = _KwargQuota(_limit_reached())
        monitor = self._monitor(quota_service=quota)

        result = monitor.tick_once()

        self.assertEqual(result["action"], "switched")
        self.assertEqual([entry[0] for entry in seen], ["b@example.com"])
        self.assertEqual(seen[0][2], "pool")
        self.assertIsNotNone(seen[0][1])

    def test_large_pool_probe_does_not_drop_results(self):
        """Parallel probing must keep every result (usable account sorts last)."""
        emails = [f"big{index:02d}@example.com" for index in range(40)]
        store = _FakeStore(
            [
                {"email": email, "expired": False, "priority": 1000 - index}
                for index, email in enumerate(emails)
            ]
        )
        by_email = {
            email: _quota(email, primary=100, limit=True) for email in emails[:-1]
        }
        by_email[emails[-1]] = _quota(emails[-1])
        quota = _FakeQuotaService(_limit_reached(), by_email)
        monitor = self._monitor(quota_service=quota, store=store)

        result = monitor.tick_once()

        self.assertEqual(result["target_email"], emails[-1])
        self.assertEqual(len(quota.calls), 40)

    def test_quota_first_beats_priority(self):
        """A high-priority account with little quota must lose to a fresh one."""
        store = _FakeStore(
            [
                {
                    "email": "priority@example.com",
                    "expired": False,
                    "priority": 900,
                    "saved_at": "2026-01-01T00:00:00+00:00",
                },
                {
                    "email": "fresh@example.com",
                    "expired": False,
                    "priority": 1,
                    "saved_at": "2026-01-01T00:00:00+00:00",
                },
            ]
        )
        quota = _FakeQuotaService(
            _limit_reached(),
            {
                "priority@example.com": _quota(
                    "priority@example.com", primary=80, secondary=90
                ),
                "fresh@example.com": _quota("fresh@example.com"),
            },
        )
        manager = _FakeSwitchManager()
        monitor = self._monitor(manager=manager, quota_service=quota, store=store)

        result = monitor.tick_once()

        self.assertEqual(result["target_email"], "fresh@example.com")

    def test_primary_window_outranks_weekly_window(self):
        """Today's 5h window is what blocks you, so it decides first."""
        store = _FakeStore(
            [
                {
                    "email": "weekly@example.com",
                    "expired": False,
                    "priority": 500,
                    "saved_at": "2026-01-01T00:00:00+00:00",
                },
                {
                    "email": "hourly@example.com",
                    "expired": False,
                    "priority": 1,
                    "saved_at": "2026-01-01T00:00:00+00:00",
                },
            ]
        )
        quota = _FakeQuotaService(
            _limit_reached(),
            {
                # weekly has room left, but the hourly window is nearly gone
                "weekly@example.com": _quota(
                    "weekly@example.com", primary=95, secondary=10
                ),
                "hourly@example.com": _quota(
                    "hourly@example.com", primary=20, secondary=90
                ),
            },
        )
        monitor = self._monitor(quota_service=quota, store=store)

        result = monitor.tick_once()

        self.assertEqual(result["target_email"], "hourly@example.com")

    def test_probes_whole_pool_not_just_the_top_four(self):
        """The usable account may sort last by priority and still be picked."""
        emails = [f"p{index}@example.com" for index in range(1, 7)]
        store = _FakeStore(
            [
                {
                    "email": email,
                    "expired": False,
                    "priority": 100 - index,
                    "saved_at": "2026-01-01T00:00:00+00:00",
                }
                for index, email in enumerate(emails)
            ]
        )
        by_email = {
            email: _quota(email, primary=100, limit=True) for email in emails[:-1]
        }
        by_email[emails[-1]] = _quota(emails[-1])
        quota = _FakeQuotaService(_limit_reached(), by_email)
        monitor = self._monitor(quota_service=quota, store=store)

        result = monitor.tick_once()

        self.assertEqual(result["target_email"], emails[-1])

    def test_exhausted_candidates_are_parked_until_reset(self):
        store = _FakeStore(
            [
                {"email": "b@example.com", "expired": False},
                {"email": "c@example.com", "expired": False},
            ]
        )
        quota = _FakeQuotaService(
            _limit_reached(),
            {
                "b@example.com": _quota(
                    "b@example.com", primary=100, limit=True, reset_at=1400
                ),
                "c@example.com": _quota(
                    "c@example.com", primary=100, limit=True, reset_at=1900
                ),
            },
        )
        monitor = self._monitor(quota_service=quota, store=store)

        result = monitor.tick_once()

        self.assertEqual(result["action"], "no_candidate")
        cooldowns = monitor.status()["cooldowns"]
        self.assertIn("b@example.com", cooldowns)
        self.assertIn("c@example.com", cooldowns)
        # The earliest window resets at 1400 (now=1000), so wake just after it.
        self.assertAlmostEqual(
            monitor._next_check_after, 1405.0, places=3  # noqa: SLF001
        )

    def test_disabled_candidates_are_skipped(self):
        store = _FakeStore(
            [
                {"email": "off@example.com", "expired": False, "priority": 900, "disabled": True},
                {"email": "on@example.com", "expired": False, "priority": 1, "disabled": False},
            ]
        )
        manager = _FakeSwitchManager()
        quota = _FakeQuotaService(
            _limit_reached(),
            {"off@example.com": _quota_ok(), "on@example.com": _quota_ok()},
        )
        monitor = self._monitor(manager=manager, quota_service=quota, store=store)

        result = monitor.tick_once()

        self.assertEqual(result["action"], "switched")
        self.assertEqual(result["target_email"], "on@example.com")

    def test_predictive_switch_fires_before_limit_is_reached(self):
        history = [
            {"epoch": 400, "primary_used": 60, "primary_reset_at": None},
            {"epoch": 700, "primary_used": 75, "primary_reset_at": None},
            {"epoch": 1000, "primary_used": 95, "primary_reset_at": None},
        ]
        store = _FakeStoreWithHistory(
            [{"email": "b@example.com", "expired": False}], history
        )
        manager = _FakeSwitchManager()
        quota = _FakeQuotaService(_near_limit(), {"b@example.com": _quota_ok()})
        monitor = self._monitor(
            manager=manager,
            quota_service=quota,
            store=store,
            predictive_enabled=lambda: True,
        )

        result = monitor.tick_once()

        self.assertEqual(result["action"], "switched")
        self.assertEqual(result["trigger"], "predicted_exhaustion")
        self.assertEqual(manager.switched, ["b@example.com"])

    def test_predictive_disabled_stays_on_quota_ok(self):
        history = [
            {"epoch": 400, "primary_used": 60, "primary_reset_at": None},
            {"epoch": 700, "primary_used": 75, "primary_reset_at": None},
            {"epoch": 1000, "primary_used": 95, "primary_reset_at": None},
        ]
        store = _FakeStoreWithHistory(
            [{"email": "b@example.com", "expired": False}], history
        )
        manager = _FakeSwitchManager()
        quota = _FakeQuotaService(_near_limit(), {"b@example.com": _quota_ok()})
        monitor = self._monitor(
            manager=manager,
            quota_service=quota,
            store=store,
            predictive_enabled=lambda: False,
        )

        result = monitor.tick_once()

        self.assertEqual(result["action"], "quota_ok")
        self.assertEqual(manager.switched, [])

    def test_no_candidate_when_all_snapshots_are_exhausted(self):
        manager = _FakeSwitchManager()
        quota = _FakeQuotaService(
            _limit_reached(),
            {"b@example.com": _limit_reached()},
        )
        monitor = self._monitor(manager=manager, quota_service=quota)

        result = monitor.tick_once()

        self.assertEqual(result["action"], "no_candidate")
        self.assertEqual(manager.switched, [])
        self.assertTrue(self.log_path.exists())
        self.assertEqual(monitor.tick_once()["action"], "cooldown")

    def test_cooldown_blocks_early_tick(self):
        manager = _FakeSwitchManager()
        monitor = self._monitor(
            manager=manager,
            quota_service=_FakeQuotaService(
                _limit_reached(), {"b@example.com": _quota_ok()}
            ),
        )
        monitor._next_check_after = self.clock.value + 100  # noqa: SLF001

        result = monitor.tick_once()

        self.assertEqual(result["action"], "cooldown")
        self.assertEqual(manager.switched, [])

    def test_hourly_switch_cap(self):
        manager = _FakeSwitchManager()
        monitor = self._monitor(
            manager=manager,
            quota_service=_FakeQuotaService(
                _limit_reached(), {"b@example.com": _quota_ok()}
            ),
            max_switches_per_hour=2,
        )
        monitor._switches.extend([self.clock.value - 10, self.clock.value - 20])  # noqa: SLF001

        result = monitor.tick_once()

        self.assertEqual(result["action"], "max_switches")
        self.assertEqual(manager.switched, [])

    def test_circuit_breaker_after_repeated_failures(self):
        manager = _FakeSwitchManager(fail=True)
        monitor = self._monitor(
            manager=manager,
            quota_service=_FakeQuotaService(
                _limit_reached(), {"b@example.com": _quota_ok()}
            ),
            cooldown_seconds=0,
            failure_threshold=2,
        )

        first = monitor.tick_once()
        second = monitor.tick_once()
        third = monitor.tick_once()

        self.assertEqual(first["action"], "switch_failed")
        self.assertFalse(first["circuit_open"])
        self.assertEqual(second["action"], "switch_failed")
        self.assertTrue(second["circuit_open"])
        self.assertTrue(monitor.status()["circuit_open"])
        self.assertEqual(third["action"], "circuit_open")
        self.assertEqual(manager.switched, [])

    def test_disabled_flags(self):
        quota = _FakeQuotaService(_limit_reached())
        disabled = self._monitor(
            quota_service=quota,
            enabled=lambda: False,
        )
        self.assertEqual(disabled.tick_once()["action"], "disabled")

        switching_disabled = self._monitor(
            manager=_FakeSwitchManager(enabled=False),
            quota_service=quota,
        )
        self.assertEqual(
            switching_disabled.tick_once()["action"],
            "switching_disabled",
        )

    def test_quota_error_does_not_switch(self):
        manager = _FakeSwitchManager()
        monitor = self._monitor(
            manager=manager,
            quota_service=_FakeQuotaService(
                {"ok": False, "error": "auth_error", "message": "expired"}
            ),
        )

        result = monitor.tick_once()

        self.assertEqual(result["action"], "quota_error")
        self.assertEqual(result["error"], "auth_error")
        self.assertEqual(manager.switched, [])

    def test_status_exposes_policy_but_not_tokens(self):
        monitor = self._monitor(
            quota_service=_FakeQuotaService(_quota_ok("a@example.com"))
        )
        status = monitor.status()
        self.assertIn("max_switches_per_hour", status)
        self.assertIn("last_quota", status)
        self.assertNotIn("access_token", json.dumps(status))
        self.assertNotIn("refresh_token", json.dumps(status))


class AutoSwitchSwitchIntegrationTests(unittest.TestCase):
    def test_auto_switch_uses_real_switch_manager_with_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_path = root / "auth.json"
            a_bytes = _make_auth(auth_path, "a@example.com", "acct-a")
            b_bytes = _make_auth(root / "b.json", "b@example.com", "acct-b")
            store = AccountStore(root / "accounts.sqlite3")
            try:
                store.save_auth_snapshot(
                    "b@example.com",
                    b_bytes,
                    {
                        "email": "b@example.com",
                        "plan": "plus",
                        "account_id": "acct-b",
                        "expires_at": 4102444800,
                    },
                )
                manager = SwitchManager(
                    store,
                    auth_path,
                    target_backup_dir=root / "backups",
                    enabled=lambda: True,
                    on_switched=lambda result: {
                        "ok": True,
                        "job": {"id": "auto-resume-after-switch"},
                        "thread_ids": ["marked-goal"],
                    },
                )
                quota = _FakeQuotaService(
                    _limit_reached(),
                    {"b@example.com": _quota_ok()},
                )
                monitor = AutoSwitchMonitor(
                    store,
                    manager,
                    quota,
                    enabled=lambda: True,
                    interval_seconds=90,
                    cooldown_seconds=900,
                    max_switches_per_hour=3,
                    failure_threshold=3,
                    log_path=root / "auto-switch.log",
                    clock=_Clock(1000),
                )

                result = monitor.tick_once()

                self.assertEqual(result["action"], "switched")
                self.assertEqual(manager.current()["email"], "b@example.com")
                self.assertEqual(
                    hashlib.sha256(auth_path.read_bytes()).hexdigest(),
                    hashlib.sha256(b_bytes).hexdigest(),
                )
                self.assertTrue(
                    list((root / "backups").glob("*.bak.json")),
                    "switch manager should create an encrypted backup",
                )
                status = monitor.status()
                self.assertEqual(status["last_switch"]["target_email"], "b@example.com")
                self.assertEqual(result["auto_resume"]["job_id"], "auto-resume-after-switch")
                self.assertEqual(status["last_switch"]["auto_resume"]["thread_count"], 1)
                serialized = json.dumps(status)
                self.assertNotIn("access_token", serialized)
                self.assertNotIn("refresh_token", serialized)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
