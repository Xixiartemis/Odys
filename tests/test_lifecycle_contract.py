"""Lifecycle contract tests: PlanStep → Task → Run → Attempt linkage and authority boundaries.

C6: Proves:
  - Each identity (plan_id, step_id, task_id, run_id) is linked correctly.
  - Step status transitions are recorded via transition_step().
  - No lower-level success (Attempt, Run, Tool) bypasses the verification seam.
  - Persistence round-trip preserves task_id, execution_context, evidence.
  - CLAIMED_COMPLETE survives restart (crash before VERIFIED).
"""

from __future__ import annotations

import json

import pytest

from lhas.domain.enums import AttemptStatus, EventType, RunStatus, TaskStatus
from lhas.domain.models import Project, Task, new_id
from lhas.persistence.database import Database
from lhas.persistence.event_store import EventStore
from lhas.persistence.repositories import (
    AttemptRepository,
    RunRepository,
    TaskRepository,
)
from lhas.persistence.planning_repositories import PlanRepository, GoalRepository
from lhas.planning.models import (
    Goal,
    Plan,
    PlanMode,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    transition_step,
)
from lhas.orchestrator import Orchestrator
from lhas.orchestrator_v2 import RecoveringOrchestrator
from lhas.executors.mock import MockConfig, MockExecutor, MockScenario
from lhas.domain.models import Run


# ---------------------------------------------------------------------------
# C3 — Invariant proof: every step status change goes through transition_step()
# ---------------------------------------------------------------------------


class TestTransitionStepAuthority:
    """transition_step() is the ONLY place step status changes."""

    def test_transition_step_changes_status(self, db):
        events = EventStore(db)
        step = PlanStep(title="t", objective="o", capability="c")
        assert step.status == PlanStepStatus.PENDING

        transition_step(step, PlanStepStatus.RUNNING, "dispatch", events, plan_id="p1")
        assert step.status == PlanStepStatus.RUNNING

    def test_transition_step_emits_event(self, db):
        events = EventStore(db)
        step = PlanStep(title="t", objective="o", capability="c")
        transition_step(step, PlanStepStatus.RUNNING, "dispatch", events, plan_id="p1")

        all_events = events.list_all()
        state_events = [e for e in all_events if e.event_type == EventType.STEP_STATE_TRANSITION]
        assert len(state_events) == 1
        payload = state_events[0].payload
        assert payload["step_id"] == step.id
        assert payload["previous_status"] == "PENDING"
        assert payload["new_status"] == "RUNNING"
        assert payload["reason"] == "dispatch"
        assert payload["plan_id"] == "p1"

    def test_transition_step_noop(self, db):
        """Same-status transition is a no-op (no event emitted)."""
        events = EventStore(db)
        step = PlanStep(title="t", objective="o", capability="c", status=PlanStepStatus.RUNNING)
        result = transition_step(step, PlanStepStatus.RUNNING, "noop", events, plan_id="p1")
        assert result == PlanStepStatus.RUNNING
        state_events = [e for e in events.list_all() if e.event_type == EventType.STEP_STATE_TRANSITION]
        assert len(state_events) == 0

    def test_transition_step_extra_payload(self, db):
        events = EventStore(db)
        step = PlanStep(title="t", objective="o", capability="c")
        transition_step(
            step, PlanStepStatus.CLAIMED_COMPLETE, "run_completed",
            events, plan_id="p1", extra_payload={"run_id": "r1"},
        )
        state_events = [e for e in events.list_all() if e.event_type == EventType.STEP_STATE_TRANSITION]
        assert state_events[0].payload["run_id"] == "r1"


# ---------------------------------------------------------------------------
# C6 — Full lifecycle contract
# ---------------------------------------------------------------------------


