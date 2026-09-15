#!/usr/bin/env python3
"""Semantic core-schema diff — compares two normalized snapshots (from
introspect.py) and classifies every table/column change as one of:

    ADDED | REMOVED | RENAMED | WIDENED | NARROWED | MODIFIED | UNCHANGED

A rename is always reported as REMOVED + ADDED for blocking purposes unless
an explicit mapping in a table contract declares it (contract.rename_map,
see config.py) — name similarity is never used to auto-suppress a
REMOVED+ADDED pair, per the assignment ("name similarity is advisory only;
it must not suppress a failure").

This module contains NO Postgres connection code and NO YAML parsing — it
operates purely on the two dict snapshots handed to it, so it's trivially
unit-testable against synthetic fixtures (see evals/fixtures/).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A narrower/coarser numeric or int type replacing a wider one, ordered
# smallest-to-largest so we can detect a downgrade even when both sides
# are technically "numeric".
_INT_WIDTH_RANK = {"int2": 1, "int4": 2, "int8": 3}
_FLOAT_WIDTH_RANK = {"float4": 1, "float8": 2}


@dataclass
class ColumnChange:
    table: str
    column: str
    classification: str  # ADDED|REMOVED|RENAMED|WIDENED|NARROWED|MODIFIED|UNCHANGED
    before: dict | None
    after: dict | None
    blocking: bool
    reason: str


@dataclass
class TableChange:
    table: str
    classification: str  # ADDED|REMOVED|MODIFIED|UNCHANGED
    column_changes: list[ColumnChange] = field(default_factory=list)
    constraint_findings: list[dict] = field(default_factory=list)


@dataclass
class SchemaDiffResult:
    table_changes: list[TableChange]

    def blocking_findings(self) -> list[dict]:
        out = []
        for tc in self.table_changes:
            for cc in tc.column_changes:
                if cc.blocking:
                    out.append({
                        "table": tc.table, "column": cc.column,
                        "classification": cc.classification, "reason": cc.reason,
                        "before": cc.before, "after": cc.after,
                    })
            for cf in tc.constraint_findings:
                if cf.get("blocking"):
                    out.append({"table": tc.table, **cf})
        return out

    def to_dict(self) -> dict:
        return {
            "tables": [
                {
                    "table": tc.table,
                    "classification": tc.classification,
                    "columns": [
                        {
                            "column": cc.column, "classification": cc.classification,
                            "blocking": cc.blocking, "reason": cc.reason,
                            "before": cc.before, "after": cc.after,
                        }
                        for cc in tc.column_changes
                    ],
                    "constraint_findings": tc.constraint_findings,
                }
                for tc in self.table_changes
            ]
        }


def _is_narrowing(before: dict, after: dict) -> tuple[bool, str] | None:
    """Returns (is_narrowing, reason) if the type change from before->after
    can truncate/round/overflow/reject a previously valid value, else None
    if this isn't a narrowing-relevant type pair at all."""
    bt, at = before["type"], after["type"]

    if bt in ("varchar", "bpchar") and at in ("varchar", "bpchar"):
        bl, al = before.get("length"), after.get("length")
        if bl is not None and al is not None and al < bl:
            return True, f"length {bl} -> {al} can truncate existing values"
        return False, ""

    if bt in ("varchar", "bpchar") and at == "text":
        return False, ""  # widening: bounded -> unbounded
    if bt == "text" and at in ("varchar", "bpchar"):
        return True, "text (unbounded) -> varchar/bpchar (bounded) can truncate existing values"

    if bt in ("numeric", "decimal") and at in ("numeric", "decimal"):
        bp, bs = before.get("precision"), before.get("scale")
        ap, as_ = after.get("precision"), after.get("scale")
        if None not in (bp, ap) and ap < bp:
            return True, f"precision {bp} -> {ap} can overflow existing values"
        if None not in (bs, as_) and as_ < bs:
            return True, f"scale {bs} -> {as_} can round existing values"
        return False, ""

    if bt in _INT_WIDTH_RANK and at in _INT_WIDTH_RANK:
        if _INT_WIDTH_RANK[at] < _INT_WIDTH_RANK[bt]:
            return True, f"integer type {bt} -> {at} can overflow existing values"
        return False, ""

    if bt in _FLOAT_WIDTH_RANK and at in _FLOAT_WIDTH_RANK:
        if _FLOAT_WIDTH_RANK[at] < _FLOAT_WIDTH_RANK[bt]:
            return True, f"float type {bt} -> {at} can lose precision for existing values"
        return False, ""

    if bt != at:
        # A cross-family type change we don't have a specific lossy rule
        # for (e.g. text -> uuid) is reported as MODIFIED, not silently
        # ignored — see caller.
        return None
    return False, ""


