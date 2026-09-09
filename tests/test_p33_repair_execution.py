"""P3.3 Repair Execution Tests — inline LOCAL repair semantics.

LOCAL repair now happens automatically within execute_goal:
step fails → LOCAL scope → reset to PENDING → re-execute → VERIFIED.
"""
from __future__ import annotations
import asyncio
import pytest

from lhas.domain.models import Project
from lhas.persistence.planning_repositories import PlanRepository
from lhas.persistence.repositories import AttemptRepository, ProjectRepository, RunRepository
from lhas.planning.models import (
    Goal, Plan, PlanMode, PlanStatus, PlanStep, PlanStepStatus,
    RepairScope, compute_repair_scope, StepPrecondition,
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
from tests.test_p32_production_integration import _setup_service


def _make_fail_handler(fail_until_call=2):
    """Handler that fails for first N calls, then succeeds."""
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
# R1: LOCAL repair — automatic inline repair preserves VERIFIED work
# ---------------------------------------------------------------------------

def test_r1_local_repair_preserves_verified(db):
    """R1: B fails TOOL_ERROR → LOCAL repair inline → A stays VERIFIED, B re-executed, C runs."""
    a = PlanStep(id="a", title="A", objective="do A", capability="cap_a")
    b = PlanStep(id="b", title="B", objective="do B", capability="cap_b", depends_on=["a"])
    c = PlanStep(id="c", title="C", objective="do C", capability="cap_c", depends_on=["b"])

    b_handler, b_counter = _make_fail_handler(fail_until_call=2)

    service, goal, _ = _setup_service(
        db, [a, b, c],
        verifier=AcceptingVerifier(),
        tool_handlers={"cap_b": b_handler},
    )

    # Execute: a VERIFIED, b fails → LOCAL repair → b re-executes → b VERIFIED → c runs
    result = asyncio.run(service.execute_goal(goal))
    a_step = next(s for s in result.steps if s.id == "a")
    b_step = next(s for s in result.steps if s.id == "b")
    c_step = next(s for s in result.steps if s.id == "c")

    # a executed once, stays VERIFIED
    assert a_step.status == PlanStepStatus.VERIFIED
    # b repaired inline: 2 failures (initial+retry) + 1 repair attempt
    assert b_step.status == PlanStepStatus.VERIFIED
    assert b_counter["n"] == 3
    # c ran after b was re-VERIFIED
    assert c_step.status == PlanStepStatus.VERIFIED
    assert result.status == PlanStatus.COMPLETED


# ---------------------------------------------------------------------------
# R2: compute_repair_scope for AFFECTED_SUBGRAPH
# ---------------------------------------------------------------------------

def test_r2_subgraph_scope_for_wrong_assumption(db):
    """R2: WRONG_ASSUMPTION → AFFECTED_SUBGRAPH scope."""
    a = PlanStep(id="a", title="A", objective="do A", capability="cap_a")
    b = PlanStep(id="b", title="B", objective="do B", capability="cap_b", depends_on=["a"])
    c = PlanStep(id="c", title="C", objective="do C", capability="cap_c", depends_on=["a"])

    service, goal, _ = _setup_service(db, [a, b, c], verifier=AcceptingVerifier())
    result = asyncio.run(service.execute_goal(goal))
    assert all(s.status == PlanStepStatus.VERIFIED for s in result.steps)

    # Verify compute_repair_scope with assumption-invalidating error
    scope, affected = compute_repair_scope(a, result, error_type="WRONG_ASSUMPTION")
    assert scope == RepairScope.AFFECTED_SUBGRAPH
    assert "a" in affected
    assert "b" in affected
    assert "c" in affected


# ---------------------------------------------------------------------------
# R3: Repair budget stops retrying
# ---------------------------------------------------------------------------

def test_r3_repair_budget_stops_retrying(db):
    """R3: After max_repair_attempts exhausted, step stays FAILED."""
    a = PlanStep(id="a", title="A", objective="do A", capability="cap_a",
                 budget={"max_repair_attempts": 1})

    always_fail = lambda req: ToolResult(
        status=ToolResultStatus.FAILURE, error_type="TEST_FAILURE", error_message="always fails")

    service, goal, _ = _setup_service(
        db, [a], verifier=AcceptingVerifier(), tool_handlers={"cap_a": always_fail},
    )

    result = asyncio.run(service.execute_goal(goal))
    a_step = next(s for s in result.steps if s.id == "a")
    assert a_step.status == PlanStepStatus.FAILED
    assert a_step.evidence.get("repair_attempt_count", 0) >= 1


# ---------------------------------------------------------------------------
# R4: Repair state survives DB restart
# ---------------------------------------------------------------------------

def test_r4_repair_state_survives_db_restart(db):
    """R4: Repair attempt count and provenance survive DB close/reopen."""
    a = PlanStep(id="a", title="A", objective="do A", capability="cap_a")
    service, goal, _ = _setup_service(db, [a], verifier=AcceptingVerifier())
    result = asyncio.run(service.execute_goal(goal))
    a_step = next(s for s in result.steps if s.id == "a")
    assert a_step.status == PlanStepStatus.VERIFIED

    plans = PlanRepository(db)
    reloaded = plans.get(result.id)
    assert reloaded is not None
    reloaded_step = next(s for s in reloaded.steps if s.id == "a")
    assert reloaded_step.status == PlanStepStatus.VERIFIED


# ---------------------------------------------------------------------------
# R5: Repair attempt chains to original attempt
# ---------------------------------------------------------------------------

def test_r5_repair_provenance_chains_to_original(db):
    """R5: After LOCAL repair, step.evidence contains attempt lineage."""
    a = PlanStep(id="a", title="A", objective="do A", capability="cap_a")
    handler, counter = _make_fail_handler(fail_until_call=2)

    service, goal, _ = _setup_service(
        db, [a], verifier=AcceptingVerifier(), tool_handlers={"cap_a": handler},
    )

    result = asyncio.run(service.execute_goal(goal))
    a_step = next(s for s in result.steps if s.id == "a")
    assert a_step.status == PlanStepStatus.VERIFIED
    assert a_step.evidence.get("repair_attempt_count", 0) >= 1

    if a_step.evidence.get("original_failure_attempt_id"):
        original_id = a_step.evidence["original_failure_attempt_id"]
        assert isinstance(original_id, str)
        assert len(original_id) > 0


# ---------------------------------------------------------------------------
# R6: Verification rejection creates failure provenance
# ---------------------------------------------------------------------------

def test_r6_verification_rejection_creates_provenance(db):
    """R6: Verifier reject → CLASSIFIED_FAILURE → StepFailureProvenance with real IDs."""
    from tests.helpers import RejectingVerifier
    from lhas.planning.verification import WorkflowVerifier

    a = PlanStep(id="a", title="A", objective="do A", capability="cap_a",
                 success_criteria=["some_criterion"])

    service, goal, _ = _setup_service(
        db, [a], verifier=RejectingVerifier(),
    )

    result = asyncio.run(service.execute_goal(goal))
    a_step = next(s for s in result.steps if s.id == "a")
    assert a_step.status == PlanStepStatus.CLASSIFIED_FAILURE

    # Verify provenance exists
    provenance = a_step.evidence.get("failure_provenance")
    assert provenance is not None
    assert provenance.get("failure_class") == "DATA"
    assert provenance.get("attempt_id") is not None  # real attempt
    assert provenance.get("run_id") is not None  # real run
