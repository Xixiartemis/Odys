"""P3.3 Repair Execution Tests — inline LOCAL repair semantics.

LOCAL repair now happens automatically within execute_goal:
step fails → LOCAL scope → reset to PENDING → re-execute → VERIFIED.
"""
from __future__ import annotations
import asyncio
import pytest

from lhas.domain.enums import AttemptStatus, EventType, FailureType, RunStatus
from lhas.domain.models import Attempt, Project, Run, Task
from lhas.persistence.event_store import EventStore
from lhas.persistence.phaseb_repos import ValidationResultRepository
from lhas.persistence.planning_repositories import PlanRepository
from lhas.persistence.repositories import AttemptRepository, ProjectRepository, RunRepository, TaskRepository
from lhas.persistence.planning_repositories import GoalRepository
from lhas.planning.models import (
    CapabilitySpec, Goal, Plan, PlanMode, PlanStatus, PlanStep, PlanStepStatus,
    RepairScope, compute_repair_scope, StepPrecondition,
)
from lhas.planning.service import PlanExecutionService
from lhas.planning.verification import WorkflowVerifier
from lhas.tools.fakes import FakeTool
from lhas.tools.protocol import ToolResult, ToolResultStatus
from lhas.tools.registry import ToolRegistry
from tests.helpers import (
    AcceptingVerifier,
    make_test_capability_definition,
    make_test_capability_registry,
)
from tests.test_p32_production_integration import _setup_service


class FixedPlanner:
    def __init__(self, plan):
        self.plan = plan

    async def create_plan(self, **kwargs):
        return self.plan


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
    from lhas.planning.verification import WorkflowVerifier

    a = PlanStep(id="a", title="A", objective="do A", capability="cap_a",
                 success_criteria=["some_criterion"])

    service, goal, _ = _setup_service(
        db, [a], verifier=WorkflowVerifier(db),
    )

    result = asyncio.run(service.execute_goal(goal))
    a_step = next(s for s in result.steps if s.id == "a")
    assert a_step.status == PlanStepStatus.CLASSIFIED_FAILURE

    # Verify provenance exists
    provenance = a_step.evidence.get("failure_provenance")
    assert provenance is not None
    assert provenance.get("failure_class") == "DATA"
    assert provenance.get("failure_type") == FailureType.VERIFICATION_REJECTED.value
    assert provenance.get("attempt_id") is not None  # real attempt
    assert provenance.get("run_id") is not None  # real run
    assert provenance.get("validation_id") is not None


def test_t1_verification_rejection_enters_local_repair_with_real_lineage(db):
    """T1/T4: real validation rejection repairs through a new Run/Attempt."""
    step = PlanStep(
        id="verify-repair",
        title="verify-repair",
        objective="repair rejected evidence",
        capability="cap_verify",
        expected_effects={"ok": True},
    )
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return ToolResult(
            status=ToolResultStatus.SUCCESS,
            output={"ok": calls["n"] > 1},
        )

    service, goal, counts = _setup_service(
        db,
        [step],
        verifier=WorkflowVerifier(db),
        tool_handlers={"cap_verify": handler},
    )
    result = asyncio.run(service.execute_goal(goal))

    repaired_step = result.steps[0]
    assert repaired_step.status == PlanStepStatus.VERIFIED
    assert counts["cap_verify"] == 2
    assert result.replan_count == 0

    tasks = TaskRepository(db).list(goal.project_id)
    assert len(tasks) == 2
    runs = [RunRepository(db).list_for_task(task.id)[0] for task in tasks]
    attempts = [AttemptRepository(db).list_for_run(run.id)[0] for run in runs]
    assert attempts[0].id != attempts[1].id

    provenance = repaired_step.evidence["failure_provenance"]
    assert provenance["failure_type"] == "VERIFICATION_REJECTED"
    assert provenance["validation_id"] is not None
    assert provenance["run_id"] == runs[0].id
    assert provenance["attempt_id"] == attempts[0].id
    failure_events = [
        event for event in EventStore(db).list_all()
        if event.event_type == EventType.STEP_FAILURE_PROVENANCE
    ]
    assert failure_events and failure_events[0].payload["failure_type"] == FailureType.VERIFICATION_REJECTED.value
    validations = ValidationResultRepository(db).list_for_attempt(attempts[0].id)
    assert validations and validations[-1].id == provenance["validation_id"]

    lineage = repaired_step.evidence["repair_lineage"]
    assert lineage == [{
        "plan_id": result.id,
        "step_id": repaired_step.id,
        "original_failure_attempt_id": attempts[0].id,
        "repair_attempt_id": attempts[1].id,
        "repair_number": 1,
        "repair_scope": "LOCAL",
    }]
    repair_events = [
        event for event in EventStore(db).list_all()
        if event.event_type == EventType.REPAIR_COMPLETED
    ]
    assert repair_events[-1].payload["repair_attempt_id"] == attempts[1].id


