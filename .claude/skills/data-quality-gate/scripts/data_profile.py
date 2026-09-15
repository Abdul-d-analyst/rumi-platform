#!/usr/bin/env python3
"""Data-profile gate — deterministic, threshold-based comparison of the
proposed database state against the approved baseline for every affected
table/critical column. No LLM, no opaque score, ever used in the merge
decision (assignment requirement) — every number here is a plain SQL
aggregate or one of three documented statistical methods (tolerance, IQR,
robust z-score); PSI is supported for distributional comparisons where a
baseline category distribution is available.

Each check function takes an open DB connection (or its query results) and
returns a Finding — never raises for a genuine data anomaly (only for a
connection/query failure, which the caller must treat as ERROR/fail-closed
per introspect.py's IntrospectionError convention).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass


@dataclass
class ProfileFinding:
    table: str
    column: str | None
    check: str
    severity: str          # "info" | "warn" | "block"
    baseline_value: object
    proposed_value: object
    detail: str

    def to_dict(self) -> dict:
        return {
            "table": self.table, "column": self.column, "check": self.check,
            "severity": self.severity, "baseline_value": self.baseline_value,
            "proposed_value": self.proposed_value, "detail": self.detail,
        }


def check_row_count(table: str, baseline_count: int, proposed_count: int,
                     tolerance_pct: float, growth_only: bool = False) -> ProfileFinding | None:
    if baseline_count == 0:
        pct_change = float("inf") if proposed_count > 0 else 0.0
    else:
        pct_change = ((proposed_count - baseline_count) / baseline_count) * 100.0

    if growth_only and proposed_count >= baseline_count:
        return None  # transactional tables: growth is expected, never flagged

    if abs(pct_change) <= tolerance_pct:
        return None

    return ProfileFinding(
        table=table, column=None, check="row_count", severity="warn",
        baseline_value=baseline_count, proposed_value=proposed_count,
        detail=f"row count changed {pct_change:+.1f}% (tolerance ±{tolerance_pct}%)",
    )


def check_duplicate_business_key(table: str, key_columns: list[str], duplicate_count: int) -> ProfileFinding | None:
    if duplicate_count == 0:
        return None
    return ProfileFinding(
        table=table, column=",".join(key_columns), check="duplicate_business_key", severity="block",
        baseline_value=0, proposed_value=duplicate_count,
        detail=f"declared unique business key ({', '.join(key_columns)}) has {duplicate_count} duplicate value(s) — must be zero",
    )


def check_dim_stability(table: str, column: str, baseline_values: set, proposed_values: set) -> ProfileFinding | None:
    added = proposed_values - baseline_values
    removed = baseline_values - proposed_values
    if not added and not removed:
        return None
    return ProfileFinding(
        table=table, column=column, check="dim_stability", severity="block",
        baseline_value=sorted(baseline_values), proposed_value=sorted(proposed_values),
        detail=(
            f"stable dimension column's allowed value set changed"
            f"{f' (+{sorted(added)})' if added else ''}{f' (-{sorted(removed)})' if removed else ''}"
            f" — requires a declared contract change to permit"
        ),
    )


def check_null_rate(table: str, column: str, baseline_pct: float, proposed_pct: float,
                     tolerance_pct: float, block: bool) -> ProfileFinding | None:
    delta = proposed_pct - baseline_pct
    if abs(delta) <= tolerance_pct:
        return None
    severity = "block" if block else "warn"
    return ProfileFinding(
        table=table, column=column, check="null_rate", severity=severity,
        baseline_value=round(baseline_pct, 2), proposed_value=round(proposed_pct, 2),
        detail=f"null rate moved {delta:+.2f} points (tolerance ±{tolerance_pct}, benchmark {baseline_pct:.2f}%)",
    )


def check_fk_orphans(table: str, column: str, orphan_count: int) -> ProfileFinding | None:
    if orphan_count == 0:
        return None
    return ProfileFinding(
        table=table, column=column, check="fk_orphans", severity="block",
        baseline_value=0, proposed_value=orphan_count,
        detail=f"{orphan_count} row(s) reference a non-existent parent key",
    )


def check_junk_value_rate(table: str, column: str, junk_count: int, total_count: int,
                           threshold_pct: float, block: bool) -> ProfileFinding | None:
    if total_count == 0:
        return None
    pct = (junk_count / total_count) * 100.0
    if pct <= threshold_pct:
        return None
    return ProfileFinding(
        table=table, column=column, check="junk_value_rate",
        severity="block" if block else "warn",
        baseline_value=f"<= {threshold_pct}%", proposed_value=round(pct, 2),
        detail=f"{junk_count}/{total_count} ({pct:.2f}%) values match a configured junk pattern (threshold {threshold_pct}%)",
    )


# --------------------------------------------------------------- anomaly methods

def iqr_anomaly(baseline_values: list[float], proposed_value: float) -> bool:
    if len(baseline_values) < 4:
        return False  # not enough history for a meaningful IQR
    sorted_vals = sorted(baseline_values)
    q1 = statistics.median(sorted_vals[:len(sorted_vals) // 2])
    q3 = statistics.median(sorted_vals[(len(sorted_vals) + 1) // 2:])
    iqr = q3 - q1
    if iqr == 0:
        return proposed_value != q1
    lower, upper = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    return proposed_value < lower or proposed_value > upper


def robust_zscore_anomaly(baseline_values: list[float], proposed_value: float, threshold: float = 3.5) -> bool:
    if len(baseline_values) < 2:
        return False
    median = statistics.median(baseline_values)
    mad = statistics.median([abs(v - median) for v in baseline_values])
    if mad == 0:
        return proposed_value != median
    score = 0.6745 * (proposed_value - median) / mad
    return abs(score) > threshold


def psi(baseline_dist: dict[str, float], proposed_dist: dict[str, float], epsilon: float = 1e-4) -> float:
    """Population Stability Index over two category->proportion mappings.
    A PSI > 0.25 conventionally indicates significant distributional drift;
    callers decide the blocking threshold, this just computes the number."""
    categories = set(baseline_dist) | set(proposed_dist)
    total = 0.0
    for cat in categories:
        b = max(baseline_dist.get(cat, 0.0), epsilon)
        p = max(proposed_dist.get(cat, 0.0), epsilon)
        total += (p - b) * math.log(p / b)
    return total


def check_anomaly(table: str, column: str, baseline_values: list[float], proposed_value: float,
                   method: str, block: bool) -> ProfileFinding | None:
    if method == "iqr":
        is_anomalous = iqr_anomaly(baseline_values, proposed_value)
    elif method == "robust_zscore":
        is_anomalous = robust_zscore_anomaly(baseline_values, proposed_value)
    else:
        raise ValueError(f"check_anomaly does not handle method {method!r} directly — use psi() for distributional comparisons")

    if not is_anomalous:
        return None
    return ProfileFinding(
        table=table, column=column, check=f"anomaly_{method}",
        severity="block" if block else "warn",
        baseline_value=baseline_values, proposed_value=proposed_value,
        detail=f"{method} flagged {proposed_value} as anomalous vs. baseline history",
    )
