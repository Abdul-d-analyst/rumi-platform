#!/usr/bin/env python3
"""Config loading + strict validation for the V1 Data Quality Gate.

Every config file under .data-quality-gate/ is validated on load. Malformed
configuration is a hard failure (raises ConfigError), never a silent
skip — an unreadable table contract must not quietly fall back to "no
contract" behavior, because that would let a broken YAML file silently
disable enforcement for a table that thinks it's protected.

This module has no side effects beyond reading files; scripts/gate.py is
the only thing that decides what a validation failure means for exit code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as e:  # pragma: no cover - environment problem, not a logic path
    raise SystemExit("Missing dependency: pyyaml. Install with: pip install pyyaml") from e


class ConfigError(Exception):
    """Raised for any malformed .data-quality-gate/** configuration file."""


_ALLOWED_ANOMALY_METHODS = {"iqr", "robust_zscore", "psi"}
_ALLOWED_NULLABILITY = {"not_null", "nullable"}
_ALLOWED_CRITICALITY = {"critical", "normal"}
_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


def _load_yaml(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"malformed YAML in {path}: {e}") from e


# --------------------------------------------------------------- table contract

@dataclass
class TableContract:
    table: str
    allowed_columns: set[str]
    expected_types: dict[str, str]
    nullability: dict[str, str]
    criticality: dict[str, str]
    primary_key: list[str]
    unique: list[list[str]]
    foreign_keys: list[dict]
    sensitivity: dict[str, dict]
    profile: dict
    allowed_tolerances: dict
    source_path: Path


def load_table_contract(path: Path) -> TableContract:
    data = _load_yaml(path)
    _require(isinstance(data, dict), f"{path}: top-level must be a mapping")
    _require(isinstance(data.get("table"), str) and data["table"], f"{path}: 'table' is required and must be a non-empty string")
    _require(_IDENT_RE.match(data["table"]), f"{path}: 'table' must be a valid SQL identifier, got {data['table']!r}")

    columns = data.get("columns", {}) or {}
    _require(isinstance(columns, dict), f"{path}: 'columns' must be a mapping")

    allowed = columns.get("allowed", [])
    _require(isinstance(allowed, list) and all(isinstance(c, str) for c in allowed),
             f"{path}: columns.allowed must be a list of strings")

    expected_types = columns.get("expected_types", {}) or {}
    _require(isinstance(expected_types, dict), f"{path}: columns.expected_types must be a mapping")

    nullability = columns.get("nullability", {}) or {}
    _require(isinstance(nullability, dict), f"{path}: columns.nullability must be a mapping")
    for col, val in nullability.items():
        _require(val in _ALLOWED_NULLABILITY, f"{path}: columns.nullability.{col} must be one of {_ALLOWED_NULLABILITY}, got {val!r}")

    criticality = columns.get("criticality", {}) or {}
    _require(isinstance(criticality, dict), f"{path}: columns.criticality must be a mapping")
    for col, val in criticality.items():
        _require(val in _ALLOWED_CRITICALITY, f"{path}: columns.criticality.{col} must be one of {_ALLOWED_CRITICALITY}, got {val!r}")

    keys = data.get("keys", {}) or {}
    _require(isinstance(keys, dict), f"{path}: 'keys' must be a mapping")
    primary_key = keys.get("primary_key", []) or []
    _require(isinstance(primary_key, list), f"{path}: keys.primary_key must be a list")
    unique = keys.get("unique", []) or []
    _require(isinstance(unique, list) and all(isinstance(u, list) for u in unique),
             f"{path}: keys.unique must be a list of lists")
    foreign_keys = keys.get("foreign_keys", []) or []
    _require(isinstance(foreign_keys, list), f"{path}: keys.foreign_keys must be a list")
    for fk in foreign_keys:
        _require(isinstance(fk, dict) and {"columns", "references_table", "references_columns"} <= fk.keys(),
                  f"{path}: each foreign_keys entry needs columns, references_table, references_columns")

    sensitivity = data.get("sensitivity", {}) or {}
    _require(isinstance(sensitivity, dict), f"{path}: 'sensitivity' must be a mapping")

    profile = data.get("profile", {}) or {}
    _require(isinstance(profile, dict), f"{path}: 'profile' must be a mapping")
    if "anomaly" in profile:
        method = profile["anomaly"].get("method") if isinstance(profile["anomaly"], dict) else None
        _require(method in _ALLOWED_ANOMALY_METHODS, f"{path}: profile.anomaly.method must be one of {_ALLOWED_ANOMALY_METHODS}")

    allowed_tolerances = data.get("allowed_tolerances", {}) or {}
    _require(isinstance(allowed_tolerances, dict), f"{path}: 'allowed_tolerances' must be a mapping")

    return TableContract(
        table=data["table"],
        allowed_columns=set(allowed),
        expected_types=expected_types,
        nullability=nullability,
        criticality=criticality,
        primary_key=list(primary_key),
        unique=[list(u) for u in unique],
        foreign_keys=list(foreign_keys),
        sensitivity=sensitivity,
        profile=profile,
        allowed_tolerances=allowed_tolerances,
        source_path=path,
    )


def load_all_table_contracts(contracts_dir: Path) -> dict[str, TableContract]:
    """Loads every <table>.yaml under contracts_dir. Files ending in
    .disabled are templates, not active contracts, and are skipped. Any
    other malformed .yaml file is a hard ConfigError — never silently
    skipped, per the assignment's requirement."""
    contracts: dict[str, TableContract] = {}
    if not contracts_dir.exists():
        return contracts
    for path in sorted(contracts_dir.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        contract = load_table_contract(path)
        _require(contract.table not in contracts,
                  f"{path}: duplicate contract for table {contract.table!r} (already defined in {contracts.get(contract.table)})")
        contracts[contract.table] = contract
    return contracts


# --------------------------------------------------------------- data-profile.yaml (global defaults)

@dataclass
class DataProfileConfig:
    row_count_tolerance_pct: float
    null_rate_tolerance_pct: float
    anomaly_method: str
    anomaly_block: bool
    advisory_rollout: dict


_ALLOWED_TOP_LEVEL_KEYS = {"defaults", "advisory_rollout"}
_ALLOWED_DEFAULTS_KEYS = {
    "row_count_tolerance_pct", "null_rate_tolerance_pct", "anomaly_method", "anomaly_block",
}


def load_data_profile_config(path: Path) -> DataProfileConfig:
    if not path.exists():
        raise ConfigError(f"{path}: required global data-profile config is missing")
    data = _load_yaml(path)
    _require(isinstance(data, dict), f"{path}: top-level must be a mapping")
    unknown = set(data.keys()) - _ALLOWED_TOP_LEVEL_KEYS
    _require(not unknown, f"{path}: unknown top-level key(s) {unknown}")

    defaults = data.get("defaults", {}) or {}
    _require(isinstance(defaults, dict), f"{path}: 'defaults' must be a mapping")
    unknown_defaults = set(defaults.keys()) - _ALLOWED_DEFAULTS_KEYS
    _require(not unknown_defaults, f"{path}: unknown defaults key(s) {unknown_defaults}")

    method = defaults.get("anomaly_method", "iqr")
    _require(method in _ALLOWED_ANOMALY_METHODS, f"{path}: defaults.anomaly_method must be one of {_ALLOWED_ANOMALY_METHODS}")

    return DataProfileConfig(
        row_count_tolerance_pct=float(defaults.get("row_count_tolerance_pct", 10.0)),
        null_rate_tolerance_pct=float(defaults.get("null_rate_tolerance_pct", 5.0)),
        anomaly_method=method,
        anomaly_block=bool(defaults.get("anomaly_block", False)),
        advisory_rollout=data.get("advisory_rollout", {}) or {},
    )


# --------------------------------------------------------------- change.yaml (evidence exception)

@dataclass
class ChangeEvidence:
    purpose: str
    ticket: str
    affected_objects: list[dict]
    reason: str
    dependency_scan: str
    archive_evidence: str
    validation_evidence: str
    recovery_plan: str
    attestation: dict
    source_path: Path


_REQUIRED_CHANGE_KEYS = [
    "purpose", "ticket", "affected_objects", "reason", "dependency_scan",
    "archive_evidence", "validation_evidence", "recovery_plan", "attestation",
]
_REQUIRED_NARRATIVE_KEYS = [
    "purpose", "reason", "dependency_scan", "archive_evidence",
    "validation_evidence", "recovery_plan",
]
_MIN_NARRATIVE_LEN = 15  # presence-check floor, not a quality judgment — see reference/evidence-exception.md


def load_change_evidence(path: Path) -> ChangeEvidence:
    data = _load_yaml(path)
    _require(isinstance(data, dict), f"{path}: top-level must be a mapping")
    missing = [k for k in _REQUIRED_CHANGE_KEYS if k not in data]
    _require(not missing, f"{path}: missing required key(s) {missing}")

    for key in _REQUIRED_NARRATIVE_KEYS:
        val = data.get(key)
        _require(isinstance(val, str), f"{path}: '{key}' must be a string")
        _require(len(val.strip()) >= _MIN_NARRATIVE_LEN,
                  f"{path}: '{key}' is present but too short ({len(val.strip())} chars) to be evidence, not a placeholder")

    objs = data["affected_objects"]
    _require(isinstance(objs, list) and len(objs) > 0, f"{path}: 'affected_objects' must be a non-empty list")
    for i, obj in enumerate(objs):
        _require(isinstance(obj, dict), f"{path}: affected_objects[{i}] must be a mapping")
        _require(isinstance(obj.get("table"), str) and obj["table"], f"{path}: affected_objects[{i}].table is required")
        _require(obj.get("change_type") in {"REMOVED", "NARROWED", "MODIFIED"},
                  f"{path}: affected_objects[{i}].change_type must be REMOVED, NARROWED, or MODIFIED")
        _require("before" in obj, f"{path}: affected_objects[{i}] missing 'before'")
        _require("after" in obj, f"{path}: affected_objects[{i}] missing 'after'")

    attestation = data["attestation"]
    _require(isinstance(attestation, dict), f"{path}: 'attestation' must be a mapping")
    for k in ("owner", "date", "statement"):
        _require(k in attestation and isinstance(attestation[k], str) and attestation[k].strip(),
                  f"{path}: attestation.{k} is required and must be non-empty")

    return ChangeEvidence(
        purpose=data["purpose"], ticket=str(data["ticket"]), affected_objects=objs,
        reason=data["reason"], dependency_scan=data["dependency_scan"],
        archive_evidence=data["archive_evidence"], validation_evidence=data["validation_evidence"],
        recovery_plan=data["recovery_plan"], attestation=attestation, source_path=path,
    )
