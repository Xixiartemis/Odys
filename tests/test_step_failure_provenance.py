"""Phase 3.3 — Failure Provenance tests.

Tests verify:
- StepFailureProvenance model creation and validation
- compute_repair_scope_hint returns correct hints for different failure types
- Step failure creates provenance with correct failure_class
- Provenance survives plan save/reload
- Provenance links to correct attempt_id and run_id
- STEP_FAILURE_PROVENANCE event is emitted
- ReplanSignal references the provenance
"""

from __future__ import annotations

import asyncio
import json

import pytest

from lhas.domain.enums import (
    AttemptStatus,
    EventType,
    ExecutionStatus,
    FailureClass,
    FailureType,
    RunStatus,
)
from lhas.domain.models import Attempt, Project, Run, Task
from lhas.executors.protocol import ExecutionRequest, ExecutionResult
from lhas.failure import FailureReport
from lhas.persistence.event_store import EventStore
from lhas.persistence.phaseb_repos import FailureReportRepository
from lhas.persistence.planning_repositories import PlanRepository
from lhas.persistence.repositories import AttemptRepository, ProjectRepository, RunRepository
from lhas.planning.models import (
    CapabilitySpec,
    Goal,
    Plan,
    PlanMode,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    RepairScopeHint,
    StepFailureProvenance,
    compute_repair_scope_hint,
)
from lhas.planning.service import PlanExecutionService
from lhas.tools.fakes import FakeTool
from lhas.tools.protocol import ToolResult, ToolResultStatus
from lhas.tools.registry import ToolRegistry
from tests.helpers import make_test_capability_definition, make_test_capability_registry


class FixedPlanner:
    def __init__(self, plan):
        self.plan = plan

    async def create_plan(self, **kwargs):
        return self.plan


# ---------------------------------------------------------------------------
# A. compute_repair_scope_hint correctness
# ---------------------------------------------------------------------------


class TestRepairScopeHint:
    """Verify compute_repair_scope_hint returns correct hints."""

    def test_provider_failures_yield_macro_replan(self):
        """PROVIDER_FAILURE / RESOURCE_EXHAUSTED → MACRO_REPLAN."""
        provider_types = [
            FailureType.QUOTA_EXHAUSTED,
            FailureType.BILLING_OR_CREDIT_EXHAUSTED,
            FailureType.AUTH_INVALID,
            FailureType.PROVIDER_UNAVAILABLE,
            FailureType.PROVIDER_TIMEOUT,
            FailureType.MALFORMED_PROVIDER_RESPONSE,
            FailureType.UNKNOWN_PROVIDER_FAILURE,
            FailureType.BUDGET_EXHAUSTED,
            FailureType.NETWORK_ERROR,
        ]
        for ft in provider_types:
            hint = compute_repair_scope_hint(FailureClass.EXECUTION, ft)
            assert hint == RepairScopeHint.MACRO_REPLAN, f"{ft} should be MACRO_REPLAN, got {hint}"

    def test_assumption_invalid_yields_affected_subgraph(self):
        """WRONG_ASSUMPTION → AFFECTED_SUBGRAPH."""
        hint = compute_repair_scope_hint(FailureClass.REASONING, FailureType.WRONG_ASSUMPTION)
        assert hint == RepairScopeHint.AFFECTED_SUBGRAPH

    def test_stale_context_yields_affected_subgraph(self):
        """STALE_CONTEXT → AFFECTED_SUBGRAPH."""
        hint = compute_repair_scope_hint(FailureClass.CONTEXT, FailureType.STALE_CONTEXT)
        assert hint == RepairScopeHint.AFFECTED_SUBGRAPH

    def test_context_conflict_yields_affected_subgraph(self):
        """CONTEXT_CONFLICT → AFFECTED_SUBGRAPH."""
        hint = compute_repair_scope_hint(FailureClass.CONTEXT, FailureType.CONTEXT_CONFLICT)
        assert hint == RepairScopeHint.AFFECTED_SUBGRAPH

    def test_tool_error_yields_local(self):
        """TOOL_ERROR → LOCAL."""
        hint = compute_repair_scope_hint(FailureClass.EXECUTION, FailureType.TOOL_ERROR)
        assert hint == RepairScopeHint.LOCAL

    def test_data_validation_yields_local(self):
        """Data/validation failures → LOCAL (re-verify)."""
        local_types = [
            FailureType.EMPTY_RESULT,
            FailureType.MISSING_REQUIRED_FIELD,
        ]
        for ft in local_types:
            hint = compute_repair_scope_hint(FailureClass.DATA, ft)
            assert hint == RepairScopeHint.LOCAL, f"{ft} should be LOCAL, got {hint}"

    def test_default_yields_local(self):
        """Unknown/default failures → LOCAL."""
        hint = compute_repair_scope_hint(FailureClass.UNKNOWN, FailureType.UNKNOWN)
        assert hint == RepairScopeHint.LOCAL

    def test_timeout_yields_local(self):
        """TIMEOUT (not a provider failure) → LOCAL."""
        hint = compute_repair_scope_hint(FailureClass.EXECUTION, FailureType.TIMEOUT)
        assert hint == RepairScopeHint.LOCAL


