#!/usr/bin/env python3
"""Live data-profile execution — the piece that actually queries both
throwaway Postgres databases and feeds real numbers into data_profile.py's
pure, unit-tested check functions.

Mirrors introspect.py's role split: introspect.py reads live SCHEMA state
(information_schema/pg_catalog), this module reads live DATA state (row
counts, null rates, distinct values, duplicate keys, FK orphans, junk-
pattern matches) — both via plain SQL aggregates only, never raw row
values, per reference/data-source-limitations.md's sanitization
requirement. data_profile.py itself stays pure (no DB access, fully
unit-testable without Postgres) — this module is its only live-data
caller.

Only tables with a table-contract's `profile` section are profiled at
all — a table with no contract stays in WARN-only rollout per
data-profile.yaml's advisory_rollout (see config.py), and this module
returns no findings for it rather than guessing what to check.

IQR / robust z-score anomaly detection needs a DISTRIBUTION (multiple
historical values), which a single base-vs-head comparison cannot
supply on its own — there is exactly one baseline number per metric per
run. Rather than fabricate a fake distribution from n=1, this module
only runs an anomaly check when the table contract explicitly supplies
`profile.anomaly.history: {<column>: [v1, v2, ...]}` (a manually curated
or externally-fed list of past values). Absent that, the anomaly check
for that column is reported as skipped with an explicit reason — never
silently omitted, never faked. PSI (distributional comparison) has the
same requirement, via `profile.anomaly.baseline_distribution` /
`proposed_distribution` category-count mappings.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import data_profile as dp  # noqa: E402
from config import TableContract, DataProfileConfig  # noqa: E402

try:
    import psycopg  # type: ignore
except ImportError:
    try:
        import psycopg2 as psycopg  # type: ignore
    except ImportError:
        psycopg = None


class ProfileRunnerError(Exception):
    """A profile query could not be run — callers must treat this as
    ERROR/fail-closed, exactly like introspect.py's IntrospectionError."""


def _connect(dsn: str):
    if psycopg is None:
        raise ProfileRunnerError("no Postgres driver available — install psycopg[binary] or psycopg2")
    try:
        return psycopg.connect(dsn)
    except Exception as e:  # noqa: BLE001
        raise ProfileRunnerError(f"could not connect for data profiling: {e}") from e


_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _quote_ident(name: str) -> str:
    """Every identifier this module ever interpolates into SQL comes from
    a table-contract YAML file, not user/PR-controlled input at query
    time — but table/column names are still validated as safe SQL
    identifiers before use, defense in depth against a malformed or
    malicious contract file rather than trusting the YAML blindly."""
    if not _IDENT_RE.match(name):
        raise ProfileRunnerError(f"refusing to use {name!r} as a SQL identifier — not a valid identifier shape")
    return f'"{name}"'


def _scalar(conn, sql: str, params: tuple = ()) -> object:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None


def row_count(conn, table: str) -> int:
    return int(_scalar(conn, f"SELECT COUNT(*) FROM {_quote_ident(table)}"))


def duplicate_key_count(conn, table: str, key_columns: list[str]) -> int:
    """Returns the number of ROWS that participate in a duplicated key
    value (not the number of distinct duplicated key GROUPS) — a naive
    `SELECT COUNT(*) FROM (... GROUP BY cols HAVING COUNT(*) > 1) dup`
    counts result rows from the grouped subquery, i.e. one row per
    duplicated GROUP, silently undercounting: 3 rows sharing one key plus
    2 rows sharing another key would report 2 (groups), not 5 (rows) —
    found during adversarial review, same bug class as introspect.py's
    earlier composite-FK cartesian-product bug (SQL that runs cleanly but
    answers the wrong question). SUM(cnt) over the per-group counts fixes
    this: it adds up every row actually inside a duplicated group."""
    cols = ", ".join(_quote_ident(c) for c in key_columns)
    sql = (
        f"SELECT COALESCE(SUM(cnt), 0) FROM ("
        f"  SELECT COUNT(*) AS cnt FROM {_quote_ident(table)}"
        f"  WHERE {' AND '.join(f'{_quote_ident(c)} IS NOT NULL' for c in key_columns)}"
        f"  GROUP BY {cols} HAVING COUNT(*) > 1"
        f") dup"
    )
    return int(_scalar(conn, sql))


def distinct_values(conn, table: str, column: str) -> set:
    with conn.cursor() as cur:
        cur.execute(f"SELECT DISTINCT {_quote_ident(column)} FROM {_quote_ident(table)} WHERE {_quote_ident(column)} IS NOT NULL")
        return {row[0] for row in cur.fetchall()}