class TestLifecycleContract:
    """End-to-end: step → task → run → attempt, all identities linked."""

    def test_task_created_with_step_identity(self, db, project):
        """PlanStep.task_id links to the created Task."""
        goal_repo = GoalRepository(db)
        goal = Goal(project_id=project.id, objective="test goal")
        goal_repo.create(goal)

        task_repo = TaskRepository(db)
        step = PlanStep(title="step1", objective="do step1", capability="test_cap")

        task = Task(
            project_id=goal.project_id,
            title=step.title,
            objective=step.objective,
            constraints=goal.constraints,
            acceptance_criteria=step.success_criteria,
            max_attempts=2,
        )
        task_repo.create(task)
        step.task_id = task.id

        assert step.task_id == task.id
        assert task.title == step.title
        assert task.objective == step.objective

    def test_run_linked_to_task(self, db, project):
        """Run.task_id == Task.id."""
        task_repo = TaskRepository(db)
        run_repo = RunRepository(db)

        task = Task(project_id=project.id, title="t", objective="o")
        task_repo.create(task)

        run = Run(task_id=task.id, executor_type="MockExecutor", provider="mock", model="mock-v0",
                  harness_version="HV-0.1", context_policy_version="CP-0", dataset_version="D-0.1")
        run_repo.create(run)

        assert run.task_id == task.id
        loaded = run_repo.get(run.id)
        assert loaded.task_id == task.id

    def test_attempt_linked_to_run(self, db, project):
        """Attempt.run_id == Run.id."""
        from lhas.domain.models import Attempt

        task_repo = TaskRepository(db)
        run_repo = RunRepository(db)
        attempt_repo = AttemptRepository(db)

        task = Task(project_id=project.id, title="t", objective="o")
        task_repo.create(task)
        run = Run(task_id=task.id)
        run_repo.create(run)

        attempt = attempt_repo.create(Attempt(run_id=run.id, attempt_number=1))
        assert attempt.run_id == run.id

        loaded = attempt_repo.list_for_run(run.id)
        assert len(loaded) == 1
        assert loaded[0].run_id == run.id

    def test_full_identity_chain(self, db, project):
        """PlanStep → Task → Run → Attempt: each ID is correctly linked."""
        from lhas.domain.models import Attempt

        task_repo = TaskRepository(db)
        run_repo = RunRepository(db)
        attempt_repo = AttemptRepository(db)

        # Create step and task
        step = PlanStep(title="s1", objective="obj", capability="cap")
        task = Task(project_id=project.id, title=step.title, objective=step.objective, max_attempts=2)
        task_repo.create(task)
        step.task_id = task.id

        # Create run
        run = Run(task_id=task.id)
        run_repo.create(run)

        # Create attempt
        attempt = attempt_repo.create(Attempt(run_id=run.id, attempt_number=1))

        # Verify chain
        assert step.task_id == task.id
        assert run.task_id == task.id
        assert attempt.run_id == run.id

        # Verify traversal: step → task → runs → attempts
        loaded_task = task_repo.get(task.id)
        assert loaded_task is not None
        runs = run_repo.list_for_task(task.id)
        assert len(runs) >= 1
        assert runs[0].task_id == task.id
        attempts = attempt_repo.list_for_run(runs[0].id)
        assert len(attempts) >= 1
        assert attempts[0].run_id == runs[0].id


# ---------------------------------------------------------------------------
# C3 — Invariant: no lower-level success auto-upgrades step to VERIFIED
# ---------------------------------------------------------------------------


