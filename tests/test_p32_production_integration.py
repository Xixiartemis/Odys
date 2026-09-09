"""Phase 3.2 — Adversarial production integration tests.

Ten tests that exercise the PlanExecutionService end-to-end with
verification seam, preconditions, stale-plan rejection, dependency
semantics, and duplicate-dispatch guards.

All tests use:
  - FixedPlanner from this module
  - FakeTool from tests/helpers.py / src/lhas/tools/fakes.py
  - AcceptingVerifier / RejectingVerifier from tests/helpers.py
  - db fixture from conftest.py
"""

from __future__ import annotations

import asyncio

import pytest

from lhas.domain.enums import EventType
from lhas.domain.models import Project
from lhas.persistence.event_store import EventStore
from lhas.persistence.planning_repositories import PlanRepository
from lhas.persistence.repositories import ProjectRepository
from lhas.planning.models import (
    CapabilitySpec,
    Goal,
    Plan,
    PlanMode,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    StepPrecondition,
)
from lhas.planning.service import PlanExecutionService
from lhas.planning.scheduler import TaskGraphScheduler
from lhas.tools.fakes import FakeTool
from lhas.tools.protocol import ToolResult, ToolResultStatus
from lhas.tools.registry import ToolRegistry
from tests.helpers import (
    AcceptingVerifier,
    RejectingVerifier,
    make_test_capability_definition,
    make_test_capability_registry,
)


class FixedPlanner:
    """Deterministic planner that returns a pre-built plan."""

    def __init__(self, plan):
        self.plan = plan

    async def create_plan(self, **kwargs):
        return self.plan


def _setup_service(db, steps, *, verifier=None, tool_handlers=None):
    """Build PlanExecutionService with FakeTool(s) for the given steps.

    Returns (service, goal, tool_call_counts).
    """
    project = Project(name="p32-integ")
    ProjectRepository(db).create(project)
    goal = Goal(project_id=project.id, objective="p32 integration test")

    plan = Plan(goal_id=goal.id, mode=PlanMode.SIMPLE_DEPENDENCY, steps=steps)

    call_counts: dict[str, int] = {}
    reg = ToolRegistry()
    defs = []

    cap_names = {s.capability for s in steps}
    for cap in cap_names:
        call_counts[cap] = 0
        handler = (tool_handlers or {}).get(cap)

        def _make_handler(c=cap, h=handler):
            def _run(req):
                call_counts[c] += 1
                if h is not None:
                    return h(req)
                return ToolResult(status=ToolResultStatus.SUCCESS, output=f"{c}_ok")
            return _run

        cap_spec = CapabilitySpec(name=cap, description=cap)
        reg.register(FakeTool(cap_spec, _make_handler()))
        defs.append(make_test_capability_definition(cap, output_schema={}))

    cap_reg, contract = make_test_capability_registry(reg, defs)
    service = PlanExecutionService(
        db,
        FixedPlanner(plan),
        reg,
        capability_registry=cap_reg,
        tool_contract=contract,
        workflow_verifier=verifier,
    )
    return service, goal, call_counts


# ---------------------------------------------------------------------------
# T1: happy verified path — single step, accepting verifier → VERIFIED → COMPLETED
# ---------------------------------------------------------------------------

def test_t1_happy_verified_path(db):
    """T1: AcceptingVerifier → step VERIFIED, plan COMPLETED."""
    a = PlanStep(id="a", title="A", objective="do A", capability="cap_a")
    service, goal, counts = _setup_service(db, [a], verifier=AcceptingVerifier())

    result = asyncio.run(service.execute_goal(goal))

    assert result.status == PlanStatus.COMPLETED
    a_step = next(s for s in result.steps if s.id == "a")
    assert a_step.status == PlanStepStatus.VERIFIED
    assert counts["cap_a"] == 1

    # Full transition chain in events
    events = EventStore(db).list_all()
    transitions = [
        e for e in events
        if e.event_type == EventType.STEP_STATE_TRANSITION
        and e.payload.get("step_id") == "a"
    ]
    status_pairs = [(e.payload["previous_status"], e.payload["new_status"]) for e in transitions]
    assert ("RUNNING", "CLAIMED_COMPLETE") in status_pairs
    assert ("CLAIMED_COMPLETE", "VERIFIED") in status_pairs


# ---------------------------------------------------------------------------
# T2: no verifier → fail-closed → WAITING_FOR_VERIFICATION
# ---------------------------------------------------------------------------

