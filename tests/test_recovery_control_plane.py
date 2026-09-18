"""Offline proofs for the Odys recovery control plane."""

from lhas.domain.models import Attempt, Run
from lhas.native.persistence import ReplanSignalRepository
from lhas.persistence.event_store import EventStore
from lhas.persistence.repositories import AttemptRepository, RunRepository
from lhas.planning.models import PlanStep
from lhas.planning.models import Plan, PlanMode, PlanStepStatus, Goal, CapabilitySpec
from lhas.planning.replan import MacroReplanService
from lhas.planning.replan_policy import ReplanTriggerPolicy
from lhas.recovery_control import (
    BudgetReservationError,
    EffectProgressEvaluator,
    ProgressStatus,
    RecoveryBudgetManager,
    RecoveryContextProjector,
    RecoveryController,
    RecoveryDecision,
)


class RootBudget:
    def __init__(self, capacity=10):
        self.remaining_provider_calls = capacity
        self.calls = []

    def reserve(self, phase):
        if self.remaining_provider_calls <= 0:
            raise RuntimeError("root budget exhausted")
        self.remaining_provider_calls -= 1
        self.calls.append(phase)


def test_effect_satisfied_is_only_a_validation_candidate():
    evaluator = EffectProgressEvaluator({"checksum": "target"})
    result = evaluator.observe(
        before_state={"checksum": "wrong"},
        after_state={"checksum": "target"},
        action={"capability": "workspace.edit", "args_sha256": "a" * 64},
        observation={"bounded_output": {"checksum": "target"}},
    )

    assert result.status is ProgressStatus.SATISFIED
    assert result.candidate_for_validation is True
    assert result.status.value != "VERIFIED"


def test_controller_persists_no_progress_and_policy_consumes_signal(db, make_task):
    task = make_task()
    run = RunRepository(db).create(Run(id="control-run", task_id=task.id, status="RUNNING"))
    attempt = AttemptRepository(db).create(
        Attempt(id="control-attempt", run_id=run.id, attempt_number=1, status="RUNNING")
    )
    controller = RecoveryController(
        db=db,
        task_id=task.id,
        run_id=run.id,
        attempt_id=attempt.id,
        step_id="step-1",
        expected_effects={"checksum": "target"},
        max_no_progress=2,
        max_repeated_action=9,
        max_repeated_state=9,
    )

    first = controller.observe(
        before_state=None,
        after_state={"checksum": "wrong-a"},
        action={"capability": "workspace.edit", "args_sha256": "1" * 64},
        observation={"safe_summary": {"checksum": "wrong-a"}},
    )
    second = controller.observe(
        before_state=None,
        after_state={"checksum": "wrong-b"},
        action={"capability": "workspace.edit", "args_sha256": "2" * 64},
        observation={"safe_summary": {"checksum": "wrong-b"}},
    )

    assert first[0] is RecoveryDecision.CONTINUE_LOCAL_REPAIR
    assert second[0] is RecoveryDecision.ESCALATE_MACRO_REPLAN
    signals = ReplanSignalRepository(db).list_for_attempt(attempt.id)
    assert [item.reason for item in signals] == ["REPAIR_NO_PROGRESS"]
    events = EventStore(db).list_for_attempt(attempt.id)
    assert any(
        event.event_type.value == "REPLAN_SIGNAL_CREATED"
        and event.payload["reason"] == "REPAIR_NO_PROGRESS"
        for event in events
    )
    trigger = ReplanTriggerPolicy(db).evaluate(
        step=PlanStep(id="step-1", title="repair", objective="repair", capability="workspace.edit"),
        run_id=run.id,
    )
    assert trigger is not None and trigger.reason == "REPAIR_NO_PROGRESS"


