"""Domain-neutral Goal, Plan and Capability models."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lhas.domain.models import new_id


def _semantic_value(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    if isinstance(value, dict):
        return {str(key): _semantic_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_semantic_value(item) for item in value]
    return value


def compute_step_semantic_fingerprint(step: "PlanStep", by_id: dict[str, "PlanStep"] | None = None, _seen: set[str] | None = None) -> str:
    by_id = by_id or {}
    seen = set(_seen or set())
    dependency_semantics = []
    if step.id not in seen:
        seen.add(step.id)
        for dependency_id in step.depends_on:
            dependency = by_id.get(dependency_id)
            if dependency is None:
                dependency_semantics.append({"id": dependency_id})
            else:
                dependency_semantics.append({
                    "capability": _semantic_value(dependency.capability),
                    "objective": _semantic_value(dependency.objective),
                    "inputs": _semantic_value(dependency.inputs),
                    "depends_on": [
                        compute_step_semantic_fingerprint(dependency, by_id, seen)
                        if dependency_id not in seen else "cycle"
                    ],
                })
    payload = {
        "capability": _semantic_value(step.capability),
        "objective": _semantic_value(step.objective),
        "inputs": _semantic_value(step.inputs),
        "dependency_semantics": dependency_semantics,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


class PlanMode(str, Enum):
    LINEAR = "LINEAR"
    SIMPLE_DEPENDENCY = "SIMPLE_DEPENDENCY"


class PlanStatus(str, Enum):
    DRAFT = "DRAFT"
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    WAITING_FOR_HUMAN_APPROVAL = "WAITING_FOR_HUMAN_APPROVAL"


class PlanStepStatus(str, Enum):
    PENDING = "PENDING"
    PLANNED = "PLANNED"
    READY = "READY"
    RUNNING = "RUNNING"
    CLAIMED_COMPLETE = "CLAIMED_COMPLETE"
    COMPLETED = "COMPLETED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CLASSIFIED_FAILURE = "CLASSIFIED_FAILURE"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    WAITING_FOR_HUMAN_APPROVAL = "WAITING_FOR_HUMAN_APPROVAL"
    STALE = "STALE"


class Goal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    project_id: str
    objective: str = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    allowed_capabilities: list[str] = Field(default_factory=list)
    requires_human_approval: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class StepPrecondition(BaseModel):
    """A single precondition evaluated against execution state at dispatch time."""
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    operator: str = Field(default="eq", pattern="^(eq|neq|gt|lt|gte|lte|in|notin|truthy|falsy)$")
    value: Any = None
    description: str = ""


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    title: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    expected_output: str = ""
    success_criteria: list[str] = Field(default_factory=list)
    suggested_role: str = "WORKER"
    required_capabilities: list[str] = Field(default_factory=list)
    optional_skill_refs: list[str] = Field(default_factory=list)
    status: PlanStepStatus = PlanStepStatus.PENDING
    task_id: Optional[str] = None
    output: Any = None
    execution_context: dict[str, Any] = Field(default_factory=dict)
    semantic_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)

    # Phase 3 — Typed TaskGraph authority fields
    preconditions: list[StepPrecondition] = Field(default_factory=list)
    expected_effects: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    risk_class: str = "LOW"
    budget: dict[str, Any] = Field(default_factory=dict)
    checkpoint_policy: str = "ON_FAILURE"
    recovery_policy: str = "RETRY_WITH_FAILURE_CONTEXT"

    @model_validator(mode="after")
    def no_self_dependency(self) -> "PlanStep":
        if self.id in self.depends_on:
            raise ValueError("PlanStep cannot depend on itself")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("PlanStep depends_on must be unique")
        return self


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    goal_id: str
    version: str = "P-0.1"
    mode: PlanMode = PlanMode.LINEAR
    status: PlanStatus = PlanStatus.DRAFT
    steps: list[PlanStep] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    invalidated_step_ids: list[str] = Field(default_factory=list)
    replan_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_dependencies(self) -> "Plan":
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("Plan step ids must be unique")
        known = set(ids)
        for step in self.steps:
            missing = set(step.depends_on) - known
            if missing:
                raise ValueError(f"PlanStep {step.id} depends on unknown step(s): {sorted(missing)}")
        visiting: set[str] = set()
        visited: set[str] = set()
        by_id = {step.id: step for step in self.steps}

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise ValueError("Plan dependencies must be acyclic")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dep in by_id[step_id].depends_on:
                visit(dep)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in ids:
            visit(step_id)
        for step in self.steps:
            step.semantic_fingerprint = compute_step_semantic_fingerprint(step, by_id)
        return self


class CapabilitySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    risk_level: str = "LOW"
    side_effect: bool = False
    requires_human_approval: bool = False
    origin: str = "native"
    server_name: str | None = None


# ---------------------------------------------------------------------------
# Phase 3 — Centralized Eligibility & Transition Authority
# ---------------------------------------------------------------------------

# Status sets used by eligibility logic (canonical, no duplication elsewhere)
_TERMINAL_VERIFIED_STATUSES = frozenset({PlanStepStatus.VERIFIED, PlanStepStatus.COMPLETED})
_FAILED_OR_BLOCKED_STATUSES = frozenset({PlanStepStatus.FAILED, PlanStepStatus.BLOCKED, PlanStepStatus.CLASSIFIED_FAILURE})


def _evaluate_single_precondition(precondition: "StepPrecondition", execution_context: dict[str, Any]) -> bool:
    """Evaluate a single precondition against the current execution state."""
    # Navigate nested keys with dot notation
    value = execution_context
    for part in precondition.key.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            value = None
            break

    op = precondition.operator
    expected = precondition.value

    if op == "eq":
        return value == expected
    elif op == "neq":
        return value != expected
    elif op == "gt":
        return value is not None and value > expected
    elif op == "lt":
        return value is not None and value < expected
    elif op == "gte":
        return value is not None and value >= expected
    elif op == "lte":
        return value is not None and value <= expected
    elif op == "in":
        return value in (expected or [])
    elif op == "notin":
        return value not in (expected or [])
    elif op == "truthy":
        return bool(value)
    elif op == "falsy":
        return not bool(value)
    return False


def evaluate_step_preconditions(
    step: "PlanStep",
    execution_context: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Evaluate all preconditions for a step.

    Returns (all_passed, decision_context) where decision_context records
    each precondition's evaluation result for durable provenance.
    """
    if not step.preconditions:
        return True, {"preconditions": [], "all_passed": True}

    results = []
    all_passed = True
    for pc in step.preconditions:
        passed = _evaluate_single_precondition(pc, execution_context)
        results.append({
            "key": pc.key,
            "operator": pc.operator,
            "expected": pc.value,
            "passed": passed,
            "description": pc.description,
        })
        if not passed:
            all_passed = False

    return all_passed, {"preconditions": results, "all_passed": all_passed}


