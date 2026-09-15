# Configuration Files — `.data-quality-gate/`

All four files live at the **target repo's root** (not inside this skill's own folder) — the same
convention `data-standards`' `.data-standards.json` and its dotfile state caches use, but with a
namespace of their own to avoid a repo owner confusing which gate reads which file.

| File | Purpose | Loader |
|---|---|---|
| `scope.json` | Cheap, stdlib-only include/exclude globs + migration source config. Read before any database starts. | `scripts/scope_detect.py` |
| `data-profile.yaml` | Global tolerance/anomaly-method defaults + the advisory-rollout deadline. Required — missing this file is a `ConfigError`. | `scripts/config.py:load_data_profile_config` |
| `table-contracts/<table>.yaml` | Per-table allowed columns, expected types, nullability, criticality, keys, sensitivity, and profile overrides. A table with no contract file runs in WARN-only mode until `data-profile.yaml`'s `advisory_rollout.enforce_after` date. | `scripts/config.py:load_all_table_contracts` |
| `change.yaml` | The developer-evidence exception record — see [evidence-exception.md](evidence-exception.md). Only read if present; its absence is normal, not an error. | `scripts/config.py:load_change_evidence` |

## Malformed configuration never silently skips

Every loader in `scripts/config.py` raises `ConfigError` on: missing required keys, wrong types,
values outside an allowed enum, an unresolvable identifier, or a YAML syntax error. `gate.py`
catches `ConfigError` at the top level and turns it into the `ERROR` verdict (exit code 2) — a
broken table contract must never be silently treated as "no contract, WARN-only," because that
would let a broken YAML file quietly disable enforcement for a table that thinks it's protected.

## `scope.json` fields

```json
{
  "include": ["glob", "patterns", "..."],
  "exclude": ["glob", "patterns", "..."],
  "migrations_path": "migrations",
  "migration_file_glob": "*.sql",
  "migration_command": null
}
```

`include` fully replaces the built-in defaults if present; `exclude` is additive to the built-in
exclude list (same asymmetry `detect_schema_changes.py`'s `.data-standards.json` uses, for
consistency across the pack — see that skill's `reference/detection-guidance.md`).

`migration_command`, if set, is run with `DATABASE_URL` in its environment instead of the plain
`.sql`-file runner — see [migrate.py](../scripts/migrate.py)'s module docstring for why V1 needs a
pluggable migration source (this pack has no real product migrations of its own).

## `table-contracts/<table>.yaml` fields

See [`.data-quality-gate/table-contracts/_example.yaml.disabled`](../../../../.data-quality-gate/table-contracts/_example.yaml.disabled)
at the repo root for a fully-annotated example — rename it (dropping `.disabled`) to activate a
real contract. Field groups: `columns` (allowed/expected_types/nullability/criticality),
`keys` (primary_key/unique/foreign_keys), `sensitivity` (classification + masking_required per
column), `profile` (comparison_key, tolerances, dim_columns, null_rate benchmarks, junk_patterns,
anomaly method/block-list), `allowed_tolerances` (e.g. `row_count_growth_only`).

A file named `_*.yaml` or ending `.disabled` is always skipped by the loader (a template, not an
active contract) — this is how the shipped example ships without being mistaken for a real
contract for a table named `_example`.