def test_policy_consumes_run_scoped_signal_without_attempt_projection(
    db, make_task
):
    task = make_task()
    run = RunRepository(db).create(
        Run(id="run-scoped-control", task_id=task.id, status="RUNNING")
    )
    AttemptRepository(db).create(
        Attempt(
            id="durable-attempt-projection",
            run_id=run.id,
            attempt_number=1,
            status="RUNNING",
        )
    )
    controller = RecoveryController(
        db=db,
        task_id=task.id,
        run_id=run.id,
        # Simulate the native runtime identity before its planning Attempt
        # projection is linked. The signal remains durably run-scoped.
        attempt_id="native-runtime-attempt",
        step_id="step-run-scoped",
        max_no_progress=2,
        max_repeated_action=9,
        max_repeated_state=1,
    )
    controller.observe(
        before_state=None,
        after_state={"checksum": "same"},
        action={"capability": "workspace.edit", "args_sha256": "a" * 64},
        observation={"safe_summary": {"checksum": "same"}},
    )
    decision, _ = controller.observe(
        before_state=None,
        after_state={"checksum": "same"},
        action={"capability": "workspace.edit", "args_sha256": "a" * 64},
        observation={"safe_summary": {"checksum": "same"}},
    )

    assert decision is RecoveryDecision.ESCALATE_MACRO_REPLAN
    trigger = ReplanTriggerPolicy(db).evaluate(
        step=PlanStep(
            id="step-run-scoped",
            title="repair",
            objective="repair",
            capability="workspace.edit",
        ),
        run_id=run.id,
    )
    assert trigger is not None
    assert trigger.reason == "REPAIR_NO_PROGRESS"


def test_repeated_validator_rejection_becomes_durable_replan_signal(db, make_task):
    task = make_task()
    run = RunRepository(db).create(Run(id="validator-control-run", task_id=task.id, status="RUNNING"))
    attempt = AttemptRepository(db).create(
        Attempt(id="validator-control-attempt", run_id=run.id, attempt_number=1, status="RUNNING")
    )
    controller = RecoveryController(
        db=db,
        task_id=task.id,
        run_id=run.id,
        attempt_id=attempt.id,
        step_id="step-1",
        max_no_progress=3,
    )

    assert controller.validator_rejected() is RecoveryDecision.CONTINUE_LOCAL_REPAIR
    assert controller.validator_rejected() is RecoveryDecision.CONTINUE_LOCAL_REPAIR
    assert controller.validator_rejected() is RecoveryDecision.ESCALATE_MACRO_REPLAN

    signals = ReplanSignalRepository(db).list_for_attempt(attempt.id)
    assert [signal.reason for signal in signals] == ["REPEATED_VALIDATOR_REJECTION"]
    trigger = ReplanTriggerPolicy(db).evaluate(
        step=PlanStep(id="step-1", title="repair", objective="repair", capability="workspace.edit"),
        run_id=run.id,
    )
    assert trigger is not None
    assert trigger.reason == "REPEATED_VALIDATOR_REJECTION"


def test_budget_manager_protects_escalation_reserve_under_one_root_authority():
    root = RootBudget(capacity=10)
    manager = RecoveryBudgetManager(root)
    manager.reserve_capacity("local_repair", 4)
    manager.reserve_capacity("macro_replan", 2)
    manager.reserve_capacity("post_replan", 2)
    manager.reserve_capacity("validation", 1)

    for _ in range(4):
        manager.acquire("local_repair")
    assert manager.has_capacity("macro_replan")
    assert manager.has_capacity("post_replan")
    assert manager.has_capacity("validation")
    assert root.calls == ["local_repair"] * 4
    assert manager.snapshot()["root_budget_single_authority"] is True

    manager.acquire("macro_replan")
    manager.acquire("post_replan")
    manager.acquire("validation")
    assert root.calls[-3:] == ["macro_replan", "post_replan", "validation"]

    try:
        manager.acquire("local_repair")
    except BudgetReservationError as exc:
        assert str(exc) == "LOCAL_REPAIR_RESERVE_EXHAUSTED"
    else:
        raise AssertionError("local repair must not borrow escalation reserve")


