#!/usr/bin/env python3
"""Developer-evidence exception verifier — no Data Team approval required.

Per the assignment: "The gate must deterministically verify that declared
object names and before/after definitions match the actual schema/profile
diff. It must state clearly that recovery-plan quality, retention-evidence
sufficiency, and the narrative are presence-checked only, not independently
proven by automation."

config.load_change_evidence() already presence-checks the narrative fields
(non-empty, minimum length, not a placeholder). This module does the part
that IS deterministically provable: does affected_objects[].before/after
match a blocking finding schema_diff.py actually produced, exactly?

A change.yaml that doesn't structurally match the real diff is REJECTED —
it does not partially unblock, and it does not fall back to a Data Team
review requirement (there is none in V1; see reference/evidence-exception.md
for why this is an accepted risk, not an oversight).
"""

from __future__ import annotations

from dataclasses import dataclass

from config import ChangeEvidence


@dataclass
class EvidenceVerdict:
    matched: bool
    covered_findings: list[dict]   # blocking findings this evidence record covers
    uncovered_findings: list[dict]  # blocking findings NOT covered — these still block
    mismatches: list[str]          # declared objects that didn't match reality


def _finding_key(finding: dict) -> tuple:
    return (finding.get("table"), finding.get("column") or finding.get("kind"), finding.get("classification") or finding.get("kind"))


def _object_key(obj: dict) -> tuple:
    return (obj.get("table"), obj.get("column") or obj.get("kind"), obj.get("change_type"))


def _definitions_match(declared: object, actual: object) -> bool:
    """Exact structural equality on the fields that matter — not a fuzzy
    match. A change.yaml claiming a column was 'text' when the real
    baseline snapshot says 'varchar(50)' does not match, even though both
    are 'text-like' — precision here is the whole point of a deterministic
    gate."""
    if declared is None and actual is None:
        return True
    if isinstance(declared, dict) and isinstance(actual, dict):
        # Only compare keys the evidence record actually declared — the
        # real snapshot may carry more fields (collation, generated, etc.)
        # than a human would bother restating.
        for key, val in declared.items():
            if key not in actual or actual[key] != val:
                return False
        return True
    return declared == actual


def verify_evidence(evidence: ChangeEvidence, blocking_findings: list[dict]) -> EvidenceVerdict:
    findings_by_key = {_finding_key(f): f for f in blocking_findings}
    covered: list[dict] = []
    mismatches: list[str] = []
    matched_keys: set[tuple] = set()

    for obj in evidence.affected_objects:
        key = _object_key(obj)
        finding = findings_by_key.get(key)
        if finding is None:
            mismatches.append(
                f"declared object {obj.get('table')}.{obj.get('column') or ''} "
                f"({obj.get('change_type')}) does not match any real blocking finding "
                f"detected in this PR — evidence must reference the actual diff, not a "
                f"hypothetical one"
            )
            continue

        before_ok = _definitions_match(obj.get("before"), finding.get("before"))
        after_ok = _definitions_match(obj.get("after"), finding.get("after"))
        if not (before_ok and after_ok):
            mismatches.append(
                f"declared before/after for {obj.get('table')}.{obj.get('column') or ''} "
                f"does not match the actual detected diff (before_ok={before_ok}, after_ok={after_ok})"
            )
            continue

        covered.append(finding)
        matched_keys.add(key)

    uncovered = [f for k, f in findings_by_key.items() if k not in matched_keys]

    return EvidenceVerdict(
        matched=(len(mismatches) == 0 and len(uncovered) == 0),
        covered_findings=covered,
        uncovered_findings=uncovered,
        mismatches=mismatches,
    )
