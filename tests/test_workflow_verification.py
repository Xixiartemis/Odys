"""Tests for WorkflowVerifier integration with PlanExecutionService.

Proves all 3 verification seam paths (B4) and strictness constraints (B5):
- ACCEPT → VERIFIED
- REJECT → CLASSIFIED_FAILURE
- No verifier → WAITING_FOR_VERIFICATION
- ToolResult SUCCESS alone does NOT qualify as ACCEPT
- Agent textual 'done' alone does NOT qualify
- Verification evaluates actual criteria
"""

import asyncio

import pytest

from lhas.domain.models import Project
from lhas.persistence.database import Database
from lhas.persistence.event_store import EventStore
from lhas.persistence.phaseb_repos import ValidationResultRepository
from lhas.persistence.repositories import ProjectRepository
from lhas.planning.models import (
    CapabilitySpec,
    Goal,
    Plan,
    PlanMode,
    PlanStep,
    PlanStepStatus,
)
from lhas.planning.service import PlanExecutionService
from lhas.planning.verification import VerificationResult, WorkflowVerifier
from lhas.tools.fakes import FakeTool
from lhas.tools.registry import ToolRegistry
from tests.helpers import (
    AcceptingVerifier,
    RejectingVerifier,
    make_test_capability_definition,
    make_test_capability_registry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path):
    db = Database(tmp_path / "test.db")
    db.init_db()
    return db


def _make_project(db):
    return ProjectRepository(db).create(Project(name="verification-test"))


class _SingleStepPlanner:
    """Planner that creates one step with explicit success_criteria."""

    def __init__(self, success_criteria=None, expected_effects=None):
        self.success_criteria = success_criteria or []
        self.expected_effects = expected_effects or {}

    async def create_plan(self, *, goal, capabilities, context=None):
        cap_name = goal.allowed_capabilities[0] if goal.allowed_capabilities else capabilities[0].name
        step = PlanStep(
            title=f"Execute {cap_name}",
            objective=goal.objective,
            capability=cap_name,
            depends_on=[],
            expected_output="bounded result",
            success_criteria=list(self.success_criteria),
            expected_effects=dict(self.expected_effects),
            inputs={},
        )
        return Plan(
            goal_id=goal.id,
            mode=PlanMode.LINEAR,
            status="READY",
            steps=[step],
            version="P-1.0",
        )


def _build_service(db, planner, handler, capability_name="test.cap", workflow_verifier=None):
    """Build a PlanExecutionService with a single FakeTool and the given verifier."""
    project = _make_project(db)
    spec = CapabilitySpec(name=capability_name, description=capability_name)
    reg = ToolRegistry()
    reg.register(FakeTool(spec, handler))
    defs = [make_test_capability_definition(spec.name, input_schema=spec.input_schema)]
    cap_reg, contract = make_test_capability_registry(reg, defs)
    svc = PlanExecutionService(
        db, planner, reg,
        capability_registry=cap_reg,
        tool_contract=contract,
        workflow_verifier=workflow_verifier,
    )
    goal = Goal(
        project_id=project.id,
        objective="verification test",
        allowed_capabilities=[capability_name],
        metadata={"plan_steps": [capability_name]},
    )
    return svc, goal


# ===========================================================================
# B4: All 3 verification paths
# ===========================================================================