def test_typed_signal_flows_policy_to_changed_macro_replan_and_preserves_verified_work(db, make_task):
    task = make_task()
    run = RunRepository(db).create(Run(id="replan-run", task_id=task.id, status="RUNNING"))
    attempt = AttemptRepository(db).create(
        Attempt(id="replan-attempt", run_id=run.id, attempt_number=1, status="FAILED")
    )
    controller = RecoveryController(
        db=db,
        task_id=task.id,
        run_id=run.id,
        attempt_id=attempt.id,
        step_id="failed",
        expected_effects={"ready": True},
        max_no_progress=1,
    )
    controller.observe(
        before_state={"ready": False},
        after_state={"ready": False},
        action={"capability": "workspace.edit", "args_sha256": "a" * 64},
        observation={"safe_summary": {"ready": False}},
    )

    verified = PlanStep(
        id="verified",
        title="prepare",
        objective="prepare",
        capability="prepare",
        status=PlanStepStatus.VERIFIED,
        output={"done": True},
    )
    failed = PlanStep(
        id="failed",
        title="old route",
        objective="old route",
        capability="old-route",
    )
    plan = Plan(
        id="control-plan",
        goal_id="goal-control",
        mode=PlanMode.SIMPLE_DEPENDENCY,
        status="RUNNING",
        steps=[verified, failed],
    )
    from lhas.persistence.planning_repositories import PlanRepository

    PlanRepository(db).create(plan)
    goal = Goal(
        id="goal-control",
        project_id=task.project_id,
        objective="finish",
        allowed_capabilities=["prepare", "alternate", "old-route"],
    )

    class ChangedPlanner:
        async def create_plan(self, *, goal, capabilities, context=None):
            return Plan(
                id="proposal-control",
                goal_id=goal.id,
                mode=PlanMode.SIMPLE_DEPENDENCY,
                status="READY",
                steps=[
                    PlanStep(id="proposal-verified", title="prepare", objective="prepare", capability="prepare"),
                    PlanStep(id="proposal-new", title="alternate", objective="alternate", capability="alternate", depends_on=["proposal-verified"]),
                ],
            )

    step = PlanStep(id="failed", title="old route", objective="old route", capability="old-route")
    policy_trigger = ReplanTriggerPolicy(db).evaluate(step=step, run_id=run.id)
    assert policy_trigger is not None
    assert policy_trigger.reason == "REPAIR_NO_PROGRESS"
    signals = ReplanSignalRepository(db).list_for_attempt(attempt.id)
    result = __import__("asyncio").run(
        MacroReplanService(db, ChangedPlanner()).consume(
            goal=goal,
            plan=plan,
            signals=signals,
            context={"capabilities": [CapabilitySpec(name="prepare"), CapabilitySpec(name="alternate"), CapabilitySpec(name="old-route")]},
        )
    )

    assert result.accepted is True
    assert result.old_strategy_fingerprint != result.new_strategy_fingerprint
    assert "verified" in result.affected_subgraph_ids or result.affected_subgraph_ids == ("failed",)
    assert next(item for item in plan.steps if item.id == "verified").status is PlanStepStatus.VERIFIED
    assert any(
        event.event_type.value == "REPLAN_ACCEPTED"
        and event.payload["old_strategy_fingerprint"] != event.payload["new_strategy_fingerprint"]
        for event in EventStore(db).list_all()
    )

def test_recovery_context_candidate_does_not_grant_verified_status():
    controller = RecoveryController(
        task_id="task",
        run_id="run",
        attempt_id="attempt",
        expected_effects={"ready": True},
    )
    decision, progress = controller.observe(
        before_state={"ready": False},
        after_state={"ready": True},
        action={
            "capability": "workspace.edit",
            "args_sha256": "a" * 64,
            "active_step_contract": {
                "step_id": "step",
                "capability": "workspace.edit",
                "inputs": {"path": "state.json"},
            },
            "planner_owned_arguments_match": True,
        },
        observation={
            "status": "SUCCESS",
            "observed_mutation": True,
            "bounded_output": {"ready": True},
        },
    )
    assert decision is RecoveryDecision.VALIDATE_CANDIDATE
    assert progress.status is ProgressStatus.SATISFIED
    assert controller.signals == []


def test_recovery_context_projection_is_bounded_and_keeps_durable_fields():
    projected = RecoveryContextProjector().project(
        goal="repair",
        acceptance_contract=["accepted"],
        current_state={"state": "x" * 10_000},
        failure_provenance={"failure_type": "TOOL_ERROR"},
        progress={"repair_turns": 8, "unique_states": 3},
        attempted_actions=["a" * 64] * 20,
        last_useful_observation={"status": "SUCCESS"},
        current_mismatch={"expected": "target"},
        budget={"local_repair": 2, "macro_replan": 1},
    )

    assert projected["failure_provenance"]["failure_type"] == "TOOL_ERROR"
    assert projected["progress_summary"]["repair_turns"] == 8
    assert len(str(projected["current_observable_state"]["projection"])) < 5_000
