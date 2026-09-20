"""Bounded projection of the durable plan step used by native execution.

The planner is authoritative for the step that is currently being executed.
This module deliberately exposes only the fields needed by the model/tool
boundary; it does not copy the full PlanStep or execution context into the
prompt.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any


def active_step_execution_contract(step: Any, plan: Any | None = None) -> dict[str, Any]:
    """Return the model-visible contract for one durable ``PlanStep``."""

    return {
        "plan_id": str(getattr(plan, "id", "")) if plan is not None else "",
        "plan_version": str(getattr(plan, "version", "")) if plan is not None else "",
        "step_id": str(step.id),
        "objective": str(step.objective),
        "capability": str(step.capability),
        "inputs": dict(step.inputs or {}),
        "success_criteria": list(step.success_criteria or []),
        "expected_effects": dict(step.expected_effects or {}),
    }


def contract_from_context(context: Mapping[str, Any]) -> dict[str, Any] | None:
    """Read a previously projected active-step contract without broadening it.

    ``active_step_contract`` is the canonical location.  The taskgraph copy is
    accepted for callers that only persist the graph projection, but both
    locations are normalized to the same bounded shape.
    """

    candidate: Any = context.get("active_step_contract")
    if not isinstance(candidate, Mapping):
        graph = context.get("taskgraph")
        candidate = graph.get("active_step_contract") if isinstance(graph, Mapping) else None
    if not isinstance(candidate, Mapping):
        return None
    capability = candidate.get("capability")
    step_id = candidate.get("step_id")
    if not isinstance(capability, str) or not capability or not isinstance(step_id, str) or not step_id:
        return None
    return {
        "plan_id": str(candidate.get("plan_id", "")),
        "plan_version": str(candidate.get("plan_version", "")),
        "step_id": step_id,
        "objective": str(candidate.get("objective", "")),
        "capability": capability,
        "inputs": dict(candidate.get("inputs", {})) if isinstance(candidate.get("inputs", {}), Mapping) else {},
        "success_criteria": list(candidate.get("success_criteria", [])) if isinstance(candidate.get("success_criteria", []), list) else [],
        "expected_effects": dict(candidate.get("expected_effects", {})) if isinstance(candidate.get("expected_effects", {}), Mapping) else {},
    }


def contract_inputs_sha256(contract: Mapping[str, Any]) -> str:
    """Hash planner-owned inputs without persisting their raw values."""

    encoded = json.dumps(
        dict(contract.get("inputs", {})),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def contract_telemetry(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Return bounded accepted/active contract identity telemetry."""

    input_hash = contract_inputs_sha256(contract)
    return {
        "accepted_plan_id": str(contract.get("plan_id", "")),
        "accepted_plan_version": str(contract.get("plan_version", "")),
        "accepted_plan_step_id": str(contract.get("step_id", "")),
        "accepted_plan_step_capability": str(contract.get("capability", "")),
        "accepted_plan_step_inputs_sha256": input_hash,
        "active_execution_plan_id": str(contract.get("plan_id", "")),
        "active_execution_plan_version": str(contract.get("plan_version", "")),
        "active_execution_step_id": str(contract.get("step_id", "")),
        "active_execution_capability": str(contract.get("capability", "")),
        "active_execution_inputs_sha256": input_hash,
    }