def null_rate_pct(conn, table: str, column: str) -> float:
    total = row_count(conn, table)
    if total == 0:
        return 0.0
    nulls = _scalar(conn, f"SELECT COUNT(*) FROM {_quote_ident(table)} WHERE {_quote_ident(column)} IS NULL")
    return (nulls / total) * 100.0


def fk_orphan_count(conn, table: str, columns: list[str], ref_table: str, ref_columns: list[str]) -> int:
    """Counts rows whose FK value(s) have no matching row in the
    referenced table. For a COMPOSITE FK (len(columns) > 1) this checks
    the columns TOGETHER as one anti-join condition — checking each
    column independently (whether `col1`'s value exists ANYWHERE in
    `ref_col1`, separately from whether `col2`'s value exists anywhere in
    `ref_col2`) would silently miss a real orphan where the pair doesn't
    exist together even though each value individually exists somewhere
    in the referenced table. Found during adversarial review — same bug
    class introspect.py's own composite-FK query already had to fix once
    (constraint_column_usage's cartesian product); this recurred one
    layer up in profile-checking rather than schema-introspection."""
    assert len(columns) == len(ref_columns), "composite FK columns/ref_columns must be positionally paired"
    local_not_null = " AND ".join(f"t.{_quote_ident(c)} IS NOT NULL" for c in columns)
    match_pairs = " AND ".join(f"r.{_quote_ident(rc)} = t.{_quote_ident(c)}" for c, rc in zip(columns, ref_columns))
    sql = (
        f"SELECT COUNT(*) FROM {_quote_ident(table)} t "
        f"WHERE {local_not_null} "
        f"AND NOT EXISTS (SELECT 1 FROM {_quote_ident(ref_table)} r WHERE {match_pairs})"
    )
    return int(_scalar(conn, sql))


def junk_match_count(conn, table: str, column: str, patterns: list[str]) -> tuple[int, int]:
    """Returns (junk_count, total_count) from a SINGLE scan (COUNT(*)
    FILTER (WHERE ...) alongside the unfiltered COUNT(*)) rather than two
    separate full-table queries — a NULL column value fails every `~*`
    comparison (Postgres: NULL ~* pattern -> NULL -> excluded by FILTER),
    so a NULL row is correctly counted in total but never in junk,
    matching what check_junk_value_rate's percentage expects. Uses
    Postgres's own regex operator (~*, case-insensitive) so pattern
    semantics match what a human reading the contract YAML would expect
    from a plain regex — never fetches actual matching values, only a
    count, per the sanitization requirement (no raw data ever leaves the
    database)."""
    if not patterns:
        return 0, row_count(conn, table)
    or_clause = " OR ".join(f"{_quote_ident(column)} ~* %s" for _ in patterns)
    sql = (
        f"SELECT COUNT(*) FILTER (WHERE {or_clause}), COUNT(*) "
        f"FROM {_quote_ident(table)}"
    )
    with conn.cursor() as cur:
        cur.execute(sql, tuple(patterns))
        junk, total = cur.fetchone()
    return int(junk), int(total)


# --------------------------------------------------------------- orchestration

