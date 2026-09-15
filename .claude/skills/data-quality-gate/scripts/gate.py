#!/usr/bin/env python3
"""The V1 Data Quality Gate orchestrator — the ONE entry point the GitHub
workflow calls. Ties together: scope detection (scope_detect.py), Postgres
introspection of two already-migrated throwaway databases (introspect.py),
the semantic schema diff (schema_diff.py), the new-object contract/naming
gate (contract_check.py), the live data-profile comparison
(profile_runner.py running real SQL, feeding data_profile.py's pure check
functions), the developer-evidence exception (evidence.py + config.py),
Slack notification (notify.py), and the durable audit trail (audit_trail.py).

Data profiling only runs for tables that have a table-contract with a
non-empty `profile` section (--skip-data-profile disables it entirely,
for callers that haven't set up contracts yet) — a table with no
contract stays WARN-only per data-profile.yaml's advisory_rollout, and
this gate does not guess what to profile for it.

This script assumes the CALLER (the GitHub workflow) has already:
  1. Run scope_detect.py and confirmed applicable=true.
  2. Spun up two throwaway Postgres databases (or one server, two schemas).
  3. Applied the base-branch migrations to one and the PR's migrations to
     the other via migrate.py.
  4. Passed both DSNs here.

Verdict vocabulary (see reference/report-format.md):
    PASS | FAIL | WARN | NOT APPLICABLE | NOT VERIFIED | ERROR

Exit codes, mirroring skills/data-standards/scripts/validate_schema.py's
contract so CI wiring stays familiar across both gates:
    0  PASS  (no blocking findings; WARN findings may exist)
    1  FAIL  (confirmed blocking finding, no covering evidence)
    2  ERROR (the gate itself could not run — snapshot/profile/timeout
              failure; ALWAYS fail closed, never silently pass)
    3  NOT APPLICABLE (scope detector found nothing relevant — this script
              itself does not compute this; the workflow short-circuits
              before calling gate.py at all when scope says not applicable)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as gate_config          # noqa: E402
import contract_check                 # noqa: E402
import evidence as evidence_mod        # noqa: E402
import profile_runner                 # noqa: E402
import schema_diff                    # noqa: E402
from introspect import snapshot_schema, IntrospectionError  # noqa: E402

GATE_ROOT = Path(__file__).resolve().parent.parent
DQG_CONFIG_DIR = Path(".data-quality-gate")


class GateError(Exception):
    """Anything that must produce exit code 2 (ERROR / fail-closed)."""


def _time_budget(fn, seconds: int, what: str):
    """Runs fn() and raises GateError if it doesn't fit in the declared
    time budget. Python's stdlib has no cross-platform hard-timeout for an
    arbitrary in-process call; here every fn we actually pass in wraps a
    subprocess (psql, or a DB driver's own network I/O) so we rely on the
    callee's own timeout parameter — this wrapper's job is only to turn a
    caught timeout into a GateError with a clear, attributable message,
    never to silently swallow one."""
    start = time.monotonic()
    try:
        return fn()
    except Exception as e:
        raise GateError(f"{what} failed: {e}") from e
    finally:
        elapsed = time.monotonic() - start
        if elapsed > seconds:
            raise GateError(f"{what} exceeded its {seconds}s time budget ({elapsed:.1f}s elapsed)")


def run_schema_gate(base_dsn: str, head_dsn: str, contracts: dict, *, schema: str = "public",
                     snapshot_timeout: int = 120) -> dict:
    try:
        before = _time_budget(lambda: snapshot_schema(base_dsn, schema), snapshot_timeout, "baseline schema snapshot")
        after = _time_budget(lambda: snapshot_schema(head_dsn, schema), snapshot_timeout, "proposed schema snapshot")
    except (IntrospectionError, GateError) as e:
        raise GateError(str(e)) from e

    rename_maps = {t: c.profile.get("rename_map", {}) for t, c in contracts.items() if c.profile.get("rename_map")}
    diff = schema_diff.diff_schema(before, after, rename_maps)

    contract_findings = []
    for tc in diff.table_changes:
        if tc.classification == "ADDED":
            contract_findings.extend(
                contract_check.check_new_table(tc.table, after["tables"][tc.table], contracts.get(tc.table))
            )
        for cc in tc.column_changes:
            if cc.classification == "ADDED":
                contract_findings.extend(
                    contract_check.check_new_column(tc.table, cc.column, cc.after, contracts.get(tc.table))
                )

    blocking = diff.blocking_findings() + [f for f in contract_findings if f.get("blocking")]
    return {
        "before_snapshot": before, "after_snapshot": after, "diff": diff.to_dict(),
        "contract_findings": contract_findings, "blocking_findings": blocking,
    }


def run_data_profile_gate(base_dsn: str, head_dsn: str, contracts: dict,
                           data_profile_config_path: Path, *, profile_timeout: int = 120) -> dict:
    """Runs the live data-profile checks (row counts, duplicates, DIM
    stability, null rates, FK orphans, junk-value rate, and anomaly
    detection where history is available) — the piece that was
    previously built and unit-tested in data_profile.py/profile_runner.py
    but never actually called from here. See profile_runner.py's module
    docstring for exactly what "skipped" (vs a real finding) means for
    anomaly checks with no supplied history."""
    try:
        global_cfg = gate_config.load_data_profile_config(data_profile_config_path)
    except gate_config.ConfigError as e:
        raise GateError(f"malformed .data-quality-gate/data-profile.yaml: {e}") from e

    findings = _time_budget(
        lambda: profile_runner.run_profile_gate(base_dsn, head_dsn, contracts, global_cfg),
        profile_timeout, "data-profile gate",
    )
    blocking = [f for f in findings if f.get("severity") == "block"]
    return {"profile_findings": findings, "blocking_profile_findings": blocking}


def load_change_evidence_if_present() -> gate_config.ChangeEvidence | None:
    path = DQG_CONFIG_DIR / "change.yaml"
    if not path.exists():
        return None
    return gate_config.load_change_evidence(path)  # raises ConfigError -> caller treats as GateError


def build_verdict(blocking_findings: list[dict], evidence: gate_config.ChangeEvidence | None) -> dict:
    if not blocking_findings:
        return {"result": "PASS", "blocking_findings": [], "evidence_verdict": None}

    if evidence is None:
        return {"result": "FAIL", "blocking_findings": blocking_findings, "evidence_verdict": None}

    verdict = evidence_mod.verify_evidence(evidence, blocking_findings)
    if verdict.matched:
        return {
            "result": "PASS WITH VERIFIED EVIDENCE",
            "blocking_findings": blocking_findings,
            "evidence_verdict": {
                "matched": True, "covered": verdict.covered_findings, "mismatches": [],
                "note": (
                    "recovery-plan quality, retention-evidence sufficiency, and the "
                    "narrative fields are presence-checked only, not independently "
                    "proven by automation — only object identity and before/after "
                    "definitions were deterministically verified against the real diff"
                ),
            },
        }
    return {
        "result": "FAIL",
        "blocking_findings": blocking_findings,
        "evidence_verdict": {
            "matched": False, "covered": verdict.covered_findings,
            "uncovered": verdict.uncovered_findings, "mismatches": verdict.mismatches,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-dsn", required=True, help="DSN of the throwaway DB with base-branch migrations applied")
    ap.add_argument("--head-dsn", required=True, help="DSN of the throwaway DB with PR/head migrations applied")
    ap.add_argument("--schema", default="public")
    ap.add_argument("--baseline-sha", required=True, help="the approved base-branch tip SHA — recorded in every report")
    ap.add_argument("--snapshot-timeout-seconds", type=int, default=120)
    ap.add_argument("--profile-timeout-seconds", type=int, default=120)
    ap.add_argument("--contracts-dir", default=str(DQG_CONFIG_DIR / "table-contracts"))
    ap.add_argument("--data-profile-config", default=str(DQG_CONFIG_DIR / "data-profile.yaml"))
    ap.add_argument("--skip-data-profile", action="store_true",
                     help="run schema-diff only — for callers that haven't set up table contracts yet")
    ap.add_argument("-o", "--output", help="write the JSON report here instead of stdout")
    args = ap.parse_args()

    try:
        contracts = gate_config.load_all_table_contracts(Path(args.contracts_dir))
    except gate_config.ConfigError as e:
        report = {"result": "ERROR", "baseline_sha": args.baseline_sha, "error": f"malformed configuration: {e}"}
        _emit(report, args.output)
        return 2

    try:
        schema_result = run_schema_gate(
            args.base_dsn, args.head_dsn, contracts, schema=args.schema,
            snapshot_timeout=args.snapshot_timeout_seconds,
        )
    except GateError as e:
        report = {"result": "ERROR", "baseline_sha": args.baseline_sha, "error": str(e)}
        _emit(report, args.output)
        return 2

    profile_findings: list[dict] = []
    blocking_profile_findings: list[dict] = []
    if not args.skip_data_profile:
        try:
            profile_result = run_data_profile_gate(
                args.base_dsn, args.head_dsn, contracts, Path(args.data_profile_config),
                profile_timeout=args.profile_timeout_seconds,
            )
        except GateError as e:
            report = {"result": "ERROR", "baseline_sha": args.baseline_sha, "error": str(e)}
            _emit(report, args.output)
            return 2
        profile_findings = profile_result["profile_findings"]
        blocking_profile_findings = profile_result["blocking_profile_findings"]

    try:
        evidence = load_change_evidence_if_present()
    except gate_config.ConfigError as e:
        report = {"result": "ERROR", "baseline_sha": args.baseline_sha,
                   "error": f"malformed .data-quality-gate/change.yaml: {e}"}
        _emit(report, args.output)
        return 2

    all_blocking_findings = schema_result["blocking_findings"] + blocking_profile_findings
    verdict = build_verdict(all_blocking_findings, evidence)
    report = {
        "baseline_sha": args.baseline_sha,
        "result": verdict["result"],
        "blocking_findings": verdict["blocking_findings"],
        "evidence_verdict": verdict["evidence_verdict"],
        "diff": schema_result["diff"],
        "contract_findings": schema_result["contract_findings"],
        "profile_findings": profile_findings,
    }
    _emit(report, args.output)

    if verdict["result"] in ("PASS", "PASS WITH VERIFIED EVIDENCE"):
        return 0
    return 1


def _emit(report: dict, output: str | None) -> None:
    text = json.dumps(report, indent=2, default=str)
    if output:
        Path(output).write_text(text, encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    sys.exit(main())
