#!/usr/bin/env python3
"""Postgres schema introspection for the V1 Data Quality Gate.

Builds a normalized schema snapshot by querying information_schema/
pg_catalog against a REAL Postgres database that the migrations have
already been applied to — never by parsing migration SQL text. This is
the capability skills/data-standards explicitly documents it cannot do
(SKILL.md: "Cannot: query a live database to see if a constraint actually
exists") — this module is exactly that missing piece, in its own
namespace, not a modification of data-standards' static validator.

Requires `psycopg` (v3) or `psycopg2` — whichever is importable. Neither
is vendored elsewhere in this repo (confirmed: no existing Postgres client
anywhere in the pack), so scripts/requirements.txt declares psycopg[binary].

Snapshot shape (see reference/snapshot-schema.md for the full field list):
{
  "tables": {
    "<table>": {
      "columns": {"<col>": {type, length, precision, scale, timezone,
                              nullable, default, generated, collation}},
      "primary_key": [...],
      "unique_constraints": [[...], ...],
      "foreign_keys": [{columns, references_table, references_columns, name}],
      "check_constraints": [{name, definition}],
      "indexes": [{name, columns, unique}],
    }
  }
}
"""

from __future__ import annotations

import json
import sys

try:
    import psycopg  # type: ignore
    _DRIVER = "psycopg"
except ImportError:
    try:
        import psycopg2 as psycopg  # type: ignore
        _DRIVER = "psycopg2"
    except ImportError:
        psycopg = None
        _DRIVER = None


class IntrospectionError(Exception):
    """A snapshot could not be produced — callers must treat this as ERROR
    (fail closed), never as an empty/absent schema, per the assignment's
    'if a snapshot cannot be generated, return ERROR and fail closed'."""


def _connect(dsn: str):
    if psycopg is None:
        raise IntrospectionError(
            "no Postgres driver available — install psycopg[binary] or psycopg2 "
            "(see skills/data-quality-gate/scripts/requirements.txt)"
        )
    try:
        return psycopg.connect(dsn)
    except Exception as e:  # noqa: BLE001 - any connection failure is fail-closed
        raise IntrospectionError(f"could not connect to {_redact_dsn(dsn)}: {e}") from e


def _redact_dsn(dsn: str) -> str:
    # Never let a connection-failure message leak a password into a report/log.
    if "@" in dsn and "://" in dsn:
        scheme, rest = dsn.split("://", 1)
        creds_and_host = rest.split("@", 1)
        if len(creds_and_host) == 2:
            return f"{scheme}://[redacted]@{creds_and_host[1]}"
    return "[dsn-redacted]"


_COLUMNS_SQL = """
select
    c.table_name, c.column_name, c.data_type, c.udt_name,
    c.character_maximum_length, c.numeric_precision, c.numeric_scale,
    c.datetime_precision, c.is_nullable, c.column_default,
    c.is_generated, c.generation_expression, c.collation_name
from information_schema.columns c
where c.table_schema = %s
order by c.table_name, c.ordinal_position
"""

_PK_SQL = """
select tc.table_name, kcu.column_name, kcu.ordinal_position
from information_schema.table_constraints tc
join information_schema.key_column_usage kcu
    on tc.constraint_name = kcu.constraint_name and tc.table_schema = kcu.table_schema
where tc.table_schema = %s and tc.constraint_type = 'PRIMARY KEY'
order by tc.table_name, kcu.ordinal_position
"""

_UNIQUE_SQL = """
select tc.table_name, tc.constraint_name, kcu.column_name, kcu.ordinal_position
from information_schema.table_constraints tc
join information_schema.key_column_usage kcu
    on tc.constraint_name = kcu.constraint_name and tc.table_schema = kcu.table_schema
where tc.table_schema = %s and tc.constraint_type = 'UNIQUE'
order by tc.table_name, tc.constraint_name, kcu.ordinal_position
"""

