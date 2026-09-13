"""P410 validation-semantics and recovery-pipeline regression tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from evals.reliability.p46_launcher import compute_summary
from evals.reliability.run_phase4 import (
    ExecutionOutcome,
    Phase4Runner,
    ProtocolSnapshot,
    select_runs,
)


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_ROOT = ROOT / "evals" / "reliability" / "phase4_v1"


class _RejectThenRepairExecutor:
    def __init__(self) -> None:
        self.recovery_calls = 0

    async def execute(self, request):
        return ExecutionOutcome(
            claimed_complete=True,
            observed_state={"runtime_source": "p410_test_runtime"},
            failure_type=None,
            attempt_count=1,
            runtime_source="p410_test_runtime",
        )

    async def recover_after_validation(self, request, outcome, validation):
        self.recovery_calls += 1
        return ExecutionOutcome(
            claimed_complete=True,
            observed_state={
                **request.task["expected_observable_effects"],
                "runtime_source": "p410_test_runtime",
                "repaired_effect": True,
            },
            attempt_count=1,
            runtime_source="p410_test_runtime",
            repair_scope="local",
            original_failure_attempt_id=f"{request.run_id}::attempt-1",
            repair_attempt_id=f"{request.run_id}::attempt-2",
        )


class _RejectOnlyExecutor:
    async def execute(self, request):
        return ExecutionOutcome(
            claimed_complete=True,
            observed_state={},
            runtime_source="p410_test_runtime",
            attempt_count=1,
        )


def _run(snapshot, *, task_id="CI-01", config_name="odys_p3"):
    return select_runs(
        snapshot,
        task_id=task_id,
        config_name=config_name,
        repeat_index=1,
    )


def test_validation_execution_and_acceptance_are_separate(tmp_path):
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    executor = _RejectThenRepairExecutor()
    runner = Phase4Runner(
        snapshot,
        output_dir=tmp_path,
        executor=executor,
        model="test-model",
        provider="test-provider",
        repo_root=ROOT,
        trace_path=tmp_path / "traces.jsonl",
        require_trace=True,
    )

    assert asyncio.run(runner.run(_run(snapshot))) == {"valid": 1, "invalid": 0}
    raw = json.loads((tmp_path / "raw.jsonl").read_text(encoding="utf-8"))
    trace_record = json.loads((tmp_path / "traces.jsonl").read_text(encoding="utf-8"))

    validation = raw["runtime_environment"]["validation"]
    assert validation["validator_execution_status"] == "SUCCESS"
    assert validation["acceptance_status"] == "REJECTED"
    assert validation["final_validator_execution_status"] == "SUCCESS"
    assert validation["final_acceptance_status"] == "ACCEPTED"
    assert validation["initial_agent_claimed_complete"] is True
    assert validation["final_agent_claimed_complete"] is True
    assert validation["false_completion_detected"] is True
    assert raw["false_completion"] is True
    assert raw["verified_completion"] is True
    state_evidence = raw["runtime_environment"]["state_evidence"]
    assert state_evidence["pre_repair_state_digest"]
    assert state_evidence["post_repair_state_digest"]
    assert state_evidence["state_changed_after_repair"] is True
    assert state_evidence["validator_observed_repaired_state"] is True

    validation_events = [
        event
        for event in trace_record["execution_trace"]
        if event["event_type"] == "VALIDATION_RESULT"
    ]
    assert validation_events[0]["metadata"]["validator_execution_status"] == "SUCCESS"
    assert validation_events[0]["metadata"]["acceptance_status"] == "REJECTED"
    assert validation_events[1]["metadata"]["acceptance_status"] == "ACCEPTED"
    assert all("result" not in event["metadata"] for event in validation_events)


def test_validation_rejection_enters_repair_and_reverification(tmp_path):
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    executor = _RejectThenRepairExecutor()
    runner = Phase4Runner(
        snapshot,
        output_dir=tmp_path,
        executor=executor,
        model="test-model",
        provider="test-provider",
        repo_root=ROOT,
        trace_path=tmp_path / "traces.jsonl",
        require_trace=True,
    )
    asyncio.run(runner.run(_run(snapshot)))

    assert executor.recovery_calls == 1
    raw = json.loads((tmp_path / "raw.jsonl").read_text(encoding="utf-8"))
    recovery = raw["runtime_environment"]["recovery"]
    assert recovery["recovery_required"] is True
    assert recovery["recovery_attempted"] is True
    assert recovery["recovery_success"] is True
    assert recovery["original_failure_attempt_id"].endswith("::attempt-1")
    assert recovery["repair_attempt_id"].endswith("::attempt-2")
    assert recovery["original_failure_attempt_id"] != recovery["repair_attempt_id"]

    trace = json.loads((tmp_path / "traces.jsonl").read_text(encoding="utf-8"))["execution_trace"]
    event_types = [event["event_type"] for event in trace]
    for required in (
        "FAILURE_DETECTED",
        "StepFailureProvenance",
        "REPAIR_STARTED",
        "REPAIR_COMPLETED",
        "STEP_VERIFIED",
        "VERIFICATION_PASSED",
    ):
        assert required in event_types
    assert event_types.index("REPAIR_STARTED") < event_types.index("REPAIR_COMPLETED")
    assert event_types.index("REPAIR_COMPLETED") < event_types.index("STEP_VERIFIED")


def test_rejection_without_recovery_capability_remains_valid_result(tmp_path):
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    runner = Phase4Runner(
        snapshot,
        output_dir=tmp_path,
        executor=_RejectOnlyExecutor(),
        model="test-model",
        provider="test-provider",
        repo_root=ROOT,
    )
    minimal_run = _run(snapshot, config_name="minimal")
    assert asyncio.run(runner.run(minimal_run)) == {"valid": 1, "invalid": 0}
    raw = json.loads((tmp_path / "raw.jsonl").read_text(encoding="utf-8"))
    assert raw["validity"] == "VALIDATED_FAIL"
    assert raw["false_completion"] is True
    assert raw["runtime_environment"]["validation"]["acceptance_status"] == "REJECTED"
    assert raw["runtime_environment"]["recovery"]["recovery_attempted"] is False


def test_p410_metrics_distinguish_detection_execution_and_success(tmp_path):
    records = [
        {
            "benchmark_run_id": "one",
            "validity": "VALIDATED_PASS",
            "verified_completion": True,
            "false_completion": True,
            "recovery_required": True,
            "recovery_attempted": True,
            "recovery_success": True,
            "model_cost": 1.0,
        },
        {
            "benchmark_run_id": "two",
            "validity": "VALIDATED_FAIL",
            "verified_completion": False,
            "false_completion": True,
            "recovery_required": True,
            "recovery_attempted": False,
            "recovery_success": False,
            "model_cost": 1.0,
        },
    ]
    output = tmp_path
    (output / "raw.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    (output / "invalid.jsonl").write_text("", encoding="utf-8")
    summary = compute_summary(output, planned_runs=2)

    assert summary["false_completion_detected_rate"] == 1.0
    assert summary["recovery_execution_rate"] == 0.5
    assert summary["recovery_success_rate"] == 1.0