def test_t2_no_verifier_fail_closed(db):
    """T2: No verifier → step WAITING_FOR_VERIFICATION, plan WAITING_FOR_VERIFICATION."""
    a = PlanStep(id="a", title="A", objective="do A", capability="cap_a")
    service, goal, counts = _setup_service(db, [a], verifier=None)

    result = asyncio.run(service.execute_goal(goal))

    a_step = next(s for s in result.steps if s.id == "a")
    assert a_step.status == PlanStepStatus.WAITING_FOR_VERIFICATION
    assert result.status == PlanStatus.WAITING_FOR_VERIFICATION
    assert counts["cap_a"] == 1  # tool DID run

    # FAIL-CLOSED: no VERIFIED transition
    events = EventStore(db).list_all()
    transitions = [
        e for e in events
        if e.event_type == EventType.STEP_STATE_TRANSITION
        and e.payload.get("step_id") == "a"
    ]
    status_pairs = [(e.payload["previous_status"], e.payload["new_status"]) for e in transitions]
    assert ("CLAIMED_COMPLETE", "VERIFIED") not in status_pairs
    assert ("CLAIMED_COMPLETE", "WAITING_FOR_VERIFICATION") in status_pairs


# ---------------------------------------------------------------------------
# T3: verifier reject → CLASSIFIED_FAILURE → plan FAILED
# ---------------------------------------------------------------------------

def test_t3_verifier_reject(db):
    """T3: RejectingVerifier → step CLASSIFIED_FAILURE, plan FAILED."""
    a = PlanStep(id="a", title="A", objective="do A", capability="cap_a")
    service, goal, counts = _setup_service(db, [a], verifier=RejectingVerifier())

    result = asyncio.run(service.execute_goal(goal))

    a_step = next(s for s in result.steps if s.id == "a")
    assert a_step.status == PlanStepStatus.CLASSIFIED_FAILURE
    assert result.status == PlanStatus.FAILED
    assert counts["cap_a"] == 1

    events = EventStore(db).list_all()
    transitions = [
        e for e in events
        if e.event_type == EventType.STEP_STATE_TRANSITION
        and e.payload.get("step_id") == "a"
    ]
    status_pairs = [(e.payload["previous_status"], e.payload["new_status"]) for e in transitions]
    assert ("RUNNING", "CLAIMED_COMPLETE") in status_pairs
    assert ("CLAIMED_COMPLETE", "CLASSIFIED_FAILURE") in status_pairs


# ---------------------------------------------------------------------------
# T4: stale plan — version mismatch → zero side effects on second dispatch
# ---------------------------------------------------------------------------

def test_t4_stale_plan_rejected(db):
    """T4: After a plan version bump, the old executor is rejected (STALE_PLAN).

    Creates and runs a plan normally, then simulates a version bump by
    updating the plan in the DB and directly invoking _TaskGraphAgentExecutor
    with the old version, verifying zero backend execution.
    """
    from lhas.executors.protocol import ExecutionRequest, ExecutionResult
    from lhas.domain.enums import ExecutionStatus
    from lhas.planning.service import _TaskGraphAgentExecutor

    project = Project(name="stale-p32")
    ProjectRepository(db).create(project)

    a = PlanStep(id="a", title="A", objective="do A", capability="cap_a")
    plan = Plan(goal_id="g", mode=PlanMode.SIMPLE_DEPENDENCY, steps=[a])

    plans = PlanRepository(db)
    plans.create(plan)

    # Simulate a replan: bump version
    plan.version = "P-0.1-r1"
    plans.update(plan)

    # Create an executor bound to the OLD version
    old_plan = Plan(goal_id="g", mode=PlanMode.SIMPLE_DEPENDENCY, steps=[a])
    old_plan.id = plan.id
    old_plan.version = "P-0.1"  # stale

    tool_called = {"count": 0}

    class TrackingExecutor:
        async def execute(self, request):
            tool_called["count"] += 1
            return ExecutionResult(status=ExecutionStatus.SUCCESS, output="ok")

    executor = _TaskGraphAgentExecutor(TrackingExecutor(), old_plan, a, db=db)
    request = ExecutionRequest(
        task_id="t1", run_id="r1", attempt_id="at1", attempt_number=1,
        context={}, metadata={},
    )
    result = asyncio.run(executor.execute(request))

    assert result.status == ExecutionStatus.FAILURE
    assert result.error_type == "STALE_PLAN"
    assert tool_called["count"] == 0  # ZERO side effects

    events = [e for e in EventStore(db).list_all() if e.event_type == EventType.PLAN_STALE_REJECTED]
    assert len(events) >= 1
    assert events[0].payload["plan_id"] == plan.id


# ---------------------------------------------------------------------------
# T5: precondition false at dispatch → PRECONDITION_FAILED, 0 tool calls
# ---------------------------------------------------------------------------

