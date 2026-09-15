"""End-to-end integration tests for profile_runner.py against REAL
throwaway Postgres databases — proves the actual SQL aggregates (row
count, duplicate keys, null rate, FK orphans, junk-value rate) work
against real inserted data, not just mocked cursors
(test_profile_runner.py covers the pure logic/mocked-cursor cases).

Skips (never fails) without a reachable Postgres — same convention as
test_gate_integration.py. Set DATA_QUALITY_GATE_TEST_DSN to run locally,
or let it run for real in the shipped GitHub Actions job.
"""

from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

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
    ".github/workflows/data-quality-gate.yml's CI job. "
    "Set DATA_QUALITY_GATE_TEST_DSN to run locally."
)


def _make_scratch_db(root_dsn: str) -> tuple[str, str]:
    import psycopg
    dbname = f"dqg_profile_test_{uuid.uuid4().hex[:12]}"
    conn = psycopg.connect(root_dsn, autocommit=True)
    try:
        conn.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        conn.close()
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
class TestProfileRunnerAgainstRealData(unittest.TestCase):
    def setUp(self):
        import psycopg
        self.dsn, self.dbname = _make_scratch_db(BASE_DSN_ROOT)
        self.conn = psycopg.connect(self.dsn, autocommit=True)
        self.conn.execute("""
            CREATE TABLE schools (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), name TEXT);
        """)
        self.conn.execute("""
            CREATE TABLE users (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                email TEXT,
                school_id UUID,
                status TEXT
            );
        """)

    def tearDown(self):
        self.conn.close()
        _drop_scratch_db(BASE_DSN_ROOT, self.dbname)

    def test_row_count_matches_real_inserted_rows(self):
        import profile_runner
        self.conn.execute("INSERT INTO users (email) SELECT 'user' || i || '@x.com' FROM generate_series(1, 50) i")
        count = profile_runner.row_count(self.conn, "users")
        self.assertEqual(count, 50)

    def test_duplicate_key_count_detects_real_duplicates(self):
        import profile_runner
        self.conn.execute("INSERT INTO users (email) VALUES ('a@x.com'), ('a@x.com'), ('b@x.com')")
        dup_count = profile_runner.duplicate_key_count(self.conn, "users", ["email"])
        self.assertEqual(dup_count, 2)  # both rows sharing the duplicated value

    def test_duplicate_key_count_zero_when_all_unique(self):
        import profile_runner
        self.conn.execute("INSERT INTO users (email) VALUES ('a@x.com'), ('b@x.com')")
        dup_count = profile_runner.duplicate_key_count(self.conn, "users", ["email"])
        self.assertEqual(dup_count, 0)

    def test_null_rate_pct_computed_from_real_nulls(self):
        import profile_runner
        self.conn.execute("INSERT INTO users (email) VALUES ('a@x.com'), (NULL), (NULL), ('d@x.com')")
        pct = profile_runner.null_rate_pct(self.conn, "users", "email")
        self.assertAlmostEqual(pct, 50.0)

    def test_distinct_values_reflects_real_data(self):
        import profile_runner
        self.conn.execute("INSERT INTO users (status) VALUES ('active'), ('active'), ('inactive'), (NULL)")
        vals = profile_runner.distinct_values(self.conn, "users", "status")
        self.assertEqual(vals, {"active", "inactive"})

    def test_fk_orphan_count_detects_real_dangling_reference(self):
        import profile_runner
        school_id = str(uuid.uuid4())  # never inserted into schools
        self.conn.execute("INSERT INTO users (school_id) VALUES (%s)", (school_id,))
        orphans = profile_runner.fk_orphan_count(self.conn, "users", ["school_id"], "schools", ["id"])
        self.assertEqual(orphans, 1)

    def test_fk_orphan_count_zero_when_reference_is_valid(self):
        import profile_runner
        cur = self.conn.cursor()
        cur.execute("INSERT INTO schools (name) VALUES ('Test School') RETURNING id")
        school_id = cur.fetchone()[0]
        self.conn.execute("INSERT INTO users (school_id) VALUES (%s)", (school_id,))
        orphans = profile_runner.fk_orphan_count(self.conn, "users", ["school_id"], "schools", ["id"])
        self.assertEqual(orphans, 0)

    def test_fk_orphan_count_composite_key_detects_orphan_that_per_column_check_would_miss(self):
        """Regression test for the composite-FK false-negative bug found
        during adversarial review: each value individually exists
        somewhere in the referenced table, but never together as one row
        — a per-column-independent check would report zero orphans here;
        the real composite anti-join must report exactly one."""
        import profile_runner
        self.conn.execute("""
            CREATE TABLE school_terms (
                school_id UUID NOT NULL, term_code TEXT NOT NULL,
                PRIMARY KEY (school_id, term_code)
            )
        """)
        self.conn.execute("""
            CREATE TABLE enrollments (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                school_id UUID, term_code TEXT
            )
        """)
        cur = self.conn.cursor()
        cur.execute("INSERT INTO schools (name) VALUES ('School A') RETURNING id")
        school_a = cur.fetchone()[0]
        cur.execute("INSERT INTO schools (name) VALUES ('School B') RETURNING id")
        school_b = cur.fetchone()[0]
        self.conn.execute("INSERT INTO school_terms (school_id, term_code) VALUES (%s, 'FALL2026')", (school_a,))
        self.conn.execute("INSERT INTO school_terms (school_id, term_code) VALUES (%s, 'SPRING2026')", (school_b,))
        # school_a exists in school_terms (paired with FALL2026), and
        # 'SPRING2026' exists in school_terms (paired with school_b) — but
        # (school_a, 'SPRING2026') together never appears as one row.
        self.conn.execute(
            "INSERT INTO enrollments (school_id, term_code) VALUES (%s, 'SPRING2026')", (school_a,),
        )
        orphans = profile_runner.fk_orphan_count(
            self.conn, "enrollments", ["school_id", "term_code"], "school_terms", ["school_id", "term_code"],
        )
        self.assertEqual(orphans, 1)

    def test_junk_match_count_detects_real_pattern_matches(self):
        import profile_runner
        self.conn.execute("INSERT INTO users (email) VALUES ('real@x.com'), ('test@x.com'), ('n/a'), ('unknown')")
        junk, total = profile_runner.junk_match_count(self.conn, "users", "email", ["^test@", "^n/?a$", "^unknown$"])
        self.assertEqual(total, 4)
        self.assertEqual(junk, 3)

    def test_quote_ident_rejects_sql_injection_attempt_end_to_end(self):
        import profile_runner
        with self.assertRaises(profile_runner.ProfileRunnerError):
            profile_runner.row_count(self.conn, "users; DROP TABLE users; --")
        # Prove the table survived (i.e. no injection occurred).
        count = profile_runner.row_count(self.conn, "users")
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
