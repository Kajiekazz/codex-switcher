import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from codex_switcher.auto_switch import AutoSwitchMonitor
from codex_switcher.model_access import ModelAccessGuard, access_error
from codex_switcher.quota import sanitize_usage
from codex_switcher.quota_state import usage_state
import test_auto_switch as switch_fixtures
from test_auto_switch import _quota_ok, _limit_reached


def auth(plan):
    token = base64.urlsafe_b64encode(json.dumps({"email": "one@example.com", "https://api.openai.com/auth": {"chatgpt_plan_type": plan}}).encode()).decode().rstrip("=")
    return json.dumps({"tokens": {"id_token": "x." + token + ".y", "access_token": "fixture"}}).encode()


class PlanQuotaTests(unittest.TestCase):
    def test_empty_and_malformed_responses_do_not_grant_access(self):
        for rate in ({}, {"allowed": "false"}, {"limit_reached": False}, {"allowed": 1}):
            with self.subTest(rate=rate):
                value = sanitize_usage({"rate_limit": rate})
                self.assertIsNone(value["allowed"])
                self.assertEqual(usage_state(value), "unknown")

    def test_pro_without_primary_window_keeps_weekly_quota(self):
        value = sanitize_usage({"plan_type": "pro", "rate_limit": {"allowed": True, "limit_reached": False,
            "primary_window": None, "secondary_window": {"used_percent": 88, "limit_window_seconds": 604800}}})
        self.assertEqual(value["window_states"]["primary_window"], "not_applicable")
        self.assertEqual(AutoSwitchMonitor._remaining(value), (-1, 12))
        self.assertEqual(usage_state(value), "available")

    def test_free_and_future_windows_are_not_assumed_to_be_five_hours(self):
        value = sanitize_usage({"plan_type": "free", "rate_limit": {"allowed": True,
            "primary_window": {"used_percent": 25, "limit_window_seconds": 10800}}})
        self.assertEqual(value["primary_window"]["window_seconds"], 10800)
        self.assertEqual(value["window_states"]["secondary_window"], "unknown")

    def test_bad_percentages_remain_unknown_instead_of_zero(self):
        for used in (None, "bad", True, -1, 101, float("nan"), float("inf")):
            value = sanitize_usage({"rate_limit": {"allowed": True, "primary_window": {"used_percent": used}}})
            self.assertIsNone(value["primary_window"]["used_percent"])
            self.assertEqual(value["window_states"]["primary_window"], "unknown")

    def test_explicit_denial_wins_over_available_flags(self):
        self.assertEqual(usage_state(sanitize_usage({"rate_limit": {"allowed": False, "limit_reached": False}})), "exhausted")


class MixedPoolTests(unittest.TestCase):
    setUp = switch_fixtures.AutoSwitchMonitorTests.setUp
    _monitor = switch_fixtures.AutoSwitchMonitorTests._monitor

    def test_unknown_quota_is_not_a_candidate(self):
        monitor = self._monitor(quota_service=switch_fixtures._FakeQuotaService(_limit_reached(), {"b@example.com": {"ok": True}}))
        self.assertIsNone(monitor._choose_candidate("a@example.com", 1000))

    def test_mixed_plans_follow_priority_instead_of_comparing_percentages(self):
        self.store.snapshots = [{"email": "b@example.com", "priority": 10}, {"email": "c@example.com", "priority": 50}]
        free, pro = _quota_ok("b@example.com"), _quota_ok("c@example.com")
        free.update(plan="free", primary_window={"used_percent": 1})
        pro.update(plan="pro", primary_window=None, secondary_window={"used_percent": 80})
        monitor = self._monitor(quota_service=switch_fixtures._FakeQuotaService(_limit_reached(), {"b@example.com": free, "c@example.com": pro}))
        self.assertEqual(monitor._choose_candidate("a@example.com", 1000)["email"], "c@example.com")

    def test_current_unknown_quota_does_not_trigger_switch_or_recovery(self):
        monitor = self._monitor(quota_service=switch_fixtures._FakeQuotaService({"ok": True}))
        monitor.on_quota_recovered = Mock()
        self.assertEqual(monitor.tick_once()["action"], "quota_unknown")
        monitor.on_quota_recovered.assert_not_called()


class ModelAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "auth.json"
        self.path.write_bytes(auth("free"))
        self.store = Mock()
        self.store.get_auth_snapshot.return_value = auth("free")
        self.catalog = Mock(return_value={"luna": {"allowed": None}})
        self.guard = ModelAccessGuard(self.store, self.path, catalog=self.catalog, path=self.root / "access.json")

    def test_catalog_presence_does_not_confirm_cross_plan_entitlement(self):
        result = self.guard.check("one@example.com", ["luna"], automatic=True, source_plan="plus")
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "model_access_unknown")

    def test_same_plan_catalog_and_cache(self):
        for _ in range(2):
            result = self.guard.check("one@example.com", ["luna"], automatic=True, source_plan="free")
            self.assertTrue(result["ok"])
            self.assertEqual(result["state"], "catalog_only")
        self.assertEqual(self.catalog.call_count, 1)

    def test_unlisted_and_explicitly_denied_models_are_blocked(self):
        self.assertFalse(self.guard.check("one@example.com", ["astra"])["ok"])
        self.guard.cache.clear()
        self.catalog.return_value = {"luna": {"allowed": False}}
        self.assertFalse(self.guard.check("one@example.com", ["luna"])["ok"])

    def test_real_execution_allows_cross_plan_and_rejection_is_remembered(self):
        self.guard.observe("one@example.com", "luna", True)
        self.assertTrue(self.guard.check("one@example.com", ["luna"], automatic=True, source_plan="pro")["ok"])
        self.guard.observe("one@example.com", "luna", False)
        result = self.guard.check("one@example.com", ["luna"])
        self.assertFalse(result["ok"])
        recreated = ModelAccessGuard(self.store, self.path, catalog=self.catalog, path=self.root / "access.json")
        self.assertEqual(recreated.check("one@example.com", ["luna"])["state"], "model_access_denied")

    def test_catalog_outage_does_not_break_same_plan_but_blocks_unverified_cross_plan(self):
        self.catalog.side_effect = OSError("fixture offline")
        self.assertTrue(self.guard.check("one@example.com", ["luna"], automatic=True, source_plan="free")["ok"])
        self.assertFalse(self.guard.check("one@example.com", ["luna"], automatic=True, source_plan="pro")["ok"])

    def test_general_errors_are_not_misclassified_as_model_permissions(self):
        self.assertFalse(access_error("network timeout"))
        self.assertTrue(access_error("model_not_found"))

    def test_business_plan_variants_cannot_use_same_plan_entitlement_shortcut(self):
        for plan in ("self_serve_business_prolite", "self_serve_business_usage_based", "ent26", "enterprise_cbp_automation", "enterprise_cbp_usage_based"):
            with self.subTest(plan=plan):
                self.path.write_bytes(auth(plan))
                result = self.guard.check("one@example.com", ["luna"], automatic=True, source_plan=plan)
                self.assertFalse(result["ok"])
                self.assertEqual(result["state"], "model_access_unknown")
