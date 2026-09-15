"""Pure-logic unit tests for sql_snapshot.py — no Postgres required, run
with:  cd skills/data-quality-gate && python3 -m unittest tests.test_sql_snapshot

Covers: the type-name translation (SQL keyword -> the same udt_name
vocabulary introspect.py's live snapshot uses), inline vs. table-level
PRIMARY KEY detection (including the composite-key case), and that the
resulting snapshot dict is directly consumable by schema_diff.py and
contract_check.py's real functions — not just structurally similar to
their expected input.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from sql_snapshot import parse_sql_to_snapshot  # noqa: E402
import schema_diff  # noqa: E402
import contract_check  # noqa: E402


class TestTypeTranslation(unittest.TestCase):
    def test_varchar_with_length(self):
        snap = parse_sql_to_snapshot("CREATE TABLE t (c VARCHAR(255));")
        col = snap["tables"]["t"]["columns"]["c"]
        self.assertEqual(col["type"], "varchar")
        self.assertEqual(col["length"], 255)

    def test_numeric_with_precision_and_scale(self):
        snap = parse_sql_to_snapshot("CREATE TABLE t (c NUMERIC(10, 2));")
        col = snap["tables"]["t"]["columns"]["c"]
        self.assertEqual(col["type"], "numeric")
        self.assertEqual(col["precision"], 10)
        self.assertEqual(col["scale"], 2)

    def test_integer_family(self):
        for sql_kw, expected in (("SMALLINT", "int2"), ("INTEGER", "int4"),
                                  ("BIGINT", "int8"), ("INT", "int4")):
            with self.subTest(sql_kw=sql_kw):
                snap = parse_sql_to_snapshot(f"CREATE TABLE t (c {sql_kw});")
                self.assertEqual(snap["tables"]["t"]["columns"]["c"]["type"], expected)

    def test_timestamptz_sets_timezone_flag(self):
        snap = parse_sql_to_snapshot("CREATE TABLE t (c TIMESTAMPTZ);")
        col = snap["tables"]["t"]["columns"]["c"]
        self.assertEqual(col["type"], "timestamptz")
        self.assertTrue(col["timezone"])

    def test_plain_timestamp_not_timezone(self):
        snap = parse_sql_to_snapshot("CREATE TABLE t (c TIMESTAMP);")
        col = snap["tables"]["t"]["columns"]["c"]
        self.assertEqual(col["type"], "timestamp")
        self.assertFalse(col["timezone"])

    def test_multiword_type_double_precision(self):
        snap = parse_sql_to_snapshot("CREATE TABLE t (c DOUBLE PRECISION);")
        self.assertEqual(snap["tables"]["t"]["columns"]["c"]["type"], "float8")

    def test_serial_maps_to_int4(self):
        snap = parse_sql_to_snapshot("CREATE TABLE t (c SERIAL);")
        self.assertEqual(snap["tables"]["t"]["columns"]["c"]["type"], "int4")

    def test_unrecognized_type_kept_as_literal_not_guessed(self):
        snap = parse_sql_to_snapshot("CREATE TABLE t (c CIDR);")
        self.assertEqual(snap["tables"]["t"]["columns"]["c"]["type"], "cidr")


class TestPrimaryKeyDetection(unittest.TestCase):
    def test_inline_primary_key(self):
        snap = parse_sql_to_snapshot(
            "CREATE TABLE users (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), name TEXT);"
        )
        self.assertEqual(snap["tables"]["users"]["primary_key"], ["id"])

    def test_inline_primary_key_with_function_call_default_does_not_confuse_paren_matching(self):
        """The exact bug found during development: naive case-sensitive
        splitting on the literal substring "primary key" left the parens
        in gen_random_uuid() unstripped, so the inline-PK check always
        saw a "(" before "primary key" and never registered id as the PK."""
        snap = parse_sql_to_snapshot(
            "CREATE TABLE t (id UUID PRIMARY KEY DEFAULT gen_random_uuid());"
        )
        self.assertEqual(snap["tables"]["t"]["primary_key"], ["id"])

    def test_table_level_composite_primary_key(self):
        snap = parse_sql_to_snapshot(
            "CREATE TABLE school_terms (school_id UUID NOT NULL, "
            "term_code VARCHAR(10) NOT NULL, PRIMARY KEY (school_id, term_code));"
        )
        self.assertEqual(snap["tables"]["school_terms"]["primary_key"],
                          ["school_id", "term_code"])

    def test_no_primary_key_at_all(self):
        snap = parse_sql_to_snapshot("CREATE TABLE t (event_type TEXT NOT NULL);")
        self.assertEqual(snap["tables"]["t"]["primary_key"], [])


class TestNullability(unittest.TestCase):
    def test_not_null_column(self):
        snap = parse_sql_to_snapshot("CREATE TABLE t (c TEXT NOT NULL);")
        self.assertFalse(snap["tables"]["t"]["columns"]["c"]["nullable"])

    def test_nullable_by_default(self):
        snap = parse_sql_to_snapshot("CREATE TABLE t (c TEXT);")
        self.assertTrue(snap["tables"]["t"]["columns"]["c"]["nullable"])

    def test_primary_key_column_implicitly_not_null(self):
        snap = parse_sql_to_snapshot("CREATE TABLE t (id UUID PRIMARY KEY);")
        self.assertFalse(snap["tables"]["t"]["columns"]["id"]["nullable"])


class TestIntegrationWithSchemaDiff(unittest.TestCase):
    """The real proof this module exists for: its output must be directly
    usable by schema_diff.py's real functions, unmodified — not just
    shaped similarly to what introspect.py would have produced."""

    def test_narrowing_varchar_blocks(self):
        before = parse_sql_to_snapshot("CREATE TABLE users (phone VARCHAR(20));")
        after = parse_sql_to_snapshot("CREATE TABLE users (phone VARCHAR(10));")
        changes = schema_diff.diff_columns(
            "users", before["tables"]["users"]["columns"], after["tables"]["users"]["columns"])
        phone = next(c for c in changes if c.column == "phone")
        self.assertEqual(phone.classification, "NARROWED")
        self.assertTrue(phone.blocking)

    def test_widening_varchar_does_not_block(self):
        before = parse_sql_to_snapshot("CREATE TABLE users (phone VARCHAR(20));")
        after = parse_sql_to_snapshot("CREATE TABLE users (phone VARCHAR(40));")
        changes = schema_diff.diff_columns(
            "users", before["tables"]["users"]["columns"], after["tables"]["users"]["columns"])
        phone = next(c for c in changes if c.column == "phone")
        self.assertEqual(phone.classification, "WIDENED")
        self.assertFalse(phone.blocking)

    def test_removed_column_blocks(self):
        before = parse_sql_to_snapshot("CREATE TABLE users (id UUID PRIMARY KEY, phone TEXT);")
        after = parse_sql_to_snapshot("CREATE TABLE users (id UUID PRIMARY KEY);")
        changes = schema_diff.diff_columns(
            "users", before["tables"]["users"]["columns"], after["tables"]["users"]["columns"])
        phone = next(c for c in changes if c.column == "phone")
        self.assertEqual(phone.classification, "REMOVED")
        self.assertTrue(phone.blocking)


class TestIntegrationWithContractCheck(unittest.TestCase):
    """Same proof, against contract_check.py's real functions."""

    def test_new_table_missing_primary_key_is_caught(self):
        snap = parse_sql_to_snapshot("CREATE TABLE audit_events (event_type TEXT NOT NULL);")
        findings = contract_check.check_new_table(
            "audit_events", snap["tables"]["audit_events"], contract=None)
        kinds = [f["kind"] for f in findings]
        self.assertIn("new_table_missing_primary_key", kinds)

    def test_snake_case_pii_column_is_caught(self):
        """The exact real-world bug proven in this pack's fork-test — a
        column named contact_phone_number, not the bare word "phone"."""
        snap = parse_sql_to_snapshot(
            "CREATE TABLE staff (id UUID PRIMARY KEY, contact_phone_number VARCHAR(20));"
        )
        findings = contract_check.check_new_column(
            "staff", "contact_phone_number",
            snap["tables"]["staff"]["columns"]["contact_phone_number"], contract=None)
        kinds = [f["kind"] for f in findings]
        self.assertIn("new_pii_column_unclassified", kinds)

    def test_compliant_table_produces_no_blocking_finding(self):
        snap = parse_sql_to_snapshot(
            "CREATE TABLE lesson_progress (id UUID PRIMARY KEY, lesson_code TEXT NOT NULL);"
        )
        findings = contract_check.check_new_table(
            "lesson_progress", snap["tables"]["lesson_progress"], contract=None)
        self.assertFalse(any(f["blocking"] for f in findings))


if __name__ == "__main__":
    unittest.main()
