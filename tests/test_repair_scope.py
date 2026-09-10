"""Tests for P3.3 Repair Scope Authority.

B5: Tests for compute_repair_scope, invalidate_affected_subgraph, and
RepairScope integration.
"""

import asyncio

from lhas.domain.enums import FailureClass
from lhas.domain.models import Project
from lhas.persistence.event_store import EventStore
from lhas.persistence.planning_repositories import PlanRepository
from lhas.persistence.repositories import ProjectRepository
from lhas.planning.models import (
    Goal, Plan, PlanMode, PlanStep, PlanStepStatus, PlanStatus,
    RepairScope, compute_repair_scope, invalidate_affected_subgraph,
    transition_step,
)
from lhas.planning.scheduler import TaskGraphScheduler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_plan(steps, mode=PlanMode.SIMPLE_DEPENDENCY):
    """Build a Plan from a list of (id, depends_on, status) tuples."""
    plan_steps = []
    for spec in steps:
        sid = spec[0]
        deps = spec[1] if len(spec) > 1 else []
        status = spec[2] if len(spec) > 2 else PlanStepStatus.PENDING
        plan_steps.append(PlanStep(
            id=sid, title=sid, objective=sid, capability=sid,
            depends_on=deps, status=status,
        ))
    return Plan(
        id="test-plan", goal_id="test-goal", mode=mode,
        status=PlanStatus.RUNNING, steps=plan_steps,
    )


# ---------------------------------------------------------------------------
# B5.1: compute_repair_scope — LOCAL for step with no dependents
# ---------------------------------------------------------------------------

def test_local_scope_no_dependents():
    """Step with no dependents → LOCAL, only failed step in affected set."""
    plan = _make_plan([
        ("a", [], PlanStepStatus.FAILED),
        ("b", [], PlanStepStatus.PENDING),
    ])
    step_a = plan.steps[0]
    scope, affected = compute_repair_scope(step_a, plan)
    assert scope == RepairScope.LOCAL
    assert affected == {"a"}


def test_local_scope_no_dependents_with_error_type():
    """Systemic error_type → MACRO_REPLAN even without dependents."""
    plan = _make_plan([
        ("a", [], PlanStepStatus.FAILED),
    ])
    step_a = plan.steps[0]
    scope, affected = compute_repair_scope(step_a, plan, error_type="QUOTA_EXHAUSTED")
    assert scope == RepairScope.MACRO_REPLAN
    assert affected == set()


# ---------------------------------------------------------------------------
# B5.2: compute_repair_scope — LOCAL for retryable failure with dependents
# ---------------------------------------------------------------------------

def test_local_scope_retryable_execution_failure():
    """Execution failure with dependents → LOCAL (retryable)."""
    plan = _make_plan([
        ("a", [], PlanStepStatus.FAILED),
        ("b", ["a"], PlanStepStatus.PENDING),
    ])
    step_a = plan.steps[0]
    scope, affected = compute_repair_scope(
        step_a, plan, failure_class=FailureClass.EXECUTION, error_type="TIMEOUT",
    )
    assert scope == RepairScope.LOCAL
    assert affected == {"a"}


def test_local_scope_retryable_no_failure_class():
    """No failure class, no error type, but execution class → LOCAL."""
    plan = _make_plan([
        ("a", [], PlanStepStatus.FAILED),
        ("b", ["a"], PlanStepStatus.PENDING),
    ])
    step_a = plan.steps[0]
    scope, affected = compute_repair_scope(
        step_a, plan, failure_class=FailureClass.EXECUTION,
    )
    assert scope == RepairScope.LOCAL
    assert affected == {"a"}


# ---------------------------------------------------------------------------
# B5.3: compute_repair_scope — AFFECTED_SUBGRAPH for assumption invalidation
# ---------------------------------------------------------------------------

def test_subgraph_scope_wrong_assumption():
    """WRONG_ASSUMPTION with dependents → AFFECTED_SUBGRAPH."""
    plan = _make_plan([
        ("a", [], PlanStepStatus.FAILED),
        ("b", ["a"], PlanStepStatus.VERIFIED),
        ("c", ["b"], PlanStepStatus.PENDING),
        ("d", [], PlanStepStatus.VERIFIED),  # unrelated
    ])
    step_a = plan.steps[0]
    scope, affected = compute_repair_scope(step_a, plan, error_type="WRONG_ASSUMPTION")
    assert scope == RepairScope.AFFECTED_SUBGRAPH
    assert affected == {"a", "b", "c"}  # a + transitive dependents
    assert "d" not in affected  # unrelated step preserved


