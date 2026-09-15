---
name: data-quality-gate
description: The V1 GitHub Data Quality Gate — a live-Postgres-backed semantic schema and data-profile check that runs as a distinct required CI status check (`data-quality-gate`), DISTINCT from the sibling `data-standards` skill's static textual DDL checks. Applies base and proposed migrations to throwaway Postgres databases, introspects information_schema/pg_catalog (never parses migration SQL text), classifies every table/column change as ADDED/REMOVED/RENAMED/WIDENED/NARROWED/MODIFIED/UNCHANGED, and compares deterministic data-quality metrics (row counts, duplicates, null rates, FK orphans, IQR/robust-z/PSI anomalies) against an approved baseline. Blocks on destructive schema changes or critical data-quality failures unless a developer submits verified `.data-quality-gate/change.yaml` evidence that structurally matches the real diff — no Data Team approval required for that exception. Use when asked to "add live-database schema validation," "check actual constraints in Postgres," "profile data quality in CI," "block destructive migrations with real DB introspection," or when data-standards' own "cannot query a live database" limitation is the actual gap to fill. Never confuse with `data-standards` — that skill's gate is `data-standards`/static; this one's gate is `data-quality-gate`/live-Postgres. A second PreToolUse hook (Edit/Write) checks the pure-logic, no-database SLICE of this gate (new-table/new-column contract rules, column-level ADDED/REMOVED/NARROWED/WIDENED) live in a session, against the target repo's git default branch as the baseline — data-profile checks (row counts, null rates, FK orphans, anomalies) still require a real database and stay CI-only.
---

# V1 Data Quality Gate

A live-database-backed sibling to [`data-standards`](../data-standards/SKILL.md), built in its own
namespace on purpose: `.data-quality-gate/` (config), `.github/workflows/data-quality-gate.yml`
(CI), GitHub check name `data-quality-gate`. It does not replace, rename, weaken, or duplicate
`data-standards` — it fills the exact gap that skill's own SKILL.md names explicitly: *"Cannot:
query a live database to see if a constraint actually exists... see whether a named human steward
is genuinely tracked anywhere... verify audit-log immutability."* This gate can, because it
actually connects to Postgres.

## The two gates side by side

| | `data-standards` | `data-quality-gate` (this skill) |
|---|---|---|
| Input | Migration SQL text / a schema dump | Two throwaway Postgres databases, already migrated |
| Method | Regex/structural text parsing (`validate_schema.py`) | Live `information_schema`/`pg_catalog` introspection |
| Catalog | Rumi's 27 hand-authored standards (`standards.yaml`) | A generic schema-diff + data-profile contract system |
| Config | `.data-standards.json`, `.data-standards-*` state files | `.data-quality-gate/{scope.json,data-profile.yaml,table-contracts/,change.yaml}` |
| GitHub check | `data-standards / validate-schema` | `data-quality-gate` |
| Slack channel var | `TALEEMABAD_DATA_STANDARDS_SLACK_CHANNEL` | `TALEEMABAD_DATA_QUALITY_GATE_SLACK_CHANNEL` |
| Can see real row data? | No, never | Yes — row counts, null rates, duplicates, via aggregate-only SQL |

Both gates can run in the same repo without conflict — they watch different files, use different
env vars, and neither imports the other's validator. `data-standards`' own SKILL.md may route a
request here when a live-database question comes up; this skill never edits that one's files.

## When this fires

- A request explicitly about live/actual database state: "does this constraint actually exist in
  Postgres," "check real row counts before merging," "profile this table's data quality,"
  "block a migration that would truncate real data."
- Setting up or explaining `.github/workflows/data-quality-gate.yml`.
- Writing or reviewing a `.data-quality-gate/table-contracts/<table>.yaml` or
  `.data-quality-gate/change.yaml` (the evidence-exception record).
