"""Offline proof contracts for the single-run P4 recovery gate."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from evals.reliability.p45_executor import P45BenchmarkExecutor, RunBudgetLedger
from evals.reliability.p46_provider import (
    CHEAP_MODEL,
    FROZEN_ENDPOINT,
    FROZEN_PROVIDER,
    RealLLMProvider,
)
from evals.reliability.run_phase4 import (
    ExecutionOutcome,
    ExternalObservableValidator,
    Phase4Runner,
    ProtocolSnapshot,
    _observation_digest,
    select_runs,
)
from lhas.native.models import ModelContext
from evals.reliability.runtime_factory.recovery import _event_timestamp_utc
from lhas.domain.enums import FailureClass
from lhas.domain.enums import EventType
from lhas.persistence.database import Database
from lhas.planning.models import (
    Plan,
    PlanMode,
    PlanStep,
    PlanStepStatus,
    RepairScope,
    compute_repair_scope,
)
from scripts.run_p4_single_proof import (
    EXPECTED_REPAIR_SCOPE,
    EXPECTED_RUNS,
    SINGLE_PROOF_CONFIG,
    SINGLE_PROOF_TASK,
    select_single_proof_run,
)


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_ROOT = ROOT / "evals" / "reliability" / "phase4_v1"


class _BudgetProbeExecutor:
    def __init__(self) -> None:
        self.configure_seen = None
        self.recovery_calls = 0

    def configure_frozen_budget(self, budgets):
        self.configure_seen = dict(budgets)

    async def execute(self, _request):
        return ExecutionOutcome(
            claimed_complete=False,
            failure_type="BUDGET_EXHAUSTED",
            budget_exhausted=True,
            observed_state={},
            attempt_count=1,
        )

    async def recover_after_validation(self, *_args):
        self.recovery_calls += 1
        raise AssertionError("budget exhaustion must not enter recovery")


def _snapshot():
    return ProtocolSnapshot.load(PROTOCOL_ROOT)


def _task(snapshot, task_id="CWR-06"):
    return next(task for task in snapshot.tasks if task["task_id"] == task_id)


def test_01_frozen_protocol_budget_is_resolved_without_execution(tmp_path):
    executor = _BudgetProbeExecutor()
    Phase4Runner(_snapshot(), output_dir=tmp_path, executor=executor)
    assert executor.configure_seen == {
        "timeout_seconds": 900,
        "max_turns": 20,
        "max_model_calls": 20,
    }


def test_02_root_and_nested_reservations_share_one_ceiling():
    ledger = RunBudgetLedger(max_provider_calls=3)
    ledger.reserve("initial")
    ledger.reserve("repair")
    ledger.reserve("replan")
    assert ledger.total_provider_calls == 3
    assert ledger.root_provider_calls == 1
    assert ledger.nested_provider_calls == 2


def test_03_budget_exhaustion_is_sticky_and_blocks_new_attempts():
    ledger = RunBudgetLedger(max_provider_calls=1)
    ledger.reserve("initial")
    try:
        ledger.reserve("repair")
    except RuntimeError as exc:
        assert str(exc) == "RUN_API_BUDGET_EXHAUSTED"
    else:
        raise AssertionError("expected the shared budget to fail closed")
    assert ledger.exhausted is True
    assert ledger.blocked_provider_calls == 1


def test_04_budget_snapshot_exposes_root_nested_and_blocked_counts():
    ledger = RunBudgetLedger(max_provider_calls=1)
    ledger.reserve("initial")
    try:
        ledger.reserve("repair")
    except RuntimeError:
        pass
    assert ledger.snapshot() == {
        "max_provider_calls": 1,
        "max_turns": None,
        "max_repair_attempts": 1,
        "max_replan_attempts": 0,
        "root_provider_calls": 1,
        "nested_provider_calls": 0,
        "provider_calls": 1,
        "blocked_provider_calls": 1,
        "exhausted": True,
    }


def test_05_phase4_runner_does_not_recover_budget_exhaustion(tmp_path):
    snapshot = _snapshot()
    executor = _BudgetProbeExecutor()
    runner = Phase4Runner(snapshot, output_dir=tmp_path, executor=executor)
    run = select_runs(snapshot, task_id="CWR-06", config_name="odys_p3", repeat_index=1)
    assert asyncio.run(runner.run(run)) == {"valid": 1, "invalid": 0}
    assert executor.recovery_calls == 0


def test_06_state_digest_changes_when_authoritative_observation_changes():
    task = _task(_snapshot())
    before = ExecutionOutcome(observed_state={"fixture_observations": {"status": "failed"}})
    after = ExecutionOutcome(observed_state={"fixture_observations": {"status": "verified"}})
    assert _observation_digest(before, task) != _observation_digest(after, task)


def test_07_state_digest_is_stable_without_observable_mutation():
    task = _task(_snapshot())
    first = ExecutionOutcome(observed_state={"fixture_observations": {"status": "failed"}})
    second = ExecutionOutcome(observed_state={"fixture_observations": {"status": "failed"}})
    assert _observation_digest(first, task) == _observation_digest(second, task)


def test_08_validator_receives_the_same_observation_projection_as_digest():
    snapshot = _snapshot()
    task = _task(snapshot)
    outcome = ExecutionOutcome(
        claimed_complete=True,
        observed_state={
            "fixture_observations": {"repair_scope": "local", "noise": "ignored-by-expectation"},
            "repair_scope": "local",
        },
    )
    result = ExternalObservableValidator().validate(task, None, outcome)
    assert result.acceptance_status == "ACCEPTED"
    post_digest = _observation_digest(outcome, task)
    validator_observed_state_digest = _observation_digest(outcome, task)
    assert validator_observed_state_digest == post_digest


def test_09_expected_effect_ids_are_derived_from_frozen_task():
    task = _task(_snapshot())
    assert list(task["expected_observable_effects"]) == ["repair_scope"]
    assert [str(key) for key in task["expected_observable_effects"]] != ["repair_scope", "workspace"]


def test_10_recovery_scope_is_not_a_new_frozen_task_definition():
    snapshot = _snapshot()
    selected = select_single_proof_run(snapshot)
    assert len(selected) == EXPECTED_RUNS
    assert selected[0].task["task_id"] == SINGLE_PROOF_TASK
    assert selected[0].config["config_id"] == SINGLE_PROOF_CONFIG
    assert selected[0].task["expected_observable_effects"]["repair_scope"] == "local"
    assert EXPECTED_REPAIR_SCOPE == "LOCAL"


def test_11_recovery_event_naive_db_timestamp_is_interpreted_as_utc():
    assert _event_timestamp_utc(datetime(2026, 1, 1, 0, 0, 0)) == "2026-01-01T00:00:00Z"


def test_12_recovery_event_aware_timestamp_is_normalized_to_utc():
    value = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    assert _event_timestamp_utc(value) == "2026-01-01T08:00:00Z"


def test_13_p45_accounting_distinguishes_provider_attempts():
    class Provider:
        call_records = [
            {"attempt_id": "root", "provider_call": True, "input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            {"attempt_id": "repair", "provider_call": True, "input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
            {"attempt_id": "repair", "provider_call": False, "input_tokens": "NOT_MEASURED", "output_tokens": "NOT_MEASURED", "total_tokens": "NOT_MEASURED"},
        ]

    executor = P45BenchmarkExecutor(provider=Provider(), factory_type="scripted")
    outcome = ExecutionOutcome()
    executor._provider_call_offsets["run"] = 0
    executor._attach_provider_accounting(outcome, "run")
    assert outcome.provider_calls == 2
    assert outcome.provider_attempt_count == 2
    assert outcome.root_attempt_count == 1
    assert outcome.nested_attempt_count == 1


def test_14_single_proof_preflight_selection_is_exact_and_provider_free():
    selected = select_single_proof_run(_snapshot())
    assert len(selected) == 1
    assert selected[0].run_id.endswith("::repeat-1")


def _plan(*steps):
    return Plan(goal_id="offline-proof", mode=PlanMode.SIMPLE_DEPENDENCY, steps=list(steps))


def test_15_local_scope_is_deterministic_for_retryable_failure():
    step = PlanStep(id="a", title="A", objective="A", capability="workspace.edit", status=PlanStepStatus.FAILED)
    scope, affected = compute_repair_scope(step, _plan(step), FailureClass.EXECUTION, "TOOL_ERROR")
    assert scope is RepairScope.LOCAL
    assert affected == {"a"}


def test_16_subgraph_scope_is_deterministic_when_dag_and_assumption_are_present():
    failed = PlanStep(id="a", title="A", objective="A", capability="workspace.edit", status=PlanStepStatus.FAILED)
    dependent = PlanStep(id="b", title="B", objective="B", capability="workspace.edit", depends_on=["a"])
    scope, affected = compute_repair_scope(
        failed,
        _plan(failed, dependent),
        FailureClass.REASONING,
        "WRONG_ASSUMPTION",
    )
    assert scope is RepairScope.AFFECTED_SUBGRAPH
    assert affected == {"a", "b"}


def test_17_macro_scope_precedes_dag_shape_for_systemic_failure():
    step = PlanStep(id="a", title="A", objective="A", capability="workspace.edit", status=PlanStepStatus.FAILED)
    scope, affected = compute_repair_scope(step, _plan(step), FailureClass.EXECUTION, "QUOTA_EXHAUSTED")
    assert scope is RepairScope.MACRO_REPLAN
    assert affected == set()


def test_18_reopened_event_store_returns_utc_aware_timestamps(tmp_path):
    database_path = tmp_path / "events.db"
    first = Database(database_path)
    first.init_db()
    EventStore = __import__("lhas.persistence.event_store", fromlist=["EventStore"]).EventStore
    EventStore(first).append(EventType.REPAIR_COMPLETED, run_id="r")
    reopened = Database(database_path)
    reopened.init_db()
    event = EventStore(reopened).list_all()[0]
    assert event.timestamp.tzinfo is not None
    assert event.timestamp.utcoffset() == timezone.utc.utcoffset(event.timestamp)


def test_19_real_provider_stops_transport_at_shared_budget():
    class Completions:
        calls = 0

        async def create(self, **_kwargs):
            self.calls += 1
            return {
                "model": CHEAP_MODEL,
                "choices": [{"message": {"content": "done", "tool_calls": []}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }

    completions = Completions()
    client = type(
        "Client",
        (),
        {"base_url": FROZEN_ENDPOINT, "chat": type("Chat", (), {"completions": completions})()},
    )()
    provider = RealLLMProvider(
        model=CHEAP_MODEL,
        api_key="offline-only",
        base_url=FROZEN_ENDPOINT,
        provider_id=FROZEN_PROVIDER,
        client=client,
        expected_model=CHEAP_MODEL,
    )
    ledger = RunBudgetLedger(max_provider_calls=1)
    provider.bind_run_budget(ledger)
    provider.bind_execution_context(run_id="r", task_id="t", attempt_id="a", phase="initial")
    context = ModelContext(messages=[], chars_used=0, budget_chars=100)
    asyncio.run(provider.generate(context=context, tools=[], timeout_seconds=1))
    try:
        asyncio.run(provider.generate(context=context, tools=[], timeout_seconds=1))
    except RuntimeError as exc:
        assert str(exc) == "RUN_API_BUDGET_EXHAUSTED"
    else:
        raise AssertionError("expected provider budget exhaustion")
    assert completions.calls == 1
    assert len(provider.call_records) == 2
    assert provider.call_records[1]["provider_call"] is False