def diff_columns(table: str, before_cols: dict, after_cols: dict, rename_map: dict | None = None) -> list[ColumnChange]:
    rename_map = rename_map or {}
    changes: list[ColumnChange] = []
    before_names, after_names = set(before_cols), set(after_cols)

    declared_renames = {v: k for k, v in rename_map.items()}  # after_name -> before_name

    for col in sorted(before_names - after_names):
        if col in rename_map:
            new_name = rename_map[col]
            if new_name in after_cols:
                changes.append(ColumnChange(
                    table=table, column=f"{col}->{new_name}", classification="RENAMED",
                    before=before_cols[col], after=after_cols[new_name], blocking=False,
                    reason=f"declared rename in table contract ({col} -> {new_name})",
                ))
                continue
        changes.append(ColumnChange(
            table=table, column=col, classification="REMOVED", before=before_cols[col],
            after=None, blocking=True, reason="column removed",
        ))

    for col in sorted(after_names - before_names):
        if col in declared_renames and declared_renames[col] in before_names:
            continue  # already emitted as the RENAMED pair above
        changes.append(ColumnChange(
            table=table, column=col, classification="ADDED", before=None,
            after=after_cols[col], blocking=False, reason="column added",
        ))

    for col in sorted(before_names & after_names):
        b, a = before_cols[col], after_cols[col]
        if b == a:
            changes.append(ColumnChange(table=table, column=col, classification="UNCHANGED",
                                          before=b, after=a, blocking=False, reason=""))
            continue

        narrow = _is_narrowing(b, a)
        nullability_tightened = (b.get("nullable") is True and a.get("nullable") is False)

        # Nullability tightening is checked FIRST and independently of the
        # type-narrowing result: a same-type-family "safe" type comparison
        # (narrow == (False, "")) must not suppress a real NOT NULL
        # tightening on that same column — the two are orthogonal risks.
        if nullability_tightened:
            changes.append(ColumnChange(table=table, column=col, classification="MODIFIED",
                                          before=b, after=a, blocking=True,
                                          reason="column made NOT NULL — can reject rows with existing NULLs unless backfilled"))
        elif narrow is True or (isinstance(narrow, tuple) and narrow[0]):
            reason = narrow[1] if isinstance(narrow, tuple) else "type narrowed"
            changes.append(ColumnChange(table=table, column=col, classification="NARROWED",
                                          before=b, after=a, blocking=True, reason=reason))
        elif isinstance(narrow, tuple) and not narrow[0]:
            changes.append(ColumnChange(table=table, column=col, classification="WIDENED",
                                          before=b, after=a, blocking=False,
                                          reason="type widened or equivalent-safe change"))
        else:
            changes.append(ColumnChange(table=table, column=col, classification="MODIFIED",
                                          before=b, after=a, blocking=False,
                                          reason="column definition changed (type family, default, or generation expression)"))

    return changes


def diff_constraints(table: str, before: dict, after: dict) -> list[dict]:
    findings = []

    before_pk, after_pk = set(before.get("primary_key", [])), set(after.get("primary_key", []))
    if before_pk and not after_pk:
        findings.append({"kind": "primary_key_removed", "blocking": True,
                          "detail": f"primary key {sorted(before_pk)} removed"})
    elif before_pk and before_pk != after_pk and not before_pk <= after_pk:
        findings.append({"kind": "primary_key_weakened", "blocking": True,
                          "detail": f"primary key changed from {sorted(before_pk)} to {sorted(after_pk)}"})

    before_uniques = {frozenset(u) for u in before.get("unique_constraints", [])}
    after_uniques = {frozenset(u) for u in after.get("unique_constraints", [])}
    for removed in before_uniques - after_uniques:
        findings.append({"kind": "unique_constraint_removed", "blocking": True,
                          "detail": f"UNIQUE({', '.join(sorted(removed))}) removed"})

    before_checks = {(c["name"], c["definition"]) for c in before.get("check_constraints", [])}
    after_checks = {(c["name"], c["definition"]) for c in after.get("check_constraints", [])}
    for name, definition in before_checks - after_checks:
        still_present_by_name = any(n == name for n, _ in after_checks)
        if not still_present_by_name:
            findings.append({"kind": "check_constraint_removed", "blocking": True,
                              "detail": f"CHECK constraint {name!r} ({definition}) removed"})

    before_fks = {(fk["name"], tuple(fk["columns"]), fk["references_table"], tuple(fk["references_columns"]))
                  for fk in before.get("foreign_keys", [])}
    after_fks = {(fk["name"], tuple(fk["columns"]), fk["references_table"], tuple(fk["references_columns"]))
                 for fk in after.get("foreign_keys", [])}
    for name, cols, ref_table, ref_cols in before_fks - after_fks:
        findings.append({"kind": "foreign_key_removed", "blocking": True,
                          "detail": f"FK {name!r} ({', '.join(cols)} -> {ref_table}.{','.join(ref_cols)}) removed"})

    before_indexes = {(idx["name"]) for idx in before.get("indexes", [])}
    after_indexes = {(idx["name"]) for idx in after.get("indexes", [])}
    for name in before_indexes - after_indexes:
        findings.append({"kind": "index_removed", "blocking": False,
                          "detail": f"index {name!r} removed — advisory only (may be a deliberate reindex)"})

    return findings


def diff_schema(before_snapshot: dict, after_snapshot: dict, rename_maps: dict[str, dict] | None = None) -> SchemaDiffResult:
    """rename_maps: {table_name: {before_col: after_col}} sourced from
    table-contract files' declared renames, if any. Absent entirely by
    default — an undeclared rename is always REMOVED + ADDED."""
    rename_maps = rename_maps or {}
    before_tables = before_snapshot.get("tables", {})
    after_tables = after_snapshot.get("tables", {})
    results: list[TableChange] = []

    for table in sorted(set(before_tables) - set(after_tables)):
        results.append(TableChange(table=table, classification="REMOVED", column_changes=[], constraint_findings=[
            {"kind": "table_removed", "blocking": True, "detail": f"table {table!r} removed"}
        ]))

    for table in sorted(set(after_tables) - set(before_tables)):
        results.append(TableChange(table=table, classification="ADDED"))

    for table in sorted(set(before_tables) & set(after_tables)):
        b, a = before_tables[table], after_tables[table]
        col_changes = diff_columns(table, b.get("columns", {}), a.get("columns", {}), rename_maps.get(table))
        constraint_findings = diff_constraints(table, b, a)
        has_change = any(c.classification != "UNCHANGED" for c in col_changes) or bool(constraint_findings)
        results.append(TableChange(
            table=table, classification="MODIFIED" if has_change else "UNCHANGED",
            column_changes=col_changes, constraint_findings=constraint_findings,
        ))

    return SchemaDiffResult(table_changes=results)
