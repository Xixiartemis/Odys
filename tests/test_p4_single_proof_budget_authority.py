"""Offline contracts for typed budget authority and bounded recovery."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from evals.reliability.p45_executor import RunBudgetLedger
from evals.reliability.run_phase4 import (
    ATTEMPT_LOCAL_BUDGET_FAILURES,
    ExecutionOutcome,
    ExternalObservableValidator,
    Phase4Runner,
    ProtocolSnapshot,
    ROOT_API_BUDGET_FAILURE,
    _observation_digest,
    select_runs,
)
from evals.reliability.runtime_factory.odys_factory import _classify_budget_failure
from evals.reliability.fixture_packages.registry import FixtureRegistry
from scripts.run_p4_single_proof_v2 import (
    EXPECTED_INITIAL_FAILURE_TYPE,
    EXPECTED_REPAIR_SCOPE,
    select_single_proof_run,
)


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_ROOT = ROOT / "evals" / "reliability" / "phase4_v1"


def _snapshot() -> ProtocolSnapshot:
    return ProtocolSnapshot.load(PROTOCOL_ROOT)


def _selected(snapshot: ProtocolSnapshot):
    return select_runs(
        snapshot,
        task_id="CWR-06",
        config_name="odys_p3",
        repeat_index=1,
    )[0]


def test_attempt_local_budget_keeps_the_same_root_ledger_for_one_repair():
    ledger = RunBudgetLedger(max_provider_calls=20)
    for _ in range(14):
        ledger.reserve("initial")

    assert ledger.can_start_repair() is True
    assert ledger.remaining_provider_calls == 6
    assert ledger.reserve_repair() is True
    assert ledger.repair_attempts == 1
    assert ledger.remaining_provider_calls == 6

    for _ in range(6):
        ledger.reserve("repair")
    assert ledger.total_provider_calls == 20
    assert ledger.remaining_provider_calls == 0

    try:
        ledger.reserve("repair")
    except RuntimeError as exc:
        assert getattr(exc, "budget_type") == ROOT_API_BUDGET_FAILURE
    else:
        raise AssertionError("root ledger must fail closed at its exact ceiling")
    assert ledger.total_provider_calls == 20
    assert ledger.repair_attempts == 1


def test_root_budget_exhaustion_is_typed_and_not_recoverable():
    ledger = RunBudgetLedger(max_provider_calls=20)
    for _ in range(20):
        ledger.reserve("initial")
    assert ledger.can_start_repair() is False
    try:
        ledger.reserve("repair")
    except RuntimeError as exc:
        assert str(exc) == "RUN_API_BUDGET_EXHAUSTED"
        assert exc.budget_type == ROOT_API_BUDGET_FAILURE
    else:
        raise AssertionError("expected a root budget failure")


def test_replan_reservation_requires_remaining_provider_capacity():
    ledger = RunBudgetLedger(max_provider_calls=1, max_replan_attempts=1)
    ledger.reserve("initial")

    assert ledger.remaining_provider_calls == 0
    assert ledger.reserve_replan() is False
    assert ledger.replan_attempts == 0
    assert ledger.snapshot()["root_budget_single_authority"] is True


def test_native_generic_budget_is_classified_from_observed_attempt_counters():
    result = SimpleNamespace(
        error_type="BUDGET_EXHAUSTED",
        error_message=None,
        turn_count=14,
        tool_call_count=20,
    )
    assert _classify_budget_failure(
        result, max_turns=20, max_tool_calls=20
    ) == "TOOL_CALL_BUDGET_EXHAUSTED"
    assert _classify_budget_failure(
        SimpleNamespace(
            error_type="BUDGET_EXHAUSTED",
            error_message=None,
            turn_count=20,
            tool_call_count=0,
        ),
        max_turns=20,
        max_tool_calls=20,
    ) == "TURN_BUDGET_EXHAUSTED"


class _AttemptLocalRecoveryExecutor:
    def __init__(self, *, root_calls: int) -> None:
        self.ledger = RunBudgetLedger(max_provider_calls=20)
        for _ in range(root_calls):
            self.ledger.reserve("initial")
        self.recovery_calls = 0

    def configure_frozen_budget(self, _budgets):
        return None

    def can_attempt_recovery(self, _run_id: str) -> bool:
        return self.ledger.can_start_repair()

    def run_budget_snapshot(self, _run_id: str):
        return self.ledger.snapshot()

    async def execute(self, _request):
        return ExecutionOutcome(
            claimed_complete=True,
            failure_type="TOOL_CALL_BUDGET_EXHAUSTED",
            budget_failure_type="TOOL_CALL_BUDGET_EXHAUSTED",
            observed_state={},
            attempt_count=1,
            provider_calls=self.ledger.total_provider_calls,
        )

    async def recover_after_validation(self, request, _outcome, _validation):
        self.recovery_calls += 1
        assert self.ledger.reserve_repair() is True
        return ExecutionOutcome(
            claimed_complete=True,
            observed_state={"repair_scope": "local"},
            runtime_source="offline-recovery-probe",
            repair_scope="LOCAL",
            repair_attempts=1,
            original_failure_attempt_id=f"{request.run_id}::attempt-1",
            repair_attempt_id=f"{request.run_id}::attempt-2",
            attempt_count=1,
            provider_calls=self.ledger.total_provider_calls,
        )

    def cleanup(self, _request):
        return None


def test_attempt_local_exhaustion_enters_one_bounded_recovery(tmp_path):
    snapshot = _snapshot()
    executor = _AttemptLocalRecoveryExecutor(root_calls=14)
    runner = Phase4Runner(
        snapshot,
        output_dir=tmp_path,
        executor=executor,
        model="offline-model",
        provider="offline-provider",
        repo_root=ROOT,
        require_trace=False,
    )
    result = asyncio.run(runner.run([_selected(snapshot)]))
    assert result == {"valid": 1, "invalid": 0}
    assert executor.recovery_calls == 1
    assert executor.ledger.repair_attempts == 1
    assert executor.ledger.total_provider_calls == 14
    raw = json.loads((tmp_path / "raw.jsonl").read_text(encoding="utf-8"))
    assert raw["verified_completion"] is True
    assert raw["runtime_environment"]["execution_accounting"]["budget_failure_type"] == (
        "TOOL_CALL_BUDGET_EXHAUSTED"
    )


def test_root_capacity_exhaustion_blocks_recovery_without_new_budget(tmp_path):
    snapshot = _snapshot()
    executor = _AttemptLocalRecoveryExecutor(root_calls=20)
    runner = Phase4Runner(
        snapshot,
        output_dir=tmp_path,
        executor=executor,
        model="offline-model",
        provider="offline-provider",
        repo_root=ROOT,
        require_trace=False,
    )
    assert asyncio.run(runner.run([_selected(snapshot)])) == {"valid": 1, "invalid": 0}
    assert executor.recovery_calls == 0
    raw = json.loads((tmp_path / "raw.jsonl").read_text(encoding="utf-8"))
    accounting = raw["runtime_environment"]["execution_accounting"]
    assert accounting["budget_failure_type"] == ROOT_API_BUDGET_FAILURE
    assert accounting["budget_exhausted"] is True


def test_validator_digest_uses_a_frozen_mutable_effect_not_a_placeholder(tmp_path):
    snapshot = _snapshot()
    task = next(item for item in snapshot.tasks if item["task_id"] == "ESR-04")
    registry = FixtureRegistry()
    fixture = registry.get("ESR-04")
    workspace = tmp_path / "esr04"
    workspace.mkdir()
    fixture.setup(workspace)
    before_observation = fixture.observe(workspace)
    fixture.inject_fault(workspace, "INTERRUPT_AFTER_EFFECT")
    after_observation = fixture.observe(workspace)
    before = ExecutionOutcome(
        claimed_complete=False,
        observed_state={"fixture_observations": before_observation},
    )
    after = ExecutionOutcome(
        claimed_complete=True,
        observed_state={"fixture_observations": after_observation},
    )
    assert list(task["expected_observable_effects"]) == ["side_effect_count"]
    assert _observation_digest(before, task) != _observation_digest(after, task)
    assert ExternalObservableValidator().validate(task, fixture, after).verified_completion
    fixture.reset(workspace)


def test_cwr06_is_not_a_strong_workspace_mutation_proof_and_esr04_is_selected():
    snapshot = _snapshot()
    cwr06 = next(item for item in snapshot.tasks if item["task_id"] == "CWR-06")
    esr04 = next(item for item in snapshot.tasks if item["task_id"] == "ESR-04")
    assert list(cwr06["expected_observable_effects"]) == ["repair_scope"]
    assert list(esr04["expected_observable_effects"]) == ["side_effect_count"]


def test_single_proof_v2_selects_the_frozen_state_effect_task():
    selected = select_single_proof_run(_snapshot())
    assert len(selected) == 1
    assert selected[0].task["task_id"] == "ESR-04"
    assert selected[0].config["config_id"] == "odys_p3"
    assert EXPECTED_INITIAL_FAILURE_TYPE == "TOOL_CALL_BUDGET_EXHAUSTED"
    assert EXPECTED_REPAIR_SCOPE == "LOCAL"
