#!/usr/bin/env python3
"""PreToolUse hook: live, per-edit check of the SCHEMA-SHAPE half of the
Data Quality Gate — the half that's pure logic over a parsed schema
snapshot, no live Postgres connection required. Fires on Edit and Write,
mirroring data-standards' own hooks/live-schema-edit-gate.py.

WHAT THIS DOES NOT DO, BY DESIGN — the gate's other half genuinely cannot
run here: row counts, null-rate drift, FK orphan counts, duplicate-key
rates, and IQR/robust-z/PSI anomaly detection (data_profile.py,
profile_runner.py) are real aggregate facts about a real, populated
database. There is no offline substitute for "how many orphaned rows
actually exist" — those checks stay CI-only, where gate.py runs them
against two real, migrated throwaway Postgres databases. Pretending to
check them here would be worse than not checking them: a live hook that
silently can't verify a live-data claim must never report as if it had.

What DOES run here — both are pure functions over snapshot dicts, with no
database or file I/O of their own (see each module's own docstring):
  - contract_check.py: new-table (missing PRIMARY KEY) and new-column
    (PII-shaped name with no sensitivity classification in a real
    .data-quality-gate/table-contracts/<table>.yaml, plus advisory
    type/nullability-vs-contract mismatches) checks.
  - schema_diff.py: COLUMN-level classification only (ADDED / REMOVED /
    NARROWED / WIDENED / MODIFIED / UNCHANGED) between a "before" and
    "after" snapshot of the same table.

Deliberately NOT run here: schema_diff.py's CONSTRAINT-removal checks
(dropped PRIMARY KEY / UNIQUE / CHECK / FOREIGN KEY / index). A real
Postgres server auto-generates a deterministic name for an unnamed
constraint (e.g. users_email_key) — sql_snapshot.py does not replicate
that naming convention (see its own module docstring for why), so a
constraint-removal finding here could be a false positive from a naming
mismatch that isn't a real removal at all. Getting a live block WRONG
erodes trust faster than not checking something — this slice stays
CI-only, where the real database has the real constraint names.

"Before" for the column diff: per explicit instruction, the target
repo's default branch (origin/HEAD if resolvable, else local `main`) —
mirroring what data-quality-gate's own CI treats as the approved baseline
(the PR's base-branch tip), just resolved from git instead of a live
database. `git show <ref>:<path>` reads that file's last-committed
content; a brand-new file (git show fails) has no meaningful "before" to
diff against, so the diff step is skipped for it — contract_check.py's
new-table/new-column rules still run against its "after" state alone.

Exit codes (PreToolUse contract, same as the sibling live-edit hook):
    0  no blocking findings (or nothing relevant, or a validated bypass)
    2  confirmed blocking finding(s) — the Edit/Write is refused

Bypass: reuses data-standards' own TALEEMABAD_DATA_STANDARDS_BYPASS
variable and bypass_audit.py, rather than inventing a second mechanism —
this gate's CI side (audit_trail.py) already forwards through the shared
governance sender, so a second bypass variable would just fragment the
one audit trail this pack has been careful to keep singular.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
SKILL_DIR = HOOK_DIR.parent
SCRIPTS = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS))

# SKILL_DIR.parent is the "skills" directory itself — data-standards is
# always a SIBLING skill folder there, regardless of how deep that skills/
# directory sits in a given repo (agent-skills-taleemabad: skills/ at the
# repo root; rumi-platform: .claude/skills/, one level deeper). Layout-
# agnostic on purpose so the exact same file works unmodified in both.
DATA_STANDARDS_SCRIPTS = SKILL_DIR.parent / "data-standards" / "scripts"
BYPASS_AUDIT = DATA_STANDARDS_SCRIPTS / "bypass_audit.py"
# This gate's OWN notify.py, not data-standards' — deliberately separate
# event vocabulary (block/evidence_verified, no bypass event of its own)
# and its own Slack channel var (TALEEMABAD_DATA_QUALITY_GATE_SLACK_
# CHANNEL) — see that script's own module docstring for why it isn't
# just data-standards' notify.py reused. Only the BYPASS *mechanism*
# (the env var + bypass_audit.py's validation/recording) is shared
# across both gates on purpose, per this pack's one-audit-trail
# principle — the Slack MESSAGE about that bypass still goes to this
# gate's own channel, using its own "block" template (there is no
# separate bypass template here to reuse).
NOTIFY = SCRIPTS / "notify.py"

CONTRACTS_DIR = Path(".data-quality-gate") / "table-contracts"


def _read_stdin_json() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return {}


def _prospective_content(tool_name: str, tool_input: dict) -> tuple[str, str] | None:
    """Same logic as the sibling data-standards hook — kept independent
    rather than imported, since each hook must keep working if the other
    pack's hook file is ever moved or refactored (no cross-hook coupling
    beyond the scripts they both legitimately share)."""
    file_path = tool_input.get("file_path")
    if not file_path:
        return None

    if tool_name == "Write":
        content = tool_input.get("content")
        if content is None:
            return None
        return file_path, content

    if tool_name == "Edit":
        old_string = tool_input.get("old_string")
        new_string = tool_input.get("new_string")
        if old_string is None or new_string is None:
            return None
        try:
            current = Path(file_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            current = ""
        if tool_input.get("replace_all"):
            prospective = current.replace(old_string, new_string)
        else:
            prospective = current.replace(old_string, new_string, 1)
        return file_path, prospective

    return None


def _is_schema_relevant(file_path: str) -> bool:
    """Reuses data-standards' shared detector (the same one CI and the
    commit-time gate use) rather than inventing a second definition of
    'schema-relevant file' — see that skill's detection-guidance.md."""
    detector = DATA_STANDARDS_SCRIPTS / "detect_schema_changes.py"
    try:
        proc = subprocess.run(
            [sys.executable, str(detector), "--files", file_path],
            capture_output=True, text=True, timeout=10,
        )
        result = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return False  # fail OPEN — CI is the backstop for a broken detector
    return bool(result.get("relevant"))


