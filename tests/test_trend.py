"""Tests for burn-rate prediction used by the dashboard and auto-switch."""

from __future__ import annotations

import unittest

from codex_switcher.trend import predict_quota, predict_window


def _sample(epoch, primary=None, secondary=None, reset_at=None, window=18000):
    return {
        "epoch": epoch,
        "primary_used": primary,
        "primary_reset_at": reset_at,
        "primary_window_seconds": window,
        "secondary_used": secondary,
        "secondary_reset_at": None,
        "secondary_window_seconds": 604800,
    }


class TrendTests(unittest.TestCase):
    def test_empty_history_is_unavailable(self):
        result = predict_window([], "primary", now=1000)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "no_samples")

    def test_insufficient_samples(self):
        samples = [_sample(100, 10), _sample(200, 12)]
        result = predict_window(samples, "primary", now=200)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "insufficient_samples")

    def test_linear_burn_rate_and_eta(self):
        # 10 points per hour over three hours.
        samples = [_sample(3600 * i, primary=40 + 10 * i) for i in range(4)]
        result = predict_window(samples, "primary", now=3600 * 3)
        self.assertTrue(result["available"])
        self.assertAlmostEqual(result["rate_per_hour"], 10.0, delta=0.05)
        self.assertAlmostEqual(result["eta_seconds"], 10800, delta=120)
        self.assertEqual(result["used_percent"], 70.0)

    def test_reset_drop_starts_new_segment(self):
        samples = [
            _sample(0, primary=90),
            _sample(600, primary=95),
            _sample(1200, primary=5),
            _sample(1800, primary=15),
            _sample(2400, primary=25),
        ]
        result = predict_window(samples, "primary", now=2400)
        self.assertTrue(result["available"])
        self.assertEqual(result["used_percent"], 25.0)
        self.assertAlmostEqual(result["rate_per_hour"], 60.0, delta=1.0)

    def test_flat_or_falling_usage_has_no_eta(self):
        samples = [_sample(1000, primary=40), _sample(1600, primary=40), _sample(2200, primary=40)]
        result = predict_window(samples, "primary", now=2200)
        self.assertTrue(result["available"])
        self.assertIsNone(result["eta_seconds"])

    def test_only_current_window_samples_are_used(self):
        # Old samples belong to a previous window (different reset_at).
        samples = [
            _sample(1000, primary=10, reset_at=5000),
            _sample(2000, primary=90, reset_at=20000),
            _sample(3000, primary=92, reset_at=20000),
            _sample(4000, primary=94, reset_at=20000),
        ]
        result = predict_window(samples, "primary", now=4000)
        self.assertTrue(result["available"])
        self.assertEqual(result["used_percent"], 94.0)
        self.assertLess(result["sample_count"], 4)

    def test_predict_quota_reports_soonest_window(self):
        now = 10000
        samples = [
            {
                "epoch": 9000,
                "primary_used": 50,
                "primary_reset_at": None,
                "primary_window_seconds": 18000,
                "secondary_used": 10,
                "secondary_reset_at": None,
                "secondary_window_seconds": 604800,
            },
            {
                "epoch": 9500,
                "primary_used": 70,
                "primary_reset_at": None,
                "primary_window_seconds": 18000,
                "secondary_used": 20,
                "secondary_reset_at": None,
                "secondary_window_seconds": 604800,
            },
            {
                "epoch": 10000,
                "primary_used": 90,
                "primary_reset_at": None,
                "primary_window_seconds": 18000,
                "secondary_used": 30,
                "secondary_reset_at": None,
                "secondary_window_seconds": 604800,
            },
        ]
        result = predict_quota({"limit_reached": False}, samples, now=now)
        self.assertEqual(result["soonest_window"], "primary")
        self.assertLess(result["soonest_eta_seconds"], 1200)


if __name__ == "__main__":
    unittest.main()
