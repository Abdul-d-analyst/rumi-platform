#!/usr/bin/env python3
"""Generic pluggable migration runner for the V1 Data Quality Gate.

This repo (agent-skills-taleemabad) is a skills/tooling pack, not the
Rumi product repo — it has no real product migrations of its own to run.
V1 is therefore built against a GENERIC migration source so any product
repo can point it at its own migrations:

  - MIGRATION_COMMAND (or .data-quality-gate/scope.json's
    "migration_command"): an arbitrary shell command run with the target
    database's DSN in DATABASE_URL (e.g. "alembic upgrade head", "npx
    prisma migrate deploy", "flyway migrate"). Used when a repo's
    migrations aren't plain .sql files.
  - Otherwise: every file matching migration_file_glob (default "*.sql")
    under migrations_path (default "migrations"), applied via psql in
    filename-sorted order — the same "no ORM assumed" fallback most repos
    can satisfy trivially.

This repo ships synthetic fixture migrations under evals/fixtures/ purely
to prove the gate's own logic end-to-end (same spirit as data-standards'
own evals/fixtures/*.sql) — they are test doubles, not real Taleemabad
schema.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


class MigrationError(Exception):
    """A migration could not be applied — callers must fail closed."""


def apply_sql_migrations(dsn: str, migrations_dir: Path, file_glob: str = "*.sql", timeout_seconds: int = 300) -> list[str]:
    if not migrations_dir.exists():
        raise MigrationError(f"migrations directory not found: {migrations_dir}")

    files = sorted(migrations_dir.glob(file_glob))
    if not files:
        raise MigrationError(f"no files matching {file_glob!r} under {migrations_dir}")

    applied = []
    for f in files:
        try:
            proc = subprocess.run(
                ["psql", dsn, "-v", "ON_ERROR_STOP=1", "-f", str(f)],
                capture_output=True, text=True, timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as e:
            raise MigrationError(f"applying {f} exceeded the {timeout_seconds}s time budget") from e
        except FileNotFoundError as e:
            raise MigrationError("psql not found on PATH — required to apply .sql migrations") from e

        if proc.returncode != 0:
            raise MigrationError(f"applying {f} failed (exit {proc.returncode}): {proc.stderr.strip()}")
        applied.append(str(f))
    return applied


def apply_via_command(dsn: str, command: str, timeout_seconds: int = 300) -> list[str]:
    env = dict(os.environ)
    env["DATABASE_URL"] = dsn
    try:
        proc = subprocess.run(command, shell=True, capture_output=True, text=True,
                                timeout=timeout_seconds, env=env)
    except subprocess.TimeoutExpired as e:
        raise MigrationError(f"migration command exceeded the {timeout_seconds}s time budget: {command!r}") from e

    if proc.returncode != 0:
        raise MigrationError(f"migration command failed (exit {proc.returncode}): {command!r}\n{proc.stderr.strip()}")
    return [command]


def apply_migrations(dsn: str, *, migrations_path: str = "migrations", file_glob: str = "*.sql",
                      migration_command: str | None = None, timeout_seconds: int = 300) -> list[str]:
    if migration_command:
        return apply_via_command(dsn, migration_command, timeout_seconds)
    return apply_sql_migrations(dsn, Path(migrations_path), file_glob, timeout_seconds)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--migrations-path", default="migrations")
    ap.add_argument("--file-glob", default="*.sql")
    ap.add_argument("--migration-command")
    ap.add_argument("--timeout-seconds", type=int, default=300)
    args = ap.parse_args()

    try:
        applied = apply_migrations(
            args.dsn, migrations_path=args.migrations_path, file_glob=args.file_glob,
            migration_command=args.migration_command, timeout_seconds=args.timeout_seconds,
        )
    except MigrationError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(f"applied {len(applied)} migration step(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