class TestVerificationPaths:
    """B4: ACCEPT → VERIFIED, REJECT → CLASSIFIED_FAILURE, no-verifier → WAITING_FOR_VERIFICATION."""

    def test_accept_produces_verified(self, tmp_path):
        """ACCEPT path: verifier accepts → step becomes VERIFIED."""
        db = _make_db(tmp_path)
        planner = _SingleStepPlanner(success_criteria=["expected:marker"])
        verifier = WorkflowVerifier(db)
        svc, goal = _build_service(db, planner, lambda req: "output contains expected:marker here", workflow_verifier=verifier)

        plan = asyncio.run(svc.execute_goal(goal))
        assert plan.status.value == "COMPLETED"
        assert plan.steps[0].status == PlanStepStatus.VERIFIED

        # Durable: validation result persisted
        validations = ValidationResultRepository(db).list_for_attempt(plan.steps[0].task_id)
        assert len(validations) >= 1
        assert validations[-1].passed is True

    def test_reject_produces_classified_failure(self, tmp_path):
        """REJECT path: verifier rejects → step becomes CLASSIFIED_FAILURE."""
        db = _make_db(tmp_path)
        planner = _SingleStepPlanner(success_criteria=["expected:marker"])
        verifier = WorkflowVerifier(db)
        # Handler returns SUCCESS (FakeTool wraps in SUCCESS) but output misses the marker
        svc, goal = _build_service(db, planner, lambda req: "some output without the marker", workflow_verifier=verifier)

        plan = asyncio.run(svc.execute_goal(goal))
        assert plan.status.value == "FAILED"
        assert plan.steps[0].status == PlanStepStatus.CLASSIFIED_FAILURE

        # Durable: validation result persisted and marked as failed
        validations = ValidationResultRepository(db).list_for_attempt(plan.steps[0].task_id)
        assert len(validations) >= 1
        assert validations[-1].passed is False

    def test_no_verifier_produces_waiting_for_verification(self, tmp_path):
        """No-verifier path: workflow_verifier=None → WAITING_FOR_VERIFICATION."""
        db = _make_db(tmp_path)
        planner = _SingleStepPlanner(success_criteria=["anything"])
        # Explicitly no verifier
        svc, goal = _build_service(db, planner, lambda req: "output with anything marker", workflow_verifier=None)

        plan = asyncio.run(svc.execute_goal(goal))
        assert plan.steps[0].status == PlanStepStatus.WAITING_FOR_VERIFICATION
        # Plan-level status also reflects waiting
        assert plan.status.value == "WAITING_FOR_VERIFICATION"


# ===========================================================================
# B5: Strictness — success alone is not acceptance
# ===========================================================================


