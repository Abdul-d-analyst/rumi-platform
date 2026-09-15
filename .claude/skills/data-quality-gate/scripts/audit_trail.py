#!/usr/bin/env python3
"""Durable audit trail for the V1 Data Quality Gate.

The assignment requires the blocked event and the verified-evidence pass
to be recorded "in a durable shared audit trail — not a local or gitignored
file." A GitHub Actions runner's local filesystem is destroyed at the end
of every job, so nothing written to disk in CI is durable on its own —
this module's actual durability comes from being uploaded as a build
artifact AND appended to the governance event stream every run (which,
per data-standards' own notify.py, already either POSTs to a real HTTPS
governance endpoint if one is configured, or falls back to a local file
"never discarded, never silently dropped" as an explicit interim state).

This module does NOT reimplement governance delivery — it reuses
data-standards' scripts/notify.py governance subcommand directly (one
governance ingestion path for the whole pack, not two), tagged with this
gate's own event shape so a downstream consumer can tell the two apart by
schema_version + gate name.

Concretely, in CI: every run appends one line to
.data-quality-gate/audit-log.jsonl (committed to the artifact upload, NOT
gitignored — see reference/audit-trail.md for why this file is tracked
rather than a dotfile local state cache like data-standards' own
.data-standards-bypass-log.jsonl) AND calls the shared governance sender.
Both must be attempted; a failure in one must not skip the other.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

GATE_ROOT = Path(__file__).resolve().parent.parent  # skills/data-quality-gate
# GATE_ROOT.parent is the "skills" directory itself — data-standards is
# always a SIBLING skill folder there, regardless of how deep that skills/
# directory sits in a given repo (agent-skills-taleemabad: skills/ at the
# repo root; rumi-platform: .claude/skills/, one level deeper). Layout-
# agnostic on purpose so the exact same file works unmodified in both.
DATA_STANDARDS_NOTIFY = GATE_ROOT.parent / "data-standards" / "scripts" / "notify.py"

AUDIT_LOG_SCHEMA_VERSION = "1"


def append_audit_record(record: dict, log_path: Path) -> dict:
    """Appends one JSONL record to the durable audit log. Never raises —
    a write failure here must not itself become a reason to fail the gate
    differently than the underlying finding already dictates; it is
    reported in the returned dict instead."""
    record = {"schema_version": AUDIT_LOG_SCHEMA_VERSION, "recorded_at_utc":
              time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **record}
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
        return {"appended": True, "path": str(log_path)}
    except OSError as e:
        return {"appended": False, "error": str(e)}


def forward_to_governance(*, repo: str, branch: str, head_sha: str, base_sha: str | None,
                           result: str, actor: str | None, pr: str | None,
                           changed_objects: list[str], issue_ids: list[str],
                           severity_counts: dict, report_ref: str | None) -> dict:
    """Best-effort forward to data-standards' shared governance ingestion
    path. This is composition, not duplication: one governance sender for
    the whole pack. Never raises — a governance-forwarding failure must
    never affect the gate's pass/fail decision."""
    if not DATA_STANDARDS_NOTIFY.exists():
        return {"forwarded": False, "reason": f"{DATA_STANDARDS_NOTIFY} not found"}

    cmd = [
        sys.executable, str(DATA_STANDARDS_NOTIFY), "governance",
        "--repo", f"data-quality-gate:{repo}",  # tag so a downstream consumer can distinguish gates
        "--branch", branch, "--head-sha", head_sha, "--result", result,
    ]
    if base_sha:
        cmd += ["--base-sha", base_sha]
    if actor:
        cmd += ["--actor", actor]
    if pr:
        cmd += ["--pr", pr]
    if changed_objects:
        cmd += ["--changed-objects", ",".join(changed_objects)]
    if issue_ids:
        cmd += ["--issue-ids", ",".join(issue_ids)]
    if severity_counts:
        cmd += ["--severity-counts", json.dumps(severity_counts)]
    if report_ref:
        cmd += ["--report-ref", report_ref]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return {"forwarded": True, "stdout": proc.stdout, "returncode": proc.returncode}
    except Exception as e:  # noqa: BLE001 - never let a subprocess failure affect the gate's own verdict
        return {"forwarded": False, "reason": str(e)}