_FK_SQL = """
-- Deliberately NOT information_schema.constraint_column_usage: that view
-- has no ordinal position that aligns with the referencing side, so
-- joining it against key_column_usage for a COMPOSITE FK produces an NxM
-- cartesian product (e.g. a 2-column FK yields 4 rows, not 2), silently
-- corrupting the column<->references_column pairing. pg_constraint's
-- conkey/confkey arrays are positionally aligned by definition — unnest
-- WITH ORDINALITY over both together is the only reliable way to recover
-- correct pairs for a composite FK.
select
    conrel.relname as table_name, con.conname as constraint_name,
    att.attname as column_name, ord.ordinality as ordinal_position,
    confrel.relname as references_table, fatt.attname as references_column
from pg_constraint con
join pg_class conrel on conrel.oid = con.conrelid
join pg_class confrel on confrel.oid = con.confrelid
join pg_namespace nsp on nsp.oid = conrel.relnamespace
join unnest(con.conkey, con.confkey) with ordinality as ord(local_attnum, foreign_attnum, ordinality) on true
join pg_attribute att on att.attrelid = con.conrelid and att.attnum = ord.local_attnum
join pg_attribute fatt on fatt.attrelid = con.confrelid and fatt.attnum = ord.foreign_attnum
where nsp.nspname = %s and con.contype = 'f'
order by conrel.relname, con.conname, ord.ordinality
"""

_CHECK_SQL = """
-- Deliberately NOT information_schema.check_constraints filtered by a
-- constraint-name suffix: that view is SQL-standard-required to also
-- synthesize an entry for every NOT NULL column (as "CHECK (col IS NOT
-- NULL)"), and the previous approach guessed at this by matching a
-- "_not_null" name suffix — a real, meaningful CHECK constraint a
-- developer happens to name that way (legal but unusual) would have been
-- silently dropped from the snapshot. pg_constraint.contype = 'c' is the
-- actual catalog-level distinction between a real CHECK and anything else
-- (NOT NULL constraints are either invisible to pg_constraint entirely on
-- pre-PG17, or carry contype = 'n' on PG17+ — either way 'c' excludes
-- them correctly with no name-guessing).
select
    rel.relname as table_name, con.conname as constraint_name,
    pg_get_constraintdef(con.oid) as definition
from pg_constraint con
join pg_class rel on rel.oid = con.conrelid
join pg_namespace nsp on nsp.oid = rel.relnamespace
where nsp.nspname = %s and con.contype = 'c'
order by rel.relname, con.conname
"""

_INDEX_SQL = """
-- indkey entries of 0 mean "this position is an expression, not a plain
-- column" (CREATE INDEX ... (lower(email))) — attnum 0 doesn't exist in
-- pg_attribute, so joining a = any(ix.indkey) silently drops that
-- position instead of erroring, producing a short/misaligned column list
-- for any expression index. Filtering ix.indexprs is null excludes
-- expression indexes from this snapshot entirely (reported as "not
-- introspected" rather than silently wrong) — safer than reporting an
-- incorrect column list with no error signal, given a plain column-list
-- comparison is the only thing schema_diff.py currently knows how to
-- compare anyway.
select
    t.relname as table_name, i.relname as index_name, ix.indisunique,
    array_to_string(array_agg(a.attname order by array_position(ix.indkey, a.attnum)), ',') as columns
from pg_index ix
join pg_class t on t.oid = ix.indrelid
join pg_class i on i.oid = ix.indexrelid
join pg_namespace n on n.oid = t.relnamespace
join pg_attribute a on a.attrelid = t.oid and a.attnum = any(ix.indkey)
where n.nspname = %s and ix.indexprs is null and ix.indisvalid
group by t.relname, i.relname, ix.indisunique
order by t.relname, i.relname
"""