- A question about why a PR was blocked by the `data-quality-gate` check (distinct from a
  `data-standards` block — check which GitHub check name actually failed before assuming which
  skill's docs apply).

## Architecture (read in this order)

1. [reference/report-format.md](reference/report-format.md) — the six-value verdict vocabulary
   (`PASS`/`PASS WITH VERIFIED EVIDENCE`/`FAIL`/`WARN`/`NOT APPLICABLE`/`NOT VERIFIED`/`ERROR`),
   report JSON shape, baseline-SHA discipline.
2. [reference/config-schema.md](reference/config-schema.md) — the four config files under
   `.data-quality-gate/` at a target repo's root, and why malformed config is always a hard
   failure, never a silent skip.
3. [reference/evidence-exception.md](reference/evidence-exception.md) — exactly what's
   deterministically verified vs. presence-checked-only in the developer-evidence exception, and
   why no Data Team approval gates it in V1.
4. [reference/audit-trail.md](reference/audit-trail.md) — the durable, git-tracked audit log plus
   the shared governance event forward (reuses `data-standards`' own governance sender, not a
   second one).
5. [reference/data-source-limitations.md](reference/data-source-limitations.md) — what the
   synthetic test fixtures do and don't prove, and when a profile check must report
   `NOT VERIFIED` instead of a false `PASS`.

## Script map

| Script | Job |
|---|---|
| [scripts/scope_detect.py](scripts/scope_detect.py) | Cheap, stdlib-only pre-check — decides if the expensive pipeline runs at all. Reads `.data-quality-gate/scope.json`. |
| [scripts/migrate.py](scripts/migrate.py) | Applies `.sql` files (or a configurable command) to a throwaway Postgres DB. |
| [scripts/introspect.py](scripts/introspect.py) | Connects to a live Postgres DB and builds a normalized schema snapshot via `information_schema`/`pg_catalog`. Raises `IntrospectionError` — never returns an empty snapshot on failure. |
| [scripts/schema_diff.py](scripts/schema_diff.py) | Pure-logic diff of two snapshots — classifies every table/column change, no DB or file I/O of its own. |
| [scripts/contract_check.py](scripts/contract_check.py) | New-table/new-column gate against a table contract — the PII-shaped-column-without-classification block lives here. |
| [scripts/data_profile.py](scripts/data_profile.py) | Deterministic data-quality checks — row count, duplicates, DIM stability, null rate, FK orphans, junk rate, IQR/robust-z/PSI anomaly detection. |
| [scripts/config.py](scripts/config.py) | Strict YAML config loading for table contracts, `data-profile.yaml`, and `change.yaml` — raises `ConfigError` on anything malformed. |
| [scripts/evidence.py](scripts/evidence.py) | Deterministic structural match of a `change.yaml` against real blocking findings. |
| [scripts/gate.py](scripts/gate.py) | The orchestrator — the one entry point CI calls. |
| [scripts/notify.py](scripts/notify.py) | Slack notification, own event vocabulary (`block`, `evidence_verified`), reuses `slack_send.py` directly. |
| [scripts/audit_trail.py](scripts/audit_trail.py) | Durable audit-log append + forward to `data-standards`' shared governance event sender. |
| [scripts/sql_snapshot.py](scripts/sql_snapshot.py) | Parses raw `CREATE TABLE` SQL text into the SAME snapshot shape `introspect.py` produces from a live connection — what makes the live-edit hook below possible without a database. See "The live-edit gate hook" and the module's own docstring for exactly what it does and does not replicate. |

## The live-edit gate hook

`hooks/live-schema-shape-gate.py` is a `PreToolUse` hook (declared in [hooks.json](hooks.json)) on
`Edit`/`Write`, mirroring `data-standards`' own live-edit hook — but scoped to only the SLICE of
this gate that's pure logic over a parsed schema snapshot, with no live Postgres connection
required. **Data-profile checks (row counts, null-rate drift, FK orphan counts, duplicate-key
rates, IQR/robust-z/PSI anomaly detection) genuinely cannot run here** — those are real aggregate
facts about a real, populated database, and stay CI-only, where `gate.py` runs them against two
real, migrated throwaway Postgres databases. Pretending to check them live would be worse than not
checking them at all.

What DOES run live, via [scripts/sql_snapshot.py](scripts/sql_snapshot.py) parsing the file's
prospective content (same computation as the sibling hook: `tool_input.content` for a `Write`, or
the on-disk text with `old_string`→`new_string` applied for an `Edit`) into a snapshot dict:

- `contract_check.py`'s new-table (missing `PRIMARY KEY`) and new-column (PII-shaped name with no
  sensitivity classification in a real `.data-quality-gate/table-contracts/<table>.yaml`) rules.
