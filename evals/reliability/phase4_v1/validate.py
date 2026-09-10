"""Fail-closed validation and deterministic hashing for phase4-v1."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).parent
CANONICAL_FAMILIES = {
    "COMPLETION_INTEGRITY",
    "EXECUTION_STATE_RECOVERY",
    "COMPLEX_WORKFLOW_REPLAN",
    "PROVIDER_TOOL_FAILURE",
    "RUNTIME_TRUTH_POLICY",
    "DELEGATION_LIFECYCLE",
}
REQUIRED_TASK_FIELDS = {
    "benchmark_version", "task_id", "family", "title", "objective",
    "fixture_id", "fixture_version", "fixture_hash_source", "initial_state",
    "required_capabilities", "acceptance_criteria", "validator_id",
    "fault_injection", "fault_timing", "max_turns", "max_model_calls",
    "timeout_seconds", "side_effect_policy", "expected_observable_effects",
    "measurement_tags",
}
PROHIBITED_BIAS_FIELDS = {
    "expected_winner", "target_success_rate", "expected_odys_advantage",
    "resume_claim",
}
P3_FEATURES = {
    "completion_authority", "workflow_verifier", "typed_taskgraph",
    "failure_provenance", "selective_repair", "macro_replan",
    "durable_workflow_recovery",
}


class ProtocolError(ValueError):
    """Raised when a frozen protocol input is incomplete or drifts."""


def _read(name: str) -> Any:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _protocol_inputs() -> dict[str, Any]:
    inputs: dict[str, Any] = {}
    for relative in [
        "protocol.json", "manifest.json", "faults.json", "validators.json",
        "fixtures/catalog.json", "ablation.json", "schemas/result.schema.json",
    ]:
        inputs[relative] = _read(relative)
    for path in sorted((ROOT / "configs").glob("*.json")):
        inputs[f"configs/{path.name}"] = json.loads(path.read_text(encoding="utf-8"))
    return inputs


def protocol_hash(root: Path = ROOT) -> str:
    """Hash all frozen inputs with deterministic path and JSON ordering."""
    if root != ROOT:
        values: dict[str, Any] = {}
        for relative in [
            "protocol.json", "manifest.json", "faults.json", "validators.json",
            "fixtures/catalog.json", "ablation.json", "schemas/result.schema.json",
        ]:
            path = root / relative
            values[relative] = json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((root / "configs").glob("*.json")):
            values[f"configs/{path.name}"] = json.loads(path.read_text(encoding="utf-8"))
    else:
        values = _protocol_inputs()
    return hashlib.sha256(canonical_json(values)).hexdigest()


def _walk_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        keys.update(value)
        for child in value.values():
            keys.update(_walk_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_walk_keys(child))
    return keys


def validate_protocol(root: Path = ROOT) -> dict[str, Any]:
    """Validate every frozen machine-readable protocol invariant."""
    protocol = json.loads((root / "protocol.json").read_text(encoding="utf-8"))
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    faults = json.loads((root / "faults.json").read_text(encoding="utf-8"))
    validators = json.loads((root / "validators.json").read_text(encoding="utf-8"))
    catalog = json.loads((root / "fixtures" / "catalog.json").read_text(encoding="utf-8"))
    ablation = json.loads((root / "ablation.json").read_text(encoding="utf-8"))
    configs = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((root / "configs").glob("*.json"))
    }
    tasks = manifest.get("tasks", [])
    if protocol.get("benchmark_version") != "phase4-v1" or manifest.get("benchmark_version") != "phase4-v1":
        raise ProtocolError("benchmark version drift")
    if len(tasks) != 60:
        raise ProtocolError(f"expected 60 tasks, got {len(tasks)}")
    if {task.get("family") for task in tasks} != CANONICAL_FAMILIES:
        raise ProtocolError("canonical family set drift")
    task_ids = [task.get("task_id") for task in tasks]
    if len(task_ids) != len(set(task_ids)):
        raise ProtocolError("duplicate task id")
    for family in CANONICAL_FAMILIES:
        members = [task for task in tasks if task.get("family") == family]
        if len(members) != 10:
            raise ProtocolError(f"family {family} does not contain 10 tasks")
        if len({task["fault_injection"] for task in members}) < 3:
            raise ProtocolError(f"family {family} lacks fault diversity")
        if len({task["objective"] for task in members}) != 10:
            raise ProtocolError(f"family {family} has duplicate objectives")
    for task in tasks:
        missing = REQUIRED_TASK_FIELDS - set(task)
        if missing:
            raise ProtocolError(f"{task.get('task_id')} missing {sorted(missing)}")
        if task["benchmark_version"] != "phase4-v1":
            raise ProtocolError("task benchmark version drift")
        if task["fixture_id"] not in catalog.get("fixtures", {}):
            raise ProtocolError(f"unresolved fixture {task['fixture_id']}")
        if task["fault_injection"] not in {item["fault_id"] for item in faults.get("faults", [])}:
            raise ProtocolError(f"unresolved fault {task['fault_injection']}")
        if task["validator_id"] not in validators:
            raise ProtocolError(f"unresolved validator {task['validator_id']}")
        if _walk_keys(task) & PROHIBITED_BIAS_FIELDS:
            raise ProtocolError(f"bias field in {task['task_id']}")
    if protocol["headline"] != {
        "tasks": 60, "families": 6, "tasks_per_family": 10, "repeats": 3,
        "configs": ["minimal", "odys_p3"], "total_runs": 360,
    }:
        raise ProtocolError("headline count or configuration drift")
    if protocol["fairness"]["validator_id"] != protocol["shared_validator_id"]:
        raise ProtocolError("fairness validator drift")
    if any(config["tool_capability_set"] != protocol["fairness"]["tool_capability_set"] for config in configs.values()):
        raise ProtocolError("configuration capability-set fairness drift")
    if set(protocol["metric_definitions"]) != set(protocol["primary_metrics"]):
        raise ProtocolError("primary metric definition drift")
    if protocol["repeat_reporting"]["repeats"] != protocol["headline"]["repeats"]:
        raise ProtocolError("repeat reporting drift")
    if set(protocol["canonical_families"]) != CANONICAL_FAMILIES:
        raise ProtocolError("protocol family drift")
    if set(protocol["headline"]["configs"]) - set(configs):
        raise ProtocolError("unresolved headline config")
    if set(ablation["configs"]) - set(configs) or len(ablation["configs"]) != 4:
        raise ProtocolError("ablation config drift")
    ablation_ids = ablation["task_ids"]
    if len(ablation_ids) != 12 or len(set(ablation_ids)) != 12 or not set(ablation_ids) <= set(task_ids):
        raise ProtocolError("ablation task drift")
    for family in CANONICAL_FAMILIES:
        if sum(next(task for task in tasks if task["task_id"] == task_id)["family"] == family for task_id in ablation_ids) != 2:
            raise ProtocolError(f"ablation family coverage drift: {family}")
    if ablation["repeats"] != 3 or ablation["total_runs"] != 144:
        raise ProtocolError("ablation count drift")
    for fault in faults.get("faults", []):
        if any(key in fault for key in ("configuration", "configuration_name", "config_name")):
            raise ProtocolError(f"configuration-specific fault {fault['fault_id']}")
    minimal = configs["minimal"]
    if any(minimal["features"].get(feature) for feature in P3_FEATURES):
        raise ProtocolError("minimal exposes P3 machinery")
    odys = configs["odys_p3"]
    if not all(odys["features"].get(feature) for feature in P3_FEATURES):
        raise ProtocolError("odys_p3 does not explicitly enable P3 semantics")
    if any(config["validator_id"] != protocol["shared_validator_id"] for config in configs.values()):
        raise ProtocolError("validator is not shared")
    if not validators[protocol["shared_validator_id"]]["configuration_agnostic"]:
        raise ProtocolError("validator is configuration-aware")
    return {
        "benchmark_version": protocol["benchmark_version"],
        "task_count": len(tasks),
        "family_count": len(CANONICAL_FAMILIES),
        "headline_runs": protocol["headline"]["total_runs"],
        "ablation_runs": ablation["total_runs"],
        "protocol_hash": protocol_hash(root),
    }


def compare_fairness_identity(left: dict[str, Any], right: dict[str, Any]) -> bool:
    fields = ("benchmark_version", "manifest_hash", "fixture_hash", "validator_id", "fault_id", "model", "provider", "budget", "tool_capabilities")
    return all(left.get(field) == right.get(field) for field in fields)


def validate_raw_result(result: dict[str, Any], schema_path: Path = ROOT / "schemas" / "result.schema.json") -> None:
    import jsonschema
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.validate(result, schema)
