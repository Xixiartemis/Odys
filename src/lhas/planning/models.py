"""Domain-neutral Goal, Plan and Capability models."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lhas.domain.enums import FailureClass
from lhas.domain.models import new_id
from lhas.domain.enums import FailureClass, FailureType


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


class RepairScopeHint(str, Enum):
    """Scope of repair needed after a step failure."""
    LOCAL = "LOCAL"
    AFFECTED_SUBGRAPH = "AFFECTED_SUBGRAPH"
    MACRO_REPLAN = "MACRO_REPLAN"


def compute_repair_scope_hint(
    failure_class: FailureClass,
    failure_type: FailureType,
    has_downstream_deps: bool = False,
) -> RepairScopeHint:
    """Compute the repair scope hint from failure classification.

    Rules:
    - TOOL_FAILURE (TOOL_ERROR) with no downstream deps → LOCAL
    - VALIDATION_FAILURE (data/validation failures) → LOCAL (re-verify)
    - ASSUMPTION_INVALID (WRONG_ASSUMPTION) → AFFECTED_SUBGRAPH
    - PROVIDER_FAILURE / RESOURCE_EXHAUSTED → MACRO_REPLAN
    - Default → LOCAL
    """
    _MACRO_REPLAN_TYPES = frozenset({
        FailureType.QUOTA_EXHAUSTED,
        FailureType.BILLING_OR_CREDIT_EXHAUSTED,
        FailureType.AUTH_INVALID,
        FailureType.PROVIDER_UNAVAILABLE,
        FailureType.PROVIDER_TIMEOUT,
        FailureType.MALFORMED_PROVIDER_RESPONSE,
        FailureType.UNKNOWN_PROVIDER_FAILURE,
        FailureType.BUDGET_EXHAUSTED,
        FailureType.NETWORK_ERROR,
    })

    _AFFECTED_SUBGRAPH_TYPES = frozenset({
        FailureType.WRONG_ASSUMPTION,
        FailureType.STALE_CONTEXT,
        FailureType.CONTEXT_CONFLICT,
    })

    if failure_type in _MACRO_REPLAN_TYPES:
        return RepairScopeHint.MACRO_REPLAN
    if failure_type in _AFFECTED_SUBGRAPH_TYPES:
        return RepairScopeHint.AFFECTED_SUBGRAPH
    if failure_type == FailureType.TOOL_ERROR:
        return RepairScopeHint.LOCAL
    # VALIDATION_FAILURE and all other types → LOCAL
    return RepairScopeHint.LOCAL


class StepFailureProvenance(BaseModel):
    """Durable provenance linking a step failure to its classification.

    Stored in step.evidence['failure_provenance'] for persistence through
    plan save/reload cycles.
    """
    model_config = ConfigDict(extra="forbid")

    step_id: str
    plan_id: str
    failure_class: FailureClass
    failure_type: FailureType
    failure_evidence: dict[str, Any] = Field(default_factory=dict)
    attempt_id: str
    run_id: str
    repair_scope_hint: RepairScopeHint
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


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
    WAITING_FOR_VERIFICATION = "WAITING_FOR_VERIFICATION"


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
    WAITING_FOR_VERIFICATION = "WAITING_FOR_VERIFICATION"
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
# _TERMINAL_VERIFIED_STATUSES: both VERIFIED and COMPLETED are "done" — used
#   for skip-done and replan-preserves-work logic (read-compat / migration).
# _DEPENDENCY_SATISFIED_STATUSES: ONLY VERIFIED satisfies a workflow dependency.
#   COMPLETED is a legacy terminal state that must NOT unlock new work.
_TERMINAL_VERIFIED_STATUSES = frozenset({PlanStepStatus.VERIFIED, PlanStepStatus.COMPLETED})
_DEPENDENCY_SATISFIED_STATUSES = frozenset({PlanStepStatus.VERIFIED})
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
    evaluation_phase: str = "DISPATCH",
) -> tuple[bool, str]:
    """Single authoritative eligibility decision for a workflow step.

    This is the ONLY place where step eligibility is determined.
    All callers (scheduler, executor, service) must use this function.

    Args:
        evaluation_phase: "SCHEDULING" or "DISPATCH".
            SCHEDULING: runtime-only preconditions (key starts with "runtime.")
            are deferred — the scheduler has no runtime context, so these
            preconditions cannot be evaluated yet. They are assumed to pass
            at scheduling time and re-evaluated at dispatch time.
            DISPATCH: all preconditions are evaluated with full context.

    Returns (eligible: bool, reason: str).
    """
    execution_context = execution_context or {}

    # Already running or terminal — not eligible
    if step.status in {PlanStepStatus.RUNNING, PlanStepStatus.READY, PlanStepStatus.CLAIMED_COMPLETE, PlanStepStatus.WAITING_FOR_VERIFICATION}:
        return False, f"already_{step.status.value.lower()}"
    if step.status in _TERMINAL_VERIFIED_STATUSES:
        return False, "already_completed"
    if step.status in _FAILED_OR_BLOCKED_STATUSES | {PlanStepStatus.STALE, PlanStepStatus.PRECONDITION_FAILED}:
        return False, f"not_eligible_{step.status.value.lower()}"

    # INVARIANT 1 — DEPENDENCY AUTHORITY: all deps must be VERIFIED (not legacy COMPLETED)
    for dep_id in step.depends_on:
        dep = by_id.get(dep_id)
        if dep is None:
            return False, f"missing_dependency_{dep_id}"
        if dep.status in _FAILED_OR_BLOCKED_STATUSES:
            return False, f"dependency_{dep_id}_failed"
        if dep.status not in _DEPENDENCY_SATISFIED_STATUSES:
            return False, f"dependency_{dep_id}_not_verified"

    # INVARIANT 2 — PRECONDITION AUTHORITY: evaluate against current state
    # During SCHEDULING: defer runtime-only preconditions (key starts with "runtime.")
    # During DISPATCH: evaluate all preconditions with full context
    if evaluation_phase == "SCHEDULING":
        # Filter out runtime-only preconditions during scheduling
        schedulable_preconditions = [
            pc for pc in step.preconditions
            if not pc.key.startswith("runtime.")
        ]
        if schedulable_preconditions:
            from lhas.planning.models import StepPrecondition
            temp_step = step.model_copy(update={"preconditions": schedulable_preconditions})
            pc_passed, _pc_ctx = evaluate_step_preconditions(temp_step, execution_context)
        else:
            pc_passed, _pc_ctx = True, {"preconditions": [], "all_passed": True, "deferred": [pc.key for pc in step.preconditions if pc.key.startswith("runtime.")]}
    else:
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


# ---------------------------------------------------------------------------
# P3.3 — Repair Scope Authority
# ---------------------------------------------------------------------------

class RepairScope(str, Enum):
    """Repair scope decision for a failed step.

    LOCAL:              Only redo the failed step (no dependents or retryable).
    AFFECTED_SUBGRAPH:  Invalidate affected descendants, preserve others.
    MACRO_REPLAN:       Full plan revision via MacroReplanService.
    """

    LOCAL = "LOCAL"
    AFFECTED_SUBGRAPH = "AFFECTED_SUBGRAPH"
    MACRO_REPLAN = "MACRO_REPLAN"


# Failure types that indicate a systemic issue (provider down, quota exceeded).
# These warrant a full macro replan regardless of dependency graph.
_SYSTEMIC_FAILURE_CLASSES: frozenset[str] = frozenset({
    FailureClass.EXECUTION.value,
})

# Specific error_type strings that indicate systemic provider failure.
_SYSTEMIC_ERROR_TYPES: frozenset[str] = frozenset({
    "QUOTA_EXHAUSTED",
    "PROVIDER_UNAVAILABLE",
    "AUTH_INVALID",
    "BILLING_OR_CREDIT_EXHAUSTED",
    "UNKNOWN_PROVIDER_FAILURE",
    "PROVIDER_TIMEOUT",
})

# Error_type strings that indicate the failure invalidates downstream assumptions.
_ASSUMPTION_INVALIDATING_ERROR_TYPES: frozenset[str] = frozenset({
    "WRONG_ASSUMPTION",
    "ASSUMPTION_INVALID",
    "INVALID_ASSUMPTION",
    "STALE_CONTEXT",
    "CONTEXT_CONFLICT",
    "MISSING_CONTEXT",
    "CONTEXT_OVERLOAD",
})


def _get_dependents(step_id: str, plan: "Plan") -> set[str]:
    """Find all step IDs that directly or transitively depend on *step_id*."""
    # Build reverse dependency map
    reverse_deps: dict[str, set[str]] = {s.id: set() for s in plan.steps}
    for step in plan.steps:
        for dep_id in step.depends_on:
            if dep_id in reverse_deps:
                reverse_deps[dep_id].add(step.id)

    # BFS through reverse dependencies
    dependents: set[str] = set()
    queue = list(reverse_deps.get(step_id, set()))
    while queue:
        current = queue.pop(0)
        if current not in dependents:
            dependents.add(current)
            queue.extend(reverse_deps.get(current, set()) - dependents)
    return dependents


def compute_repair_scope(
    failed_step: "PlanStep",
    plan: "Plan",
    failure_class: FailureClass | str | None = None,
    error_type: str | None = None,
) -> tuple[RepairScope, set[str]]:
    """Determine the repair scope for a failed step.

    Decision matrix:
    1. No dependents → LOCAL
    2. Has dependents + failure is retryable (not assumption-invalidating) → LOCAL
    3. Has dependents + failure invalidates assumptions → AFFECTED_SUBGRAPH
    4. Systemic failure (provider down, quota) → MACRO_REPLAN

    Args:
        failed_step: The step that failed.
        plan: The current plan (for dependency graph traversal).
        failure_class: Optional FailureClass enum or string value.
        error_type: Optional specific error type string for finer classification.

    Returns:
        (RepairScope, set of affected step_ids).
        For LOCAL: affected set contains only the failed step.
        For AFFECTED_SUBGRAPH: affected set contains the failed step + all
            transitively dependent steps.
        For MACRO_REPLAN: affected set is empty (full replan handles it).
    """
    dependents = _get_dependents(failed_step.id, plan)

    # Normalize failure_class to string for comparison
    fc_value = None
    if failure_class is not None:
        fc_value = failure_class.value if isinstance(failure_class, FailureClass) else str(failure_class)

    # Normalize error_type
    et_value = str(error_type).upper() if error_type else None

    # 1. No dependents → LOCAL (always)
    if not dependents:
        return RepairScope.LOCAL, {failed_step.id}

    # 2. Systemic failure → MACRO_REPLAN
    if et_value and et_value in _SYSTEMIC_ERROR_TYPES:
        return RepairScope.MACRO_REPLAN, set()
    if fc_value and fc_value in _SYSTEMIC_FAILURE_CLASSES and et_value and et_value in _SYSTEMIC_ERROR_TYPES:
        return RepairScope.MACRO_REPLAN, set()

    # 3. Failure invalidates assumptions → AFFECTED_SUBGRAPH
    if et_value and et_value in _ASSUMPTION_INVALIDATING_ERROR_TYPES:
        return RepairScope.AFFECTED_SUBGRAPH, {failed_step.id} | dependents

    # If failure_class is CONTEXT or REASONING with dependents, assume invalidation
    if fc_value in {FailureClass.CONTEXT.value, FailureClass.REASONING.value}:
        return RepairScope.AFFECTED_SUBGRAPH, {failed_step.id} | dependents

    # 4. Has dependents but failure is retryable (transient/execution error) → LOCAL
    # Default: if we can't classify, be conservative with AFFECTED_SUBGRAPH
    # Only default to LOCAL if we have positive evidence it's retryable
    if fc_value == FailureClass.EXECUTION.value:
        # Execution failures (timeout, crash, network) are retryable
        return RepairScope.LOCAL, {failed_step.id}

    # Unknown failure with dependents → LOCAL (retry first, escalate if repeated)
    return RepairScope.LOCAL, {failed_step.id}


def invalidate_affected_subgraph(
    plan: "Plan",
    affected_step_ids: set[str],
    event_store: Any,
) -> set[str]:
    """Mark affected steps as STALE, preserving unrelated VERIFIED work.

    Steps in *affected_step_ids* that are not already terminal (VERIFIED,
    COMPLETED, or STALE) will be transitioned to STALE via transition_step().
    Steps already STALE are also counted as invalidated.

    Args:
        plan: The current plan.
        affected_step_ids: Set of step IDs to invalidate.
        event_store: EventStore for recording transitions.

    Returns:
        Set of step IDs that were actually invalidated (transitioned or
        already STALE).
    """
    invalidated: set[str] = set()
    by_id = {s.id: s for s in plan.steps}

    for step_id in affected_step_ids:
        step = by_id.get(step_id)
        if step is None:
            continue
        # Already STALE — count but don't re-transition
        if step.status == PlanStepStatus.STALE:
            invalidated.add(step_id)
            continue
        # Already terminal (VERIFIED/COMPLETED) — still invalidate (mark STALE)
        # because the failure of a dependency invalidates this step's output.
        if step.status in _TERMINAL_VERIFIED_STATUSES:
            transition_step(step, PlanStepStatus.STALE, "repair_scope_invalidation", event_store, plan_id=plan.id)
            invalidated.add(step_id)
            continue
        # Non-terminal, non-STALE: mark STALE
        if step.status not in {PlanStepStatus.STALE}:
            transition_step(step, PlanStepStatus.STALE, "repair_scope_invalidation", event_store, plan_id=plan.id)
            invalidated.add(step_id)

    return invalidated