def _persist_claimed_plan(db, mode):
    """Create a persisted producing chain for deferred-verification tests."""
    project = Project(name=f"deferred-{mode.value.lower()}")
    ProjectRepository(db).create(project)
    goal = Goal(project_id=project.id, objective="deferred verification")
    GoalRepository(db).create(goal)
    task = Task(project_id=project.id, title="producing task", objective="produce evidence")
    TaskRepository(db).create(task)
    run = Run(task_id=task.id, status=RunStatus.COMPLETED, result='{"output": "bad"}')
    RunRepository(db).create(run)
    attempt = Attempt(run_id=run.id, attempt_number=1, status=AttemptStatus.COMPLETED, executor_result="{}")
    AttemptRepository(db).create(attempt)
    step = PlanStep(
        id="deferred-step",
        title="deferred-step",
        objective="verify persisted evidence",
        capability="cap_deferred",
        expected_effects={"ok": True},
        status=PlanStepStatus.CLAIMED_COMPLETE,
        task_id=task.id,
        execution_context={
            "steps": {
                "deferred-step": {
                    "provenance": "TOOL_CONTRACT_EVIDENCE",
                    "output": {"ok": False},
                    "artifacts": {},
                }
            }
        },
    )
    plan = Plan(goal_id=goal.id, mode=mode, steps=[step])
    PlanRepository(db).create(plan)
    return project, goal, plan, task, run, attempt


def _repair_registry(capability):
    registry = ToolRegistry()
    registry.register(FakeTool(
        CapabilitySpec(name=capability, description=capability),
        lambda req: {"ok": True},
    ))
    definition = make_test_capability_definition(capability, output_schema={})
    return registry, make_test_capability_registry(registry, [definition])


def test_t2_deferred_simple_dependency_rejection_repairs_after_reload(db):
    """T2/T5: reload rejection uses real validation identity and repairs."""
    project, goal, plan, task, run, attempt = _persist_claimed_plan(db, PlanMode.SIMPLE_DEPENDENCY)
    db_path = db.engine.url.database
    db.close()
    from lhas.persistence.database import Database
    reopened = Database(db_path)
    reopened.init_db()
    registry, (cap_reg, contract) = _repair_registry("cap_deferred")
    service = PlanExecutionService(
        reopened,
        FixedPlanner(plan),
        registry,
        capability_registry=cap_reg,
        tool_contract=contract,
        workflow_verifier=WorkflowVerifier(reopened),
    )

    result = asyncio.run(service.execute_goal(goal, resume_plan_id=plan.id))
    assert result.steps[0].status == PlanStepStatus.VERIFIED
    provenance = result.steps[0].evidence["failure_provenance"]
    assert provenance["validation_id"]
    assert provenance["run_id"] == run.id
    assert provenance["attempt_id"] == attempt.id
    assert provenance["failure_type"] == "VERIFICATION_REJECTED"
    reloaded = PlanRepository(reopened).get(plan.id)
    assert reloaded.steps[0].evidence["repair_lineage"][0]["original_failure_attempt_id"] == attempt.id
    assert EventStore(reopened).list_all()
    reopened.close()