- `schema_diff.py`'s **column-level** classification only (ADDED/REMOVED/NARROWED/WIDENED/MODIFIED)
  between a "before" and "after" snapshot of the same table.

**Deliberately NOT run live: constraint-removal checks** (a dropped `PRIMARY KEY`/`UNIQUE`/`CHECK`/
`FOREIGN KEY`/index). A real Postgres server auto-generates a deterministic name for an unnamed
constraint (e.g. `users_email_key`) — `sql_snapshot.py` does not replicate that naming convention,
so a constraint-removal finding here could be a false positive from a naming mismatch that isn't a
real removal. Getting a live block WRONG erodes trust faster than not checking something; this
stays CI-only, where the real database has the real constraint names.

**"Before" for the column diff, per explicit instruction:** the target repo's default branch
(`origin/HEAD` if resolvable, else local `main`) — mirroring what this gate's own CI treats as the
approved baseline (the PR's base-branch tip), just resolved from git instead of a live database.
`git show <ref>:<path>` reads that file's last-committed content; a brand-new file has no
meaningful "before," so the diff step is skipped for it and only the new-table/new-column rules run.

Same bypass mechanism as `data-standards`' own live hook, reused rather than duplicated —
`TALEEMABAD_DATA_STANDARDS_BYPASS`, validated and recorded by that skill's `bypass_audit.py`, same
`.data-standards-bypass-log.jsonl` — this pack's one-audit-trail principle applies across both
gates. The Slack notification for a block (bypassed or not) still goes through **this gate's own**
[scripts/notify.py](scripts/notify.py) (its own channel, its own `block` event template) — only the
bypass mechanism is shared, not the notification path; see that script's own module docstring for
why it's deliberately separate from `data-standards`' notifier.

See [evals/evals.json](evals/evals.json) cases C10-C15 for the verified behavior (SQL-to-snapshot
type translation and composite-PK detection; the hook blocking a new table with no PK, blocking an
unclassified PII column against a REAL contract shipped in this repo, correctly NOT blocking on an
advisory-only finding, diffing against the real git default branch, and the shared bypass mechanism).

## Tests

`tests/test_*.py` (pure-logic, no Postgres needed — `schema_diff`, `contract_check`,
`data_profile`, `config`, `evidence`, `scope_detect`, `notify`) run anywhere with:

```bash
cd skills/data-quality-gate && python3 -m unittest discover tests -v
```

`tests/test_gate_integration.py` needs a real reachable Postgres — it **skips itself** (not a
failure) when none is available, and runs for real inside
`.github/workflows/data-quality-gate.yml`'s own service-container job. To run it locally:

```bash
export DATA_QUALITY_GATE_TEST_DSN=postgresql://postgres:postgres@localhost:5432/postgres
cd skills/data-quality-gate && python3 -m unittest tests.test_gate_integration -v
```

`evals/evals.json` follows this pack's established trigger-rate convention (see
`skills/data-standards/evals/evals.json` for the methodology this mirrors) plus deterministic
correctness cases against `evals/fixtures/migrations_*`.

## Rollout

`.data-quality-gate/data-profile.yaml`'s `advisory_rollout` block documents the WARN-only period
for tables with no contract file yet, and the date enforcement begins. The GitHub check itself
must **not** be marked as a required branch-protection status check until that rollout has shown
an acceptable false-positive rate — the workflow file's own header says this explicitly, and
[reference/evidence-exception.md](reference/evidence-exception.md) documents the accepted risk of
`.data-quality-gate/**` protection depending on a repo admin actually enabling
"Require review from Code Owners."

## Related skills

- **data-standards** — the sibling static-check skill this gate is explicitly built alongside, not
  instead of. See the comparison table above before assuming a finding belongs to one or the other.
- **implementation-plans** — a plan with a schema change (§9) that touches live-data concerns
  (real row counts, real null rates) should note this gate as the mechanism that will actually
  verify those claims in CI, distinct from `data-standards`' static audit.