# ---------------------------------------------------------------------------
# B. StepFailureProvenance model
# ---------------------------------------------------------------------------


class TestStepFailureProvenanceModel:
    """Verify the StepFailureProvenance Pydantic model."""

    def test_model_creation(self):
        """StepFailureProvenance creates with all required fields."""
        prov = StepFailureProvenance(
            step_id="s1",
            plan_id="p1",
            failure_class=FailureClass.EXECUTION,
            failure_type=FailureType.TOOL_ERROR,
            failure_evidence={"summary": "tool failed", "evidence": "error msg"},
            attempt_id="a1",
            run_id="r1",
            repair_scope_hint=RepairScopeHint.LOCAL,
        )
        assert prov.step_id == "s1"
        assert prov.plan_id == "p1"
        assert prov.failure_class == FailureClass.EXECUTION
        assert prov.failure_type == FailureType.TOOL_ERROR
        assert prov.attempt_id == "a1"
        assert prov.run_id == "r1"
        assert prov.repair_scope_hint == RepairScopeHint.LOCAL

    def test_model_dump_roundtrip(self):
        """Provenance survives model_dump → dict → reconstruction."""
        prov = StepFailureProvenance(
            step_id="s1",
            plan_id="p1",
            failure_class=FailureClass.EXECUTION,
            failure_type=FailureType.QUOTA_EXHAUSTED,
            failure_evidence={"summary": "quota exhausted"},
            attempt_id="a1",
            run_id="r1",
            repair_scope_hint=RepairScopeHint.MACRO_REPLAN,
        )
        dumped = prov.model_dump(mode="json")
        assert dumped["failure_class"] == "EXECUTION"
        assert dumped["failure_type"] == "QUOTA_EXHAUSTED"
        assert dumped["repair_scope_hint"] == "MACRO_REPLAN"

        # Reconstruct from dump
        restored = StepFailureProvenance(**dumped)
        assert restored.failure_class == prov.failure_class
        assert restored.failure_type == prov.failure_type
        assert restored.repair_scope_hint == prov.repair_scope_hint

    def test_model_forbids_extra_fields(self):
        """StepFailureProvenance rejects unknown fields."""
        with pytest.raises(Exception):
            StepFailureProvenance(
                step_id="s1",
                plan_id="p1",
                failure_class=FailureClass.EXECUTION,
                failure_type=FailureType.TOOL_ERROR,
                attempt_id="a1",
                run_id="r1",
                repair_scope_hint=RepairScopeHint.LOCAL,
                unknown_field="bad",
            )


# ---------------------------------------------------------------------------
# C. Integration: step failure creates provenance
# ---------------------------------------------------------------------------


def _make_failing_tool(name: str, error_type: str = "TOOL_ERROR"):
    """Create a FakeTool that always fails with the given error_type."""
    def fail_fn(req):
        return ToolResult(
            status=ToolResultStatus.FAILURE,
            error_type=error_type,
            error_message=f"{error_type} simulated failure",
        )
    return FakeTool(CapabilitySpec(name=name, description=name), fail_fn)


def _setup_service_with_failing_tool(db, plan, error_type="TOOL_ERROR"):
    """Set up a PlanExecutionService with a tool that fails."""
    reg = ToolRegistry()
    reg.register(_make_failing_tool(plan.steps[0].capability, error_type))
    defs = [make_test_capability_definition(plan.steps[0].capability, output_schema={})]
    cap_reg, contract = make_test_capability_registry(reg, defs)
    return PlanExecutionService(
        db, FixedPlanner(plan), reg,
        capability_registry=cap_reg, tool_contract=contract,
    )