def test_t5_precondition_blocks_at_dispatch(db):
    """T5: Precondition TRUE at scheduling, FALSE at dispatch → zero execution.

    Uses a 'falsy' precondition on runtime.ready:
    - At scheduling (empty context): runtime.ready=None → falsy → TRUE → step is ready
    - At dispatch (actual context): runtime.ready=True → falsy → FALSE → step rejected

    This is a real dispatch-time TOCTOU test: the precondition's truth value
    flips between scheduling and dispatch.
    """
    pc = StepPrecondition(key="runtime.ready", operator="falsy", description="runtime must NOT be ready")
    a = PlanStep(id="a", title="A", objective="do A", capability="cap_a")
    b = PlanStep(id="b", title="B", objective="do B", capability="cap_b",
                 depends_on=["a"], preconditions=[pc])

    service, goal, counts = _setup_service(db, [a, b], verifier=AcceptingVerifier())

    # Pass context that sets runtime.ready=True (makes precondition FALSE)
    result = asyncio.run(service.execute_goal(goal, context={"ready": True}))

    a_step = next(s for s in result.steps if s.id == "a")
    b_step = next(s for s in result.steps if s.id == "b")

    # a runs and is verified
    assert a_step.status == PlanStepStatus.VERIFIED
    assert counts.get("cap_a", 0) == 1

    # b is rejected at dispatch time because runtime.ready=True makes falsy=False
    # The dispatch-time recheck evaluates with actual context → precondition fails
    assert b_step.status in {PlanStepStatus.BLOCKED, PlanStepStatus.PRECONDITION_FAILED}
    assert counts.get("cap_b", 0) == 0  # ZERO backend executions


# ---------------------------------------------------------------------------
# T6: run succeeds, no verifier → WAITING_FOR_VERIFICATION (not VERIFIED)
# ---------------------------------------------------------------------------

def test_t6_run_success_no_verify(db):
    """T6: Tool success without verifier → WAITING_FOR_VERIFICATION, never VERIFIED."""
    a = PlanStep(id="a", title="A", objective="do A", capability="cap_a")
    service, goal, counts = _setup_service(db, [a], verifier=None)

    result = asyncio.run(service.execute_goal(goal))

    a_step = next(s for s in result.steps if s.id == "a")
    assert a_step.status == PlanStepStatus.WAITING_FOR_VERIFICATION

    # No VERIFIED transition exists
    events = EventStore(db).list_all()
    verified_transitions = [
        e for e in events
        if e.event_type == EventType.STEP_STATE_TRANSITION
        and e.payload.get("new_status") == "VERIFIED"
    ]
    assert len(verified_transitions) == 0

    # Plan is not COMPLETED
    assert result.status != PlanStatus.COMPLETED


# ---------------------------------------------------------------------------
# T7: tool success alone does not unlock dependent unless step is VERIFIED
# ---------------------------------------------------------------------------

def test_t7_tool_success_no_verify_blocks_dependent(db):
    """T7: A runs OK but is only CLAIMED_COMPLETE/WAITING → B never dispatched."""
    a = PlanStep(id="a", title="A", objective="do A", capability="cap_a")
    b = PlanStep(id="b", title="B", objective="do B", capability="cap_b",
                 depends_on=["a"])
    # No verifier: a will be WAITING_FOR_VERIFICATION, b should stay blocked
    service, goal, counts = _setup_service(db, [a, b], verifier=None)

    result = asyncio.run(service.execute_goal(goal))

    a_step = next(s for s in result.steps if s.id == "a")
    b_step = next(s for s in result.steps if s.id == "b")

    # a ran successfully but not verified
    assert a_step.status == PlanStepStatus.WAITING_FOR_VERIFICATION
    # b was never dispatched because a is not VERIFIED
    assert b_step.status in {PlanStepStatus.PENDING, PlanStepStatus.BLOCKED}
    assert counts.get("cap_b", 0) == 0


# ---------------------------------------------------------------------------
# T8: reload durability — plan state persists across DB reloads
# ---------------------------------------------------------------------------

def test_t8_reload_durability(db):
    """T8: After execution, reloading plan from DB preserves step statuses."""
    a = PlanStep(id="a", title="A", objective="do A", capability="cap_a")
    b = PlanStep(id="b", title="B", objective="do B", capability="cap_b",
                 depends_on=["a"])
    service, goal, counts = _setup_service(db, [a, b], verifier=AcceptingVerifier())

    result = asyncio.run(service.execute_goal(goal))

    assert result.status == PlanStatus.COMPLETED

    # Reload from DB
    plans = PlanRepository(db)
    reloaded = plans.get(result.id)
    assert reloaded is not None
    assert reloaded.status == PlanStatus.COMPLETED

    reloaded_steps = {s.id: s for s in reloaded.steps}
    assert reloaded_steps["a"].status == PlanStepStatus.VERIFIED
    assert reloaded_steps["b"].status == PlanStepStatus.VERIFIED

    # Event log is also durable
    events = EventStore(db).list_all()
    plan_completed = [e for e in events if e.event_type == EventType.PLAN_COMPLETED]
    assert len(plan_completed) >= 1


