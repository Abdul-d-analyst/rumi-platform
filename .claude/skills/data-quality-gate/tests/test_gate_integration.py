"""End-to-end integration tests against REAL throwaway Postgres databases —
these are the tests that actually prove introspect.py/gate.py work against
a live database, not just against synthetic dict snapshots (test_schema_diff.py
covers the pure logic; this file covers the assignment's "For the actual
schema and data-profile comparison... apply the migrations to separate
throwaway PostgreSQL databases and introspect information_schema/pg_catalog"
requirement for real).

These tests are SKIPPED, not failed, when no Postgres server is reachable
at DATA_QUALITY_GATE_TEST_DSN (or the default localhost:5432) and no
psycopg driver is installed — this is expected on a plain dev machine
(confirmed: this repo's own dev environment has neither). They run for
real in .github/workflows/data-quality-gate.yml's own CI job, which spins
up the two Postgres service containers this gate is designed around.

Set DATA_QUALITY_GATE_TEST_DSN to point at a scratch Postgres server to
run these locally, e.g.:
    export DATA_QUALITY_GATE_TEST_DSN=postgresql://postgres:postgres@localhost:5432/postgres
    python3 -m unittest tests.test_gate_integration -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

FIXTURES = Path(__file__).resolve().parent.parent / "evals" / "fixtures"
BASE_DSN_ROOT = os.environ.get("DATA_QUALITY_GATE_TEST_DSN", "postgresql://postgres:postgres@localhost:5432/postgres")


def _driver_available() -> bool:
    try:
        import psycopg  # noqa: F401
        return True
    except ImportError:
        try:
            import psycopg2  # noqa: F401
            return True
        except ImportError:
            return False


def _postgres_reachable() -> bool:
    if not _driver_available():
        return False
    try:
        import psycopg
        conn = psycopg.connect(BASE_DSN_ROOT, connect_timeout=3)
        conn.close()
        return True
    except Exception:
        try:
            import psycopg2
            conn = psycopg2.connect(BASE_DSN_ROOT, connect_timeout=3)
            conn.close()
            return True
        except Exception:
            return False


_SKIP_REASON = (
    "no reachable Postgres server / driver for integration testing — "
    "expected on a plain dev machine; runs for real in "
    ".github/workflows/data-quality-gate.yml's CI job with its two "
    "Postgres service containers. Set DATA_QUALITY_GATE_TEST_DSN to run locally."
)


def _make_scratch_db(root_dsn: str) -> tuple[str, str]:
    """Creates a uniquely-named scratch database on the same server as
    root_dsn and returns (dsn, dbname). Caller is responsible for dropping
    it via _drop_scratch_db."""
    import psycopg
    dbname = f"dqg_test_{uuid.uuid4().hex[:12]}"
    conn = psycopg.connect(root_dsn, autocommit=True)
    try:
        conn.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        conn.close()
    # rebuild the DSN with the new dbname
    prefix = root_dsn.rsplit("/", 1)[0]
    return f"{prefix}/{dbname}", dbname


def _drop_scratch_db(root_dsn: str, dbname: str) -> None:
    import psycopg
    conn = psycopg.connect(root_dsn, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
    except Exception:
        pass
    finally:
        conn.close()


@unittest.skipUnless(_postgres_reachable(), _SKIP_REASON)
class TestIntrospectionAgainstRealPostgres(unittest.TestCase):
    def setUp(self):
        self.dsn, self.dbname = _make_scratch_db(BASE_DSN_ROOT)

    def tearDown(self):
        _drop_scratch_db(BASE_DSN_ROOT, self.dbname)

    def _apply(self, fixture_dir: str):
        from migrate import apply_sql_migrations
        apply_sql_migrations(self.dsn, FIXTURES / fixture_dir)

    def test_snapshot_reflects_real_table_and_columns(self):
        self._apply("migrations_base")
        from introspect import snapshot_schema
        snap = snapshot_schema(self.dsn)
        self.assertIn("users", snap["tables"])
        self.assertIn("email", snap["tables"]["users"]["columns"])
        self.assertEqual(snap["tables"]["users"]["primary_key"], ["id"])

    def test_unreachable_dsn_raises_introspection_error_not_silent_empty(self):
        from introspect import snapshot_schema, IntrospectionError
        with self.assertRaises(IntrospectionError):
            snapshot_schema("postgresql://nouser:nopass@localhost:1/doesnotexist")


@unittest.skipUnless(_postgres_reachable(), _SKIP_REASON)
class TestGateEndToEnd(unittest.TestCase):
    def setUp(self):
        self.base_dsn, self.base_db = _make_scratch_db(BASE_DSN_ROOT)
        self.head_dsn, self.head_db = _make_scratch_db(BASE_DSN_ROOT)

    def tearDown(self):
        _drop_scratch_db(BASE_DSN_ROOT, self.base_db)
        _drop_scratch_db(BASE_DSN_ROOT, self.head_db)

    def _apply(self, dsn: str, fixture_dir: str):
        from migrate import apply_sql_migrations
        apply_sql_migrations(dsn, FIXTURES / fixture_dir)

    def _run_gate(self, extra_args=None):
        import gate
        argv = [
            "gate.py", "--base-dsn", self.base_dsn, "--head-dsn", self.head_dsn,
            "--baseline-sha", "deadbeef",
        ] + (extra_args or [])
        old_argv = sys.argv
        sys.argv = argv
        try:
            return gate.main()
        finally:
            sys.argv = old_argv

    def test_compliant_addition_passes(self):
        self._apply(self.base_dsn, "migrations_base")
        self._apply(self.head_dsn, "migrations_head_pass")
        exit_code = self._run_gate()
        self.assertEqual(exit_code, 0)

    def test_widening_change_passes(self):
        self._apply(self.base_dsn, "migrations_base")
        self._apply(self.head_dsn, "migrations_head_widen_ok")
        exit_code = self._run_gate()
        self.assertEqual(exit_code, 0)

    def test_dropped_column_blocks(self):
        self._apply(self.base_dsn, "migrations_base")
        self._apply(self.head_dsn, "migrations_head_block_drop")
        exit_code = self._run_gate()
        self.assertEqual(exit_code, 1)

    def test_narrowed_column_blocks(self):
        self._apply(self.base_dsn, "migrations_base")
        self._apply(self.head_dsn, "migrations_head_block_narrow")
        exit_code = self._run_gate()
        self.assertEqual(exit_code, 1)

    def test_unreachable_head_db_fails_closed_with_error_exit_code(self):
        self._apply(self.base_dsn, "migrations_base")
        import gate
        old_argv = sys.argv
        sys.argv = ["gate.py", "--base-dsn", self.base_dsn,
                     "--head-dsn", "postgresql://nouser:nopass@localhost:1/doesnotexist",
                     "--baseline-sha", "deadbeef"]
        try:
            exit_code = gate.main()
        finally:
            sys.argv = old_argv
        self.assertEqual(exit_code, 2)


if __name__ == "__main__":
    unittest.main()
