#!/usr/bin/env python3
"""Parses raw CREATE TABLE SQL text into the SAME normalized snapshot shape
introspect.py produces from a live Postgres connection —
{"schema": ..., "tables": {name: {"columns": {...}, "primary_key": [...],
...}}} — so schema_diff.py and contract_check.py can run against SQL TEXT,
never touching a database, exactly the way they already run against a real
introspected snapshot in gate.py's normal CI flow. Nothing downstream
needs to know or care which source produced its input.

Why this exists: data-quality-gate's whole identity is live-Postgres
introspection (real row counts, real constraint state) — most of that
genuinely cannot run in a session with no database. But schema_diff.py's
column-shape classification (ADDED/REMOVED/NARROWED/WIDENED/MODIFIED) and
contract_check.py's new-table/new-column rules are BOTH pure functions
over a snapshot dict, with no Postgres connection code of their own (see
each module's own docstring) — this module is the missing piece that lets
that specific, narrower slice run live, the same way data-standards'
validate_schema.py already runs live against SQL text.

Deliberately narrower than introspect.py's real output:
  - Constraints (PRIMARY KEY, UNIQUE, CHECK, FOREIGN KEY) are read for
    PRIMARY KEY only. UNIQUE/CHECK/FOREIGN KEY constraint tracking is left
    out ON PURPOSE — a real Postgres server auto-generates a deterministic
    name for an unnamed constraint (e.g. users_email_key), and guessing
    that naming convention wrong would make diff_constraints() produce a
    false "constraint removed" finding on every edit that happens to
    reorder or restate an unnamed UNIQUE/CHECK. Getting a live hook's
    finding WRONG is worse than not checking that slice at all — this
    stays a CI-only check (the real database has the real name), an
    explicit, documented gap, not a silent approximation.
  - Indexes are not tracked at all (CREATE INDEX is a separate statement
    from CREATE TABLE; also not needed by any check this module feeds).

Type-name translation: schema_diff.py's narrowing logic compares Postgres's
own internal type names (udt_name from information_schema — "int4",
"varchar", "bpchar", "numeric", not the SQL keywords a human types), so a
SQL type like INTEGER or VARCHAR(50) has to be translated to the same
vocabulary a real introspection would have produced. Every mapping below
was checked against a real Postgres 16's information_schema.columns.udt_name
column (see reference/sql-snapshot-limitations.md) for the types this
pack's own fixtures and table contracts actually use. A type this module
doesn't recognize is kept as its literal (lowercased) SQL spelling rather
than guessed at — schema_diff.py still correctly reports a MODIFIED
finding on a type change either side of an unrecognized type (any
before != after difference falls through to its own "cross-family type
change" branch), it just won't get the specific NARROWED/WIDENED
sub-classification for that one type family.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
# SKILL_DIR.parent is the "skills" directory itself — data-standards is
# always a SIBLING skill folder there, regardless of how deep that skills/
# directory sits in a given repo (agent-skills-taleemabad: skills/ at the
# repo root; rumi-platform: .claude/skills/, one level deeper). Layout-
# agnostic on purpose so the exact same file works unmodified in both.
DATA_STANDARDS_SCRIPTS = SKILL_DIR.parent / "data-standards" / "scripts"


def _data_standards_parser():
    """Lazy import — data-standards' own validate_schema.py already has a
    working CREATE-TABLE-block finder and a top-level-comma column
    splitter (find_create_table_blocks / split_columns); reusing them
    means this module never re-solves "how do you split a column list
    without tripping on a comma inside a nested type or a CHECK(...)
    expression" a second time. Lazy so a caller who only wants the
    (rare) case of no data-standards checkout at all still gets an
    import error at the point of actual use, not at module load."""
    if str(DATA_STANDARDS_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(DATA_STANDARDS_SCRIPTS))
    import validate_schema  # noqa: PLC0415
    return validate_schema


# SQL type keyword (lowercased, whitespace-normalized) -> Postgres udt_name,
# the same vocabulary introspect.py's live snapshot uses. Ordered so a
# longer/more-specific phrase is matched before a shorter prefix of it
# (e.g. "double precision" before a bare "double" would ever be tried).
_TYPE_MAP = {
    "uuid": "uuid",
    "text": "text",
    "boolean": "bool", "bool": "bool",
    "smallint": "int2", "int2": "int2",
    "integer": "int4", "int": "int4", "int4": "int4",
    "bigint": "int8", "int8": "int8",
    "real": "float4", "float4": "float4",
    "double precision": "float8", "float8": "float8",
    "date": "date",
    "timestamp with time zone": "timestamptz", "timestamptz": "timestamptz",
    "timestamp without time zone": "timestamp", "timestamp": "timestamp",
    "time with time zone": "timetz", "timetz": "timetz",
    "time without time zone": "time", "time": "time",
    "jsonb": "jsonb",
    "json": "json",
    "serial": "int4",       # SERIAL is sugar for int4 + a sequence default
    "bigserial": "int8",
    "smallserial": "int2",
}

# These carry a length/precision/scale argument, e.g. VARCHAR(255),
# NUMERIC(10,2) — matched separately since the base keyword needs the
# parenthesized part split off first.
_VARYING_TYPES = {"varchar": "varchar", "character varying": "varchar",
                   "char": "bpchar", "character": "bpchar", "bpchar": "bpchar"}
_NUMERIC_TYPES = {"numeric": "numeric", "decimal": "numeric"}

_TYPE_ARG_RE = re.compile(r"^([a-zA-Z_ ]+?)\s*\(\s*([0-9]+)\s*(?:,\s*([0-9]+)\s*)?\)\s*(.*)$")


def _parse_type(type_text: str) -> dict:
    """Returns {"type": <udt_name>, "length": int|None, "precision": int|None,
    "scale": int|None} from a raw type-syntax fragment like "VARCHAR(255)",
    "NUMERIC(10, 2)", "TIMESTAMPTZ", "integer"."""
    raw = type_text.strip()
    m = _TYPE_ARG_RE.match(raw)
    if m:
        base, arg1, arg2, _rest = m.groups()
        base_norm = re.sub(r"\s+", " ", base.strip().lower())
        if base_norm in _VARYING_TYPES:
            return {"type": _VARYING_TYPES[base_norm], "length": int(arg1),
                    "precision": None, "scale": None}
        if base_norm in _NUMERIC_TYPES:
            return {"type": _NUMERIC_TYPES[base_norm], "length": None,
                    "precision": int(arg1), "scale": int(arg2) if arg2 else 0}
        # An arg'd type this module doesn't special-case (e.g. bit(n)) —
        # keep the base keyword as a literal, arg dropped rather than
        # guessed into the wrong field.
        return {"type": base_norm, "length": None, "precision": None, "scale": None}

    norm = re.sub(r"\s+", " ", raw.lower())
    udt = _TYPE_MAP.get(norm, norm)
    return {"type": udt, "length": None, "precision": None, "scale": None}


# Just the leading "<column_name>" — the type that follows is resolved
# separately by _MULTIWORD_TYPE_RE / a single-word fallback below, rather
# than guessed at generically. A non-greedy "capture words until we hit
# something that looks like a stop point" pattern was tried first and
# rejected: it has no principled way to know a multi-word type like
# "double precision" or "timestamp with time zone" should keep going
# past the first word rather than stop there, so it silently truncated
# to just "double" — caught by test_multiword_type_double_precision.
_COLUMN_NAME_RE = re.compile(r'^"?([\w]+)"?\s+')

_CONSTRAINT_LINE_RE = re.compile(r"^(PRIMARY\s+KEY|CONSTRAINT|UNIQUE|CHECK|FOREIGN\s+KEY)\b", re.IGNORECASE)

# Explicit multi-word type phrases, longest/most-specific first so e.g.
# "timestamp with time zone" is tried before a bare "timestamp" would
# ever get the chance to match a prefix of it.
_MULTIWORD_TYPES = [
    "timestamp with time zone", "timestamp without time zone",
    "time with time zone", "time without time zone",
    "double precision", "character varying",
]
_MULTIWORD_TYPE_RE = re.compile(
    "^(" + "|".join(re.escape(t) for t in _MULTIWORD_TYPES) + r")\b",
    re.IGNORECASE,
)
# A single-word type, optionally followed by an argument list —
# VARCHAR(255), NUMERIC(10,2), TIMESTAMPTZ, integer.
_SINGLEWORD_TYPE_RE = re.compile(r'^([A-Za-z_][A-Za-z_0-9]*)\s*(\([^)]*\))?')


def _match_type_text(rest: str) -> tuple[str, str] | None:
    """Returns (type_text, remainder_after_type) from the start of `rest`
    (everything after the column name), or None if nothing recognizable
    as a type leads it."""
    m = _MULTIWORD_TYPE_RE.match(rest)
    if m:
        return m.group(1), rest[m.end():]
    m = _SINGLEWORD_TYPE_RE.match(rest)
    if m:
        return m.group(0), rest[m.end():]
    return None


def _parse_column(col_text: str) -> tuple[str, dict] | None:
    """Returns (column_name, definition) for one column-definition
    fragment, or None if this fragment is actually a table-level
    constraint clause (PRIMARY KEY (...), CONSTRAINT ... , etc.) rather
    than a column — split_columns() returns both shapes interleaved in
    source order, same as a real CREATE TABLE body."""
    text = col_text.strip()
    if _CONSTRAINT_LINE_RE.match(text):
        return None

    name_m = _COLUMN_NAME_RE.match(text)
    if not name_m:
        return None
    name = name_m.group(1)
    after_name = text[name_m.end():]

    type_match = _match_type_text(after_name)
    if type_match is None:
        return None
    type_text, rest = type_match
    rest = rest.strip()

    parsed_type = _parse_type(type_text)
    rest_low = rest.lower()
    nullable = "not null" not in rest_low
    if "primary key" in rest_low:
        nullable = False  # a PK column is implicitly NOT NULL

    default = None
    default_m = re.search(r"\bdefault\s+(.+?)(?:\s+not\s+null\b|\s+null\b|$)", rest, re.IGNORECASE)
    if default_m:
        default = default_m.group(1).strip().rstrip(",")

    definition = {
        "type": parsed_type["type"],
        "sql_type": type_text,
        "length": parsed_type["length"],
        "precision": parsed_type["precision"],
        "scale": parsed_type["scale"],
        "datetime_precision": None,
        "timezone": parsed_type["type"] in ("timestamptz", "timetz"),
        "nullable": nullable,
        "default": default,
        "generated": False,
        "generation_expression": None,
        "collation": None,
    }
    return name, definition


def _parse_table_level_primary_key(body: str) -> list[str] | None:
    """A table-level `PRIMARY KEY (col1, col2)` clause, as opposed to an
    inline `col UUID PRIMARY KEY`. Returns the column list, or None if no
    table-level PK clause is present in this body."""
    m = re.search(r"PRIMARY\s+KEY\s*\(\s*([^)]+)\s*\)", body, re.IGNORECASE)
    if not m:
        return None
    return [c.strip().strip('"') for c in m.group(1).split(",") if c.strip()]


def parse_sql_to_snapshot(text: str, schema: str = "public") -> dict:
    """The main entry point — same return shape as introspect.py's
    snapshot_schema(), built from SQL text instead of a live connection.
    A table with a table-level composite PRIMARY KEY as well as one or
    more inline `PRIMARY KEY` column markers is not expected in valid SQL
    (Postgres itself would reject two primary keys on one table), so the
    table-level clause — the only shape that can express a COMPOSITE key —
    takes precedence when both are somehow present in the parsed text."""
    validate_schema = _data_standards_parser()
    tables: dict[str, dict] = {}

    for table_name, body in validate_schema.find_create_table_blocks(text):
        columns: dict[str, dict] = {}
        primary_key: list[str] = []

        for fragment in validate_schema.split_columns(body):
            parsed = _parse_column(fragment)
            if parsed is None:
                continue
            name, definition = parsed
            columns[name] = definition
            fragment_low = fragment.lower()
            if "primary key" in fragment_low and "(" not in fragment_low.split("primary key", 1)[0]:
                # Only an INLINE marker on this exact column counts here —
                # a table-level "PRIMARY KEY (...)" clause is its own
                # fragment (caught by _CONSTRAINT_LINE_RE, returns None
                # above) and handled separately below.
                if name not in primary_key:
                    primary_key.append(name)

        table_level_pk = _parse_table_level_primary_key(body)
        if table_level_pk is not None:
            primary_key = table_level_pk

        tables[table_name] = {
            "columns": columns,
            "primary_key": primary_key,
            "unique_constraints": [],
            "foreign_keys": [],
            "check_constraints": [],
            "indexes": [],
        }

    return {"schema": schema, "tables": tables}
