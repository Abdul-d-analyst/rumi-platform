"""Tests for contract_check.py — new-column/new-table gate logic. No
Postgres required."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import contract_check  # noqa: E402
from config import TableContract  # noqa: E402


def make_contract(**overrides) -> TableContract:
    base = dict(
        table="users", allowed_columns=set(), expected_types={}, nullability={},
        criticality={}, primary_key=[], unique=[], foreign_keys=[], sensitivity={},
        profile={}, allowed_tolerances={}, source_path=Path("test.yaml"),
    )
    base.update(overrides)
    return TableContract(**base)


class TestNewColumn(unittest.TestCase):
    def test_new_pii_column_without_contract_sensitivity_blocks(self):
        findings = contract_check.check_new_column("users", "phone", {"type": "text", "nullable": True}, None)
        self.assertTrue(any(f["kind"] == "new_pii_column_unclassified" and f["blocking"] for f in findings))

    def test_new_pii_column_with_declared_sensitivity_does_not_block(self):
        contract = make_contract(sensitivity={"phone": {"classification": "pii", "masking_required": True}})
        findings = contract_check.check_new_column("users", "phone", {"type": "text", "nullable": True}, contract)
        self.assertFalse(any(f["kind"] == "new_pii_column_unclassified" for f in findings))

    def test_new_non_pii_column_without_contract_is_fine(self):
        findings = contract_check.check_new_column("users", "notes", {"type": "text", "nullable": True}, None)
        self.assertEqual(findings, [])

    def test_column_not_in_allowed_list_is_advisory_only(self):
        contract = make_contract(allowed_columns={"id", "email"})
        findings = contract_check.check_new_column("users", "notes", {"type": "text", "nullable": True}, contract)
        notes_finding = next(f for f in findings if f["kind"] == "column_not_in_contract")
        self.assertFalse(notes_finding["blocking"])

    def test_type_mismatch_vs_contract_is_advisory_only(self):
        contract = make_contract(expected_types={"id": "uuid"})
        findings = contract_check.check_new_column("users", "id", {"type": "text", "nullable": False}, contract)
        mismatch = next(f for f in findings if f["kind"] == "type_mismatch_vs_contract")
        self.assertFalse(mismatch["blocking"])

    def test_email_variants_are_detected_as_pii(self):
        for name in ("email", "cnic", "date_of_birth", "national_id"):
            findings = contract_check.check_new_column("users", name, {"type": "text", "nullable": True}, None)
            self.assertTrue(any(f["kind"] == "new_pii_column_unclassified" for f in findings), f"{name} should be flagged as PII-shaped")

    def test_realistic_snake_case_pii_column_names_are_detected(self):
        """Regression test: a plain \\bphone\\b-style regex never matches
        inside phone_number/contact_phone_number/user_phone at all, because
        \\b treats underscore as a word character — so it only ever matches
        the bare standalone word "phone". Found via a live fork-test run
        against a real Postgres column literally named
        contact_phone_number, which produced zero PII findings under the
        old pattern. Every one of these is the realistic snake_case
        convention this gate exists to check, not an edge case."""
        realistic_names = [
            "phone_number", "contact_phone_number", "user_phone",
            "email_address", "user_email", "cnic_number",
            "national_id", "date_of_birth", "home_address", "passport_number",
        ]
        for name in realistic_names:
            findings = contract_check.check_new_column("users", name, {"type": "text", "nullable": True}, None)
            self.assertTrue(
                any(f["kind"] == "new_pii_column_unclassified" for f in findings),
                f"{name} should be flagged as PII-shaped (snake_case regression)",
            )

    def test_non_pii_column_names_are_not_falsely_flagged(self):
        for name in ("id", "name", "created_at", "status", "priority_level", "phonebook_reference"):
            findings = contract_check.check_new_column("users", name, {"type": "text", "nullable": True}, None)
            self.assertFalse(
                any(f["kind"] == "new_pii_column_unclassified" for f in findings),
                f"{name} should NOT be flagged as PII-shaped",
            )


class TestNewTable(unittest.TestCase):
    def test_new_table_without_primary_key_blocks(self):
        snapshot = {"columns": {"name": {"type": "text", "nullable": True}}, "primary_key": []}
        findings = contract_check.check_new_table("audit_log", snapshot, None)
        self.assertTrue(any(f["kind"] == "new_table_missing_primary_key" and f["blocking"] for f in findings))

    def test_new_table_with_primary_key_does_not_trigger_pk_finding(self):
        snapshot = {"columns": {"id": {"type": "uuid", "nullable": False}}, "primary_key": ["id"]}
        findings = contract_check.check_new_table("audit_log", snapshot, None)
        self.assertFalse(any(f["kind"] == "new_table_missing_primary_key" for f in findings))


if __name__ == "__main__":
    unittest.main()