def _default_branch_ref() -> str | None:
    """origin/HEAD if the remote's default-branch symlink resolves; else
    local `main`, per explicit instruction to treat main as the baseline.
    Returns None if neither resolves (e.g. no remote, no main branch —
    a genuinely new/local-only repo) so the caller can skip the diff step
    cleanly rather than erroring."""
    for ref in ("origin/HEAD", "main"):
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            capture_output=True, text=True,
        )
        if probe.returncode == 0:
            return ref
    return None


def _before_text(ref: str, file_path: str) -> str | None:
    """The file's content at `ref`, or None if it doesn't exist there at
    all (a brand-new file — nothing to diff against)."""
    proc = subprocess.run(
        ["git", "show", f"{ref}:{file_path}"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def _actor() -> str:
    try:
        git_email = subprocess.run(
            ["git", "config", "user.email"], capture_output=True, text=True,
        ).stdout.strip()
    except OSError:
        git_email = ""
    return (os.environ.get("TALEEMABAD_USER_EMAIL")
            or git_email
            or os.environ.get("USER")
            or os.environ.get("USERNAME")
            or "unknown")


def _notify_slack(actor: str, file_path: str, findings: list[dict], bypass_reason: str | None) -> None:
    """Always the "block" event — this gate's notify.py has no separate
    bypass template (see EVENT_TEMPLATES in that script), so a bypassed
    live finding is still reported via "block" with the bypass reason
    folded into the rationale field, same information either way, just
    without a dedicated template to route it through."""
    detected_change = "; ".join(f"{f['table']}.{f.get('column') or ''}".rstrip(".") + f": {f['kind']}" for f in findings)
    rationale = f"Caught live in a Claude Code session (Edit/Write), before any commit."
    if bypass_reason:
        rationale += f" BYPASSED — {bypass_reason}"
    try:
        subprocess.Popen(
            [sys.executable, str(NOTIFY), "--event", "block",
             "--repo", Path.cwd().name, "--actor", actor,
             "--affected-object", file_path, "--detected-change", detected_change,
             "--rationale", rationale],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def _collect_findings(file_path: str, prospective_text: str) -> list[dict]:
    import contract_check
    import schema_diff
    from sql_snapshot import parse_sql_to_snapshot
    import config as gate_config

    after_snapshot = parse_sql_to_snapshot(prospective_text)

    contracts: dict = {}
    if CONTRACTS_DIR.is_dir():
        try:
            contracts = gate_config.load_all_table_contracts(CONTRACTS_DIR)
        except Exception:
            # A broken table-contract file must not crash every live edit —
            # CI's own gate.py treats a ConfigError as a hard ERROR (exit 2)
            # because it controls the whole run; this hook fires on every
            # keystroke-scale edit across a whole session, so a malformed
            # contract degrades to "no contract for that table" here
            # instead (WARN-only rollout territory, same as a genuinely
            # absent contract file) rather than blocking unrelated edits
            # on a YAML typo in an unrelated table's contract.
            contracts = {}

    findings: list[dict] = []

    before_ref = _default_branch_ref()
    before_snapshot = None
    if before_ref is not None:
        before_text = _before_text(before_ref, file_path)
        if before_text is not None:
            before_snapshot = parse_sql_to_snapshot(before_text)

    for table_name, after_table in after_snapshot["tables"].items():
        contract = contracts.get(table_name)
        before_table = (before_snapshot or {}).get("tables", {}).get(table_name) if before_snapshot else None

        if before_table is None:
            # New table (or no resolvable "before" at all) — only the
            # contract/new-table rules apply; there is nothing to diff.
            # Not calling check_new_table() directly: it loops columns
            # internally via check_new_column() but never tags a
            # resulting finding with WHICH column it's about, since its
            # own caller (gate.py, in the real CI flow) doesn't need
            # per-column attribution — schema_diff.py's blocking_findings()
            # already carries table+column for everything else in that
            # flow. This hook's own error messages need that attribution,
            # so the per-column loop is done here instead, one call per
            # column, same underlying check_new_column() either way.
            if not after_table.get("primary_key"):
                findings.append({
                    "kind": "new_table_missing_primary_key", "blocking": True,
                    "table": table_name,
                    "detail": f"new table {table_name!r} has no PRIMARY KEY",
                })
            for column, definition in after_table.get("columns", {}).items():
                for f in contract_check.check_new_column(table_name, column, definition, contract):
                    findings.append({**f, "table": table_name, "column": column})
            continue

        for cc in schema_diff.diff_columns(table_name, before_table["columns"], after_table["columns"]):
            if cc.classification == "ADDED":
                for f in contract_check.check_new_column(table_name, cc.column, cc.after, contract):
                    findings.append({**f, "table": table_name, "column": cc.column})
            elif cc.blocking:
                findings.append({
                    "kind": f"column_{cc.classification.lower()}",
                    "blocking": True,
                    "table": table_name,
                    "column": cc.column,
                    "detail": cc.reason,
                })

    return findings


def main() -> int:
    data = _read_stdin_json()
    tool_name = data.get("tool_name")
    if tool_name not in ("Edit", "Write"):
        return 0

    tool_input = data.get("tool_input") or {}
    parsed = _prospective_content(tool_name, tool_input)
    if parsed is None:
        return 0
    file_path, prospective_text = parsed

    if not _is_schema_relevant(file_path):
        return 0

    try:
        all_findings = _collect_findings(file_path, prospective_text)
    except Exception:
        # A parser/contract-loading bug must fail OPEN here, same
        # reasoning as _is_schema_relevant's own detector-failure case —
        # CI's real gate.py (with a real database) is the backstop.
        return 0

    # contract_check.py's own contract (see its module docstring) is that
    # only a NEW unclassified-PII column and a missing PRIMARY KEY on a
    # NEW table are blocking — column_not_in_contract, type_mismatch_vs_
    # contract, and nullability_mismatch_vs_contract are advisory findings
    # (blocking: False) that must never refuse an Edit/Write on their own.
    # schema_diff.py's column classifications this hook forwards
    # (column_removed / column_narrowed / the NOT-NULL-tightened MODIFIED
    # case) are always constructed with blocking=True here already, so
    # this filter only ever actually drops contract_check.py's advisory
    # findings, never silently drops something that should have blocked.
    findings = [f for f in all_findings if f.get("blocking")]
    if not findings:
        return 0

    actor = _actor()
    bypass_reason = os.environ.get("TALEEMABAD_DATA_STANDARDS_BYPASS", "")

    if bypass_reason:
        check = subprocess.run(
            [sys.executable, str(BYPASS_AUDIT), "--check", bypass_reason],
            capture_output=True, text=True,
        )
        if check.returncode == 0:
            report_tmp = Path(tempfile.gettempdir()) / f".data-quality-gate-live-{os.getpid()}.json"
            standards_shaped = [{"standard": f["kind"]} for f in findings]
            try:
                report_tmp.write_text(json.dumps({"findings": standards_shaped}), encoding="utf-8")
                subprocess.run(
                    [sys.executable, str(BYPASS_AUDIT), "--record", "--repo", ".",
                     "--reason", bypass_reason, "--actor", actor,
                     "--findings-json", str(report_tmp)],
                    capture_output=True, text=True,
                )
            finally:
                report_tmp.unlink(missing_ok=True)
            print("DATA QUALITY GATE (live edit): BYPASSED — a validated reason was provided and recorded.", file=sys.stderr)
            print(f"  file:   {file_path}", file=sys.stderr)
            print(f"  reason: {bypass_reason}", file=sys.stderr)
            print(f"  actor:  {actor}", file=sys.stderr)
            _notify_slack(actor, file_path, findings, bypass_reason)
            return 0
        print(f"DATA QUALITY GATE (live edit): bypass REJECTED — {check.stdout.strip()}", file=sys.stderr)
        print("  TALEEMABAD_DATA_STANDARDS_BYPASS was set, but its reason did not pass validation,", file=sys.stderr)
        print("  so no bypass is in effect. Falling through to the normal check below.", file=sys.stderr)
        print("", file=sys.stderr)

    print(f"DATA QUALITY GATE (live edit): blocking — confirmed schema-shape finding(s) in {file_path}.", file=sys.stderr)
    print("", file=sys.stderr)
    for f in findings:
        col = f.get("column")
        loc = f"{f['table']}.{col}" if col else f["table"]
        print(f"  {f['kind']} ({loc}): {f['detail']}", file=sys.stderr)
    print("", file=sys.stderr)
    print("Data-profile checks (row counts, null rates, FK orphans, anomalies) still only", file=sys.stderr)
    print("run in CI against a real database — this is the schema-shape half only.", file=sys.stderr)
    print("Fix the finding(s) above, or bypass with an approved reason:", file=sys.stderr)
    print('export TALEEMABAD_DATA_STANDARDS_BYPASS="<a real reason — ticket, incident, or named approver>"', file=sys.stderr)
    _notify_slack(actor, file_path, findings, None)
    return 2


if __name__ == "__main__":
    sys.exit(main())
