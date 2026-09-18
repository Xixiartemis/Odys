"""Offline contracts for P4 effectiveness evidence."""

from __future__ import annotations

import asyncio
import json

from evals.reliability.p45_executor import P45BenchmarkExecutor
from evals.reliability.p46_provider import (
    CHEAP_MODEL,
    FROZEN_ENDPOINT,
    FROZEN_PROVIDER,
    RealLLMProvider,
)
from evals.reliability.run_phase4 import (
    ProtocolSnapshot,
    TraceWriter,
    select_runs,
)
from evals.reliability.fixture_packages.registry import FixtureRegistry


ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
PROTOCOL_ROOT = ROOT / "evals" / "reliability" / "phase4_v1"


class _Completions:
    async def create(self, **_kwargs):
        return {
            "id": "p4-accounting-test",
            "model": CHEAP_MODEL,
            "choices": [{"message": {"content": "done", "tool_calls": []}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }


class _Client:
    base_url = FROZEN_ENDPOINT

    def __init__(self):
        self.chat = type("Chat", (), {"completions": _Completions()})()


def _provider() -> RealLLMProvider:
    return RealLLMProvider(
        model=CHEAP_MODEL,
        api_key="offline-test-secret",
        base_url=FROZEN_ENDPOINT,
        provider_id=FROZEN_PROVIDER,
        client=_Client(),
        expected_model=CHEAP_MODEL,
    )


def test_real_provider_call_accounting_includes_usage_and_phase():
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    provider = _provider()
    executor = P45BenchmarkExecutor(
        fixture_registry=FixtureRegistry(),
        factory_type="real",
        provider=provider,
        expected_model=CHEAP_MODEL,
    )
    spec = select_runs(
        snapshot,
        task_id="CI-01",
        config_name="minimal",
        repeat_index=1,
    )[0]
    from evals.reliability.run_phase4 import FaultContext, FaultInjector, FixtureHandle, ExecutionRequest

    request = ExecutionRequest(
        run_id=spec.run_id,
        repeat_index=1,
        task=spec.task,
        config=spec.config,
        fixture=FixtureHandle(
            fixture_id=spec.task["fixture_id"],
            version=spec.task["fixture_version"],
            initial_state=spec.task["initial_state"],
            fixture_hash="offline-fixture",
            metadata={},
        ),
        fault=FaultInjector(snapshot).plan_for(spec.task),
        fault_context=FaultContext(FaultInjector(snapshot).plan_for(spec.task)),
    )
    outcome = asyncio.run(executor.execute(request))
    try:
        assert outcome.provider_calls == 1
        assert outcome.model_calls == 1
        assert outcome.provider_call_records[0]["phase"] == "initial"
        assert outcome.provider_call_records[0]["input_tokens"] == 3
        assert outcome.provider_call_records[0]["total_tokens"] == 5
    finally:
        executor.cleanup(request)


def test_trace_writer_persists_canonical_utc_timestamps(tmp_path):
    writer = TraceWriter(tmp_path / "traces.jsonl")
    writer.append(
        run_id="r1",
        task_id="CI-01",
        config="minimal",
        repeat=1,
        runtime_source="offline-test",
        model_identity=CHEAP_MODEL,
        protocol_hash="protocol-test",
        events=[
            {
                "timestamp": "2026-01-01T00:00:00+00:00",
                "event_type": "TASK_STARTED",
                "task_id": "CI-01",
                "step_id": "root",
                "attempt_id": "a1",
                "status": "started",
                "metadata": {},
            }
        ],
    )
    record = json.loads((tmp_path / "traces.jsonl").read_text(encoding="utf-8"))
    assert record["execution_trace"][0]["timestamp"] == "2026-01-01T00:00:00Z"