class TestStepFailureProvenanceIntegration:
    """Integration tests: step failure creates provenance."""

    def test_step_failure_creates_provenance_tool_error(self, db):
        """Step with TOOL_ERROR failure gets provenance with correct failure_class."""
        project = Project(name="prov-test")
        ProjectRepository(db).create(project)
        goal = Goal(project_id=project.id, objective="provenance test")

        a = PlanStep(id="a", title="A", objective="A", capability="a")
        plan = Plan(goal_id=goal.id, mode=PlanMode.SIMPLE_DEPENDENCY, steps=[a])

        service = _setup_service_with_failing_tool(db, plan, "TOOL_ERROR")
        result = asyncio.run(service.execute_goal(goal))

        assert result.status == PlanStatus.FAILED
        a_step = next(s for s in result.steps if s.id == "a")
        assert a_step.status == PlanStepStatus.FAILED

        # Verify provenance was stored
        assert "failure_provenance" in a_step.evidence
        prov = a_step.evidence["failure_provenance"]
        assert prov["failure_class"] == "EXECUTION"
        assert prov["failure_type"] == "TOOL_ERROR"
        assert prov["step_id"] == "a"
        assert prov["plan_id"] == plan.id
        assert "attempt_id" in prov
        assert "run_id" in prov
        assert prov["repair_scope_hint"] == "LOCAL"

    def test_step_failure_creates_provenance_quota_exhausted(self, db):
        """Step with QUOTA_EXHAUSTED failure gets MACRO_REPLAN hint."""
        project = Project(name="quota-test")
        ProjectRepository(db).create(project)
        goal = Goal(project_id=project.id, objective="quota test")

        a = PlanStep(id="a", title="A", objective="A", capability="a")
        plan = Plan(goal_id=goal.id, mode=PlanMode.SIMPLE_DEPENDENCY, steps=[a])

        service = _setup_service_with_failing_tool(db, plan, "QUOTA_EXHAUSTED")
        result = asyncio.run(service.execute_goal(goal))

        a_step = next(s for s in result.steps if s.id == "a")
        assert a_step.status == PlanStepStatus.FAILED
        prov = a_step.evidence["failure_provenance"]
        assert prov["failure_type"] == "QUOTA_EXHAUSTED"
        assert prov["repair_scope_hint"] == "MACRO_REPLAN"

    def test_step_failure_emits_provenance_event(self, db):
        """STEP_FAILURE_PROVENANCE event is emitted on step failure."""
        project = Project(name="event-test")
        ProjectRepository(db).create(project)
        goal = Goal(project_id=project.id, objective="event test")

        a = PlanStep(id="a", title="A", objective="A", capability="a")
        plan = Plan(goal_id=goal.id, mode=PlanMode.SIMPLE_DEPENDENCY, steps=[a])

        service = _setup_service_with_failing_tool(db, plan, "TOOL_ERROR")
        asyncio.run(service.execute_goal(goal))

        events = EventStore(db).list_all()
        prov_events = [e for e in events if e.event_type == EventType.STEP_FAILURE_PROVENANCE]
        assert len(prov_events) >= 1
        event = prov_events[0]
        assert event.payload["step_id"] == "a"
        assert event.payload["failure_class"] == "EXECUTION"
        assert event.payload["failure_type"] == "TOOL_ERROR"
        assert event.payload["repair_scope_hint"] == "LOCAL"
        assert "run_id" in event.payload
        assert "attempt_id" in event.payload

    def test_provenance_links_to_correct_attempt_and_run(self, db):
        """Provenance links to the correct attempt_id and run_id."""
        project = Project(name="link-test")
        ProjectRepository(db).create(project)
        goal = Goal(project_id=project.id, objective="link test")

        a = PlanStep(id="a", title="A", objective="A", capability="a")
        plan = Plan(goal_id=goal.id, mode=PlanMode.SIMPLE_DEPENDENCY, steps=[a])

        service = _setup_service_with_failing_tool(db, plan, "TOOL_ERROR")
        result = asyncio.run(service.execute_goal(goal))

        a_step = next(s for s in result.steps if s.id == "a")
        prov = a_step.evidence["failure_provenance"]

        # Verify the run and attempt exist in the DB
        run = RunRepository(db).get(prov["run_id"])
        assert run is not None
        assert run.task_id is not None

        attempts = AttemptRepository(db).list_for_run(prov["run_id"])
        attempt_ids = [a.id for a in attempts]
        assert prov["attempt_id"] in attempt_ids