def snapshot_schema(dsn: str, schema: str = "public") -> dict:
    """Connects to a live Postgres database and produces a normalized
    schema snapshot. Raises IntrospectionError on any failure — callers
    must fail closed, never fall back to an empty snapshot."""
    conn = _connect(dsn)
    try:
        cur = conn.cursor()
        tables: dict[str, dict] = {}

        cur.execute(_COLUMNS_SQL, (schema,))
        for row in cur.fetchall():
            (table_name, column_name, data_type, udt_name, char_max_len,
             num_precision, num_scale, dt_precision, is_nullable, column_default,
             is_generated, generation_expression, collation_name) = row
            t = tables.setdefault(table_name, _empty_table())
            timezone = data_type in ("timestamp with time zone", "time with time zone")
            t["columns"][column_name] = {
                "type": udt_name,
                "sql_type": data_type,
                "length": char_max_len,
                "precision": num_precision,
                "scale": num_scale,
                "datetime_precision": dt_precision,
                "timezone": timezone,
                "nullable": is_nullable == "YES",
                "default": column_default,
                "generated": is_generated == "ALWAYS",
                "generation_expression": generation_expression,
                "collation": collation_name,
            }

        cur.execute(_PK_SQL, (schema,))
        for table_name, column_name, _pos in cur.fetchall():
            tables.setdefault(table_name, _empty_table())["primary_key"].append(column_name)

        cur.execute(_UNIQUE_SQL, (schema,))
        unique_by_constraint: dict[tuple[str, str], list[str]] = {}
        for table_name, constraint_name, column_name, _pos in cur.fetchall():
            key = (table_name, constraint_name)
            unique_by_constraint.setdefault(key, []).append(column_name)
        for (table_name, _cname), cols in unique_by_constraint.items():
            tables.setdefault(table_name, _empty_table())["unique_constraints"].append(cols)

        cur.execute(_FK_SQL, (schema,))
        fk_by_constraint: dict[tuple[str, str], dict] = {}
        for table_name, constraint_name, column_name, _pos, ref_table, ref_column in cur.fetchall():
            key = (table_name, constraint_name)
            fk = fk_by_constraint.setdefault(key, {
                "name": constraint_name, "columns": [], "references_table": ref_table,
                "references_columns": [],
            })
            fk["columns"].append(column_name)
            fk["references_columns"].append(ref_column)
        for (table_name, _cname), fk in fk_by_constraint.items():
            tables.setdefault(table_name, _empty_table())["foreign_keys"].append(fk)

        cur.execute(_CHECK_SQL, (schema,))
        for table_name, constraint_name, definition in cur.fetchall():
            # _CHECK_SQL already filters to pg_constraint.contype = 'c' — a
            # real, catalog-level distinction, not a name-suffix guess — so
            # every row here is a genuine user-defined CHECK constraint,
            # never a synthesized NOT NULL entry.
            tables.setdefault(table_name, _empty_table())["check_constraints"].append({
                "name": constraint_name, "definition": definition,
            })

        cur.execute(_INDEX_SQL, (schema,))
        for table_name, index_name, is_unique, columns_csv in cur.fetchall():
            tables.setdefault(table_name, _empty_table())["indexes"].append({
                "name": index_name, "columns": columns_csv.split(","), "unique": bool(is_unique),
            })

        return {"schema": schema, "tables": tables}
    except IntrospectionError:
        raise
    except Exception as e:  # noqa: BLE001 - any query failure is fail-closed
        raise IntrospectionError(f"introspection query failed: {e}") from e
    finally:
        conn.close()


def _empty_table() -> dict:
    return {
        "columns": {}, "primary_key": [], "unique_constraints": [],
        "foreign_keys": [], "check_constraints": [], "indexes": [],
    }


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", required=True, help="Postgres connection string, e.g. postgresql://user:pass@host:5432/dbname")
    ap.add_argument("--schema", default="public")
    ap.add_argument("-o", "--output", help="write JSON here instead of stdout")
    args = ap.parse_args()

    try:
        snap = snapshot_schema(args.dsn, args.schema)
    except IntrospectionError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 2

    out = json.dumps(snap, indent=2, default=str)
    if args.output:
        from pathlib import Path
        Path(args.output).write_text(out, encoding="utf-8")
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