class TestVerificationStrictness:
    """B5: ToolResult SUCCESS alone does NOT qualify; 'done' alone does NOT qualify."""

    def test_tool_success_without_criteria_match_is_reject(self, tmp_path):
        """ToolResult SUCCESS but output misses criteria → REJECT."""
        db = _make_db(tmp_path)
        planner = _SingleStepPlanner(success_criteria=["file_created:ok"])
        verifier = WorkflowVerifier(db)
        # FakeTool returns SUCCESS, but output does NOT contain "file_created:ok"
        svc, goal = _build_service(db, planner, lambda req: {"status": "completed"}, workflow_verifier=verifier)

        plan = asyncio.run(svc.execute_goal(goal))
        # Tool returned SUCCESS, but verifier rejects because criteria not met
        assert plan.steps[0].status == PlanStepStatus.CLASSIFIED_FAILURE
        assert plan.status.value == "FAILED"

    def test_agent_textual_done_is_not_accept(self, tmp_path):
        """Agent says 'done' but criteria require specific evidence → REJECT."""
        db = _make_db(tmp_path)
        planner = _SingleStepPlanner(success_criteria=["exit_code:0"])
        verifier = WorkflowVerifier(db)
        # Agent just says "done" — no structured evidence matching criteria
        svc, goal = _build_service(db, planner, lambda req: "done", workflow_verifier=verifier)

        plan = asyncio.run(svc.execute_goal(goal))
        assert plan.steps[0].status == PlanStepStatus.CLASSIFIED_FAILURE

    def test_verification_evaluates_actual_criteria(self, tmp_path):
        """Verification checks actual criteria markers, not just output existence."""
        db = _make_db(tmp_path)
        planner = _SingleStepPlanner(success_criteria=["artifact:saved", "count:3"])
        verifier = WorkflowVerifier(db)
        # Output matches first criterion but NOT the second
        svc, goal = _build_service(db, planner, lambda req: "artifact:saved successfully", workflow_verifier=verifier)

        plan = asyncio.run(svc.execute_goal(goal))
        # First criterion matches, second does not → REJECT
        assert plan.steps[0].status == PlanStepStatus.CLASSIFIED_FAILURE

        # Verify the validation record has specific check details
        validations = ValidationResultRepository(db).list_for_attempt(plan.steps[0].task_id)
        assert len(validations) >= 1
        v = validations[-1]
        assert v.passed is False
        check_names = [c.name for c in v.checks]
        assert "criterion:artifact:saved" in check_names
        assert "criterion:count:3" in check_names
        # The first should pass, the second should fail
        artifact_check = next(c for c in v.checks if c.name == "criterion:artifact:saved")
        count_check = next(c for c in v.checks if c.name == "criterion:count:3")
        assert artifact_check.passed is True
        assert count_check.passed is False

    def test_output_without_all_criteria_fails_check_detail(self, tmp_path):
        """Partial criteria match: detail shows which criterion failed."""
        db = _make_db(tmp_path)
        planner = _SingleStepPlanner(success_criteria=["alpha", "beta", "gamma"])
        verifier = WorkflowVerifier(db)
        # Output matches only "alpha", misses "beta" and "gamma"
        svc, goal = _build_service(db, planner, lambda req: "alpha found", workflow_verifier=verifier)

        plan = asyncio.run(svc.execute_goal(goal))
        assert plan.steps[0].status == PlanStepStatus.CLASSIFIED_FAILURE

        validations = ValidationResultRepository(db).list_for_attempt(plan.steps[0].task_id)
        assert len(validations) >= 1
        v = validations[-1]
        assert v.passed is False
        # Structural check passes (output is non-empty)
        output_check = next(c for c in v.checks if c.name == "step_output_non_empty")
        assert output_check.passed is True
        # First criterion passes
        alpha_check = next(c for c in v.checks if c.name == "criterion:alpha")
        assert alpha_check.passed is True
        # Second and third fail
        beta_check = next(c for c in v.checks if c.name == "criterion:beta")
        gamma_check = next(c for c in v.checks if c.name == "criterion:gamma")
        assert beta_check.passed is False
        assert gamma_check.passed is False

    def test_matching_criteria_is_accept(self, tmp_path):
        """All criteria present in output → ACCEPT and VERIFIED."""
        db = _make_db(tmp_path)
        planner = _SingleStepPlanner(success_criteria=["build:ok", "tests:pass"])
        verifier = WorkflowVerifier(db)
        svc, goal = _build_service(db, planner, lambda req: "build:ok\nAll tests:pass", workflow_verifier=verifier)

        plan = asyncio.run(svc.execute_goal(goal))
        assert plan.steps[0].status == PlanStepStatus.VERIFIED
        assert plan.status.value == "COMPLETED"

        # Durable: validation result persisted and marked as passed
        validations = ValidationResultRepository(db).list_for_attempt(plan.steps[0].task_id)
        assert len(validations) >= 1
        assert validations[-1].passed is True


# ===========================================================================
# WorkflowVerifier unit tests (without PlanExecutionService)
# ===========================================================================