# ---------------------------------------------------------------------------
# D. Provenance survives plan save/reload
# ---------------------------------------------------------------------------


class TestProvenancePersistence:
    """Provenance stored in step.evidence persists through plan save/reload."""

    def test_provenance_survives_plan_reload(self, db):
        """After plan save and reload, failure_provenance is preserved."""
        project = Project(name="persist-test")
        ProjectRepository(db).create(project)
        goal = Goal(project_id=project.id, objective="persist test")

        a = PlanStep(id="a", title="A", objective="A", capability="a")
        plan = Plan(goal_id=goal.id, mode=PlanMode.SIMPLE_DEPENDENCY, steps=[a])

        service = _setup_service_with_failing_tool(db, plan, "TOOL_ERROR")
        result = asyncio.run(service.execute_goal(goal))

        # Reload the plan from DB
        plans = PlanRepository(db)
        reloaded = plans.get(result.id)
        assert reloaded is not None

        a_step = next(s for s in reloaded.steps if s.id == "a")
        assert "failure_provenance" in a_step.evidence

        prov = a_step.evidence["failure_provenance"]
        assert prov["failure_class"] == "EXECUTION"
        assert prov["failure_type"] == "TOOL_ERROR"
        assert prov["repair_scope_hint"] == "LOCAL"
        assert prov["step_id"] == "a"
        assert prov["plan_id"] == reloaded.id

    def test_provenance_fingerprint_survives_reload(self, db):
        """Provenance data is identical before and after plan reload."""
        project = Project(name="fingerprint-test")
        ProjectRepository(db).create(project)
        goal = Goal(project_id=project.id, objective="fingerprint test")

        a = PlanStep(id="a", title="A", objective="A", capability="a")
        plan = Plan(goal_id=goal.id, mode=PlanMode.SIMPLE_DEPENDENCY, steps=[a])

        service = _setup_service_with_failing_tool(db, plan, "BUDGET_EXHAUSTED")
        result = asyncio.run(service.execute_goal(goal))

        # Get provenance from in-memory result
        a_step = next(s for s in result.steps if s.id == "a")
        prov_before = a_step.evidence["failure_provenance"]

        # Reload from DB
        reloaded = PlanRepository(db).get(result.id)
        a_reloaded = next(s for s in reloaded.steps if s.id == "a")
        prov_after = a_reloaded.evidence["failure_provenance"]

        # Must be identical
        assert prov_before == prov_after


# ---------------------------------------------------------------------------
# E. Dependency plan (DEP path) provenance
# ---------------------------------------------------------------------------


class TestDependencyPathProvenance:
    """Provenance creation works in the SIMPLE_DEPENDENCY execution path."""

    def test_dep_path_failure_creates_provenance(self, db):
        """DEP path: step failure creates provenance."""
        project = Project(name="dep-prov-test")
        ProjectRepository(db).create(project)
        goal = Goal(project_id=project.id, objective="dep provenance test")

        a = PlanStep(id="a", title="A", objective="A", capability="a")
        b = PlanStep(id="b", title="B", objective="B", capability="b", depends_on=["a"])
        plan = Plan(goal_id=goal.id, mode=PlanMode.SIMPLE_DEPENDENCY, steps=[a, b])

        reg = ToolRegistry()
        # A succeeds, B fails
        reg.register(FakeTool(
            CapabilitySpec(name="a", description="a"),
            lambda r: ToolResult(status=ToolResultStatus.SUCCESS, output="ok"),
        ))
        reg.register(_make_failing_tool("b", "TOOL_ERROR"))
        defs = [
            make_test_capability_definition("a", output_schema={}),
            make_test_capability_definition("b", output_schema={}),
        ]
        cap_reg, contract = make_test_capability_registry(reg, defs)

        from tests.helpers import AcceptingVerifier

        service = PlanExecutionService(
            db, FixedPlanner(plan), reg,
            capability_registry=cap_reg, tool_contract=contract,
            workflow_verifier=AcceptingVerifier(),
        )
        result = asyncio.run(service.execute_goal(goal))

        b_step = next(s for s in result.steps if s.id == "b")
        assert b_step.status == PlanStepStatus.FAILED
        assert "failure_provenance" in b_step.evidence

        prov = b_step.evidence["failure_provenance"]
        assert prov["failure_type"] == "TOOL_ERROR"
        assert prov["step_id"] == "b"


