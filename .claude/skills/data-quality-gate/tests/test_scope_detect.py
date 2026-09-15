"""Tests for scope_detect.py — the cheap, stdlib-only scope gate. No
Postgres required; this must run fast enough that an irrelevant PR never
needs a database at all."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "scope_detect.py"
sys.path.insert(0, str(SCRIPT.parent))

import scope_detect  # noqa: E402


class TestIsRelevant(unittest.TestCase):
    def test_sql_file_is_relevant(self):
        self.assertTrue(scope_detect.is_relevant("migrations/001_init.sql", scope_detect.DEFAULT_CONFIG))

    def test_readme_is_not_relevant(self):
        self.assertFalse(scope_detect.is_relevant("README.md", scope_detect.DEFAULT_CONFIG))

    def test_excluded_pattern_wins_over_include(self):
        cfg = {"include": ["**/*.sql"], "exclude": ["**/node_modules/**"]}
        self.assertFalse(scope_detect.is_relevant("node_modules/pkg/seed.sql", cfg))

    def test_data_quality_gate_config_dir_itself_is_relevant(self):
        self.assertTrue(scope_detect.is_relevant(".data-quality-gate/table-contracts/users.yaml", scope_detect.DEFAULT_CONFIG))

    def test_windows_style_path_separators_are_normalized(self):
        self.assertTrue(scope_detect.is_relevant("migrations\\001_init.sql", scope_detect.DEFAULT_CONFIG))


class TestLoadConfig(unittest.TestCase):
    def test_missing_config_falls_back_to_default(self):
        cfg = scope_detect.load_config("/nonexistent/scope.json")
        self.assertEqual(cfg, scope_detect.DEFAULT_CONFIG)

    def test_malformed_config_exits_2(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{not valid json")
            path = f.name
        with self.assertRaises(SystemExit) as ctx:
            scope_detect.load_config(path)
        self.assertEqual(ctx.exception.code, 2)

    def test_custom_exclude_is_additive_to_defaults(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"exclude": ["custom_dir/**"]}, f)
            path = f.name
        cfg = scope_detect.load_config(path)
        self.assertIn("custom_dir/**", cfg["exclude"])
        self.assertIn(".git/**", cfg["exclude"])  # default still present


class TestCliEndToEnd(unittest.TestCase):
    def test_files_mode_reports_applicable_true(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--files", "migrations/001.sql", "README.md"],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0)
        result = json.loads(proc.stdout)
        self.assertTrue(result["applicable"])
        self.assertEqual(result["relevant_files"], ["migrations/001.sql"])

    def test_files_mode_reports_not_applicable_when_nothing_relevant(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--files", "README.md", "docs/guide.md"],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0)
        result = json.loads(proc.stdout)
        self.assertFalse(result["applicable"])

    def test_neither_mode_given_exits_2(self):
        proc = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
