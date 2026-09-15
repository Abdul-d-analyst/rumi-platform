"""Pure-logic tests for profile_runner.py — identifier validation and the
anomaly-skip-when-no-history/no-value_agg behavior. No Postgres required;
the actual SQL execution is proven against a real database in
test_profile_runner_integration.py (skips without reachable Postgres).

Uses a lightweight FakeConnection/FakeCursor instead of unittest.mock —
MagicMock's auto-generated magic methods (int(), the `with` context-
manager protocol) silently produce plausible-looking-but-wrong values
(e.g. int(MagicMock()) == 1, and conn.cursor().__enter__() is a DIFFERENT
mock object than conn.cursor().return_value unless explicitly wired) that
can make a test pass for the wrong reason — found the hard way here when
_scalar()'s `with conn.cursor() as cur:` change silently stopped picking
up a MagicMock fixture's configured fetchone() return value. A fake class
makes exactly what each call returns unambiguous."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import profile_runner  # noqa: E402
from config import TableContract, DataProfileConfig  # noqa: E402


def make_contract(**overrides) -> TableContract:
    base = dict(
        table="users", allowed_columns=set(), expected_types={}, nullability={},
        criticality={}, primary_key=[], unique=[], foreign_keys=[], sensitivity={},
        profile={}, allowed_tolerances={}, source_path=Path("test.yaml"),
    )
    base.update(overrides)
    return TableContract(**base)


def make_global_cfg(**overrides) -> DataProfileConfig:
    base = dict(row_count_tolerance_pct=10.0, null_rate_tolerance_pct=5.0,
                anomaly_method="iqr", anomaly_block=False, advisory_rollout={})
    base.update(overrides)
    return DataProfileConfig(**base)


class FakeCursor:
    """Replays one canned row per execute() call, in call order."""

    def __init__(self, rows: list[tuple]):
        self._rows = list(rows)
        self._call_index = -1

    def execute(self, sql, params=()):
        self._call_index += 1

    def fetchone(self):
        return self._rows[self._call_index]

    def fetchall(self):
        return [self._rows[self._call_index]] if self._rows[self._call_index] else []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, rows: list[tuple]):
        self._cursor = FakeCursor(rows)

    def cursor(self):
        return self._cursor


class TestQuoteIdent(unittest.TestCase):
    def test_valid_identifier_is_quoted(self):
        self.assertEqual(profile_runner._quote_ident("users"), '"users"')

    def test_identifier_with_underscore_and_digits_is_valid(self):
        self.assertEqual(profile_runner._quote_ident("audit_log_2"), '"audit_log_2"')

    def test_sql_injection_shaped_identifier_is_rejected(self):
        with self.assertRaises(profile_runner.ProfileRunnerError):
            profile_runner._quote_ident("users; DROP TABLE x --")

    def test_identifier_with_space_is_rejected(self):
        with self.assertRaises(profile_runner.ProfileRunnerError):
            profile_runner._quote_ident("users table")

    def test_empty_identifier_is_rejected(self):
        with self.assertRaises(profile_runner.ProfileRunnerError):
            profile_runner._quote_ident("")


class TestProfileTableNoContractSections(unittest.TestCase):
    def test_empty_profile_produces_no_findings_beyond_row_count(self):
        """A contract with only a bare {} profile section still gets the
        row-count check (it always runs when a table is profiled at all),
        but nothing else — no duplicate/DIM/null-rate/junk/anomaly checks
        fire without their own declared config."""
        base_conn = FakeConnection([(100,)])
        head_conn = FakeConnection([(100,)])

        contract = make_contract(profile={})
        findings = profile_runner.profile_table(base_conn, head_conn, "users", contract, make_global_cfg())
        # Same row count both sides -> check_row_count returns None -> no findings at all.
        self.assertEqual(findings, [])

    def test_differing_row_count_beyond_tolerance_produces_a_finding(self):
        base_conn = FakeConnection([(100,)])
        head_conn = FakeConnection([(200,)])  # +100% change, way outside default 10% tolerance

        contract = make_contract(profile={})
        findings = profile_runner.profile_table(base_conn, head_conn, "users", contract, make_global_cfg())
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["check"], "row_count")


class TestAnomalySkipWithoutHistoryOrValueAgg(unittest.TestCase):
    def test_anomaly_column_without_history_reports_skipped_not_silently_omitted(self):
        base_conn = FakeConnection([(100,)])
        head_conn = FakeConnection([(100,)])

        contract = make_contract(profile={
            "anomaly": {"method": "iqr", "columns": ["signup_count"], "history": {}},
        })
        findings = profile_runner.profile_table(base_conn, head_conn, "users", contract, make_global_cfg())
        anomaly_findings = [f for f in findings if f["check"] == "anomaly_iqr"]
        self.assertEqual(len(anomaly_findings), 1)
        self.assertEqual(anomaly_findings[0]["severity"], "skipped")
        self.assertIn("no profile.anomaly.history", anomaly_findings[0]["detail"])

    def test_anomaly_column_with_history_but_no_value_agg_is_still_skipped(self):
        """History alone isn't enough — an explicit value_agg is required
        too, since an arbitrary ORDER BY on the metric column itself would
        silently pick the wrong 'current value' for most real metrics
        (found during adversarial review)."""
        base_conn = FakeConnection([(100,)])
        head_conn = FakeConnection([(100,)])

        contract = make_contract(profile={
            "anomaly": {
                "method": "iqr", "columns": ["signup_count"],
                "history": {"signup_count": [100, 102, 98, 101, 99, 103, 97, 100]},
                # value_agg deliberately omitted
            },
        })
        findings = profile_runner.profile_table(base_conn, head_conn, "users", contract, make_global_cfg())
        anomaly_findings = [f for f in findings if f["check"] == "anomaly_iqr"]
        self.assertEqual(len(anomaly_findings), 1)
        self.assertEqual(anomaly_findings[0]["severity"], "skipped")
        self.assertIn("no profile.anomaly.value_agg", anomaly_findings[0]["detail"])

    def test_anomaly_column_with_history_and_value_agg_runs_the_real_check(self):
        # row_count() always runs first regardless of contract content, so
        # each fake connection needs one row for that call, then head_conn
        # needs a second row for the anomaly SUM(...) aggregate.
        base_conn = FakeConnection([(100,)])
        head_conn = FakeConnection([(100,), (9999,)])

        contract = make_contract(profile={
            "anomaly": {
                "method": "iqr", "columns": ["signup_count"],
                "history": {"signup_count": [100, 102, 98, 101, 99, 103, 97, 100]},
                "value_agg": {"signup_count": "sum"},
                "block_columns": ["signup_count"],
            },
        })
        findings = profile_runner.profile_table(base_conn, head_conn, "users", contract, make_global_cfg())
        anomaly_findings = [f for f in findings if f["check"] == "anomaly_iqr"]
        self.assertEqual(len(anomaly_findings), 1)
        self.assertEqual(anomaly_findings[0]["severity"], "block")


if __name__ == "__main__":
    unittest.main()
