# Report Format — V1 Data Quality Gate

## Verdicts

Every check in this gate reports one of exactly six values — never ambiguous prose:

| Verdict | Meaning |
|---|---|
| `PASS` | No blocking findings. WARN-level findings may still be present and are always shown. |
| `PASS WITH VERIFIED EVIDENCE` | A blocking finding existed, but a `.data-quality-gate/change.yaml` on the branch structurally matched the real diff and covers every blocking finding. The GitHub check conclusion for this result is a passing status — see reference/evidence-exception.md. |
| `FAIL` | A confirmed blocking finding with no covering evidence (or evidence that didn't match). |
| `WARN` | An advisory-only finding (profile anomaly on a table with no contract yet, index removal, etc.) — never blocks by itself. |
| `NOT APPLICABLE` | Scope detection found nothing schema-relevant in this PR — returned before any database was even started. |
| `NOT VERIFIED` | A required live-data profile could not run safely in this CI environment (e.g. no sanitized/staging snapshot available) — reported explicitly rather than silently treated as PASS. A controlled post-deployment profile is required before claiming the change is fully verified. |
| `ERROR` | The gate itself could not run to completion (snapshot/profile failure, timeout, malformed config) — always fails closed, never silently passes. |

## Report structure (gate.py's JSON output)

```json
{
  "baseline_sha": "the approved base-branch tip SHA at workflow startup",
  "result": "PASS | PASS WITH VERIFIED EVIDENCE | FAIL | WARN | NOT APPLICABLE | NOT VERIFIED | ERROR",
  "blocking_findings": [ /* schema_diff.py + contract_check.py blocking findings */ ],
  "evidence_verdict": { "matched": bool, "covered": [...], "uncovered": [...], "mismatches": [...] } ,
  "diff": { "tables": [ /* full ADDED/REMOVED/RENAMED/WIDENED/NARROWED/MODIFIED/UNCHANGED classification per table/column */ ] },
  "contract_findings": [ /* non-blocking advisory findings from contract_check.py */ ]
}
```

Every blocking finding cites: `table`, `column` (or constraint kind), `classification`, `reason`,
and the exact `before`/`after` definition it was computed from — never a bare narrative sentence
with no traceable structural check behind it (per enforcement-policy parity with the sibling
data-standards skill: a model's narrative judgment is never sufficient on its own).

## Baseline SHA discipline

The `baseline_sha` field is the approved PR base-branch tip AT WORKFLOW STARTUP
(`github.event.pull_request.base.sha`, or `github.event.merge_group.base_sha` for a merge-queue
run) — never `git merge-base`, which is used only to scope which files changed for the cheap
scope-detection pass. Every report, PR comment, and Slack message carries this exact SHA so a
reviewer can always answer "what was this compared against."

## Navigation

The report supports both directions the assignment requires:

- **table → failed standards → recommended changes**: iterate `diff.tables[].columns[]` /
  `.constraint_findings[]` for a given table name.
- **standard/check → affected tables → detailed issues**: group `blocking_findings` by
  `classification`/`kind` across all tables.
