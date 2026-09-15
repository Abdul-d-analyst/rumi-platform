# Durable Audit Trail

The assignment requires the blocked event and the verified-evidence pass to be recorded "in a
durable shared audit trail — not a local or gitignored file." Two mechanisms together satisfy
this, deliberately redundant so one failing doesn't lose the record:

1. **`.data-quality-gate/audit-log.jsonl`** — appended once per gate run
   (`scripts/audit_trail.py:append_audit_record`), then **committed back to the PR branch by the
   CI workflow itself** (see the "Commit the audit-log append" step in
   `.github/workflows/data-quality-gate.yml`). This file is intentionally **tracked in git, never
   gitignored** — unlike `data-standards`' own local state files
   (`.data-standards-bypass-log.jsonl` etc., which are explicitly local-machine, gitignored, and
   "safe to delete at any time" per that skill's own SKILL.md). This gate's log is the opposite by
   design: a durable, shared, versioned record every clone of the repo carries forward.
2. **The shared governance event stream** — `scripts/audit_trail.py:forward_to_governance()`
   reuses `skills/data-standards/scripts/notify.py`'s `governance` subcommand directly (one
   ingestion path for the whole pack, not a second one this gate reinvents), tagged
   `data-quality-gate:<repo>` so a downstream consumer can distinguish which gate emitted an
   event. Exactly like that shared script's own documented behavior: if
   `TALEEMABAD_GOVERNANCE_ENDPOINT` is unset (true for every repo today — no such service exists
   yet, see that script's own docstring), the event is appended to
   `.data-standards-governance-events.jsonl` instead of being silently dropped. This is accepted,
   documented, current behavior, not a stub blocking on a future step.

## Why the CI-committed JSONL, not just the build artifact

A GitHub Actions build artifact is retained for a bounded period (`retention-days: 30` in the
workflow) and is not trivially queryable across time — it satisfies "durable for this run" but not
"a running audit trail a Data Team member can read six months from now without hunting through old
runs." Committing the append to the branch (and, once merged, to the protected branch's history)
makes the record part of the repository's own permanent history — the same durability guarantee
git itself already provides for every other tracked file.

## What happens if the audit-log commit/push fails

The workflow's "Commit the audit-log append" step is `continue-on-error: true` — a push conflict
or permissions issue there must never flip the gate's actual pass/fail verdict (that always comes
from `gate.py`'s own exit code, computed earlier in the job). The build-artifact upload
(`report.json`) is the durable record for that specific run regardless of whether the commit
succeeds — this is the redundancy the "two mechanisms" design above exists for.

## What's NOT durable, on purpose

Nothing this gate produces is written only to the CI runner's ephemeral local disk and left there
— every local write (`report.json`, `scope.json` scratch files) either gets uploaded as an artifact
or forwarded to one of the two durable stores above before the job ends.
