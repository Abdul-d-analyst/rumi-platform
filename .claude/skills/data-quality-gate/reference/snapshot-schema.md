# Schema Snapshot Shape

`scripts/introspect.py:snapshot_schema()` returns this JSON-serializable shape. `schema_diff.py`
consumes exactly this shape and nothing else — it never re-queries Postgres, so any two snapshots
(from two different databases, or from a cached prior run) can be diffed offline.

```json
{
  "schema": "public",
  "tables": {
    "<table_name>": {
      "columns": {
        "<column_name>": {
          "type": "the udt_name (e.g. varchar, int4, uuid, timestamptz)",
          "sql_type": "information_schema.columns.data_type (human-readable form)",
          "length": "character_maximum_length or null",
          "precision": "numeric_precision or null",
          "scale": "numeric_scale or null",
          "datetime_precision": "datetime_precision or null",
          "timezone": "true if sql_type contains 'with time zone'",
          "nullable": true,
          "default": "column_default expression or null",
          "generated": "true if is_generated == ALWAYS",
          "generation_expression": "the GENERATED ALWAYS AS (...) expression or null",
          "collation": "collation_name or null"
        }
      },
      "primary_key": ["column", "..."],
      "unique_constraints": [["column", "..."], "..."],
      "foreign_keys": [
        {"name": "constraint_name", "columns": ["..."], "references_table": "...", "references_columns": ["..."]}
      ],
      "check_constraints": [
        {"name": "constraint_name", "definition": "the pg_get_constraintdef() text — real user-defined CHECK constraints only, never a synthesized NOT NULL entry"}
      ],
      "indexes": [
        {"name": "index_name", "columns": ["..."], "unique": true}
      ]
    }
  }
}
```

## What is deliberately excluded, and why

- **Expression indexes** (`CREATE INDEX ... (lower(email))`) are excluded from `indexes` entirely
  (`ix.indexprs is null` filter) rather than reported with a wrong/short column list — see the
  `_INDEX_SQL` query's own comment in `introspect.py` for the `attnum = 0` pitfall this avoids.
  Consequence: dropping or changing an expression index is currently invisible to
  `schema_diff.py` — a known V1 gap, not a silent lie.
- **Invalid indexes** (`ix.indisvalid = false`, e.g. left over from a failed `CREATE INDEX
  CONCURRENTLY`) are excluded — they aren't live constraints from the database's perspective.
- **Synthesized NOT NULL "check constraints"** are never in `check_constraints` — nullability is
  already fully captured per-column via `columns.<col>.nullable`; surfacing it a second time as a
  fake CHECK would double-count the same fact under two different finding types.
- **Row data** never appears anywhere in a snapshot — this is a schema/shape-only structure. Data
  values only ever enter the gate as aggregate numbers, computed separately in
  `scripts/data_profile.py` — see [data-source-limitations.md](data-source-limitations.md).

## Why `pg_constraint` instead of `information_schema` for foreign keys and check constraints

`information_schema.constraint_column_usage` has no ordinal position that aligns with the
referencing side of a foreign key — for a composite (multi-column) FK this produces a cartesian
product of (local column × referenced column) pairs, not the correct positional pairs. `pg_catalog.
pg_constraint`'s `conkey`/`confkey` arrays are positionally aligned by definition, so
`unnest(conkey, confkey) with ordinality` is the only reliable way to recover the right pairing —
see the `_FK_SQL` query's own comment for the full explanation. The same catalog also gives a
clean `contype = 'c'` filter for real CHECK constraints, avoiding the alternative of guessing which
`information_schema.check_constraints` rows are user-defined vs. a synthesized NOT NULL entry by
matching a name suffix (fragile — a real constraint could coincidentally be named that way too).