def profile_table(base_conn, head_conn, table: str, contract: TableContract,
                   global_cfg: DataProfileConfig) -> list[dict]:
    """Runs every profile check this contract declares for `table` and
    returns a flat list of finding dicts (data_profile.ProfileFinding.to_dict()
    shape, plus a `skipped` variant for anomaly checks with no history).
    Never raises for a data condition — only ProfileRunnerError for a
    genuine query/connection failure, which the caller must treat as
    ERROR/fail-closed."""
    profile = contract.profile or {}
    findings: list[dict] = []

    tolerance_pct = profile.get("row_count_tolerance_pct", global_cfg.row_count_tolerance_pct)
    growth_only = bool(contract.allowed_tolerances.get("row_count_growth_only", False))
    base_count = row_count(base_conn, table)
    head_count = row_count(head_conn, table)
    f = dp.check_row_count(table, base_count, head_count, tolerance_pct, growth_only)
    if f:
        findings.append(f.to_dict())

    for key_cols in profile.get("duplicate_business_keys", []):
        dup_count = duplicate_key_count(head_conn, table, key_cols)
        f = dp.check_duplicate_business_key(table, key_cols, dup_count)
        if f:
            findings.append(f.to_dict())

    for col in profile.get("dim_columns", []):
        base_vals = distinct_values(base_conn, table, col)
        head_vals = distinct_values(head_conn, table, col)
        f = dp.check_dim_stability(table, col, base_vals, head_vals)
        if f:
            findings.append(f.to_dict())

    for col, rule in (profile.get("null_rate") or {}).items():
        base_pct = null_rate_pct(base_conn, table, col)
        head_pct = null_rate_pct(head_conn, table, col)
        tol = rule.get("tolerance_pct", global_cfg.null_rate_tolerance_pct)
        block = bool(rule.get("block", global_cfg.anomaly_block))
        benchmark = rule.get("benchmark_pct", base_pct)
        f = dp.check_null_rate(table, col, benchmark, head_pct, tol, block)
        if f:
            findings.append(f.to_dict())

    for col, patterns in (profile.get("junk_patterns") or {}).items():
        junk_count, total_count = junk_match_count(head_conn, table, col, patterns)
        threshold = profile.get("junk_threshold_pct", 1.0)
        block = bool(profile.get("junk_block", False))
        f = dp.check_junk_value_rate(table, col, junk_count, total_count, threshold, block)
        if f:
            findings.append(f.to_dict())

    for fk in contract.foreign_keys:
        # Checked as ONE composite anti-join, not per-column — checking
        # each column independently would miss a real orphan where each
        # value individually exists somewhere in the referenced table but
        # never together as the same row (see fk_orphan_count's docstring).
        orphan_count = fk_orphan_count(head_conn, table, fk["columns"], fk["references_table"], fk["references_columns"])
        f = dp.check_fk_orphans(table, ",".join(fk["columns"]), orphan_count)
        if f:
            findings.append(f.to_dict())

    anomaly_cfg = profile.get("anomaly") or {}
    method = anomaly_cfg.get("method", global_cfg.anomaly_method)
    block_columns = set(anomaly_cfg.get("block_columns", []))
    history = anomaly_cfg.get("history", {})
    value_aggs = anomaly_cfg.get("value_agg", {})  # {column: "sum"|"avg"|"max"|"min"|"count"}
    _ALLOWED_AGGS = {"sum", "avg", "max", "min", "count"}
    for col in anomaly_cfg.get("columns", []):
        skip_reasons = []
        if col not in history or not history[col]:
            skip_reasons.append(
                f"no profile.anomaly.history supplied for {table}.{col} — a single "
                f"base-vs-head run has only one baseline number, not a distribution, "
                f"so {method} cannot run honestly without externally-supplied history"
            )
        agg = value_aggs.get(col)
        if agg not in _ALLOWED_AGGS:
            skip_reasons.append(
                f"no profile.anomaly.value_agg declared for {table}.{col} (must be one "
                f"of {sorted(_ALLOWED_AGGS)}) — an arbitrary 'pick a row' query (e.g. "
                f"ORDER BY the metric column itself) would silently choose the wrong "
                f"value for most metrics, so this requires an explicit aggregate choice "
                f"rather than a guess"
            )
        if skip_reasons:
            findings.append({
                "table": table, "column": col, "check": f"anomaly_{method}",
                "severity": "skipped", "detail": "; ".join(skip_reasons) + ". NOT run, NOT silently passed.",
            })
            continue

        head_val = _scalar(head_conn, f"SELECT {agg.upper()}({_quote_ident(col)}) FROM {_quote_ident(table)}")
        if head_val is None:
            continue
        f = dp.check_anomaly(table, col, history[col], float(head_val), method, col in block_columns)
        if f:
            findings.append(f.to_dict())

    return findings


def run_profile_gate(base_dsn: str, head_dsn: str, contracts: dict[str, TableContract],
                      global_cfg: DataProfileConfig) -> list[dict]:
    """Profiles every table that HAS a contract with a non-empty `profile`
    section. Tables with no contract, or a contract with an empty
    profile block, are intentionally not profiled — WARN-only rollout,
    per data-profile.yaml's advisory_rollout (see SKILL.md)."""
    profiled_tables = {t: c for t, c in contracts.items() if c.profile}
    if not profiled_tables:
        return []

    base_conn = _connect(base_dsn)
    head_conn = _connect(head_dsn)
    try:
        all_findings: list[dict] = []
        for table, contract in profiled_tables.items():
            try:
                all_findings.extend(profile_table(base_conn, head_conn, table, contract, global_cfg))
            except ProfileRunnerError:
                raise
            except Exception as e:  # noqa: BLE001 — a query failure on one table must fail closed, not be swallowed
                raise ProfileRunnerError(f"profiling {table} failed: {e}") from e
        return all_findings
    finally:
        base_conn.close()
        head_conn.close()
