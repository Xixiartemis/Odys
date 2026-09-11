import asyncio
import json
from pathlib import Path

from evals.reliability.run_phase4 import (
    ExecutionOutcome,
    Phase4Runner,
    ProtocolSnapshot,
    select_runs,
    validate_raw_result,
)


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_ROOT = ROOT / "evals" / "reliability" / "phase4_v1"


def _trace(task_id: str) -> list[dict]:
    return [
        {
            "timestamp": "2026-09-11T00:00:00+00:00",
            "event_type": "TASK_STARTED",
            "task_id": task_id,
            "step_id": "root",
            "attempt_id": "attempt-1",
            "status": "started",
            "metadata": {},
        },
        {
            "timestamp": "2026-09-11T00:00:01+00:00",
            "event_type": "STEP_VERIFIED",
            "task_id": task_id,
            "step_id": "root",
            "attempt_id": "attempt-1",
            "status": "verified",
            "metadata": {"source": "test-executor"},
        },
    ]


class _TraceExecutor:
    async def execute(self, request):
        return ExecutionOutcome(
            claimed_complete=True,
            observed_state=request.task["expected_observable_effects"],
            execution_trace=_trace(request.task["task_id"]),
            runtime_source="real_test_adapter",
            attempt_count=1,
        )


class _ExplodingExecutor:
    async def execute(self, request):
        raise AssertionError("resumed run must not execute")


def test_official_trace_is_append_only_and_referenced_by_schema_valid_raw(tmp_path):
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    run = select_runs(snapshot, task_id="CI-01", config_name="minimal", repeat_index=1)
    runner = Phase4Runner(
        snapshot,
        output_dir=tmp_path,
        executor=_TraceExecutor(),
        model="mimo-v2.5-pro",
        provider="xiaomimimo-openai-compatible",
        repo_root=ROOT,
        trace_path=tmp_path / "traces.jsonl",
        require_trace=True,
    )

    assert asyncio.run(runner.run(run)) == {"valid": 1, "invalid": 0}
    raw = json.loads((tmp_path / "raw.jsonl").read_text(encoding="utf-8"))
    trace_record = json.loads((tmp_path / "traces.jsonl").read_text(encoding="utf-8"))

    environment = raw["runtime_environment"]
    assert environment["execution_trace_ref"] == "traces.jsonl#L1"
    assert environment["trace_event_count"] == 2
    assert environment["runtime_source"] == "real_test_adapter"
    assert trace_record["run_id"] == run[0].run_id
    assert trace_record["trace_event_count"] == 2
    for event in trace_record["execution_trace"]:
        assert {
            "timestamp", "event_type", "task_id", "step_id",
            "attempt_id", "status", "metadata",
        } <= set(event)
    validate_raw_result(raw, PROTOCOL_ROOT / "schemas" / "result.schema.json")


def test_official_trace_resume_requires_existing_trace(tmp_path):
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    run = select_runs(snapshot, task_id="CI-01", config_name="minimal", repeat_index=1)
    first = Phase4Runner(
        snapshot,
        output_dir=tmp_path,
        executor=_TraceExecutor(),
        model="mimo-v2.5-pro",
        provider="xiaomimimo-openai-compatible",
        repo_root=ROOT,
        trace_path=tmp_path / "traces.jsonl",
        require_trace=True,
    )
    asyncio.run(first.run(run))

    resumed = Phase4Runner(
        snapshot,
        output_dir=tmp_path,
        executor=_ExplodingExecutor(),
        model="mimo-v2.5-pro",
        provider="xiaomimimo-openai-compatible",
        repo_root=ROOT,
        trace_path=tmp_path / "traces.jsonl",
        require_trace=True,
    )
    assert asyncio.run(resumed.run(run)) == {"valid": 1, "invalid": 0}