# ---------------------------------------------------------------------------
# F. ReplanSignal references provenance
# ---------------------------------------------------------------------------


class TestReplanSignalProvenanceReference:
    """ReplanSignal's evidence includes the failure_provenance reference."""

    def test_replan_signal_includes_provenance(self, db):
        """When a replan signal is created, it references the provenance."""
        from lhas.native.persistence import ReplanSignalRepository

        project = Project(name="signal-test")
        ProjectRepository(db).create(project)
        goal = Goal(project_id=project.id, objective="signal test")

        a = PlanStep(id="a", title="A", objective="A", capability="a")
        plan = Plan(goal_id=goal.id, mode=PlanMode.SIMPLE_DEPENDENCY, steps=[a])

        # Use an error type that triggers a replan signal (ASSUMPTION_INVALID).
        # The RuleFailureClassifier doesn't have a specific rule for this type,
        # so it classifies as UNKNOWN, but the ReplanTriggerPolicy still triggers
        # based on the raw error_type in the attempt.
        service = _setup_service_with_failing_tool(db, plan, "ASSUMPTION_INVALID")
        result = asyncio.run(service.execute_goal(goal))

        # Verify provenance was created on the step
        a_step = next(s for s in result.steps if s.id == "a")
        assert "failure_provenance" in a_step.evidence
        prov = a_step.evidence["failure_provenance"]
        assert prov["step_id"] == "a"

        # Verify that a replan signal was created and references the provenance
        attempts = AttemptRepository(db).list_for_run(prov["run_id"])
        signal_repo = ReplanSignalRepository(db)
        signals_found = []
        for attempt in attempts:
            signals_found.extend(signal_repo.list_for_attempt(attempt.id))
        # At least one signal should have the provenance reference
        provenance_refs = [s for s in signals_found if s.evidence.get("failure_provenance")]
        assert len(provenance_refs) >= 1, f"Expected replan signal with provenance ref, found {len(signals_found)} signals"
        ref = provenance_refs[0]
        assert ref.evidence["failure_provenance"]["step_id"] == "a"


# ---------------------------------------------------------------------------
# G. Multiple failure types through the service
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error_type", "expected_failure_type", "expected_hint"),
    [
        ("TOOL_ERROR", "TOOL_ERROR", "LOCAL"),
        ("QUOTA_EXHAUSTED", "QUOTA_EXHAUSTED", "MACRO_REPLAN"),
        ("PROVIDER_UNAVAILABLE", "PROVIDER_UNAVAILABLE", "MACRO_REPLAN"),
        ("BUDGET_EXHAUSTED", "BUDGET_EXHAUSTED", "MACRO_REPLAN"),
        ("AUTH_INVALID", "AUTH_INVALID", "MACRO_REPLAN"),
    ],
)
def test_parametrized_failure_types(db, error_type, expected_failure_type, expected_hint):
    """Different failure types produce correct provenance."""
    project = Project(name=f"param-{error_type.lower()}")
    ProjectRepository(db).create(project)
    goal = Goal(project_id=project.id, objective=f"param test {error_type}")

    a = PlanStep(id="a", title="A", objective="A", capability="a")
    plan = Plan(goal_id=goal.id, mode=PlanMode.SIMPLE_DEPENDENCY, steps=[a])

    service = _setup_service_with_failing_tool(db, plan, error_type)
    result = asyncio.run(service.execute_goal(goal))

    a_step = next(s for s in result.steps if s.id == "a")
    assert a_step.status == PlanStepStatus.FAILED
    assert "failure_provenance" in a_step.evidence

    prov = a_step.evidence["failure_provenance"]
    assert prov["failure_type"] == expected_failure_type
    assert prov["repair_scope_hint"] == expected_hint
