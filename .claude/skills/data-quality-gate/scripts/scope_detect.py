#!/usr/bin/env python3
"""Cheap, stdlib-only scope detector for the V1 Data Quality Gate.

This is the FIRST thing the GitHub workflow runs. Its only job is to answer
"does this PR need the expensive Postgres-backed schema/profile gate at
all?" using nothing but the standard library (no PyYAML, no psycopg) so an
irrelevant PR (docs, a README, an unrelated skill) gets a fast, conclusive
NOT_APPLICABLE without spinning up any database.

Distinct from skills/data-standards/scripts/detect_schema_changes.py on
purpose: that script is data-standards' own detector, keyed off
.data-standards.json, and is a static textual-DDL detector. This one is
keyed off .data-quality-gate/scope.json and exists purely to gate whether
the live-Postgres-introspection pipeline below it runs — a different
question (data-standards never touches a live database at all).

Usage:
    python3 scope_detect.py --git-diff <base> <head>
    python3 scope_detect.py --files a.sql b.py
    python3 scope_detect.py --config .data-quality-gate/scope.json --git-diff <base> <head>

Exit code: always 0 if the detector itself ran correctly (detection is
reported in JSON, not via exit code) — 2 on a genuine detector error (git
not available when --git-diff was requested, malformed config).

Output JSON: {"applicable": bool, "relevant_files": [...], "reason": str}
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from pathlib import Path

DEFAULT_CONFIG = {
    "include": [
        "**/*.sql",
        "**/migrations/**",
        "**/*migration*.py",
        "**/db/migrate/**",
        "**/schema.prisma",
        ".data-quality-gate/**",
    ],
    "exclude": [
        ".git/**", "node_modules/**", "vendor/**", ".venv/**", "venv/**",
        "dist/**", "build/**", "__pycache__/**", "*.lock",
    ],
}


def load_config(config_path: str | None) -> dict:
    if not config_path:
        return DEFAULT_CONFIG
    p = Path(config_path)
    if not p.exists():
        return DEFAULT_CONFIG
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"malformed scope config at {p}: {e}", file=sys.stderr)
        sys.exit(2)
    cfg = dict(DEFAULT_CONFIG)
    if "include" in data:
        cfg["include"] = data["include"]
    if "exclude" in data:
        cfg["exclude"] = list(DEFAULT_CONFIG["exclude"]) + list(data["exclude"])
    return cfg


def matches_any(path: str, patterns: list[str]) -> bool:
    """fnmatch doesn't natively support "**" the way glob does across path
    separators — "**/node_modules/**" requires at least one "/" before
    "node_modules", so it misses a path where node_modules is the FIRST
    segment. Same fix data-standards' own detect_schema_changes.py applies
    to the identical pattern shape (its matches_any(), not imported here
    since this gate's config is deliberately its own file/vocabulary, but
    the underlying fnmatch limitation is universal, not skill-specific)."""
    norm = path.replace("\\", "/")
    for pat in patterns:
        if fnmatch.fnmatch(norm, pat):
            return True
        if pat.startswith("**/") and fnmatch.fnmatch(norm, pat[3:]):
            return True
        if fnmatch.fnmatch("/" + norm, "*/" + pat.lstrip("*/")):
            return True
    return False


def is_relevant(path: str, cfg: dict) -> bool:
    if matches_any(path, cfg["exclude"]):
        return False
    return matches_any(path, cfg["include"])


def git_diff_files(base: str, head: str) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", f"{base}...{head}"],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"git diff failed: {e}", file=sys.stderr)
        sys.exit(2)
    return [line for line in out.stdout.splitlines() if line.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=".data-quality-gate/scope.json")
    ap.add_argument("--git-diff", nargs=2, metavar=("BASE", "HEAD"))
    ap.add_argument("--files", nargs="*")
    args = ap.parse_args()

    cfg = load_config(args.config)

    if args.git_diff:
        base, head = args.git_diff
        files = git_diff_files(base, head)
    elif args.files is not None:
        files = args.files
    else:
        print("one of --git-diff or --files is required", file=sys.stderr)
        return 2

    relevant = [f for f in files if is_relevant(f, cfg)]
    result = {
        "applicable": len(relevant) > 0,
        "relevant_files": relevant,
        "reason": (
            f"{len(relevant)} file(s) matched the scope config"
            if relevant else
            "no changed file matched .data-quality-gate/scope.json's include patterns"
        ),
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
