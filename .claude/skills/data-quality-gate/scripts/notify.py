#!/usr/bin/env python3
"""Slack notification for the V1 Data Quality Gate.

A DELIBERATELY SEPARATE script from skills/data-standards/scripts/notify.py
— that script's SLACK_EVENT_TEMPLATES is a closed dict (pr_update,
validation_failure, pass_after_failure, bypass, merge,
standards_version_change) with no event that fits "verified evidence
resolution", and its channel env var (TALEEMABAD_DATA_STANDARDS_SLACK_
CHANNEL) is that skill's own. Extending its dict would couple this gate
into data-standards' file for no shared benefit. Instead this reuses the
SAME underlying primitive both scripts share — slack_send.py — so there is
still exactly one authoritative Slack client in the pack, just two thin
callers with their own event vocabularies (the same pattern data-standards
itself used against storytime).

Events:
  - block                 confirmed destructive/critical finding, PR blocked
  - evidence_verified      developer evidence matched, PR unblocked without
                           Data Team approval (second message per the brief)

Own env var: TALEEMABAD_DATA_QUALITY_GATE_SLACK_CHANNEL (distinct from
data-standards' channel var on purpose — could be the same physical Slack
channel if an org wants that, but is never assumed to be).

Never blocks the calling process: both subcommands always exit 0; delivery
outcome is reported in the JSON body only, same guarantee data-standards'
notify.py gives.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
# SKILL_DIR.parent is the "skills" directory itself — storytime is always a
# SIBLING skill folder there, regardless of how deep that skills/ directory
# sits in a given repo (agent-skills-taleemabad: skills/ at the repo root;
# a different pack: skills/ nested deeper). Layout-agnostic on purpose;
# if storytime genuinely isn't installed alongside this skill (e.g. this
# skill was copied into a product repo on its own), send_slack()'s own
# try/except ImportError already degrades to "not sent" rather than
# crashing — see that function below.
STORYTIME_SCRIPTS = SKILL_DIR.parent / "storytime" / "scripts"

_LOCAL_PATH_PATTERN = re.compile(r"[A-Za-z]:\\[^\s\"']+|/(?:Users|home)/[^/\s\"']+/[^\s\"']*")
_SECRET_PATTERN = re.compile(
    r"sk-[A-Za-z0-9]+|xoxb-[A-Za-z0-9-]+|ghp_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+|"
    r"ntn_[A-Za-z0-9]+|AKIA[A-Z0-9]+|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


def sanitize(value):
    if not isinstance(value, str):
        return value
    value = _LOCAL_PATH_PATTERN.sub("[local-path-redacted]", value)
    value = _SECRET_PATTERN.sub("[credential-redacted]", value)
    return value


def sanitize_deep(obj):
    if isinstance(obj, str):
        return sanitize(obj)
    if isinstance(obj, dict):
        return {k: sanitize_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_deep(v) for v in obj]
    return obj


EVENT_TEMPLATES = {
    "block": ("🛑", "Data Quality Gate — BLOCKED (confirmed violation)"),
    "evidence_verified": ("✅", "Data Quality Gate — unblocked via verified developer evidence"),
}


def build_message(event: str, fields: dict) -> str:
    if event not in EVENT_TEMPLATES:
        raise ValueError(f"unknown event type: {event} (expected one of {list(EVENT_TEMPLATES)})")
    emoji, title = EVENT_TEMPLATES[event]
    fields = sanitize_deep(fields)

    lines = [f"{emoji} *{title}*"]
    order = ["repo", "pr", "branch", "commit", "actor", "baseline_sha", "affected_object",
             "detected_change", "risk", "blocking_issue_id", "rationale", "evidence_links",
             "verification_result", "remaining_risk", "recovery_plan", "audit_ref", "report_url"]
    labels = {
        "repo": "Repo", "pr": "PR", "branch": "Branch", "commit": "Commit", "actor": "Actor",
        "baseline_sha": "Baseline SHA", "affected_object": "Affected object",
        "detected_change": "Detected change", "risk": "Risk", "blocking_issue_id": "Issue ID",
        "rationale": "Rationale", "evidence_links": "Evidence", "verification_result": "Verification",
        "remaining_risk": "Remaining risk", "recovery_plan": "Recovery plan", "audit_ref": "Audit ref",
        "report_url": "Report",
    }
    for key in order:
        if key in fields and fields[key] not in (None, "", []):
            val = fields[key]
            if isinstance(val, (list, dict)):
                val = json.dumps(val)
            lines.append(f"• *{labels[key]}:* {val}")
    return "\n".join(lines)


def send_slack(event: str, channel: str | None, fields: dict) -> dict:
    if not channel:
        return {"sent": False, "reason": "no channel configured — set --channel or "
                 "TALEEMABAD_DATA_QUALITY_GATE_SLACK_CHANNEL"}
    try:
        sys.path.insert(0, str(STORYTIME_SCRIPTS))
        import slack_send  # noqa: E402
    except ImportError as e:
        return {"sent": False, "reason": f"couldn't import the pack's Slack helper: {e}"}

    try:
        text = build_message(event, fields)
    except ValueError as e:
        return {"sent": False, "reason": str(e)}

    try:
        result = slack_send.post_message(channel_id=channel, text=text)
        return {"sent": True, "ts": result.get("ts"), "channel": channel}
    except SystemExit as e:
        return {"sent": False, "reason": f"Slack token unavailable: {e}"}
    except Exception as e:  # noqa: BLE001 - Slack failure must never propagate as a blocking error
        return {"sent": False, "reason": f"send failed: {e}"}


def main() -> int:
    try:
        sys.path.insert(0, str(STORYTIME_SCRIPTS))
        import slack_send
        slack_send._load_env()
    except ImportError:
        pass

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--event", required=True, choices=list(EVENT_TEMPLATES))
    ap.add_argument("--channel", default=os.environ.get("TALEEMABAD_DATA_QUALITY_GATE_SLACK_CHANNEL"))
    ap.add_argument("--repo")
    ap.add_argument("--pr")
    ap.add_argument("--branch")
    ap.add_argument("--commit")
    ap.add_argument("--actor")
    ap.add_argument("--baseline-sha")
    ap.add_argument("--affected-object")
    ap.add_argument("--detected-change")
    ap.add_argument("--risk")
    ap.add_argument("--blocking-issue-id")
    ap.add_argument("--rationale")
    ap.add_argument("--evidence-links")
    ap.add_argument("--verification-result")
    ap.add_argument("--remaining-risk")
    ap.add_argument("--recovery-plan")
    ap.add_argument("--audit-ref")
    ap.add_argument("--report-url")
    args = ap.parse_args()

    fields = {
        "repo": args.repo, "pr": args.pr, "branch": args.branch, "commit": args.commit,
        "actor": args.actor, "baseline_sha": args.baseline_sha, "affected_object": args.affected_object,
        "detected_change": args.detected_change, "risk": args.risk, "blocking_issue_id": args.blocking_issue_id,
        "rationale": args.rationale, "evidence_links": args.evidence_links,
        "verification_result": args.verification_result, "remaining_risk": args.remaining_risk,
        "recovery_plan": args.recovery_plan, "audit_ref": args.audit_ref, "report_url": args.report_url,
    }
    result = send_slack(args.event, args.channel, fields)
    print(json.dumps(result, indent=2))
    return 0  # never fail the calling process over a notification outcome


if __name__ == "__main__":
    sys.exit(main())
