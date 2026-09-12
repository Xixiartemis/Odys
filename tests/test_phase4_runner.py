import asyncio
import json
from pathlib import Path

import pytest

from evals.reliability.run_phase4 import (
    ExecutionOutcome,
    Phase4Runner,
    ProtocolSnapshot,
    ResumeIntegrityError,
    RunSelectionError,
    select_runs,
)
from evals.reliability.p46_launcher import ProgressTracker, compute_summary


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_ROOT = ROOT / "evals" / "reliability" / "phase4_v1"


class PassingExecutor:
    def __init__(self):
        self.requests = []

    async def execute(self, request):
        self.requests.append(request)
        return ExecutionOutcome(
            claimed_complete=True,
            observed_state=request.task["expected_observable_effects"],
            attempt_count=1,
        )


class ExplodingExecutor:
    async def execute(self, request):
        raise RuntimeError("adapter unavailable")


def test_frozen_selection_counts_and_scope():
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    assert len(select_runs(snapshot, headline=True)) == 360
    assert len(select_runs(snapshot, ablation=True)) == 144
    selected = select_runs(snapshot, task_id="CI-01", config_name="odys_p3", repeat_index=1)
    assert [item.run_id for item in selected] == ["CI-01::odys_p3::repeat-1"]


def test_selection_rejects_mixed_batch_overrides():
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    with pytest.raises(RunSelectionError):
        select_runs(snapshot, headline=True, config_name="minimal")


def test_runner_writes_schema_validated_raw_and_aggregation_input(tmp_path):
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    executor = PassingExecutor()
    runner = Phase4Runner(
        snapshot,
        output_dir=tmp_path,
        executor=executor,
        repo_root=ROOT,
        model="test-model",
        provider="test-provider",
    )
    run = select_runs(snapshot, task_id="CI-01", config_name="minimal", repeat_index=1)
    assert asyncio.run(runner.run(run)) == {"valid": 1, "invalid": 0}

    raw = [json.loads(line) for line in (tmp_path / "raw.jsonl").read_text().splitlines()]
    aggregate = [json.loads(line) for line in (tmp_path / "aggregation-input.jsonl").read_text().splitlines()]
    assert raw[0]["validity"] == "VALIDATED_PASS"
    assert raw == aggregate
    assert raw[0]["protocol_hash"] == snapshot.protocol_hash
    assert raw[0]["manifest_hash"] == snapshot.manifest_hash
    assert raw[0]["fixture_hash"]
    assert (tmp_path / "invalid.jsonl").exists()
    assert (tmp_path / "invalid.jsonl").read_text(encoding="utf-8") == ""
    assert executor.requests[0].fault.fault_id == "PARTIAL_OUTPUT"


def test_runner_records_adapter_failure_as_invalid_and_not_aggregation(tmp_path):
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    runner = Phase4Runner(snapshot, output_dir=tmp_path, executor=ExplodingExecutor(), repo_root=ROOT)
    run = select_runs(snapshot, task_id="CI-01", config_name="minimal", repeat_index=1)
    assert asyncio.run(runner.run(run)) == {"valid": 0, "invalid": 1}
    assert not (tmp_path / "raw.jsonl").exists()
    invalid = json.loads((tmp_path / "invalid.jsonl").read_text().splitlines()[0])
    assert invalid["validity"] == "INVALID_RUN"
    assert "adapter unavailable" in invalid["invalid_reason"]
    assert (tmp_path / "aggregation-input.jsonl").read_text() == ""


def test_runner_resume_does_not_reexecute_completed_run(tmp_path):
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    first = PassingExecutor()
    run = select_runs(snapshot, task_id="CI-01", config_name="minimal", repeat_index=1)
    runner = Phase4Runner(snapshot, output_dir=tmp_path, executor=first, repo_root=ROOT)
    assert asyncio.run(runner.run(run)) == {"valid": 1, "invalid": 0}

    second = ExplodingExecutor()
    resumed = Phase4Runner(snapshot, output_dir=tmp_path, executor=second, repo_root=ROOT)
    assert asyncio.run(resumed.run(run)) == {"valid": 1, "invalid": 0}
    assert (tmp_path / "invalid.jsonl").exists()
    assert (tmp_path / "invalid.jsonl").read_text(encoding="utf-8") == ""