class TestWorkflowVerifierUnit:
    """Direct unit tests for WorkflowVerifier logic."""

    def test_verification_result_attributes(self):
        """VerificationResult exposes .accepted and .reason."""
        r = VerificationResult(True, "all good")
        assert r.accepted is True
        assert r.reason == "all good"
        assert r.validation is None

    def test_no_criteria_nonempty_output_accepts(self, tmp_path):
        """No success_criteria + non-empty output → accept."""
        db = _make_db(tmp_path)
        verifier = WorkflowVerifier(db)

        step = PlanStep(
            title="test", objective="test", capability="cap",
            output="some output", success_criteria=[],
        )
        events = EventStore(db)
        result = verifier.verify(step, type("Plan", (), {"id": "p1"})(), events)
        assert result.accepted is True
        assert result.validation is not None
        assert result.validation.passed is True

    def test_criteria_match_accepts(self, tmp_path):
        """Criteria present in output → accept."""
        db = _make_db(tmp_path)
        verifier = WorkflowVerifier(db)

        step = PlanStep(
            title="test", objective="test", capability="cap",
            output="the build:ok result is here", success_criteria=["build:ok"],
        )
        events = EventStore(db)
        result = verifier.verify(step, type("Plan", (), {"id": "p1"})(), events)
        assert result.accepted is True

    def test_criteria_mismatch_rejects(self, tmp_path):
        """Criteria NOT present in output → reject."""
        db = _make_db(tmp_path)
        verifier = WorkflowVerifier(db)

        step = PlanStep(
            title="test", objective="test", capability="cap",
            output="some unrelated output", success_criteria=["build:ok"],
        )
        events = EventStore(db)
        result = verifier.verify(step, type("Plan", (), {"id": "p1"})(), events)
        assert result.accepted is False
        assert "build:ok" in result.reason

    def test_expected_effects_verified(self, tmp_path):
        """Expected effects checked against execution context."""
        db = _make_db(tmp_path)
        verifier = WorkflowVerifier(db)

        step = PlanStep(
            title="test", objective="test", capability="cap",
            output="ok", success_criteria=[],
            expected_effects={"artifact_url": "https://example.com"},
            execution_context={
                "steps": {
                    "s1": {
                        "output": {},
                        "artifacts": {"artifact_url": "https://example.com"},
                    }
                }
            },
            id="s1",
        )
        events = EventStore(db)
        result = verifier.verify(step, type("Plan", (), {"id": "p1"})(), events)
        assert result.accepted is True

    def test_expected_effects_missing_rejects(self, tmp_path):
        """Missing expected effects → reject."""
        db = _make_db(tmp_path)
        verifier = WorkflowVerifier(db)

        step = PlanStep(
            title="test", objective="test", capability="cap",
            output="ok", success_criteria=[],
            expected_effects={"artifact_url": "https://example.com"},
            execution_context={"steps": {"s1": {"output": {}, "artifacts": {}}}},
            id="s1",
        )
        events = EventStore(db)
        result = verifier.verify(step, type("Plan", (), {"id": "p1"})(), events)
        assert result.accepted is False
        assert "artifact_url" in result.reason

    def test_persistence_survives_reload(self, tmp_path):
        """Validation result persists and can be reloaded."""
        db = _make_db(tmp_path)
        verifier = WorkflowVerifier(db)

        step = PlanStep(
            title="test", objective="test", capability="cap",
            output="expected:marker present",
            success_criteria=["expected:marker"],
            task_id="task-123",
        )
        events = EventStore(db)
        result = verifier.verify(step, type("Plan", (), {"id": "p1"})(), events)

        # Reload from DB
        repo = ValidationResultRepository(db)
        stored = repo.list_for_attempt("task-123")
        assert len(stored) == 1
        assert stored[0].id == result.validation.id
        assert stored[0].passed is True
        assert len(stored[0].checks) >= 2  # structural + criterion


# ===========================================================================
# Platform default verifier test
# ===========================================================================


class TestPlatformDefaultVerifier:
    """B3: PlatformGoalService uses WorkflowVerifier by default."""

    def test_platform_goal_service_default_verifier(self, tmp_path):
        """PlatformGoalService without explicit verifier uses WorkflowVerifier."""
        from lhas.agent.platform import PlatformGoalService
        from lhas.planning.planner import DeterministicPlanner

        db = _make_db(tmp_path)
        _make_project(db)
        reg = ToolRegistry()
        spec = CapabilitySpec(name="test.cap", description="test")
        reg.register(FakeTool(spec, lambda req: "output"))
        defs = [make_test_capability_definition(spec.name)]
        cap_reg, contract = make_test_capability_registry(reg, defs)

        # No workflow_verifier passed — should default to WorkflowVerifier
        svc = PlatformGoalService(
            db, DeterministicPlanner(), reg,
            tool_contract=contract, capability_registry=cap_reg,
        )
        assert svc.workflow_verifier is not None
        assert isinstance(svc.workflow_verifier, WorkflowVerifier)

    def test_explicit_verifier_not_overridden(self, tmp_path):
        """PlatformGoalService with explicit verifier keeps it."""
        from lhas.agent.platform import PlatformGoalService
        from lhas.planning.planner import DeterministicPlanner

        db = _make_db(tmp_path)
        reg = ToolRegistry()
        custom = AcceptingVerifier()

        svc = PlatformGoalService(
            db, DeterministicPlanner(), reg,
            workflow_verifier=custom,
        )
        assert svc.workflow_verifier is custom
