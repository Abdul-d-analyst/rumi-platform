"""Tests for evidence.py — the developer-evidence exception's deterministic
structural match. No Postgres required."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import evidence  # noqa: E402
from config import ChangeEvidence  # noqa: E402


def make_evidence(affected_objects) -> ChangeEvidence:
    return ChangeEvidence(
        purpose="p" * 20, ticket="T-1", affected_objects=affected_objects,
        reason="r" * 20, dependency_scan="d" * 20, archive_evidence="a" * 20,
        validation_evidence="v" * 20, recovery_plan="rp" * 20,
        attestation={"owner": "x", "date": "2026-01-01", "statement": "s"},
        source_path=Path("test.yaml"),
    )


class TestVerifyEvidence(unittest.TestCase):
    def test_matching_evidence_passes(self):
        ev = make_evidence([{
            "table": "users", "column": "legacy_phone", "change_type": "REMOVED",
            "before": {"type": "text"}, "after": None,
        }])
        findings = [{"table": "users", "column": "legacy_phone", "classification": "REMOVED",
                     "before": {"type": "text", "nullable": True}, "after": None}]
        verdict = evidence.verify_evidence(ev, findings)
        self.assertTrue(verdict.matched)
        self.assertEqual(len(verdict.covered_findings), 1)
        self.assertEqual(verdict.uncovered_findings, [])

    def test_mismatched_before_definition_fails(self):
        ev = make_evidence([{
            "table": "users", "column": "legacy_phone", "change_type": "REMOVED",
            "before": {"type": "varchar"}, "after": None,  # real diff says "text", not "varchar"
        }])
        findings = [{"table": "users", "column": "legacy_phone", "classification": "REMOVED",
                     "before": {"type": "text"}, "after": None}]
        verdict = evidence.verify_evidence(ev, findings)
        self.assertFalse(verdict.matched)
        self.assertTrue(verdict.mismatches)

    def test_evidence_for_nonexistent_finding_fails(self):
        ev = make_evidence([{
            "table": "ghost_table", "column": "ghost_col", "change_type": "REMOVED",
            "before": {"type": "text"}, "after": None,
        }])
        findings = []  # nothing actually detected in the real diff
        verdict = evidence.verify_evidence(ev, findings)
        self.assertFalse(verdict.matched)
        self.assertTrue(verdict.mismatches)

    def test_evidence_covering_only_some_findings_leaves_rest_uncovered(self):
        ev = make_evidence([{
            "table": "users", "column": "legacy_phone", "change_type": "REMOVED",
            "before": {"type": "text"}, "after": None,
        }])
        findings = [
            {"table": "users", "column": "legacy_phone", "classification": "REMOVED", "before": {"type": "text"}, "after": None},
            {"table": "users", "column": "ssn", "classification": "REMOVED", "before": {"type": "text"}, "after": None},
        ]
        verdict = evidence.verify_evidence(ev, findings)
        self.assertFalse(verdict.matched)
        self.assertEqual(len(verdict.covered_findings), 1)
        self.assertEqual(len(verdict.uncovered_findings), 1)

    def test_no_findings_and_no_declared_objects_trivially_matches(self):
        ev = make_evidence([])
        # config.py's presence-check requires affected_objects non-empty, so
        # this scenario (empty evidence, empty findings) is only reachable
        # if evidence.py is called directly bypassing config validation —
        # exercised here purely to confirm evidence.py's own logic doesn't
        # crash on an empty list.
        verdict = evidence.verify_evidence(ev, [])
        self.assertTrue(verdict.matched)


if __name__ == "__main__":
    unittest.main()