def test_immutable_result_collision_is_recorded_as_infra_invalid(tmp_path):
    snapshot, run = _seed_result(tmp_path)
    collision_runner = Phase4Runner(
        snapshot,
        output_dir=tmp_path,
        executor=PassingExecutor(),
        repo_root=ROOT,
    )

    # Normal resume skips an existing run.  Calling the one-run path directly
    # simulates a duplicate execution whose result differs from immutable raw
    # evidence and exercises the collision accounting path.
    asyncio.run(collision_runner._run_one(run[0]))

    invalid = [
        json.loads(line)
        for line in (tmp_path / "invalid.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert invalid[-1]["run_id"] == run[0].run_id
    assert invalid[-1]["status"] == "INFRA_FAILURE"
    assert invalid[-1]["error_type"] == "IMMUTABLE_RESULT_COLLISION"
    assert collision_runner.output.counts == {"valid": 1, "invalid": 1}

    summary = compute_summary(tmp_path, planned_runs=1)
    assert summary["valid_runs"] == 0
    assert summary["invalid_runs"] == 1
    assert summary["total_runs"] == 1
    assert summary["collision_count"] == 1
    assert summary["execution_attempts"] == 2
    assert summary["planned_runs"] == 1

    reopened = Phase4Runner(
        snapshot,
        output_dir=tmp_path,
        executor=ExplodingExecutor(),
        repo_root=ROOT,
    )
    assert asyncio.run(reopened.run(run)) == {"valid": 1, "invalid": 1}


def test_progress_separates_planned_runs_from_execution_attempts(tmp_path):
    tracker = ProgressTracker(total=12, output_dir=tmp_path)
    for _ in range(12):
        tracker.record_result(is_valid=True)
    tracker.record_result(is_valid=False, counts_as_completion=False)

    snapshot = tracker.snapshot()
    assert snapshot["planned_runs"] == 12
    assert snapshot["completed"] == 12
    assert snapshot["execution_attempts"] == 13
    assert snapshot["remaining"] == 0


def test_progress_load_clamps_legacy_overcount(tmp_path):
    (tmp_path / "progress.json").write_text(
        json.dumps(
            {
                "total": 12,
                "completed": 13,
                "valid": 12,
                "invalid": 1,
                "execution_attempts": 13,
            }
        ),
        encoding="utf-8",
    )
    tracker = ProgressTracker(total=12, output_dir=tmp_path)

    assert tracker.completed == 12
    assert tracker.execution_attempts == 13


def _seed_result(tmp_path):
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    run = select_runs(snapshot, task_id="CI-01", config_name="minimal", repeat_index=1)
    runner = Phase4Runner(snapshot, output_dir=tmp_path, executor=PassingExecutor(), repo_root=ROOT)
    assert asyncio.run(runner.run(run)) == {"valid": 1, "invalid": 0}
    return snapshot, run


def _rewrite_raw(tmp_path, mutate):
    path = tmp_path / "raw.jsonl"
    record = json.loads(path.read_text().splitlines()[0])
    mutate(record)
    path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")


def test_resume_rejects_protocol_hash_mismatch(tmp_path):
    snapshot, run = _seed_result(tmp_path)
    _rewrite_raw(tmp_path, lambda record: record.__setitem__("protocol_hash", "protocol-A"))
    resumed = Phase4Runner(snapshot, output_dir=tmp_path, executor=ExplodingExecutor(), repo_root=ROOT)
    with pytest.raises(ResumeIntegrityError, match="PROTOCOL_HASH_MISMATCH"):
        asyncio.run(resumed.run(run))


def test_resume_accepts_same_full_experiment_identity(tmp_path):
    snapshot, run = _seed_result(tmp_path)
    resumed = Phase4Runner(snapshot, output_dir=tmp_path, executor=ExplodingExecutor(), repo_root=ROOT)
    assert asyncio.run(resumed.run(run)) == {"valid": 1, "invalid": 0}


def test_resume_rejects_missing_protocol_hash(tmp_path):
    snapshot, run = _seed_result(tmp_path)
    _rewrite_raw(tmp_path, lambda record: record.pop("protocol_hash"))
    resumed = Phase4Runner(snapshot, output_dir=tmp_path, executor=ExplodingExecutor(), repo_root=ROOT)
    with pytest.raises(ResumeIntegrityError, match="PROTOCOL_HASH_MISMATCH"):
        asyncio.run(resumed.run(run))


def test_partial_jsonl_tail_is_discarded_and_run_can_resume(tmp_path):
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    run = select_runs(snapshot, task_id="CI-01", config_name="minimal", repeat_index=1)
    (tmp_path / "raw.jsonl").write_text('{"benchmark_run_id":"partial', encoding="utf-8")
    runner = Phase4Runner(snapshot, output_dir=tmp_path, executor=PassingExecutor(), repo_root=ROOT)
    assert asyncio.run(runner.run(run)) == {"valid": 1, "invalid": 0}
    assert len((tmp_path / "raw.jsonl").read_text().splitlines()) == 1


def test_resume_rejects_benchmark_version_mismatch(tmp_path):
    snapshot, run = _seed_result(tmp_path)
    _rewrite_raw(tmp_path, lambda record: record.__setitem__("benchmark_version", "phase4-v0"))
    resumed = Phase4Runner(snapshot, output_dir=tmp_path, executor=ExplodingExecutor(), repo_root=ROOT)
    with pytest.raises(ResumeIntegrityError, match="EXPERIMENT_IDENTITY_MISMATCH"):
        asyncio.run(resumed.run(run))


def test_fault_plan_is_shared_across_configurations():
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    minimal = select_runs(snapshot, task_id="CWR-03", config_name="minimal", repeat_index=1)[0]
    odys = select_runs(snapshot, task_id="CWR-03", config_name="odys_p3", repeat_index=1)[0]
    assert minimal.task["fault_injection"] == odys.task["fault_injection"] == "PROVIDER_UNAVAILABLE"
    assert snapshot.fault_by_id[minimal.task["fault_injection"]]["deterministic_seed"] == 105
