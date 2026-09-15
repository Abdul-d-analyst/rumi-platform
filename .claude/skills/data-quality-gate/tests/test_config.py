"""Tests for config.py — malformed config must fail loudly, never silently
skip (assignment requirement). No Postgres required."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import config  # noqa: E402


class TestTableContract(unittest.TestCase):
    def _write(self, text: str) -> Path:
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8")
        f.write(text)
        f.close()
        return Path(f.name)

    def test_valid_contract_loads(self):
        path = self._write("""
table: users
columns:
  allowed: [id, email]
  expected_types: {id: uuid, email: text}
  nullability: {id: not_null}
  criticality: {email: critical}
keys:
  primary_key: [id]
  unique: [[email]]
  foreign_keys: []
sensitivity:
  email: {classification: pii, masking_required: true}
profile: {}
allowed_tolerances: {}
""")
        contract = config.load_table_contract(path)
        self.assertEqual(contract.table, "users")
        self.assertEqual(contract.allowed_columns, {"id", "email"})
        self.assertEqual(contract.primary_key, ["id"])

    def test_missing_table_key_raises(self):
        path = self._write("columns: {}\n")
        with self.assertRaises(config.ConfigError):
            config.load_table_contract(path)

    def test_invalid_nullability_value_raises(self):
        path = self._write("""
table: users
columns:
  nullability: {id: sometimes}
""")
        with self.assertRaises(config.ConfigError):
            config.load_table_contract(path)

    def test_malformed_yaml_raises_configerror_not_generic_exception(self):
        path = self._write("table: users\n  bad indent: [\n")
        with self.assertRaises(config.ConfigError):
            config.load_table_contract(path)

    def test_invalid_table_identifier_raises(self):
        path = self._write("table: 'users; DROP TABLE x'\n")
        with self.assertRaises(config.ConfigError):
            config.load_table_contract(path)

    def test_duplicate_contract_for_same_table_raises(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a.yaml").write_text("table: users\n", encoding="utf-8")
            (d / "b.yaml").write_text("table: users\n", encoding="utf-8")
            with self.assertRaises(config.ConfigError):
                config.load_all_table_contracts(d)

    def test_disabled_template_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "_example.yaml.disabled").write_text("not even yaml: [\n", encoding="utf-8")
            contracts = config.load_all_table_contracts(d)
            self.assertEqual(contracts, {})

    def test_underscore_prefixed_yaml_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "_example.yaml").write_text("table: users\n", encoding="utf-8")
            contracts = config.load_all_table_contracts(d)
            self.assertEqual(contracts, {})


class TestDataProfileConfig(unittest.TestCase):
    def _write(self, text: str) -> Path:
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8")
        f.write(text)
        f.close()
        return Path(f.name)

    def test_valid_config_loads_with_defaults(self):
        path = self._write("defaults: {}\nadvisory_rollout: {}\n")
        cfg = config.load_data_profile_config(path)
        self.assertEqual(cfg.anomaly_method, "iqr")
        self.assertEqual(cfg.row_count_tolerance_pct, 10.0)

    def test_missing_file_raises(self):
        with self.assertRaises(config.ConfigError):
            config.load_data_profile_config(Path("/nonexistent/data-profile.yaml"))

    def test_unknown_top_level_key_raises(self):
        path = self._write("defaults: {}\nsome_typo: {}\n")
        with self.assertRaises(config.ConfigError):
            config.load_data_profile_config(path)

    def test_invalid_anomaly_method_raises(self):
        path = self._write("defaults: {anomaly_method: made_up_method}\n")
        with self.assertRaises(config.ConfigError):
            config.load_data_profile_config(path)


class TestChangeEvidence(unittest.TestCase):
    def _write(self, text: str) -> Path:
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8")
        f.write(text)
        f.close()
        return Path(f.name)

    VALID = """
purpose: "Removing legacy_phone, replaced by phone_e164 after dual-write completed."
ticket: "JIRA-1234"
affected_objects:
  - table: users
    column: legacy_phone
    change_type: REMOVED
    before: {type: text, nullable: true}
    after: null
reason: "Deprecated in JIRA-1234, replaced by phone_e164 field two months ago."
dependency_scan: "Grepped all services for legacy_phone references, none found."
archive_evidence: "Full table snapshot archived at s3://backups/users-2026-08-01.parquet"
validation_evidence: "Row counts reconciled: 41213 rows before, 41213 rows after (column drop only)."
recovery_plan: "Restore from the archived snapshot above and re-add the column via a rollback migration."
attestation:
  owner: "dev@taleemabad.com"
  date: "2026-09-02"
  statement: "I attest the above is accurate."
"""

    def test_valid_evidence_loads(self):
        path = self._write(self.VALID)
        ev = config.load_change_evidence(path)
        self.assertEqual(ev.affected_objects[0]["table"], "users")

    def test_missing_required_key_raises(self):
        path = self._write("purpose: too short\n")
        with self.assertRaises(config.ConfigError):
            config.load_change_evidence(path)

    def test_placeholder_short_narrative_raises(self):
        broken = self.VALID.replace(
            'reason: "Deprecated in JIRA-1234, replaced by phone_e164 field two months ago."',
            'reason: "n/a"',
        )
        path = self._write(broken)
        with self.assertRaises(config.ConfigError):
            config.load_change_evidence(path)

    def test_invalid_change_type_raises(self):
        broken = self.VALID.replace("change_type: REMOVED", "change_type: DELETED_FOREVER")
        path = self._write(broken)
        with self.assertRaises(config.ConfigError):
            config.load_change_evidence(path)

    def test_missing_attestation_field_raises(self):
        broken = self.VALID.replace('date: "2026-09-02"\n', "")
        path = self._write(broken)
        with self.assertRaises(config.ConfigError):
            config.load_change_evidence(path)


if __name__ == "__main__":
    unittest.main()
