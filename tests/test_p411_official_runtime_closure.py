"""P411 tests for the actual P45 -> real factory execution chain."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from evals.reliability.p45_executor import P45BenchmarkExecutor
from evals.reliability.run_phase4 import (
    ExecutionOutcome,
    ExecutionRequest,
    FaultContext,
    FixtureHandle,
    Phase4Runner,
    ProtocolSnapshot,
    RunSpec,
    select_runs,
)
from evals.reliability.p46_provider import (
    CHEAP_MODEL,
    FROZEN_ENDPOINT,
    FROZEN_PROVIDER,
    RealLLMProvider,
)
from evals.reliability.fixture_packages.registry import FixtureRegistry


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_ROOT = ROOT / "evals" / "reliability" / "phase4_v1"
CHEAP_VERSION = "phase4-v1-cheap-model"
CHEAP_CONFIG_HASH = "318a4fdf7d87b446780b5ca79381df77bf9dea5619598cd9917cee51de38f2fd"


class _Completions:
    async def create(self, **_kwargs):
        return {
            "id": "p411-test-response",
            "model": CHEAP_MODEL,
            "choices": [
                {"message": {"content": "Task completed.", "tool_calls": []}}
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
        }


class _FailingCompletions:
    async def create(self, **_kwargs):
        raise RuntimeError("upstream opaque failure")


class _Client:
    def __init__(self, completions=None):
        self.base_url = FROZEN_ENDPOINT
        self.chat = type(
            "Chat",
            (),
            {"completions": completions or _Completions()},
        )()


class _RejectingOdysExecutor:
    """Return a validator rejection, then fail during the recovery bridge."""

    async def execute(self, request):
        attempt_id = f"{request.run_id}::attempt-1"
        event = {
            "timestamp": "2026-01-01T00:00:00Z",
            "event_type": "EXECUTION_COMPLETE",
            "task_id": request.task["task_id"],
            "step_id": "root",
            "attempt_id": attempt_id,
            "status": "completed",
            "metadata": {},
        }
        return ExecutionOutcome(
            claimed_complete=True,
            observed_state={"runtime_source": "odys_factory"},
            execution_trace=[event],
            runtime_source="odys_factory",
            attempt_count=1,
            model_calls=1,
        )

    async def recover_after_validation(self, _request, _outcome, _validation):
        raise RuntimeError("synthetic recovery bridge failure")


def _provider(*, completions=None) -> RealLLMProvider:
    return RealLLMProvider(
        model=CHEAP_MODEL,
        api_key="p411-test-secret",
        base_url=FROZEN_ENDPOINT,
        provider_id=FROZEN_PROVIDER,
        client=_Client(completions),
        expected_model=CHEAP_MODEL,
    )


def _request(snapshot: ProtocolSnapshot, task_id: str, config_id: str) -> ExecutionRequest:
    spec = select_runs(
        snapshot,
        task_id=task_id,
        config_name=config_id,
        repeat_index=1,
    )[0]
    fault = __import__("evals.reliability.run_phase4", fromlist=["FaultInjector"]).FaultInjector(snapshot).plan_for(spec.task)
    fixture = FixtureHandle(
        fixture_id=spec.task["fixture_id"],
        version=spec.task["fixture_version"],
        initial_state=spec.task["initial_state"],
        fixture_hash="test-fixture-hash",
        metadata={},
    )
    return ExecutionRequest(
        run_id=spec.run_id,
        repeat_index=1,
        task=spec.task,
        config=spec.config,
        fixture=fixture,
        fault=fault,
        fault_context=FaultContext(fault),
    )


def _executor(*, provider=None) -> P45BenchmarkExecutor:
    return P45BenchmarkExecutor(
        fixture_registry=FixtureRegistry(),
        factory_type="real",
        provider=provider or _provider(),
        expected_model=CHEAP_MODEL,
    )


def test_minimal_real_provider_execution_is_a_functioning_baseline():
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    outcome = asyncio.run(_executor().execute(_request(snapshot, "CI-01", "minimal")))

    assert outcome.claimed_complete is True
    assert outcome.infrastructure_failure is False
    assert outcome.model_calls == 1
    assert outcome.failure_type is None
    assert outcome.observed_state["runtime_source"] == "minimal_factory"
    assert outcome.observed_state["execution_trace"]


def test_minimal_internal_attribute_error_is_infrastructure_invalid(tmp_path, monkeypatch):
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    from lhas.native.context import NativeContextAssembler

    def broken_build(*_args, **_kwargs):
        raise AttributeError("synthetic internal snapshot defect")

    monkeypatch.setattr(NativeContextAssembler, "build", broken_build)
    runner = Phase4Runner(
        snapshot,
        output_dir=tmp_path,
        executor=_executor(),
        model=CHEAP_MODEL,
        provider=FROZEN_PROVIDER,
        repo_root=ROOT,
    )
    run = select_runs(snapshot, task_id="CI-01", config_name="minimal", repeat_index=1)

    assert asyncio.run(runner.run(run)) == {"valid": 0, "invalid": 1}
    invalid = json.loads((tmp_path / "invalid.jsonl").read_text().splitlines()[0])
    assert invalid["validity"] == "INVALID_RUN"
    assert "MINIMAL_RUNTIME_EXCEPTION:AttributeError" in invalid["invalid_reason"]


def test_unexpected_odys_provider_failure_is_infrastructure_invalid(tmp_path):
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    runner = Phase4Runner(
        snapshot,
        output_dir=tmp_path,
        executor=_executor(provider=_provider(completions=_FailingCompletions())),
        model=CHEAP_MODEL,
        provider=FROZEN_PROVIDER,
        repo_root=ROOT,
    )
    run = select_runs(snapshot, task_id="CI-01", config_name="odys_p3", repeat_index=1)

    assert asyncio.run(runner.run(run)) == {"valid": 0, "invalid": 1}
    invalid = json.loads((tmp_path / "invalid.jsonl").read_text().splitlines()[0])
    assert invalid["validity"] == "INVALID_RUN"
    assert "UNKNOWN_PROVIDER_FAILURE" in invalid["invalid_reason"]
    assert "upstream opaque failure" in invalid["invalid_reason"]


def test_official_odys_recovery_uses_canonical_attempt_lineage(tmp_path):
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    executor = _executor()
    runner = Phase4Runner(
        snapshot,
        output_dir=tmp_path,
        executor=executor,
        model=CHEAP_MODEL,
        provider=FROZEN_PROVIDER,
        repo_root=ROOT,
        trace_path=tmp_path / "traces.jsonl",
        require_trace=True,
    )
    run = select_runs(snapshot, task_id="CI-01", config_name="odys_p3", repeat_index=1)

    assert asyncio.run(runner.run(run)) == {"valid": 1, "invalid": 0}
    raw = json.loads((tmp_path / "raw.jsonl").read_text().splitlines()[0])
    trace_record = json.loads((tmp_path / "traces.jsonl").read_text().splitlines()[0])
    recovery = raw["runtime_environment"]["recovery"]
    event_types = [event["event_type"] for event in trace_record["execution_trace"]]

    assert recovery["recovery_required"] is True
    assert recovery["recovery_attempted"] is True
    assert recovery["original_failure_attempt_id"] != recovery["repair_attempt_id"]
    assert recovery["original_failure_attempt_id"].endswith("::attempt-1")
    assert "StepFailureProvenance" in event_types
    assert "REPAIR_STARTED" in event_types
    assert "REPAIR_COMPLETED" in event_types
    assert "VERIFICATION_FAILED" in event_types


def test_cheap_profile_identity_propagates_to_raw_and_trace(tmp_path):
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    runner = Phase4Runner(
        snapshot,
        output_dir=tmp_path,
        executor=_executor(),
        model=CHEAP_MODEL,
        provider=FROZEN_PROVIDER,
        benchmark_version=CHEAP_VERSION,
        benchmark_config_hash=CHEAP_CONFIG_HASH,
        repo_root=ROOT,
        trace_path=tmp_path / "traces.jsonl",
        require_trace=True,
    )
    run = select_runs(snapshot, task_id="CI-01", config_name="minimal", repeat_index=1)

    assert asyncio.run(runner.run(run)) == {"valid": 1, "invalid": 0}
    raw = json.loads((tmp_path / "raw.jsonl").read_text().splitlines()[0])
    trace = json.loads((tmp_path / "traces.jsonl").read_text().splitlines()[0])

    assert raw["benchmark_version"] == CHEAP_VERSION
    assert raw["runtime_environment"]["identity"]["benchmark_version"] == CHEAP_VERSION
    assert raw["runtime_environment"]["identity"]["benchmark_config_hash"] == CHEAP_CONFIG_HASH
    assert trace["benchmark_version"] == CHEAP_VERSION
    assert trace["benchmark_config_hash"] == CHEAP_CONFIG_HASH


def test_invalid_run_keeps_diagnostic_trace_and_profile_identity(tmp_path):
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    runner = Phase4Runner(
        snapshot,
        output_dir=tmp_path,
        executor=_RejectingOdysExecutor(),
        model=CHEAP_MODEL,
        provider=FROZEN_PROVIDER,
        benchmark_version=CHEAP_VERSION,
        benchmark_config_hash=CHEAP_CONFIG_HASH,
        repo_root=ROOT,
        trace_path=tmp_path / "traces.jsonl",
        require_trace=True,
    )
    run = select_runs(snapshot, task_id="CI-01", config_name="odys_p3", repeat_index=1)

    assert asyncio.run(runner.run(run)) == {"valid": 0, "invalid": 1}
    invalid = json.loads((tmp_path / "invalid.jsonl").read_text().splitlines()[0])
    trace = json.loads((tmp_path / "traces.jsonl").read_text().splitlines()[0])
    event_types = [event["event_type"] for event in trace["execution_trace"]]

    assert invalid["benchmark_version"] == CHEAP_VERSION
    assert invalid["runtime_environment"]["identity"]["benchmark_version"] == CHEAP_VERSION
    assert invalid["runtime_environment"]["execution_trace_ref"]
    assert invalid["runtime_environment"]["diagnostic_trace_status"] == "PARTIAL_DIAGNOSTIC_TRACE"
    assert "VALIDATION_RESULT" in event_types
    assert "FAILURE_DETECTED" in event_types
