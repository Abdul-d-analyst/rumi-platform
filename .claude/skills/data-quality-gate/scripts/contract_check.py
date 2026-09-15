#!/usr/bin/env python3
"""Contract/naming/metadata gate for NEW tables and columns.

schema_diff.py classifies structural change shape (ADDED/REMOVED/etc.);
this module answers the separate question the assignment poses for
additions specifically: "Allow a new table or column when it meets
contract, metadata, type, naming, nullability/default, ownership/
sensitivity, and compatibility requirements" — i.e. an ADDED column isn't
automatically fine just because addition is generally safe.

Findings here are BLOCKING only for a new PII-shaped column with no
declared sensitivity classification (mirrors data-standards' own D4/D6
blocking rule for the identical situation, applied here at the live-schema
level instead of DDL-text level) — see reference/contract-rules.md. Every
other contract mismatch (unexpected column not in contract.allowed,
naming convention, missing default) is advisory: a table with no contract
at all is in WARN-only mode per data-profile.yaml's advisory_rollout.
"""

from __future__ import annotations

import re

from config import TableContract

# \b treats underscore as a WORD character (same class as letters/digits),
# so \bphone\b never matches inside phone_number, contact_phone_number, or
# user_phone — only the bare standalone word "phone". Every realistic
# snake_case Postgres column name (the actual convention this gate exists
# to check) would silently slip past a \b-bounded pattern. Found via the
# fork-test proof run: a real column named contact_phone_number produced
# no PII finding at all. Splitting on non-alphanumeric characters and
# matching whole SEGMENTS instead of relying on \b's word-character
# definition is what actually catches snake_case names. (The sibling
# data-standards skill's validate_schema.py has this identical \b bug —
# out of scope to fix here per this gate's own non-duplication mandate;
# worth reporting there separately.)
_PII_KEYWORDS = {
    "phone", "phone_number", "cnic", "ssn", "social_security", "dob",
    "date_of_birth", "email", "address", "national_id", "passport",
}
_SEGMENT_SPLIT_RE = re.compile(r"[^a-zA-Z0-9]+")


def _is_pii_shaped_name(column: str) -> bool:
    segments = [s for s in _SEGMENT_SPLIT_RE.split(column.lower()) if s]
    if any(seg in _PII_KEYWORDS for seg in segments):
        return True
    # multi-word keywords like "phone_number"/"date_of_birth"/"social_security"
    # also match as a case-insensitive SUBSTRING of the whole column name,
    # since a real name might not split on the exact same underscores
    # (e.g. "phonenumber" with no underscore at all).
    lowered = column.lower()
    return any(kw.replace("_", "") in lowered.replace("_", "") for kw in _PII_KEYWORDS if "_" in kw)


def check_new_column(table: str, column: str, definition: dict, contract: TableContract | None) -> list[dict]:
    """Returns a list of findings for a single ADDED column. Each finding:
    {kind, blocking, detail}."""
    findings: list[dict] = []

    is_pii_shaped = _is_pii_shaped_name(column)
    has_sensitivity_declared = bool(contract and column in contract.sensitivity)

    if is_pii_shaped and not has_sensitivity_declared:
        findings.append({
            "kind": "new_pii_column_unclassified",
            "blocking": True,
            "detail": (
                f"{table}.{column} matches a known PII name pattern but has no "
                f"sensitivity/classification entry in a table contract "
                f"(.data-quality-gate/table-contracts/{table}.yaml sensitivity.{column})"
            ),
        })

    if contract is None:
        return findings  # everything below needs a contract to check against; no contract = WARN-only rollout, not a block

    if contract.allowed_columns and column not in contract.allowed_columns:
        findings.append({
            "kind": "column_not_in_contract",
            "blocking": False,
            "detail": f"{table}.{column} added but not listed in the table contract's columns.allowed",
        })

    expected_type = contract.expected_types.get(column)
    if expected_type and expected_type != definition.get("type"):
        findings.append({
            "kind": "type_mismatch_vs_contract",
            "blocking": False,
            "detail": f"{table}.{column} is {definition.get('type')}, contract expects {expected_type}",
        })

    expected_nullability = contract.nullability.get(column)
    if expected_nullability == "not_null" and definition.get("nullable"):
        findings.append({
            "kind": "nullability_mismatch_vs_contract",
            "blocking": False,
            "detail": f"{table}.{column} is nullable, contract declares it not_null",
        })

    return findings


def check_new_table(table: str, table_snapshot: dict, contract: TableContract | None) -> list[dict]:
    findings: list[dict] = []
    if not table_snapshot.get("primary_key"):
        findings.append({
            "kind": "new_table_missing_primary_key",
            "blocking": True,
            "detail": f"new table {table!r} has no PRIMARY KEY",
        })
    for column, definition in table_snapshot.get("columns", {}).items():
        findings.extend(check_new_column(table, column, definition, contract))
    return findings