def test_t3_deferred_linear_rejection_has_provenance_but_no_selective_repair(db):
    """T3: LINEAR is legacy scope; rejection is durable but not auto-repaired."""
    project, goal, plan, task, run, attempt = _persist_claimed_plan(db, PlanMode.LINEAR)
    registry, (cap_reg, contract) = _repair_registry("cap_deferred")
    service = PlanExecutionService(
        db,
        FixedPlanner(plan),
        registry,
        capability_registry=cap_reg,
        tool_contract=contract,
        workflow_verifier=WorkflowVerifier(db),
    )

    result = asyncio.run(service.execute_goal(goal, resume_plan_id=plan.id))
    assert result.steps[0].status == PlanStepStatus.CLASSIFIED_FAILURE
    assert result.status == PlanStatus.FAILED
    provenance = result.steps[0].evidence["failure_provenance"]
    assert provenance["failure_type"] == "VERIFICATION_REJECTED"
    assert provenance["run_id"] == run.id
    assert provenance["attempt_id"] == attempt.id
    assert len(AttemptRepository(db).list_for_run(run.id)) == 1


def test_t4_repeated_verification_repairs_are_bounded_and_keep_first_attempt(db):
    """T6: repeated rejection records A1→A2/A3, then stops at the budget."""
    step = PlanStep(
        id="repeat-verify",
        title="repeat-verify",
        objective="bounded repeated verification",
        capability="cap_repeat",
        expected_effects={"ok": True},
        budget={"max_repair_attempts": 2},
    )
    service, goal, counts = _setup_service(
        db,
        [step],
        verifier=WorkflowVerifier(db),
        tool_handlers={"cap_repeat": lambda req: {"ok": False}},
    )
    result = asyncio.run(service.execute_goal(goal))
    final_step = result.steps[0]
    assert final_step.status == PlanStepStatus.CLASSIFIED_FAILURE
    assert final_step.evidence["repair_attempt_count"] == 2
    lineage = final_step.evidence["repair_lineage"]
    assert [item["repair_number"] for item in lineage] == [1, 2]
    assert all(item["original_failure_attempt_id"] == lineage[0]["original_failure_attempt_id"] for item in lineage)
    assert len({item["repair_attempt_id"] for item in lineage}) == 2
    assert counts["cap_repeat"] == 3
    assert result.replan_count == 0
    assert not any(event.event_type == EventType.REPLAN_SIGNAL_CREATED for event in EventStore(db).list_all())


def test_t5_quota_failure_routes_to_macro_without_local_repair(db, monkeypatch):
    """T7: systemic quota failure never takes the LOCAL repair branch."""
    step = PlanStep(id="quota", title="quota", objective="quota", capability="cap_quota")
    dependent = PlanStep(id="after-quota", title="after-quota", objective="after quota", capability="cap_after", depends_on=["quota"])
    service, goal, counts = _setup_service(
        db,
        [step, dependent],
        verifier=None,
        tool_handlers={
            "cap_quota": lambda req: ToolResult(
                status=ToolResultStatus.FAILURE,
                error_type="QUOTA_EXHAUSTED",
                error_message="quota",
            ),
            "cap_after": lambda req: {"ok": True},
        },
    )
    replan_calls = {"n": 0}

    async def no_replan(*args, **kwargs):
        replan_calls["n"] += 1
        return False

    monkeypatch.setattr(service, "_maybe_replan", no_replan)
    result = asyncio.run(service.execute_goal(goal))
    quota_step = next(item for item in result.steps if item.id == "quota")
    assert quota_step.evidence.get("repair_attempt_count", 0) == 0
    assert replan_calls["n"] == 1
    assert counts["cap_quota"] >= 1