# ---------------------------------------------------------------------------
# T9: dependency semantics — multi-dep, partial failure blocks dependents
# ---------------------------------------------------------------------------

def test_t9_dependency_semantics(db):
    """T9: A+B → C.  A verified, B rejected → C never dispatched."""
    a = PlanStep(id="a", title="A", objective="do A", capability="cap_a")
    b = PlanStep(id="b", title="B", objective="do B", capability="cap_b")
    c = PlanStep(id="c", title="C", objective="do C", capability="cap_c",
                 depends_on=["a", "b"])

    # RejectingVerifier rejects everything, but we need A to pass and B to fail.
    # Use a verifier that accepts "a" and rejects "b".
    class SelectiveVerifier:
        def verify(self, step, plan, events):
            if step.id == "a":
                return type("V", (), {"accepted": True, "reason": "ok"})()
            return type("V", (), {"accepted": False, "reason": "reject_b"})()

    service, goal, counts = _setup_service(db, [a, b, c], verifier=SelectiveVerifier())

    result = asyncio.run(service.execute_goal(goal))

    a_step = next(s for s in result.steps if s.id == "a")
    b_step = next(s for s in result.steps if s.id == "b")
    c_step = next(s for s in result.steps if s.id == "c")

    assert a_step.status == PlanStepStatus.VERIFIED
    assert b_step.status == PlanStepStatus.CLASSIFIED_FAILURE
    # c depends on both a and b; b failed → c blocked
    assert c_step.status in {PlanStepStatus.PENDING, PlanStepStatus.BLOCKED}
    assert counts.get("cap_c", 0) == 0


# ---------------------------------------------------------------------------
# T10: duplicate dispatch — RUNNING step not re-dispatched
# ---------------------------------------------------------------------------

def test_t10_duplicate_dispatch_prevented(db):
    """T10: Terminal steps are never re-dispatched. Side-effect count = 1.

    Proves that after a step reaches any terminal state, the scheduler and
    eligibility authority prevent duplicate dispatch:
    - VERIFIED → not eligible, not in ready_steps
    - WAITING_FOR_VERIFICATION → not eligible, not in ready_steps
    - CLASSIFIED_FAILURE → not eligible, not in ready_steps
    - FAILED → not eligible, not in ready_steps
    - BLOCKED → not eligible, not in ready_steps
    """
    from lhas.planning.models import evaluate_step_eligibility

    terminal_statuses = [
        PlanStepStatus.VERIFIED,
        PlanStepStatus.WAITING_FOR_VERIFICATION,
        PlanStepStatus.CLASSIFIED_FAILURE,
        PlanStepStatus.FAILED,
        PlanStepStatus.BLOCKED,
        PlanStepStatus.STALE,
    ]

    for status in terminal_statuses:
        step = PlanStep(id="x", title="X", objective="X", capability="x", status=status)
        by_id = {"x": step}
        eligible, reason = evaluate_step_eligibility(step, by_id)
        assert eligible is False, f"{status} should not be eligible, got {reason}"

    # Also verify via scheduler: terminal steps never appear in ready_steps
    for status in terminal_statuses:
        step = PlanStep(id="x", title="X", objective="X", capability="x", status=status)
        plan = Plan(goal_id="g", mode=PlanMode.SIMPLE_DEPENDENCY, steps=[step])
        schedule = TaskGraphScheduler().calculate(plan)
        ready_ids = [s.id for s in schedule.ready_steps]
        assert "x" not in ready_ids, f"{status} should not be in ready_steps"

    # Integration: run a step once, verify counter = 1
    a = PlanStep(id="a", title="A", objective="do A", capability="cap_a")
    service, goal, counts = _setup_service(db, [a], verifier=AcceptingVerifier())
    result = asyncio.run(service.execute_goal(goal))
    assert counts["cap_a"] == 1  # executed exactly once
    assert result.steps[0].status == PlanStepStatus.VERIFIED

    # Verify via scheduler that VERIFIED step is not in ready_steps
    plan = PlanRepository(db).get(result.id)
    schedule = TaskGraphScheduler().calculate(plan)
    assert len(schedule.ready_steps) == 0  # no steps ready for re-dispatch
