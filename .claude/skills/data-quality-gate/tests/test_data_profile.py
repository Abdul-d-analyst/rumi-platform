"""Tests for data_profile.py — deterministic, threshold-based checks. No
Postgres, no LLM, no opaque score — every check here is arithmetic against
a documented threshold, per the assignment's requirement."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import data_profile as dp  # noqa: E402


class TestRowCount(unittest.TestCase):
    def test_within_tolerance_is_none(self):
        self.assertIsNone(dp.check_row_count("users", 1000, 1030, tolerance_pct=5.0))

    def test_outside_tolerance_warns(self):
        finding = dp.check_row_count("users", 1000, 1200, tolerance_pct=5.0)
        self.assertIsNotNone(finding)
        self.assertEqual(finding.severity, "warn")

    def test_growth_only_never_flags_an_increase(self):
        finding = dp.check_row_count("events", 1000, 5000, tolerance_pct=1.0, growth_only=True)
        self.assertIsNone(finding)

    def test_growth_only_still_flags_a_decrease(self):
        finding = dp.check_row_count("events", 1000, 500, tolerance_pct=1.0, growth_only=True)
        self.assertIsNotNone(finding)


class TestDuplicateBusinessKey(unittest.TestCase):
    def test_zero_duplicates_passes(self):
        self.assertIsNone(dp.check_duplicate_business_key("users", ["email"], 0))

    def test_any_duplicate_blocks(self):
        finding = dp.check_duplicate_business_key("users", ["email"], 3)
        self.assertEqual(finding.severity, "block")


class TestDimStability(unittest.TestCase):
    def test_unchanged_value_set_passes(self):
        self.assertIsNone(dp.check_dim_stability("users", "status", {"active", "inactive"}, {"active", "inactive"}))

    def test_added_value_blocks(self):
        finding = dp.check_dim_stability("users", "status", {"active", "inactive"}, {"active", "inactive", "banned"})
        self.assertEqual(finding.severity, "block")

    def test_removed_value_blocks(self):
        finding = dp.check_dim_stability("users", "status", {"active", "inactive"}, {"active"})
        self.assertEqual(finding.severity, "block")


class TestNullRate(unittest.TestCase):
    def test_within_tolerance_passes(self):
        self.assertIsNone(dp.check_null_rate("users", "phone", 8.0, 9.0, tolerance_pct=3.0, block=False))

    def test_outside_tolerance_warns_when_not_configured_to_block(self):
        finding = dp.check_null_rate("users", "phone", 8.0, 20.0, tolerance_pct=3.0, block=False)
        self.assertEqual(finding.severity, "warn")

    def test_outside_tolerance_blocks_when_configured_to_block(self):
        finding = dp.check_null_rate("users", "phone", 8.0, 20.0, tolerance_pct=3.0, block=True)
        self.assertEqual(finding.severity, "block")


class TestFkOrphans(unittest.TestCase):
    def test_zero_orphans_passes(self):
        self.assertIsNone(dp.check_fk_orphans("users", "school_id", 0))

    def test_any_orphan_blocks(self):
        finding = dp.check_fk_orphans("users", "school_id", 5)
        self.assertEqual(finding.severity, "block")


class TestJunkValueRate(unittest.TestCase):
    def test_below_threshold_passes(self):
        self.assertIsNone(dp.check_junk_value_rate("users", "email", 1, 1000, threshold_pct=1.0, block=False))

    def test_above_threshold_warns(self):
        finding = dp.check_junk_value_rate("users", "email", 50, 1000, threshold_pct=1.0, block=False)
        self.assertEqual(finding.severity, "warn")

    def test_zero_total_rows_is_none_not_a_division_error(self):
        self.assertIsNone(dp.check_junk_value_rate("users", "email", 0, 0, threshold_pct=1.0, block=False))


class TestAnomalyMethods(unittest.TestCase):
    def test_iqr_flags_outlier(self):
        baseline = [100, 102, 98, 101, 99, 103, 97, 100]
        self.assertTrue(dp.iqr_anomaly(baseline, 500))

    def test_iqr_does_not_flag_normal_value(self):
        baseline = [100, 102, 98, 101, 99, 103, 97, 100]
        self.assertFalse(dp.iqr_anomaly(baseline, 101))

    def test_iqr_with_insufficient_history_never_flags(self):
        self.assertFalse(dp.iqr_anomaly([100, 101], 9999))

    def test_robust_zscore_flags_outlier(self):
        baseline = [100, 101, 99, 100, 102, 98, 100]
        self.assertTrue(dp.robust_zscore_anomaly(baseline, 1000))

    def test_psi_zero_for_identical_distributions(self):
        dist = {"a": 0.5, "b": 0.5}
        self.assertAlmostEqual(dp.psi(dist, dist), 0.0, places=6)

    def test_psi_positive_for_shifted_distribution(self):
        baseline = {"a": 0.9, "b": 0.1}
        proposed = {"a": 0.1, "b": 0.9}
        self.assertGreater(dp.psi(baseline, proposed), 0.25)

    def test_check_anomaly_respects_block_flag(self):
        baseline = [100, 102, 98, 101, 99, 103, 97, 100]
        finding = dp.check_anomaly("users", "signup_count", baseline, 9999, method="iqr", block=True)
        self.assertEqual(finding.severity, "block")
        finding_warn = dp.check_anomaly("users", "signup_count", baseline, 9999, method="iqr", block=False)
        self.assertEqual(finding_warn.severity, "warn")


if __name__ == "__main__":
    unittest.main()
