"""Provider-free convergence and single-authority recovery proofs."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from lhas.domain.enums import EventType
from lhas.persistence.event_store import EventStore
from lhas.planning.models import PlanStep, PlanStepStatus, RepairScope
from lhas.planning.service import PlanExecutionService
from lhas.recovery_control import RecoveryController, RecoveryDecision
from evals.reliability.run_phase4 import (
    ExecutionOutcome,
    ExternalObservableValidator,
    ValidationOutcome,
)
from evals.reliability.runtime_factory.recovery import OfficialOdysRecoveryCoordinator


def _candidate_action(capability: str = "workspace.edit_lines") -> dict:
    return {
        "capability": capability,
        "args_sha256": "a" * 64,
        "active_step_contract": {
            "step_id": "step-1",
            "capability": "workspace.edit_lines",
            "inputs": {"path": "state.json", "old_string": "old", "new_string": "new"},
        },
        "planner_owned_arguments_match": True,
    }


def _candidate_observation(*, mutated: bool = True) -> dict:
    return {
        "status": "SUCCESS",
        "observed_mutation": mutated,
        "bounded_output": {"route": "alternate"},
    }


def test_success_without_confirmed_mutation_is_not_a_validation_candidate():
    controller = RecoveryController(
        task_id="task",
        run_id="run",
        attempt_id="attempt",
        expected_effects={"route": "alternate"},
    )

    decision, progress = controller.observe(
        before_state={"route": "local"},
        after_state={"route": "alternate"},
        action=_candidate_action(),
        observation=_candidate_observation(mutated=False),
    )

    assert decision is RecoveryDecision.CONTINUE_LOCAL_REPAIR
    assert progress.evidence["candidate_for_validation"] is False
    assert progress.evidence["candidate_rejection_reason"] == "CONFIRMED_MUTATION_MISSING"


def test_unrelated_successful_mutation_is_not_a_validation_candidate():
    controller = RecoveryController(
        task_id="task",
        run_id="run",
        attempt_id="attempt",
        expected_effects={"route": "alternate"},
    )

    decision, progress = controller.observe(
        before_state={"route": "local"},
        after_state={"route": "alternate"},
        action=_candidate_action(capability="workspace.write_unrelated"),
        observation=_candidate_observation(),
    )

    assert decision is RecoveryDecision.CONTINUE_LOCAL_REPAIR
    assert progress.evidence["candidate_rejection_reason"] == "ACTIVE_CAPABILITY_MISMATCH"


def test_rejected_external_observation_never_becomes_verified():
    outcome = ExecutionOutcome(
        claimed_complete=True,
        observed_state={"fixture_observations": {"route": "local"}},
    )
    validation = ExternalObservableValidator().validate(
        {"expected_observable_effects": {"route": "alternate"}},
        None,
        outcome,
    )

    assert validation.validator_execution_status == "SUCCESS"
    assert validation.acceptance_status == "REJECTED"
    assert validation.verified_completion is False


def test_unavailable_external_validator_never_becomes_verified():
    validation = ValidationOutcome(
        verified_completion=False,
        validity="INVALID_RUN",
        failure_type="VALIDATOR_UNAVAILABLE",
        validator_execution_status="NOT_EXECUTED",
        acceptance_status="NOT_EVALUATED",
    )

    assert validation.verified_completion is False
    assert validation.acceptance_status == "NOT_EVALUATED"


def test_post_replan_event_projection_requires_plan_step_and_phase_identity():
    timestamp = datetime.now(timezone.utc)
    events = SimpleNamespace(
        list_all=lambda: [
            SimpleNamespace(
                id=1,
                event_type=SimpleNamespace(value="REPLAN_ACCEPTED"),
                payload={"plan_id": "plan-1"},
                timestamp=timestamp,
                attempt_id="attempt-replan",
            ),
            SimpleNamespace(
                id=2,
                event_type=SimpleNamespace(value="NATIVE_TOOL_OBSERVED"),
                payload={
                    "plan_id": "other-plan",
                    "plan_version": "P-1",
                    "step_id": "step-1",
                    "execution_phase": "post_replan",
                    "invocation_id": "bad",
                    "status": "SUCCESS",
                },
                timestamp=timestamp,
                attempt_id="attempt-post",
            ),
            SimpleNamespace(
                id=3,
                event_type=SimpleNamespace(value="NATIVE_TOOL_OBSERVED"),
                payload={
                    "plan_id": "plan-1",
                    "plan_version": "P-1",
                    "step_id": "step-1",
                    "execution_phase": "post_replan",
                    "invocation_id": "good",
                    "status": "SUCCESS",
                },
                timestamp=timestamp,
                attempt_id="attempt-post",
            ),
        ]
    )

    projected = OfficialOdysRecoveryCoordinator._project_events(
        events,
        before_ids=set(),
        plan_id="plan-1",
        task_id="task-1",
        original_attempt_id="attempt-initial",
        repair_attempt_id="attempt-post",
        recovery_step_id="step-1",
        recovery_plan_version="P-0.1",
        post_replan_step_id="step-1",
        post_replan_plan_version="P-1",
    )

    native = [
        item for item in projected if item["event_type"] == "TOOL_CALL_OBSERVED"
    ]
    assert [item["metadata"]["invocation_id"] for item in native] == ["good"]


def test_inline_repair_cannot_mint_second_root_repair_lease(db):
    service = object.__new__(PlanExecutionService)
    service.db = db
    events = EventStore(db)
    step = PlanStep(
        id="step-budget",
        title="repair",
        objective="repair",
        capability="workspace.edit_lines",
        status=PlanStepStatus.FAILED,
        budget={"max_repair_attempts": 3},
    )
    lease = {"remaining": 1}

    def root_repair_lease() -> bool:
        if lease["remaining"] == 0:
            return False
        lease["remaining"] -= 1
        return True

    first = service._prepare_inline_local_repair(
        step,
        None,
        RepairScope.LOCAL,
        events,
        "plan-budget",
        producing_attempt_id="attempt-1",
        repair_budget_guard=root_repair_lease,
    )
    second = service._prepare_inline_local_repair(
        step,
        None,
        RepairScope.LOCAL,
        events,
        "plan-budget",
        producing_attempt_id="attempt-1",
        repair_budget_guard=root_repair_lease,
    )

    assert first is True
    assert second is False
    assert step.evidence["repair_attempt_count"] == 1
    assert "repair_attempt_id" not in step.evidence
    assert any(
        event.event_type is EventType.REPAIR_COMPLETED
        and event.payload.get("error_type") == "REPAIR_BUDGET_EXHAUSTED"
        for event in events.list_all()
    )