def test_subgraph_scope_context_failure_class():
    """CONTEXT failure class with dependents → AFFECTED_SUBGRAPH."""
    plan = _make_plan([
        ("a", [], PlanStepStatus.FAILED),
        ("b", ["a"], PlanStepStatus.PENDING),
    ])
    step_a = plan.steps[0]
    scope, affected = compute_repair_scope(
        step_a, plan, failure_class=FailureClass.CONTEXT,
    )
    assert scope == RepairScope.AFFECTED_SUBGRAPH
    assert affected == {"a", "b"}


def test_subgraph_scope_stale_context():
    """STALE_CONTEXT → AFFECTED_SUBGRAPH."""
    plan = _make_plan([
        ("a", [], PlanStepStatus.FAILED),
        ("b", ["a"], PlanStepStatus.VERIFIED),
    ])
    step_a = plan.steps[0]
    scope, affected = compute_repair_scope(step_a, plan, error_type="STALE_CONTEXT")
    assert scope == RepairScope.AFFECTED_SUBGRAPH
    assert affected == {"a", "b"}


# ---------------------------------------------------------------------------
# B5.4: compute_repair_scope — MACRO_REPLAN for systemic failures
# ---------------------------------------------------------------------------

def test_macro_replan_scope_quota_exhausted():
    """QUOTA_EXHAUSTED with dependents → MACRO_REPLAN."""
    plan = _make_plan([
        ("a", [], PlanStepStatus.FAILED),
        ("b", ["a"], PlanStepStatus.PENDING),
    ])
    step_a = plan.steps[0]
    scope, affected = compute_repair_scope(step_a, plan, error_type="QUOTA_EXHAUSTED")
    assert scope == RepairScope.MACRO_REPLAN
    assert affected == set()


def test_macro_replan_scope_provider_unavailable():
    """PROVIDER_UNAVAILABLE → MACRO_REPLAN."""
    plan = _make_plan([
        ("a", [], PlanStepStatus.FAILED),
        ("b", ["a"], PlanStepStatus.PENDING),
    ])
    step_a = plan.steps[0]
    scope, affected = compute_repair_scope(step_a, plan, error_type="PROVIDER_UNAVAILABLE")
    assert scope == RepairScope.MACRO_REPLAN
    assert affected == set()


def test_macro_replan_scope_auth_invalid():
    """AUTH_INVALID → MACRO_REPLAN."""
    plan = _make_plan([
        ("a", [], PlanStepStatus.FAILED),
        ("b", ["a"], PlanStepStatus.PENDING),
    ])
    step_a = plan.steps[0]
    scope, affected = compute_repair_scope(step_a, plan, error_type="AUTH_INVALID")
    assert scope == RepairScope.MACRO_REPLAN
    assert affected == set()


# ---------------------------------------------------------------------------
# B5.5: compute_repair_scope — transitive dependents
# ---------------------------------------------------------------------------

def test_transitive_dependents():
    """AFFECTED_SUBGRAPH includes all transitive dependents."""
    plan = _make_plan([
        ("a", [], PlanStepStatus.FAILED),
        ("b", ["a"], PlanStepStatus.VERIFIED),
        ("c", ["b"], PlanStepStatus.PENDING),
        ("d", ["c"], PlanStepStatus.PENDING),
        ("e", [], PlanStepStatus.VERIFIED),  # unrelated
    ])
    step_a = plan.steps[0]
    scope, affected = compute_repair_scope(step_a, plan, error_type="WRONG_ASSUMPTION")
    assert scope == RepairScope.AFFECTED_SUBGRAPH
    assert affected == {"a", "b", "c", "d"}
    assert "e" not in affected


# ---------------------------------------------------------------------------
# B5.6: invalidate_affected_subgraph — marks steps STALE
# ---------------------------------------------------------------------------

