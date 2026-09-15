"""Pure-logic unit tests for schema_diff.py — no Postgres required, run
with:  python3 -m unittest skills.data-quality-gate.tests.test_schema_diff
(or from this skill's own directory: python3 -m unittest tests.test_schema_diff)

Covers the assignment's required-tests list for structural classification:
compliant additions pass, removed table/column blocks, narrowing/truncating
blocks, PK/FK/unique/check weakening blocks.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from schema_diff import diff_schema, diff_columns, diff_constraints  # noqa: E402


def col(type_, **kw):
    base = {"type": type_, "length": None, "precision": None, "scale": None,
            "nullable": True, "default": None, "generated": False,
            "generation_expression": None, "collation": None, "timezone": False}
    base.update(kw)
    return base


class TestColumnDiff(unittest.TestCase):
    def test_added_column_is_not_blocking(self):
        before = {"id": col("uuid", nullable=False)}
        after = {"id": col("uuid", nullable=False), "notes": col("text")}
        changes = diff_columns("users", before, after)
        notes = next(c for c in changes if c.column == "notes")
        self.assertEqual(notes.classification, "ADDED")
        self.assertFalse(notes.blocking)

    def test_removed_column_blocks(self):
        before = {"id": col("uuid"), "phone": col("varchar", length=20)}
        after = {"id": col("uuid")}
        changes = diff_columns("users", before, after)
        phone = next(c for c in changes if c.column == "phone")
        self.assertEqual(phone.classification, "REMOVED")
        self.assertTrue(phone.blocking)

    def test_narrowed_varchar_length_blocks(self):
        before = {"phone": col("varchar", length=20)}
        after = {"phone": col("varchar", length=10)}
        changes = diff_columns("users", before, after)
        self.assertEqual(changes[0].classification, "NARROWED")
        self.assertTrue(changes[0].blocking)

    def test_widened_varchar_length_does_not_block(self):
        before = {"phone": col("varchar", length=20)}
        after = {"phone": col("varchar", length=40)}
        changes = diff_columns("users", before, after)
        self.assertEqual(changes[0].classification, "WIDENED")
        self.assertFalse(changes[0].blocking)

    def test_narrowed_numeric_precision_blocks(self):
        before = {"amount": col("numeric", precision=10, scale=2)}
        after = {"amount": col("numeric", precision=6, scale=2)}
        changes = diff_columns("orders", before, after)
        self.assertEqual(changes[0].classification, "NARROWED")
        self.assertTrue(changes[0].blocking)

    def test_int_downgrade_blocks(self):
        before = {"count": col("int8")}
        after = {"count": col("int4")}
        changes = diff_columns("stats", before, after)
        self.assertEqual(changes[0].classification, "NARROWED")
        self.assertTrue(changes[0].blocking)

    def test_int_upgrade_does_not_block(self):
        before = {"count": col("int4")}
        after = {"count": col("int8")}
        changes = diff_columns("stats", before, after)
        self.assertEqual(changes[0].classification, "WIDENED")
        self.assertFalse(changes[0].blocking)

    def test_making_column_not_null_blocks(self):
        before = {"phone": col("text", nullable=True)}
        after = {"phone": col("text", nullable=False)}
        changes = diff_columns("users", before, after)
        self.assertEqual(changes[0].classification, "MODIFIED")
        self.assertTrue(changes[0].blocking)

    def test_undeclared_rename_is_removed_plus_added_not_suppressed(self):
        before = {"old_name": col("text")}
        after = {"new_name": col("text")}
        changes = diff_columns("users", before, after)  # no rename_map passed
        classes = {c.column: c.classification for c in changes}
        self.assertEqual(classes.get("old_name"), "REMOVED")
        self.assertEqual(classes.get("new_name"), "ADDED")
        removed = next(c for c in changes if c.column == "old_name")
        self.assertTrue(removed.blocking)

    def test_declared_rename_is_reported_as_renamed_not_blocking(self):
        before = {"old_name": col("text")}
        after = {"new_name": col("text")}
        changes = diff_columns("users", before, after, rename_map={"old_name": "new_name"})
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].classification, "RENAMED")
        self.assertFalse(changes[0].blocking)

    def test_unchanged_column_reported_as_unchanged(self):
        before = {"id": col("uuid", nullable=False)}
        after = {"id": col("uuid", nullable=False)}
        changes = diff_columns("users", before, after)
        self.assertEqual(changes[0].classification, "UNCHANGED")
        self.assertFalse(changes[0].blocking)


class TestConstraintDiff(unittest.TestCase):
    def test_primary_key_removed_blocks(self):
        before = {"primary_key": ["id"], "unique_constraints": [], "check_constraints": [], "foreign_keys": [], "indexes": []}
        after = {"primary_key": [], "unique_constraints": [], "check_constraints": [], "foreign_keys": [], "indexes": []}
        findings = diff_constraints("users", before, after)
        kinds = {f["kind"] for f in findings}
        self.assertIn("primary_key_removed", kinds)
        self.assertTrue(all(f["blocking"] for f in findings if f["kind"] == "primary_key_removed"))

    def test_unique_constraint_removed_blocks(self):
        before = {"primary_key": ["id"], "unique_constraints": [["email"]], "check_constraints": [], "foreign_keys": [], "indexes": []}
        after = {"primary_key": ["id"], "unique_constraints": [], "check_constraints": [], "foreign_keys": [], "indexes": []}
        findings = diff_constraints("users", before, after)
        self.assertTrue(any(f["kind"] == "unique_constraint_removed" and f["blocking"] for f in findings))

    def test_foreign_key_removed_blocks(self):
        before = {"primary_key": ["id"], "unique_constraints": [], "check_constraints": [], "indexes": [],
                  "foreign_keys": [{"name": "fk1", "columns": ["school_id"], "references_table": "schools", "references_columns": ["id"]}]}
        after = {"primary_key": ["id"], "unique_constraints": [], "check_constraints": [], "foreign_keys": [], "indexes": []}
        findings = diff_constraints("users", before, after)
        self.assertTrue(any(f["kind"] == "foreign_key_removed" and f["blocking"] for f in findings))

    def test_check_constraint_removed_blocks(self):
        before = {"primary_key": ["id"], "unique_constraints": [], "foreign_keys": [], "indexes": [],
                  "check_constraints": [{"name": "status_check", "definition": "status IN ('a','b')"}]}
        after = {"primary_key": ["id"], "unique_constraints": [], "foreign_keys": [], "indexes": [], "check_constraints": []}
        findings = diff_constraints("users", before, after)
        self.assertTrue(any(f["kind"] == "check_constraint_removed" and f["blocking"] for f in findings))

    def test_index_removed_is_advisory_only(self):
        before = {"primary_key": ["id"], "unique_constraints": [], "foreign_keys": [], "check_constraints": [],
                  "indexes": [{"name": "idx1", "columns": ["school_id"], "unique": False}]}
        after = {"primary_key": ["id"], "unique_constraints": [], "foreign_keys": [], "check_constraints": [], "indexes": []}
        findings = diff_constraints("users", before, after)
        idx_finding = next(f for f in findings if f["kind"] == "index_removed")
        self.assertFalse(idx_finding["blocking"])


class TestTableDiff(unittest.TestCase):
    def test_removed_table_blocks(self):
        before = {"tables": {"legacy": {"columns": {}, "primary_key": ["id"], "unique_constraints": [], "foreign_keys": [], "check_constraints": [], "indexes": []}}}
        after = {"tables": {}}
        result = diff_schema(before, after)
        legacy = next(tc for tc in result.table_changes if tc.table == "legacy")
        self.assertEqual(legacy.classification, "REMOVED")
        self.assertTrue(any(f["blocking"] for f in legacy.constraint_findings))
        self.assertTrue(any(f["table"] == "legacy" for f in result.blocking_findings()))

    def test_added_table_is_not_blocking(self):
        before = {"tables": {}}
        after = {"tables": {"new_table": {"columns": {"id": col("uuid")}, "primary_key": ["id"], "unique_constraints": [], "foreign_keys": [], "check_constraints": [], "indexes": []}}}
        result = diff_schema(before, after)
        self.assertEqual(result.table_changes[0].classification, "ADDED")
        self.assertEqual(result.blocking_findings(), [])

    def test_unchanged_schema_produces_no_blocking_findings(self):
        snap = {"tables": {"users": {"columns": {"id": col("uuid", nullable=False)}, "primary_key": ["id"], "unique_constraints": [], "foreign_keys": [], "check_constraints": [], "indexes": []}}}
        result = diff_schema(snap, snap)
        self.assertEqual(result.blocking_findings(), [])


if __name__ == "__main__":
    unittest.main()