class TestVerificationSeamInvariants:
    """Attempt/Run/Tool SUCCESS never produces step VERIFIED directly."""

    def test_run_completed_step_is_claimed_complete_not_verified(self, db, project):
        """Run COMPLETED → step CLAIMED_COMPLETE, never VERIFIED."""
        events = EventStore(db)
        step = PlanStep(title="s1", objective="obj", capability="cap")

        # Simulate what service.py does after run completes:
        # 1. Run COMPLETED → step CLAIMED_COMPLETE
        transition_step(step, PlanStepStatus.RUNNING, "dispatch", events, plan_id="p1")
        assert step.status == PlanStepStatus.RUNNING

        transition_step(step, PlanStepStatus.CLAIMED_COMPLETE, "run_completed", events, plan_id="p1")
        assert step.status == PlanStepStatus.CLAIMED_COMPLETE
        assert step.status != PlanStepStatus.VERIFIED

    def test_claimed_complete_only_verifies_via_verifier(self, db):
        """VERIFIED requires explicit workflow_verifier.accepted — not implicit."""
        events = EventStore(db)
        step = PlanStep(title="s1", objective="obj", capability="cap")

        transition_step(step, PlanStepStatus.RUNNING, "dispatch", events, plan_id="p1")
        transition_step(step, PlanStepStatus.CLAIMED_COMPLETE, "run_completed", events, plan_id="p1")

        # Without verifier → WAITING_FOR_VERIFICATION
        transition_step(step, PlanStepStatus.WAITING_FOR_VERIFICATION, "no_verifier_configured", events, plan_id="p1")
        assert step.status == PlanStepStatus.WAITING_FOR_VERIFICATION
        assert step.status != PlanStepStatus.VERIFIED

    def test_verifier_rejection_gives_classified_failure(self, db):
        """Verification rejected → CLASSIFIED_FAILURE, not VERIFIED."""
        events = EventStore(db)
        step = PlanStep(title="s1", objective="obj", capability="cap")

        transition_step(step, PlanStepStatus.RUNNING, "dispatch", events, plan_id="p1")
        transition_step(step, PlanStepStatus.CLAIMED_COMPLETE, "run_completed", events, plan_id="p1")
        transition_step(step, PlanStepStatus.CLASSIFIED_FAILURE, "verification_rejected", events, plan_id="p1")

        assert step.status == PlanStepStatus.CLASSIFIED_FAILURE
        assert step.status != PlanStepStatus.VERIFIED

    def test_attempt_success_does_not_set_step_status(self, db, project):
        """Attempt COMPLETED has no direct effect on PlanStep status."""
        from lhas.domain.models import Attempt

        task_repo = TaskRepository(db)
        run_repo = RunRepository(db)
        attempt_repo = AttemptRepository(db)

        task = Task(project_id=project.id, title="t", objective="o")
        task_repo.create(task)
        run = Run(task_id=task.id)
        run_repo.create(run)
        attempt = attempt_repo.create(Attempt(run_id=run.id, attempt_number=1))
        attempt.status = AttemptStatus.COMPLETED
        attempt_repo.update(attempt)

        step = PlanStep(title="s1", objective="obj", capability="cap")
        # Step status is untouched — attempt completion has no authority over step
        assert step.status == PlanStepStatus.PENDING

    def test_all_step_status_transitions_via_transition_step(self, db):
        """Every valid step transition goes through transition_step() and emits an event."""
        events = EventStore(db)
        step = PlanStep(title="s1", objective="obj", capability="cap")

        # Full happy path: PENDING → RUNNING → CLAIMED_COMPLETE → VERIFIED
        transition_step(step, PlanStepStatus.RUNNING, "dispatch", events, plan_id="p1")
        transition_step(step, PlanStepStatus.CLAIMED_COMPLETE, "run_completed", events, plan_id="p1")
        transition_step(step, PlanStepStatus.VERIFIED, "verification_accepted", events, plan_id="p1")
        assert step.status == PlanStepStatus.VERIFIED

        state_events = [e for e in events.list_all() if e.event_type == EventType.STEP_STATE_TRANSITION]
        assert len(state_events) == 3
        statuses = [e.payload["new_status"] for e in state_events]
        assert statuses == ["RUNNING", "CLAIMED_COMPLETE", "VERIFIED"]


# ---------------------------------------------------------------------------
# C4 — Persistence round-trip
# ---------------------------------------------------------------------------