def test_invalidate_subgraph_marks_stale(db):
    """invalidate_affected_subgraph marks specified steps as STALE."""
    events = EventStore(db)
    plan = _make_plan([
        ("a", [], PlanStepStatus.FAILED),
        ("b", ["a"], PlanStepStatus.PENDING),
        ("c", ["a"], PlanStepStatus.VERIFIED),
        ("d", [], PlanStepStatus.VERIFIED),  # unrelated
    ])
    invalidated = invalidate_affected_subgraph(plan, {"a", "b", "c"}, events)
    assert invalidated == {"a", "b", "c"}
    by_id = {s.id: s for s in plan.steps}
    assert by_id["a"].status == PlanStepStatus.STALE
    assert by_id["b"].status == PlanStepStatus.STALE
    assert by_id["c"].status == PlanStepStatus.STALE
    assert by_id["d"].status == PlanStepStatus.VERIFIED  # preserved


# ---------------------------------------------------------------------------
# B5.7: invalidate_affected_subgraph — preserves VERIFIED not in affected set
# ---------------------------------------------------------------------------

def test_invalidate_preserves_unrelated_verified(db):
    """VERIFIED steps outside the affected set are untouched."""
    events = EventStore(db)
    plan = _make_plan([
        ("a", [], PlanStepStatus.FAILED),
        ("b", ["a"], PlanStepStatus.PENDING),
        ("unrelated-1", [], PlanStepStatus.VERIFIED),
        ("unrelated-2", [], PlanStepStatus.VERIFIED),
    ])
    invalidated = invalidate_affected_subgraph(plan, {"a", "b"}, events)
    assert invalidated == {"a", "b"}
    by_id = {s.id: s for s in plan.steps}
    assert by_id["unrelated-1"].status == PlanStepStatus.VERIFIED
    assert by_id["unrelated-2"].status == PlanStepStatus.VERIFIED


# ---------------------------------------------------------------------------
# B5.8: invalidate_affected_subgraph — already STALE counted, not re-transitioned
# ---------------------------------------------------------------------------

def test_invalidate_already_stale(db):
    """Already STALE steps are counted but not re-transitioned."""
    events = EventStore(db)
    plan = _make_plan([
        ("a", [], PlanStepStatus.STALE),
        ("b", [], PlanStepStatus.PENDING),
    ])
    invalidated = invalidate_affected_subgraph(plan, {"a", "b"}, events)
    assert invalidated == {"a", "b"}
    by_id = {s.id: s for s in plan.steps}
    assert by_id["a"].status == PlanStepStatus.STALE
    assert by_id["b"].status == PlanStepStatus.STALE


# ---------------------------------------------------------------------------
# B5.9: compute_repair_scope — unknown failure class with dependents
# ---------------------------------------------------------------------------

def test_unknown_failure_class_conservative():
    """Unknown failure with dependents → LOCAL (retry first, escalate if repeated)."""
    plan = _make_plan([
        ("a", [], PlanStepStatus.FAILED),
        ("b", ["a"], PlanStepStatus.PENDING),
    ])
    step_a = plan.steps[0]
    scope, affected = compute_repair_scope(step_a, plan)  # no failure_class, no error_type
    assert scope == RepairScope.LOCAL
    assert affected == {"a"}


# ---------------------------------------------------------------------------
# B5.10: invalidate_affected_subgraph — empty affected set
# ---------------------------------------------------------------------------

def test_invalidate_empty_set(db):
    """Empty affected set → nothing invalidated."""
    events = EventStore(db)
    plan = _make_plan([
        ("a", [], PlanStepStatus.VERIFIED),
    ])
    invalidated = invalidate_affected_subgraph(plan, set(), events)
    assert invalidated == set()
    assert plan.steps[0].status == PlanStepStatus.VERIFIED


# ---------------------------------------------------------------------------
# B5.11: invalidate_affected_subgraph — PENDING step
# ---------------------------------------------------------------------------

def test_invalidate_pending_step(db):
    """PENDING step in affected set → STALE."""
    events = EventStore(db)
    plan = _make_plan([
        ("a", [], PlanStepStatus.PENDING),
    ])
    invalidated = invalidate_affected_subgraph(plan, {"a"}, events)
    assert invalidated == {"a"}
    assert plan.steps[0].status == PlanStepStatus.STALE


# ---------------------------------------------------------------------------
# B5.12: invalidate_affected_subgraph — RUNNING step
# ---------------------------------------------------------------------------

def test_invalidate_running_step(db):
    """RUNNING step in affected set → STALE."""
    events = EventStore(db)
    plan = _make_plan([
        ("a", [], PlanStepStatus.RUNNING),
    ])
    invalidated = invalidate_affected_subgraph(plan, {"a"}, events)
    assert invalidated == {"a"}
    assert plan.steps[0].status == PlanStepStatus.STALE
