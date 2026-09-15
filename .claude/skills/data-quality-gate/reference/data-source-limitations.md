# Data Source Limitations — What This Gate Actually Proves

## Synthetic fixtures prove the validator, not production correctness

`evals/fixtures/migrations_*` are synthetic test doubles (a `schools`/`users` toy schema) used
purely to prove `gate.py`'s own logic works end-to-end against a real Postgres server — they are
**not** real Rumi schema, and a green result on them proves nothing about any actual
production table. This mirrors `skills/data-standards/evals/fixtures/*.sql`'s own disclaimer, just
extended to a live-database context instead of static text.

## The data-profile gate needs REAL data to mean anything

The schema-diff half of this gate (structural ADDED/REMOVED/NARROWED/etc.) is fully meaningful
against an empty freshly-migrated database — DDL shape doesn't depend on row contents. The
**data-profile** half (row counts, null rates, duplicate keys, DIM stability, anomaly detection)
is fundamentally about *data*, and an empty throwaway database has none. Concretely:

- In CI, profile an **approved sanitized/staging snapshot** where one is available and can be
  loaded into the throwaway databases safely.
- If no such snapshot exists yet for a given repo (true for this pack's own CI, which has no
  product data at all), the profile checks that need real row data report `NOT VERIFIED` — never
  silently treated as `PASS` (see [report-format.md](report-format.md)'s verdict table).
- A `NOT VERIFIED` result means: **a controlled post-deployment profile is required** before
  anyone claims the change is fully verified on the data side, even though the schema-shape side of
  the same PR passed cleanly.

## What must never appear in a report, Slack message, or log

Per the assignment's explicit requirement: no raw production rows, no real PII values, no
credentials, no complete unredacted schema dump for a sensitive/unclassified object. Concretely,
this gate:

- Never SELECTs actual row *values* for anything other than an aggregate (`COUNT`, distinct-value
  *count*, null *rate*) — the profile checks in `scripts/data_profile.py` all operate on numbers a
  caller already computed via SQL aggregates, never on raw row payloads passed through this
  module.
- Sanitizes every string field passed to Slack or the governance event
  (`scripts/notify.py:sanitize`/`sanitize_deep`) against local-path and credential-shaped patterns
  before it's ever rendered — the exact same denylist pattern `data-standards`' own `notify.py`
  uses, applied independently here (not imported, since the two scripts intentionally don't share
  a module — see `notify.py`'s own docstring for why).
- Never logs a DSN with its password intact — `introspect.py:_redact_dsn` strips credentials from
  any connection-failure message before it reaches stderr/a report.
- A full report's "before"/"after" column definitions describe **shape** (type, length,
  nullability) — never a value — so even the most detailed report never becomes a schema dump of
  actual sensitive column contents.

## Anomaly detection: deterministic, never an LLM or opaque score

Every anomaly check in `scripts/data_profile.py` (IQR, robust z-score, PSI) is a documented,
reproducible formula over numbers — never a model's holistic judgment. This is a hard requirement,
not a style preference: an opaque "this looks off" verdict cannot be the actual gate on a merge
decision, per the assignment's explicit instruction.