def test_t5a_single_node_quota_routes_to_macro_without_local_repair(db, monkeypatch):
    """Systemic quota failure takes MACRO_REPLAN even without dependents."""
    step = PlanStep(id="quota-only", title="quota-only", objective="quota", capability="cap_quota")
    service, goal, counts = _setup_service(
        db,
        [step],
        verifier=None,
        tool_handlers={
            "cap_quota": lambda req: ToolResult(
                status=ToolResultStatus.FAILURE,
                error_type="QUOTA_EXHAUSTED",
                error_message="quota",
            ),
        },
    )
    replan_calls = {"n": 0}

    async def no_replan(*args, **kwargs):
        replan_calls["n"] += 1
        return False

    monkeypatch.setattr(service, "_maybe_replan", no_replan)
    result = asyncio.run(service.execute_goal(goal))
    quota_step = result.steps[0]
    assert quota_step.evidence.get("repair_attempt_count", 0) == 0
    assert counts["cap_quota"] == 1
    assert replan_calls["n"] == 1
    assert result.replan_count == 0


def test_t5b_single_node_tool_error_remains_local(db):
    """A non-systemic TOOL_ERROR with no dependents remains LOCAL."""
    step = PlanStep(id="tool-error", title="tool-error", objective="tool error", capability="cap_tool")
    plan = Plan(goal_id="goal-tool-error", steps=[step], mode=PlanMode.SIMPLE_DEPENDENCY)
    scope, affected = compute_repair_scope(step, plan, error_type="TOOL_ERROR")
    assert scope == RepairScope.LOCAL
    assert affected == {step.id}


def test_t7_lineage_and_event_survive_reopen_after_repair(db):
    """Repair lineage is durable after the repair itself and a real DB reopen."""
    step = PlanStep(id="reopen-lineage", title="reopen-lineage", objective="repair", capability="cap_reopen")
    handler, _ = _make_fail_handler(fail_until_call=2)
    service, goal, _ = _setup_service(
        db,
        [step],
        verifier=AcceptingVerifier(),
        tool_handlers={"cap_reopen": handler},
    )
    result = asyncio.run(service.execute_goal(goal))
    completed_step = result.steps[0]
    tasks = TaskRepository(db).list(goal.project_id)
    runs = [RunRepository(db).list_for_task(task.id)[0] for task in tasks]
    attempts = [AttemptRepository(db).list_for_run(run.id) for run in runs]
    plan_id = result.id
    original_attempt_id = attempts[0][-1].id
    repair_attempt_id = attempts[1][-1].id
    assert completed_step.evidence["repair_lineage"]

    db_path = db.engine.url.database
    db.close()
    from lhas.persistence.database import Database
    reopened = Database(db_path)
    reopened.init_db()
    reloaded = PlanRepository(reopened).get(plan_id)
    assert reloaded is not None
    lineage = reloaded.steps[0].evidence["repair_lineage"]
    assert lineage[0]["original_failure_attempt_id"] == original_attempt_id
    assert lineage[0]["repair_attempt_id"] == repair_attempt_id
    assert original_attempt_id != repair_attempt_id
    repair_events = [
        event for event in EventStore(reopened).list_all()
        if event.event_type == EventType.REPAIR_COMPLETED
        and event.payload.get("step_id") == completed_step.id
    ]
    assert repair_events
    assert repair_events[-1].payload["original_failure_attempt_id"] == original_attempt_id
    assert repair_events[-1].payload["repair_attempt_id"] == repair_attempt_id
    reopened.close()


def test_t6_verified_ancestor_is_not_reexecuted_during_local_repair(db):
    """T8: a VERIFIED ancestor remains untouched while B is locally repaired."""
    a = PlanStep(id="a", title="A", objective="A", capability="cap_a")
    b = PlanStep(id="b", title="B", objective="B", capability="cap_b", depends_on=["a"])
    c = PlanStep(id="c", title="C", objective="C", capability="cap_c", depends_on=["b"])
    b_handler, _ = _make_fail_handler(fail_until_call=1)
    service, goal, counts = _setup_service(
        db,
        [a, b, c],
        verifier=AcceptingVerifier(),
        tool_handlers={"cap_b": b_handler},
    )
    result = asyncio.run(service.execute_goal(goal))
    states = {step.id: step.status for step in result.steps}
    assert states == {"a": PlanStepStatus.VERIFIED, "b": PlanStepStatus.VERIFIED, "c": PlanStepStatus.VERIFIED}
    assert counts == {"cap_a": 1, "cap_b": 2, "cap_c": 1}
