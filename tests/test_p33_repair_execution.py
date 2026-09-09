"""Phase 3.3 — Repair execution and state preservation tests.

Five tests that exercise repair execution semantics:
- Local repair: re-execute failed step, VERIFIED work preserved
- Subgraph repair: re-execute affected subgraph, unrelated VERIFIED preserved
- Repair budget: if step fails N times, stop retrying (respect max_attempts)
- Restart after repair: repair state survives DB close/reopen
- Repair attempt chains to original attempt (provenance)
"""

from __future__ import annotations

import asyncio
import json

import pytest

from lhas.domain.enums import EventType
from lhas.domain.models import Project
from lhas.persistence.database import Database
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
)
from lhas.planning.service import PlanExecutionService
from lhas.tools.fakes import FakeTool
from lhas.tools.protocol import ToolResult, ToolResultStatus
from lhas.tools.registry import ToolRegistry
from tests.helpers import (
    AcceptingVerifier,
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
    project = Project(name="p33-repair")
    ProjectRepository(db).create(project)
    goal = Goal(project_id=project.id, objective="p33 repair test")

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
        defs.append(make_test_capability_definition(cap, output_schema={}, retryable=False))

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


def _make_fail_handler(fail_until_call):
    """Create a handler that fails until the given call number, then succeeds.

    The RecoveringOrchestrator retries failed tasks (max_attempts=2),
    so a handler must fail on calls 1 AND 2 to ensure the step truly fails.
    On call 3+ (repair attempts), it succeeds.

    Returns (handler_func, call_counter_dict).
    """
    counter = {"n": 0}

    def handler(req):
        counter["n"] += 1
        if counter["n"] <= fail_until_call:
            return ToolResult(
                status=ToolResultStatus.FAILURE,
                error_type="TEST_FAILURE",
                error_message="simulated failure",
            )
        return ToolResult(status=ToolResultStatus.SUCCESS, output="ok")

    return handler, counter


# ---------------------------------------------------------------------------
# R1: Local repair — re-execute failed step, VERIFIED work preserved
# ---------------------------------------------------------------------------

def test_r1_local_repair_preserves_verified(db):
    """R1: After B fails, repair B. A stays VERIFIED, B re-executed, C runs."""
    a = PlanStep(id="a", title="A", objective="do A", capability="cap_a")
    b = PlanStep(id="b", title="B", objective="do B", capability="cap_b", depends_on=["a"])
    c = PlanStep(id="c", title="C", objective="do C", capability="cap_c", depends_on=["b"])

    # Fail on calls 1 and 2 (initial + orchestrator retry), succeed on call 3+ (repair)
    b_handler, b_counter = _make_fail_handler(fail_until_call=2)

    service, goal, counts = _setup_service(
        db, [a, b, c],
        verifier=AcceptingVerifier(),
        tool_handlers={"cap_b": b_handler},
    )

    # First execution: a VERIFIED, b FAILED, c never runs
    result = asyncio.run(service.execute_goal(goal))
    a_step = next(s for s in result.steps if s.id == "a")
    b_step = next(s for s in result.steps if s.id == "b")
    c_step = next(s for s in result.steps if s.id == "c")

    assert a_step.status == PlanStepStatus.VERIFIED
    assert b_step.status == PlanStepStatus.FAILED
    assert c_step.status in {PlanStepStatus.PENDING, PlanStepStatus.BLOCKED}
    assert counts["cap_a"] == 1
    assert b_counter["n"] == 2  # initial + retry
    assert counts["cap_c"] == 0

    # Repair: call repair_after_failure for step b
    result2 = asyncio.run(service.repair_after_failure(result.id, "b", goal))

    a_step2 = next(s for s in result2.steps if s.id == "a")
    b_step2 = next(s for s in result2.steps if s.id == "b")
    c_step2 = next(s for s in result2.steps if s.id == "c")

    # a was NOT re-executed (still 1 call)
    assert a_step2.status == PlanStepStatus.VERIFIED
    assert counts["cap_a"] == 1

    # b was re-executed and now VERIFIED
    assert b_step2.status == PlanStepStatus.VERIFIED
    assert b_counter["n"] == 3  # 2 from first run + 1 from repair

    # c ran after b was re-VERIFIED
    assert c_step2.status == PlanStepStatus.VERIFIED
    assert counts["cap_c"] == 1

    # Plan completed
    assert result2.status == PlanStatus.COMPLETED


# ---------------------------------------------------------------------------
# R2: Subgraph repair — re-execute affected subgraph, unrelated VERIFIED preserved
# ---------------------------------------------------------------------------

def test_r2_subgraph_repair_preserves_unrelated(db):
    """R2: A,B→C. B fails. Repair invalidates B+C (dependents). A preserved."""
    a = PlanStep(id="a", title="A", objective="do A", capability="cap_a")
    b = PlanStep(id="b", title="B", objective="do B", capability="cap_b")
    c = PlanStep(id="c", title="C", objective="do C", capability="cap_c", depends_on=["a", "b"])

    b_handler, b_counter = _make_fail_handler(fail_until_call=2)

    service, goal, counts = _setup_service(
        db, [a, b, c],
        verifier=AcceptingVerifier(),
        tool_handlers={"cap_b": b_handler},
    )

    # First execution: a VERIFIED, b FAILED, c blocked
    result = asyncio.run(service.execute_goal(goal))
    assert next(s for s in result.steps if s.id == "a").status == PlanStepStatus.VERIFIED
    assert next(s for s in result.steps if s.id == "b").status == PlanStepStatus.FAILED
    assert counts["cap_a"] == 1
    assert b_counter["n"] == 2

    # Repair b → scope should be {b, c} (c depends on b)
    result2 = asyncio.run(service.repair_after_failure(result.id, "b", goal))

    a_step2 = next(s for s in result2.steps if s.id == "a")
    b_step2 = next(s for s in result2.steps if s.id == "b")
    c_step2 = next(s for s in result2.steps if s.id == "c")

    # a was NOT re-executed
    assert a_step2.status == PlanStepStatus.VERIFIED
    assert counts["cap_a"] == 1

    # b was re-executed
    assert b_step2.status == PlanStepStatus.VERIFIED
    assert b_counter["n"] == 3

    # c was executed (both deps now satisfied)
    assert c_step2.status == PlanStepStatus.VERIFIED
    assert counts["cap_c"] == 1

    assert result2.status == PlanStatus.COMPLETED


# ---------------------------------------------------------------------------
# R3: Repair budget — if step fails N times, stop retrying (respect max_attempts)
# ---------------------------------------------------------------------------

def test_r3_repair_budget_stops_retrying(db):
    """R3: After max_repair_attempts, repair_after_failure returns without re-executing."""
    a = PlanStep(
        id="a", title="A", objective="do A", capability="cap_a",
        budget={"max_repair_attempts": 2},
    )

    # Always fail (fail on every call)
    always_fail = lambda req: ToolResult(
        status=ToolResultStatus.FAILURE,
        error_type="PERMANENT_FAILURE",
        error_message="always fails",
    )

    service, goal, counts = _setup_service(
        db, [a],
        verifier=AcceptingVerifier(),
        tool_handlers={"cap_a": always_fail},
    )

    # First execution: a FAILED
    result = asyncio.run(service.execute_goal(goal))
    assert next(s for s in result.steps if s.id == "a").status == PlanStepStatus.FAILED
    initial_calls = counts["cap_a"]  # 2 (initial + retry)
    assert initial_calls == 2

    # First repair attempt: fails again
    result2 = asyncio.run(service.repair_after_failure(result.id, "a", goal))
    assert next(s for s in result2.steps if s.id == "a").status == PlanStepStatus.FAILED
    assert counts["cap_a"] == initial_calls + 2  # +2 for repair (initial + retry)

    # Second repair attempt: fails again → count reaches max_repair_attempts=2
    result3 = asyncio.run(service.repair_after_failure(result2.id, "a", goal))
    assert next(s for s in result3.steps if s.id == "a").status == PlanStepStatus.FAILED
    assert counts["cap_a"] == initial_calls + 4  # +4 for 2 repairs

    calls_before_budget_check = counts["cap_a"]

    # Third repair attempt: budget exhausted (repair_attempt_count=2 >= max_repair_attempts=2) → no re-execution
    result4 = asyncio.run(service.repair_after_failure(result3.id, "a", goal))
    assert counts["cap_a"] == calls_before_budget_check  # NOT incremented
    assert next(s for s in result4.steps if s.id == "a").status == PlanStepStatus.FAILED

    # Check BUDGET_EXHAUSTED event
    events = EventStore(db).list_all()
    budget_events = [
        e for e in events
        if e.event_type == EventType.REPAIR_COMPLETED
        and e.payload.get("outcome") == "BUDGET_EXHAUSTED"
    ]
    assert len(budget_events) >= 1


# ---------------------------------------------------------------------------
# R4: Restart after repair — repair state survives DB close/reopen
# ---------------------------------------------------------------------------

def test_r4_repair_state_survives_db_restart(tmp_path):
    """R4: After repair, reload plan from fresh DB connection — state is durable."""
    db = Database(tmp_path / "test.db")
    db.init_db()

    a = PlanStep(id="a", title="A", objective="do A", capability="cap_a")
    b = PlanStep(id="b", title="B", objective="do B", capability="cap_b", depends_on=["a"])

    b_handler, b_counter = _make_fail_handler(fail_until_call=2)

    service, goal, counts = _setup_service(
        db, [a, b],
        verifier=AcceptingVerifier(),
        tool_handlers={"cap_b": b_handler},
    )

    # Execute: a VERIFIED, b FAILED
    result = asyncio.run(service.execute_goal(goal))
    plan_id = result.id
    assert next(s for s in result.steps if s.id == "a").status == PlanStepStatus.VERIFIED
    assert next(s for s in result.steps if s.id == "b").status == PlanStepStatus.FAILED

    # Repair b
    result2 = asyncio.run(service.repair_after_failure(plan_id, "b", goal))
    assert result2.status == PlanStatus.COMPLETED

    # Close and reopen DB
    db.close()
    db2 = Database(tmp_path / "test.db")
    db2.init_db()

    # Reload plan from fresh connection
    reloaded = PlanRepository(db2).get(plan_id)
    assert reloaded is not None
    assert reloaded.status == PlanStatus.COMPLETED

    steps_by_id = {s.id: s for s in reloaded.steps}
    assert steps_by_id["a"].status == PlanStepStatus.VERIFIED
    assert steps_by_id["b"].status == PlanStepStatus.VERIFIED

    # Repair events are durable
    events = EventStore(db2).list_all()
    repair_events = [e for e in events if e.event_type in {EventType.REPAIR_STARTED, EventType.REPAIR_COMPLETED}]
    assert len(repair_events) >= 2  # at least one REPAIR_STARTED + one REPAIR_COMPLETED

    db2.close()


# ---------------------------------------------------------------------------
# R5: Repair attempt chains to original attempt (provenance)
# ---------------------------------------------------------------------------

def test_r5_repair_provenance_chains_to_original(db):
    """R5: After repair, step.evidence contains repair_parent_step_id and repair_attempt_count."""
    a = PlanStep(id="a", title="A", objective="do A", capability="cap_a")

    a_handler, a_counter = _make_fail_handler(fail_until_call=2)

    service, goal, counts = _setup_service(
        db, [a],
        verifier=AcceptingVerifier(),
        tool_handlers={"cap_a": a_handler},
    )

    # First execution: a FAILED
    result = asyncio.run(service.execute_goal(goal))
    a_step = next(s for s in result.steps if s.id == "a")
    assert a_step.status == PlanStepStatus.FAILED
    assert a_counter["n"] == 2

    # Repair a
    result2 = asyncio.run(service.repair_after_failure(result.id, "a", goal))
    a_step2 = next(s for s in result2.steps if s.id == "a")
    assert a_step2.status == PlanStepStatus.VERIFIED

    # Verify provenance chain
    assert a_step2.evidence.get("repair_attempt_count") == 1
    assert a_step2.evidence.get("repair_parent_step_id") == "a"

    # Verify REPAIR_STARTED event includes repair_step_ids
    events = EventStore(db).list_all()
    repair_started = [e for e in events if e.event_type == EventType.REPAIR_STARTED]
    assert len(repair_started) >= 1
    assert "a" in repair_started[-1].payload["repair_step_ids"]

    # Verify STEP_STATE_TRANSITION events show the repair_invalidation transition
    invalidation_events = [
        e for e in events
        if e.event_type == EventType.STEP_STATE_TRANSITION
        and e.payload.get("reason") == "repair_invalidation"
        and e.payload.get("step_id") == "a"
    ]
    assert len(invalidation_events) >= 1
    assert invalidation_events[0].payload["new_status"] == "PENDING"

    # Verify REPAIR_STEP_INVALIDATED event
    invalidated_events = [e for e in events if e.event_type == EventType.REPAIR_STEP_INVALIDATED]
    assert len(invalidated_events) >= 1
    assert invalidated_events[0].payload["step_id"] == "a"

    # Verify the evidence persists across DB reload
    reloaded = PlanRepository(db).get(result.id)
    reloaded_a = next(s for s in reloaded.steps if s.id == "a")
    assert reloaded_a.evidence.get("repair_attempt_count") == 1
    assert reloaded_a.evidence.get("repair_parent_step_id") == "a"
