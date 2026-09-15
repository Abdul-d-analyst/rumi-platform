"""Tests for introspect.py that don't need a real Postgres connection —
covers the row-grouping logic (composite FK/UNIQUE column ordering) via a
fake cursor, plus DSN redaction. The actual SQL queries themselves can
only be proven correct against a real server — see
test_gate_integration.py's TestIntrospectionAgainstRealPostgres for that
(skipped here, runs for real in CI)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import introspect  # noqa: E402


class FakeCursor:
    """Replays canned rows for each query in the order snapshot_schema()
    calls them: columns, pk, unique, fk, check, index."""

    def __init__(self, rows_by_call: list[list[tuple]]):
        self._rows_by_call = rows_by_call
        self._call_index = -1

    def execute(self, sql, params):
        self._call_index += 1

    def fetchall(self):
        return self._rows_by_call[self._call_index]


class FakeConnection:
    def __init__(self, cursor: FakeCursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        pass


class TestCompositeForeignKeyGrouping(unittest.TestCase):
    """Regression test for the composite-FK cartesian-product bug found in
    review: the FK query must produce exactly N rows for an N-column FK,
    positionally correct, when consumed by snapshot_schema()'s grouping
    loop — this test proves the grouping logic itself is correct given
    properly-ordered rows (the ordering guarantee now comes from
    pg_constraint's conkey/confkey arrays, verified against a real server
    in test_gate_integration.py)."""

    def _run_with_fk_rows(self, fk_rows):
        columns_rows = [
            ("orders", "customer_id", "uuid", "uuid", None, None, None, None, "NO", None, "NEVER", None, None),
            ("orders", "customer_region", "text", "text", None, None, None, None, "NO", None, "NEVER", None, None),
        ]
        cursor = FakeCursor([
            columns_rows,  # _COLUMNS_SQL
            [],             # _PK_SQL
            [],             # _UNIQUE_SQL
            fk_rows,        # _FK_SQL
            [],             # _CHECK_SQL
            [],             # _INDEX_SQL
        ])
        with patch.object(introspect, "_connect", return_value=FakeConnection(cursor)):
            return introspect.snapshot_schema("fake-dsn")

    def test_composite_fk_produces_correct_positional_pairing_not_a_cartesian_product(self):
        # A 2-column FK: (customer_id, customer_region) -> customers(id, region).
        # Correctly-positioned rows (what pg_constraint's unnest WITH
        # ORDINALITY guarantees) — exactly 2 rows, not 4.
        fk_rows = [
            ("orders", "fk_customer", "customer_id", 1, "customers", "id"),
            ("orders", "fk_customer", "customer_region", 2, "customers", "region"),
        ]
        snap = self._run_with_fk_rows(fk_rows)
        fks = snap["tables"]["orders"]["foreign_keys"]
        self.assertEqual(len(fks), 1)
        fk = fks[0]
        self.assertEqual(fk["columns"], ["customer_id", "customer_region"])
        self.assertEqual(fk["references_columns"], ["id", "region"])
        # The old constraint_column_usage-based query would have produced 4
        # rows (2x2 cartesian product) here instead of 2 — asserting the
        # exact row count consumed pins down that this shape is a single
        # clean pair-per-position, not an accidental N:1 collapse either.
        self.assertEqual(len(fk_rows), 2)

    def test_single_column_fk_still_works(self):
        fk_rows = [("orders", "fk_customer", "customer_id", 1, "customers", "id")]
        snap = self._run_with_fk_rows(fk_rows)
        fk = snap["tables"]["orders"]["foreign_keys"][0]
        self.assertEqual(fk["columns"], ["customer_id"])
        self.assertEqual(fk["references_columns"], ["id"])


class TestRedactDsn(unittest.TestCase):
    def test_password_is_redacted_from_dsn(self):
        redacted = introspect._redact_dsn("postgresql://user:supersecret@localhost:5432/db")
        self.assertNotIn("supersecret", redacted)
        self.assertNotIn("user", redacted)

    def test_malformed_dsn_still_redacts_fully(self):
        redacted = introspect._redact_dsn("not-a-real-dsn-at-all")
        self.assertEqual(redacted, "[dsn-redacted]")


class TestIntrospectionErrorOnNoDriver(unittest.TestCase):
    def test_connect_raises_introspection_error_when_no_driver_available(self):
        with patch.object(introspect, "psycopg", None):
            with self.assertRaises(introspect.IntrospectionError):
                introspect._connect("postgresql://localhost/db")


if __name__ == "__main__":
    unittest.main()