def evaluate_step_eligibility(
    step: "PlanStep",
    by_id: dict[str, "PlanStep"],
    execution_context: dict[str, Any] | None = None,
    event_store: Any | None = None,
    plan_id: str | None = None,
) -> tuple[bool, str]:
    """Single authoritative eligibility decision for a workflow step.

    This is the ONLY place where step eligibility is determined.
    All callers (scheduler, executor, service) must use this function.

    Returns (eligible: bool, reason: str).
    """
    execution_context = execution_context or {}

    # Already running or terminal — not eligible
    if step.status in {PlanStepStatus.RUNNING, PlanStepStatus.READY, PlanStepStatus.CLAIMED_COMPLETE}:
        return False, f"already_{step.status.value.lower()}"
    if step.status in _TERMINAL_VERIFIED_STATUSES:
        return False, "already_completed"
    if step.status in {PlanStepStatus.STALE, PlanStepStatus.CLASSIFIED_FAILURE, PlanStepStatus.PRECONDITION_FAILED}:
        return False, f"not_eligible_{step.status.value.lower()}"

    # INVARIANT 1 — DEPENDENCY AUTHORITY: all deps must be VERIFIED (or COMPLETED for backward compat)
    for dep_id in step.depends_on:
        dep = by_id.get(dep_id)
        if dep is None:
            return False, f"missing_dependency_{dep_id}"
        if dep.status in _FAILED_OR_BLOCKED_STATUSES:
            return False, f"dependency_{dep_id}_failed"
        if dep.status not in _TERMINAL_VERIFIED_STATUSES:
            return False, f"dependency_{dep_id}_not_verified"

    # INVARIANT 2 — PRECONDITION AUTHORITY: evaluate against current state
    pc_passed, _pc_ctx = evaluate_step_preconditions(step, execution_context)
    if not pc_passed:
        if event_store is not None and plan_id is not None:
            from lhas.domain.enums import EventType
            event_store.append(
                EventType.STEP_PRECONDITION_FAILED,
                payload={
                    "plan_id": plan_id,
                    "step_id": step.id,
                    "precondition_context": _pc_ctx,
                },
            )
        return False, "precondition_failed"

    return True, "all_conditions_met"


def transition_step(
    step: "PlanStep",
    new_status: PlanStepStatus,
    reason: str,
    event_store: Any,
    plan_id: str | None = None,
    extra_payload: dict[str, Any] | None = None,
) -> PlanStepStatus:
    """Record a durable state transition with provenance.

    This is the ONLY place where step status transitions are recorded.
    Every transition emits a STEP_STATE_TRANSITION event with full provenance.
    """
    old_status = step.status

    # Validate transition is not a no-op
    if old_status == new_status:
        return step.status

    step.status = new_status

    payload = {
        "step_id": step.id,
        "previous_status": old_status.value,
        "new_status": new_status.value,
        "reason": reason,
        "plan_id": plan_id,
    }
    if extra_payload:
        payload.update(extra_payload)

    from lhas.domain.enums import EventType
    event_store.append(EventType.STEP_STATE_TRANSITION, payload=payload)

    return new_status