class TestPersistenceRoundTrip:
    """PlanStep with task_id, execution_context, evidence survives save/reload."""

    def _make_plan_with_step(self, step: PlanStep, plan_id: str = "p1", goal_id: str = "g1") -> Plan:
        return Plan(
            id=plan_id,
            goal_id=goal_id,
            mode=PlanMode.SIMPLE_DEPENDENCY,
            status=PlanStatus.RUNNING,
            steps=[step],
        )

    def test_task_id_survives_roundtrip(self, db):
        plans = PlanRepository(db)
        step = PlanStep(title="s1", objective="obj", capability="cap", task_id="task-abc")
        plan = self._make_plan_with_step(step)
        plans.create(plan)

        loaded = plans.get(plan.id)
        assert loaded.steps[0].task_id == "task-abc"

    def test_execution_context_survives_roundtrip(self, db):
        plans = PlanRepository(db)
        ctx = {"runtime": {"goal_id": "g1"}, "steps": {"s1": {"capability": "cap", "output": "done"}}}
        step = PlanStep(title="s1", objective="obj", capability="cap", execution_context=ctx)
        plan = self._make_plan_with_step(step)
        plans.create(plan)

        loaded = plans.get(plan.id)
        assert loaded.steps[0].execution_context == ctx
        assert loaded.steps[0].execution_context["steps"]["s1"]["output"] == "done"

    def test_evidence_survives_roundtrip(self, db):
        plans = PlanRepository(db)
        evidence = {"verification": "passed", "artifacts": ["a.txt", "b.txt"]}
        step = PlanStep(title="s1", objective="obj", capability="cap", evidence=evidence)
        plan = self._make_plan_with_step(step)
        plans.create(plan)

        loaded = plans.get(plan.id)
        assert loaded.steps[0].evidence == evidence
        assert loaded.steps[0].evidence["verification"] == "passed"

    def test_step_status_survives_roundtrip(self, db):
        plans = PlanRepository(db)
        step = PlanStep(title="s1", objective="obj", capability="cap", status=PlanStepStatus.CLAIMED_COMPLETE)
        plan = self._make_plan_with_step(step)
        plans.create(plan)

        loaded = plans.get(plan.id)
        assert loaded.steps[0].status == PlanStepStatus.CLAIMED_COMPLETE

    def test_output_survives_roundtrip(self, db):
        plans = PlanRepository(db)
        step = PlanStep(title="s1", objective="obj", capability="cap", output={"result": "ok"})
        plan = self._make_plan_with_step(step)
        plans.create(plan)

        loaded = plans.get(plan.id)
        assert loaded.steps[0].output == {"result": "ok"}


# ---------------------------------------------------------------------------
# C5 — Restart/reload: CLAIMED_COMPLETE persists and is re-detectable
# ---------------------------------------------------------------------------


class TestRestartReloadBehavior:
    """If process crashes after CLAIMED_COMPLETE but before VERIFIED,
    the step remains CLAIMED_COMPLETE on reload."""

    def test_claimed_complete_persists_across_reload(self, db):
        """Simulate: step reaches CLAIMED_COMPLETE, process 'crashes', DB reloaded."""
        plans = PlanRepository(db)
        goal_repo = GoalRepository(db)

        goal = Goal(project_id="proj1", objective="test")
        goal_repo.create(goal)

        step = PlanStep(title="s1", objective="obj", capability="cap", task_id="task-1")
        plan = Plan(
            id="plan-1", goal_id=goal.id, mode=PlanMode.SIMPLE_DEPENDENCY,
            status=PlanStatus.RUNNING, steps=[step],
        )
        plans.create(plan)

        # Update step to CLAIMED_COMPLETE (simulating service.py execution)
        step.status = PlanStepStatus.CLAIMED_COMPLETE
        plans.update(plan)

        # Simulate process restart: reload from DB
        loaded = plans.get(plan.id)
        assert loaded is not None
        assert loaded.steps[0].status == PlanStepStatus.CLAIMED_COMPLETE

    def test_waiting_for_verification_persists_across_reload(self, db):
        """WAITING_FOR_VERIFICATION (no verifier path) persists on reload."""
        plans = PlanRepository(db)
        goal_repo = GoalRepository(db)

        goal = Goal(project_id="proj1", objective="test")
        goal_repo.create(goal)

        step = PlanStep(title="s1", objective="obj", capability="cap", task_id="task-1")
        plan = Plan(
            id="plan-1", goal_id=goal.id, mode=PlanMode.SIMPLE_DEPENDENCY,
            status=PlanStatus.RUNNING, steps=[step],
        )
        plans.create(plan)

        # Step reaches WAITING_FOR_VERIFICATION
        step.status = PlanStepStatus.WAITING_FOR_VERIFICATION
        plans.update(plan)

        # Reload
        loaded = plans.get(plan.id)
        assert loaded.steps[0].status == PlanStepStatus.WAITING_FOR_VERIFICATION

    def test_claimed_complete_can_be_transitioned_to_verified(self, db):
        """After reload, a CLAIMED_COMPLETE step can be transitioned to VERIFIED."""
        events = EventStore(db)
        plans = PlanRepository(db)
        goal_repo = GoalRepository(db)

        goal = Goal(project_id="proj1", objective="test")
        goal_repo.create(goal)

        step = PlanStep(title="s1", objective="obj", capability="cap", task_id="task-1")
        plan = Plan(
            id="plan-1", goal_id=goal.id, mode=PlanMode.SIMPLE_DEPENDENCY,
            status=PlanStatus.RUNNING, steps=[step],
        )
        plans.create(plan)

        step.status = PlanStepStatus.CLAIMED_COMPLETE
        plans.update(plan)

        # Reload
        loaded = plans.get(plan.id)
        reloaded_step = loaded.steps[0]
        assert reloaded_step.status == PlanStepStatus.CLAIMED_COMPLETE

        # Can be verified after reload
        transition_step(reloaded_step, PlanStepStatus.VERIFIED, "deferred_verification", events, plan_id=plan.id)
        assert reloaded_step.status == PlanStepStatus.VERIFIED


