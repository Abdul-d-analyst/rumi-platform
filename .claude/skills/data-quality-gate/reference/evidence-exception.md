# Developer-Evidence Exception — No Data Team Approval Required

## What this is

A destructive or narrowing schema/data change is blocked by default. The **developer or
remediation owner** (not the Data Team) can unblock it themselves by committing a
version-controlled `.data-quality-gate/change.yaml` on the same PR branch — see
[change.yaml.example](../../../../.data-quality-gate/change.yaml.example) at the repo root for the
exact fields required. **No Data Team or `CODEOWNERS` approval is required for this V1 exception
workflow** — this is an explicit, deliberate accepted risk, not an oversight. It exists because V1's
job is to catch genuine mistakes fast without making every legitimate, reviewed removal wait on a
second team's calendar.

## What is deterministically verified vs. presence-checked only

`scripts/config.py`'s `load_change_evidence()` and `scripts/evidence.py`'s `verify_evidence()`
split the record into two categories, and the gate is explicit about which is which in its own
report (never blur the two):

**Deterministically verified** (an actual structural equality check against the real diff):
- `affected_objects[].table` / `.column` / `.change_type` must exactly match a real blocking
  finding `schema_diff.py` produced for this PR — not a hypothetical or a typo'd name.
- `affected_objects[].before` / `.after` must exactly match the real snapshot fields
  `schema_diff.py` detected — a claim that a narrowed column "was already a text field" when the
  real baseline snapshot says `varchar(50)` does **not** pass, even though both are "text-like."
- Every blocking finding for this PR must be covered by some `affected_objects[]` entry —
  partial coverage leaves the uncovered findings still blocking (`evidence.py`'s
  `uncovered_findings`).

**Presence-checked only, NOT independently proven by automation** (the assignment's own
requirement — stated plainly in every report that carries a verified-evidence result):
- `purpose`, `reason`, `dependency_scan`, `archive_evidence`, `validation_evidence`,
  `recovery_plan` — each must be a non-empty string past a minimum length floor
  (`config._MIN_NARRATIVE_LEN`, currently 15 characters) so a one-word placeholder
  (`"n/a"`, `"fix later"`) is rejected. This proves *something was written*, never that the
  recovery plan is *actually good*, that the retention evidence is *actually sufficient*, or that
  the dependency scan was *actually thorough*. A human reviewer (even informally, in the PR itself)
  is still the check on narrative quality — the gate cannot and does not claim to replace that.
- `attestation.owner` / `.date` / `.statement` — presence-checked the same way. This is a
  self-attestation, not a verified identity claim.

## Why this is safe as a self-service exception

The *only* thing that changes the PR's merge status is the structural match against the real
diff — a developer cannot talk their way past the gate with better prose, because the narrative
fields never factor into `matched` at all (see `evidence.py`'s `EvidenceVerdict.matched`, which is
computed purely from `mismatches` and `uncovered_findings`). Writing a longer recovery plan does
not change whether `before`/`after` line up with reality.

## What still happens even on a verified-evidence pass

1. The GitHub check conclusion is a passing status, but the report result string is literally
   `PASS WITH VERIFIED EVIDENCE`, never a bare `PASS` — so anyone reading history later can tell
   the difference between "nothing was ever blocking" and "something was blocking and a developer
   attested a reason."
2. A **second** Slack message goes to the Data Team (`notify.py --event evidence_verified`) with
   the rationale, evidence links, verification result, remaining risk, recovery plan, PR link, and
   the audit-trail reference — visibility without requiring pre-approval.
3. The event is appended to the durable audit trail (`scripts/audit_trail.py`) — see
   [audit-trail.md](audit-trail.md) — so this is discoverable later even though no one had to
   click "approve."

## Protecting the exception mechanism itself

The assignment is explicit that a PR must not be able to both loosen a threshold in
`.data-quality-gate/**` *and* pass its own loosened check in the same diff. This repo's
`.github/CODEOWNERS` now names an owner for `.data-quality-gate/**` — but a CODEOWNERS entry by
itself does **nothing** until a repo admin turns on "Require review from Code Owners" in branch
protection for the target branch. **Until that step is done, treat `.data-quality-gate/**`
protection as an accepted, documented risk, not a live control** — say so plainly rather than
claiming enforcement that isn't actually configured. See the workflow file's own manual-setup
notes for the exact admin action required.
