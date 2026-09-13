"""Deterministic tests for the validation-boundary recovery semantics."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from evals.reliability.p46_launcher import compute_summary
from evals.reliability.report_generator import compute_metrics
from evals.reliability.run_phase4 import ExecutionOutcome, Phase4Runner, ProtocolSnapshot, select_runs


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_ROOT = ROOT / "evals" / "reliability" / "phase4_v1"


class _AcceptedAfterRuntimeFailureExecutor:
    """Model an internal failure signal whose external state is already valid."""

    async def execute(self, _request):
        return ExecutionOutcome(
            claimed_complete=True,
            observed_state={
                "fixture_observations": {
                    "side_effect_count": 1,
                    "committed": True,
                },
                "runtime_source": "deterministic-test-runtime",
            },
            recovery_required=True,
            runtime_source="deterministic-test-runtime",
            attempt_count=1,
        )


def test_accepted_initial_validation_projects_verification_and_not_recovery(tmp_path):
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    run = select_runs(
        snapshot,
        task_id="ESR-04",
        config_name="odys_p3",
        repeat_index=1,
    )
    runner = Phase4Runner(
        snapshot,
        output_dir=tmp_path,
        executor=_AcceptedAfterRuntimeFailureExecutor(),
        model="offline-model",
        provider="offline-provider",
        repo_root=ROOT,
        trace_path=tmp_path / "traces.jsonl",
        require_trace=True,
    )

    assert asyncio.run(runner.run(run)) == {"valid": 1, "invalid": 0}
    raw = json.loads((tmp_path / "raw.jsonl").read_text(encoding="utf-8"))
    trace_record = json.loads((tmp_path / "traces.jsonl").read_text(encoding="utf-8"))

    assert raw["verified_completion"] is True
    assert raw["recovery_required"] is False
    recovery = raw["runtime_environment"]["recovery"]
    assert recovery["recovery_candidate"] is True
    assert recovery["recovery_required_after_validation"] is False
    assert recovery["recovery_attempted"] is False

    event_types = [event["event_type"] for event in trace_record["execution_trace"]]
    assert event_types[-2:] == ["VALIDATION_RESULT", "VERIFICATION_PASSED"]

    summary = compute_summary(tmp_path, planned_runs=1)
    assert summary["recovery_eligible"] == 0
    assert summary["recovery_execution_rate"] == "NOT_MEASURED"
    assert summary["recovery_success_rate"] == "NOT_MEASURED"


def test_report_metrics_honor_validation_boundary_field():
    record = {
        "validity": "VALIDATED_PASS",
        "verified_completion": True,
        "recovery_required": True,
        "recovery_attempted": False,
        "recovery_success": False,
        "lost_work_units": 0,
        "duplicate_side_effect_count": 0,
        "model_cost": "NOT_MEASURED",
        "runtime_environment": {
            "recovery": {
                "recovery_candidate": True,
                "recovery_required_after_validation": False,
            }
        },
    }

    metrics = compute_metrics([record])
    assert metrics["recovery_execution_rate"] == "NOT_MEASURED"
    assert metrics["recovery_success_rate"] == "NOT_MEASURED"
    assert metrics["lost_work_rate"] == "NOT_MEASURED"