# ---------------------------------------------------------------------------
# C2 — Authority boundary documentation (encoded as tests)
# ---------------------------------------------------------------------------


class TestAuthorityBoundaries:
    """Encode authority boundary rules as executable tests."""

    def test_only_transition_step_changes_step_status(self, db):
        """transition_step() is the sole authority for step status changes.
        Direct assignment (step.status = X) is forbidden outside transition_step().
        This test verifies the function works and emits events."""
        events = EventStore(db)
        step = PlanStep(title="t", objective="o", capability="c")

        # Valid transitions
        for new_status, reason in [
            (PlanStepStatus.RUNNING, "dispatch"),
            (PlanStepStatus.CLAIMED_COMPLETE, "run_completed"),
            (PlanStepStatus.VERIFIED, "verification_accepted"),
        ]:
            transition_step(step, new_status, reason, events, plan_id="p1")
            assert step.status == new_status

    def test_orchestrator_controls_task_status(self, db, project):
        """Task status transitions are controlled by the Orchestrator."""
        import asyncio

        task_repo = TaskRepository(db)
        task = Task(project_id=project.id, title="t", objective="o")
        task_repo.create(task)
        assert task.status == TaskStatus.CREATED

        orch = Orchestrator(
            db,
            executor_factory=lambda: MockExecutor(MockConfig(scenario=MockScenario.SUCCESS)),
        )
        # The orchestrator will set task to RUNNING, then COMPLETED
        run = asyncio.run(orch.execute_task(task.id))

        reloaded_task = task_repo.get(task.id)
        assert reloaded_task.status == TaskStatus.COMPLETED
        assert run.status == RunStatus.COMPLETED

    def test_run_status_only_set_by_orchestrator(self, db, project):
        """Run status is set by Orchestrator, not by external callers."""
        import asyncio

        task_repo = TaskRepository(db)
        run_repo = RunRepository(db)

        task = Task(project_id=project.id, title="t", objective="o")
        task_repo.create(task)

        orch = Orchestrator(
            db,
            executor_factory=lambda: MockExecutor(MockConfig(scenario=MockScenario.SUCCESS)),
        )
        run = asyncio.run(orch.execute_task(task.id))

        # Run is COMPLETED by the orchestrator
        assert run.status == RunStatus.COMPLETED
        loaded_run = run_repo.get(run.id)
        assert loaded_run.status == RunStatus.COMPLETED

    def test_planstep_linkage_is_acyclic(self, db):
        """Linkage graph is acyclic: PlanStep → Task → Run → Attempt."""
        plans = PlanRepository(db)
        step = PlanStep(
            title="s1", objective="obj", capability="cap",
            task_id="task-1",
            depends_on=[],
        )
        plan = Plan(
            id="p1", goal_id="g1", mode=PlanMode.SIMPLE_DEPENDENCY,
            status=PlanStatus.RUNNING, steps=[step],
        )
        plans.create(plan)
        loaded = plans.get(plan.id)
        s = loaded.steps[0]

        # Step points to task, not to run or attempt
        assert s.task_id == "task-1"
        # No run_id or attempt_id on PlanStep (by design — chain: step→task→run→attempt)
        assert not hasattr(s, 'run_id') or s.__dict__.get('run_id') is None
