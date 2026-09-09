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


def _create_execution_chain(db, step, project_id="p1"):
    """Create Task → Run → Attempt chain for a step so WorkflowVerifier can resolve attempt_id."""
    from lhas.domain.models import Task, Run, Attempt
    from lhas.domain.enums import TaskStatus, RunStatus, AttemptStatus
    task = Task(project_id=project_id, title=step.title, objective=step.objective, max_attempts=1)
    run = Run(task_id=task.id, status=RunStatus.COMPLETED)
    attempt = Attempt(run_id=run.id, attempt_number=1, status=AttemptStatus.COMPLETED)
    from lhas.persistence.repositories import TaskRepository, RunRepository, AttemptRepository
    TaskRepository(db).create(task)
    RunRepository(db).create(run)
    AttemptRepository(db).create(attempt)
    step.task_id = task.id
    return task, run, attempt


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
        """ACCEPT path: structured evidence satisfies criterion → VERIFIED."""
        db = _make_db(tmp_path)
        planner = _SingleStepPlanner(success_criteria=["exit_code:0"])
        verifier = WorkflowVerifier(db)
        # Tool returns structured dict with exit_code=0
        svc, goal = _build_service(db, planner, lambda req: {"exit_code": 0, "status": "ok"}, workflow_verifier=verifier)

        plan = asyncio.run(svc.execute_goal(goal))
        assert plan.status.value == "COMPLETED"
        assert plan.steps[0].status == PlanStepStatus.VERIFIED

        # Durable: validation result persisted with real attempt_id
        from lhas.persistence.repositories import AttemptRepository, RunRepository
        runs = RunRepository(db).list_for_task(plan.steps[0].task_id)
        attempts = AttemptRepository(db).list_for_run(runs[-1].id)
        validations = ValidationResultRepository(db).list_for_attempt(attempts[-1].id)
        assert len(validations) >= 1
        assert validations[-1].passed is True

    def test_reject_produces_classified_failure(self, tmp_path):
        """REJECT path: unsupported criterion → verifier rejects → CLASSIFIED_FAILURE."""
        db = _make_db(tmp_path)
        planner = _SingleStepPlanner(success_criteria=["exit_code:0"])
        verifier = WorkflowVerifier(db)
        # Output does NOT contain "exit_code" → criterion fails → reject
        svc, goal = _build_service(db, planner, lambda req: "some output without exit code", workflow_verifier=verifier)

        plan = asyncio.run(svc.execute_goal(goal))
        assert plan.status.value == "FAILED"
        assert plan.steps[0].status == PlanStepStatus.CLASSIFIED_FAILURE

        # Durable: validation result persisted with real attempt_id
        from lhas.persistence.repositories import AttemptRepository, RunRepository
        runs = RunRepository(db).list_for_task(plan.steps[0].task_id)
        attempts = AttemptRepository(db).list_for_run(runs[-1].id)
        validations = ValidationResultRepository(db).list_for_attempt(attempts[-1].id)
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
        """ToolResult SUCCESS but unsupported criteria → REJECT (fail closed)."""
        db = _make_db(tmp_path)
        planner = _SingleStepPlanner(success_criteria=["file_created:ok"])
        verifier = WorkflowVerifier(db)
        # FakeTool returns SUCCESS, but "file_created:ok" is unsupported → fail closed
        svc, goal = _build_service(db, planner, lambda req: {"status": "completed"}, workflow_verifier=verifier)

        plan = asyncio.run(svc.execute_goal(goal))
        # Tool returned SUCCESS, but verifier rejects because criterion is unsupported
        assert plan.steps[0].status == PlanStepStatus.CLASSIFIED_FAILURE
        assert plan.status.value == "FAILED"

    def test_agent_textual_done_is_not_accept(self, tmp_path):
        """Agent says 'done' but no independent evidence → REJECT."""
        db = _make_db(tmp_path)
        planner = _SingleStepPlanner(success_criteria=["tests passed"])
        verifier = WorkflowVerifier(db)
        # Agent just says "tests passed" — self-assertion, not independent evidence
        svc, goal = _build_service(db, planner, lambda req: "tests passed", workflow_verifier=verifier)

        plan = asyncio.run(svc.execute_goal(goal))
        assert plan.steps[0].status == PlanStepStatus.CLASSIFIED_FAILURE

    def test_verification_evaluates_actual_criteria(self, tmp_path):
        """Verification checks structured evidence keys, not agent text."""
        db = _make_db(tmp_path)
        # "exit_code:0" — key "exit_code" must exist with value 0 in structured output
        # "status:ok" — key "status" must exist with value "ok"
        planner = _SingleStepPlanner(success_criteria=["exit_code:0", "status:ok"])
        verifier = WorkflowVerifier(db)
        # Structured output has exit_code=0 but status=fail
        svc, goal = _build_service(db, planner, lambda req: {"exit_code": 0, "status": "fail"}, workflow_verifier=verifier)

        plan = asyncio.run(svc.execute_goal(goal))
        # exit_code:0 passes, status:ok fails (status="fail" != "ok") → REJECT
        assert plan.steps[0].status == PlanStepStatus.CLASSIFIED_FAILURE

        from lhas.persistence.repositories import AttemptRepository, RunRepository
        runs = RunRepository(db).list_for_task(plan.steps[0].task_id)
        attempts = AttemptRepository(db).list_for_run(runs[-1].id)
        validations = ValidationResultRepository(db).list_for_attempt(attempts[-1].id)
        assert len(validations) >= 1
        v = validations[-1]
        assert v.passed is False

    def test_output_without_all_criteria_fails_check_detail(self, tmp_path):
        """Unsupported criteria all fail closed."""
        db = _make_db(tmp_path)
        planner = _SingleStepPlanner(success_criteria=["alpha", "beta", "gamma"])
        verifier = WorkflowVerifier(db)
        # All criteria are unsupported → all fail closed
        svc, goal = _build_service(db, planner, lambda req: "alpha found", workflow_verifier=verifier)

        plan = asyncio.run(svc.execute_goal(goal))
        assert plan.steps[0].status == PlanStepStatus.CLASSIFIED_FAILURE

        from lhas.persistence.repositories import AttemptRepository, RunRepository
        runs = RunRepository(db).list_for_task(plan.steps[0].task_id)
        attempts = AttemptRepository(db).list_for_run(runs[-1].id)
        validations = ValidationResultRepository(db).list_for_attempt(attempts[-1].id)
        assert len(validations) >= 1
        v = validations[-1]
        assert v.passed is False
        output_check = next(c for c in v.checks if c.name == "step_output_non_empty")
        assert output_check.passed is True
        # All unsupported criteria fail closed
        for cname in ["criterion:alpha", "criterion:beta", "criterion:gamma"]:
            check = next(c for c in v.checks if c.name == cname)
            assert check.passed is False, f"{cname} should fail closed (unsupported)"

    def test_matching_criteria_is_accept(self, tmp_path):
        """All structured criteria satisfied → ACCEPT and VERIFIED."""
        db = _make_db(tmp_path)
        planner = _SingleStepPlanner(success_criteria=["exit_code:0"])
        verifier = WorkflowVerifier(db)
        svc, goal = _build_service(db, planner, lambda req: {"exit_code": 0}, workflow_verifier=verifier)

        plan = asyncio.run(svc.execute_goal(goal))
        assert plan.steps[0].status == PlanStepStatus.VERIFIED
        assert plan.status.value == "COMPLETED"

        # Durable: validation result persisted and marked as passed
        from lhas.persistence.repositories import AttemptRepository, RunRepository
        runs = RunRepository(db).list_for_task(plan.steps[0].task_id)
        attempts = AttemptRepository(db).list_for_run(runs[-1].id)
        validations = ValidationResultRepository(db).list_for_attempt(attempts[-1].id)
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
        _create_execution_chain(db, step)
        events = EventStore(db)
        result = verifier.verify(step, type("Plan", (), {"id": "p1"})(), events)
        assert result.accepted is True
        assert result.validation is not None
        assert result.validation.passed is True

    def test_criteria_match_accepts(self, tmp_path):
        """Structured criterion satisfied → accept."""
        db = _make_db(tmp_path)
        verifier = WorkflowVerifier(db)

        step = PlanStep(
            title="test", objective="test", capability="cap",
            output={"exit_code": 0}, success_criteria=["exit_code:0"],
        )
        _create_execution_chain(db, step)
        events = EventStore(db)
        result = verifier.verify(step, type("Plan", (), {"id": "p1"})(), events)
        assert result.accepted is True

    def test_criteria_mismatch_rejects(self, tmp_path):
        """Supported criterion NOT satisfied → reject."""
        db = _make_db(tmp_path)
        verifier = WorkflowVerifier(db)

        step = PlanStep(
            title="test", objective="test", capability="cap",
            output="Error: something went wrong. Traceback...", success_criteria=["no errors"],
        )
        _create_execution_chain(db, step)
        events = EventStore(db)
        result = verifier.verify(step, type("Plan", (), {"id": "p1"})(), events)
        assert result.accepted is False
        assert "not independently verified" in result.reason

    def test_expected_effects_verified(self, tmp_path):
        """Expected effects checked against execution context VALUES."""
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
        _create_execution_chain(db, step)
        events = EventStore(db)
        result = verifier.verify(step, type("Plan", (), {"id": "p1"})(), events)
        assert result.accepted is True

    def test_expected_effects_value_mismatch_rejects(self, tmp_path):
        """Expected effect key present but VALUE wrong → reject (BLOCKER 3)."""
        db = _make_db(tmp_path)
        verifier = WorkflowVerifier(db)

        step = PlanStep(
            title="test", objective="test", capability="cap",
            output="ok", success_criteria=[],
            expected_effects={"exit_code": 0},
            execution_context={
                "steps": {
                    "s1": {
                        "output": {"exit_code": 1},
                        "artifacts": {},
                    }
                }
            },
            id="s1",
        )
        _create_execution_chain(db, step)
        events = EventStore(db)
        result = verifier.verify(step, type("Plan", (), {"id": "p1"})(), events)
        assert result.accepted is False
        assert "exit_code" in result.reason
        assert "expected" in result.reason.lower()

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
        _create_execution_chain(db, step)
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
            output={"exit_code": 0},
            success_criteria=["exit_code:0"],
        )
        task, run, attempt = _create_execution_chain(db, step)
        events = EventStore(db)
        result = verifier.verify(step, type("Plan", (), {"id": "p1"})(), events)

        # Reload from DB using real attempt_id
        repo = ValidationResultRepository(db)
        stored = repo.list_for_attempt(attempt.id)
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
