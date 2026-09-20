"""Provider-free P4.5 semantic closure tests.

These tests exercise the durable external-validator boundary directly. They do
not start a provider, execute Phase 4, or change the frozen benchmark inputs.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from lhas.domain.enums import EventType
from lhas.persistence.event_store import EventStore
from lhas.persistence.planning_repositories import PlanRepository
from lhas.planning.models import (
    Plan,
    PlanMode,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
)
from evals.reliability.p45_executor import P45BenchmarkExecutor
from evals.reliability.run_phase4 import (
    ExecutionOutcome,
    RuntimeInfrastructureError,
    ValidationOutcome,
    _merge_tool_invocation_evidence,
)
from evals.reliability.runtime_factory.recovery import (
    OfficialOdysRecoveryCoordinator,
)


def _waiting_plan(plan_id: str, step_id: str) -> Plan:
    return Plan(
        id=plan_id,
        goal_id=f"goal-{plan_id}",
        mode=PlanMode.SIMPLE_DEPENDENCY,
        status=PlanStatus.WAITING_FOR_VERIFICATION,
        steps=[
            PlanStep(
                id=step_id,
                title="complete step",
                objective="Complete the step",
                capability="workspace.edit_lines",
                status=PlanStepStatus.WAITING_FOR_VERIFICATION,
                task_id=f"task-{plan_id}",
            )
        ],
    )


def _coordinator(db, plan_id: str, step_id: str, run_id: str):
    coordinator = object.__new__(OfficialOdysRecoveryCoordinator)
    coordinator.db = db
    coordinator._pending_external_finalizations = {
        run_id: {"plan_id": plan_id, "step_id": step_id}
    }
    coordinator._completed_external_finalizations = {}
    return coordinator


def _finalize(coordinator, run_id: str, *, accepted: bool, verified: bool):
    return asyncio.run(
        coordinator.finalize_after_external_validation(
            SimpleNamespace(run_id=run_id),
            ExecutionOutcome(),
            ValidationOutcome(
                verified_completion=verified,
                validity="VALIDATED_PASS" if accepted else "VALIDATED_FAIL",
                failure_type=None if accepted else "VERIFICATION_REJECTED",
                acceptance_status="ACCEPTED" if accepted else "REJECTED",
            ),
        )
    )


def test_accepted_external_verdict_is_durable_and_idempotent(db):
    plan = _waiting_plan("p45-accepted", "p45-accepted-step")
    PlanRepository(db).create(plan)
    coordinator = _coordinator(db, plan.id, plan.steps[0].id, "p45-accepted-run")

    first = _finalize(coordinator, "p45-accepted-run", accepted=True, verified=True)
    event_count = len(EventStore(db).list_all())
    second = asyncio.run(
        coordinator.finalize_after_external_validation(
            SimpleNamespace(run_id="p45-accepted-run"),
            ExecutionOutcome(),
            ValidationOutcome(
                verified_completion=True,
                validity="VALIDATED_PASS",
                acceptance_status="ACCEPTED",
            ),
        )
    )
    reloaded = PlanRepository(db).get(plan.id)

    assert first == second
    assert reloaded.status is PlanStatus.COMPLETED
    assert reloaded.steps[0].status is PlanStepStatus.VERIFIED
    assert len(EventStore(db).list_all()) == event_count
    assert first["budget_issued"] is False


def test_acceptance_without_verified_completion_fails_closed(db):
    plan = _waiting_plan("p45-accepted-unverified", "p45-accepted-unverified-step")
    PlanRepository(db).create(plan)
    coordinator = _coordinator(
        db, plan.id, plan.steps[0].id, "p45-accepted-unverified-run"
    )

    result = _finalize(
        coordinator,
        "p45-accepted-unverified-run",
        accepted=True,
        verified=False,
    )
    reloaded = PlanRepository(db).get(plan.id)

    assert result["acceptance_status"] == "REJECTED"
    assert reloaded.steps[0].status is PlanStepStatus.CLASSIFIED_FAILURE
    assert reloaded.status is PlanStatus.FAILED


def test_rejection_with_verified_completion_does_not_verify(db):
    plan = _waiting_plan("p45-rejected-verified", "p45-rejected-verified-step")
    PlanRepository(db).create(plan)
    coordinator = _coordinator(
        db, plan.id, plan.steps[0].id, "p45-rejected-verified-run"
    )

    result = _finalize(
        coordinator,
        "p45-rejected-verified-run",
        accepted=False,
        verified=True,
    )
    reloaded = PlanRepository(db).get(plan.id)

    assert result["acceptance_status"] == "REJECTED"
    assert reloaded.steps[0].status is PlanStepStatus.CLASSIFIED_FAILURE
    assert reloaded.status is PlanStatus.FAILED
    assert EventType.PLAN_COMPLETED not in [event.event_type for event in EventStore(db).list_all()]


def test_missing_runtime_finalizer_fails_closed_without_execution(db):
    executor = P45BenchmarkExecutor(factory_type="scripted")
    executor._active_runtimes["p45-missing-finalizer"] = object()
    request = SimpleNamespace(run_id="p45-missing-finalizer", execution_control=None)

    with pytest.raises(
        RuntimeInfrastructureError,
        match="EXTERNAL_FINALIZATION_DELEGATE_MISSING",
    ):
        asyncio.run(
            executor.finalize_after_external_validation(
                request,
                ExecutionOutcome(),
                ValidationOutcome(
                    verified_completion=True,
                    validity="VALIDATED_PASS",
                    acceptance_status="ACCEPTED",
                ),
            )
        )


def test_tool_evidence_merge_is_ordered_and_attempt_scoped():
    initial = [
        {"attempt_id": "initial", "invocation_id": "call-1"},
        {"attempt_id": "initial", "invocation_id": "call-2"},
    ]
    recovery = [
        {"attempt_id": "initial", "invocation_id": "call-2"},
        {"attempt_id": "repair", "invocation_id": "call-1"},
    ]

    merged = _merge_tool_invocation_evidence(initial, recovery)

    assert [(item["attempt_id"], item["invocation_id"]) for item in merged] == [
        ("initial", "call-1"),
        ("initial", "call-2"),
        ("repair", "call-1"),
    ]

def test_executor_cleanup_releases_all_run_scoped_state():
    class Recovery:
        def __init__(self):
            self.controller_discarded = False
            self.finalization_discarded = False

        def discard_controller(self, _run_id):
            self.controller_discarded = True

        def discard_external_finalization(self, _run_id):
            self.finalization_discarded = True

    recovery = Recovery()
    runtime = SimpleNamespace(recovery=recovery)
    executor = P45BenchmarkExecutor(factory_type="scripted")
    run_id = "p45-cleanup-run"
    executor._active_runtimes[run_id] = runtime
    executor._execution_controls[run_id] = object()
    executor._provider_call_offsets[run_id] = 0
    executor._run_budgets[run_id] = object()
    request = SimpleNamespace(run_id=run_id, task={})

    executor.cleanup(request)

    assert recovery.controller_discarded is True
    assert recovery.finalization_discarded is True
    assert run_id not in executor._active_runtimes
    assert run_id not in executor._execution_controls
    assert run_id not in executor._provider_call_offsets
    assert run_id not in executor._run_budgets