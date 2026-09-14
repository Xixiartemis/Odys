"""Offline proof of the versioned job-ready recovery benchmark."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path

from evals.reliability.job_ready_recovery_v1 import (
    BROKEN_CONTENT,
    JOB_READY_VERSION,
    TARGET_CONTENT,
    TARGET_HASH,
    TASK_ID,
    JobReadyFixtureRegistry,
    build_runner,
    job_ready_config_hash,
    load_snapshot,
    select_single_odys_run,
    validate_job_ready_protocol,
)
from evals.reliability.p45_executor import P45BenchmarkExecutor
from evals.reliability.p46_provider import (
    CHEAP_MODEL,
    FROZEN_ENDPOINT,
    FROZEN_PROVIDER,
    RealLLMProvider,
)
from evals.reliability.run_phase4 import ExternalObservableValidator, select_runs


ROOT = Path(__file__).resolve().parents[1]


class _FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("offline provider response sequence exhausted")
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.base_url = FROZEN_ENDPOINT
        self.chat = type("Chat", (), {"completions": _FakeCompletions(responses)})()


def _response(*, content: str, tool_calls: list[dict] | None = None) -> dict:
    return {
        "id": "job-ready-offline",
        "model": CHEAP_MODEL,
        "choices": [{
            "message": {
                "content": content,
                "tool_calls": tool_calls or [],
            }
        }],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
    }


def _repair_response() -> dict:
    return _response(
        content="Repairing the state document.",
        tool_calls=[{
            "id": "job-ready-repair-call",
            "type": "function",
            "function": {
                "name": "workspace.edit",
                "arguments": json.dumps({
                    "path": "state.json",
                    "content": TARGET_CONTENT,
                }),
            },
        }],
    )


def _provider(responses) -> RealLLMProvider:
    return RealLLMProvider(
        model=CHEAP_MODEL,
        api_key="offline-job-ready-test-secret",
        base_url=FROZEN_ENDPOINT,
        provider_id=FROZEN_PROVIDER,
        client=_FakeClient(responses),
        expected_model=CHEAP_MODEL,
    )


def _run(tmp_path, snapshot, config: str, responses):
    output = tmp_path / config
    provider = _provider(responses)
    executor = P45BenchmarkExecutor(
        fixture_registry=JobReadyFixtureRegistry(),
        factory_type="real",
        provider=provider,
        expected_model=CHEAP_MODEL,
    )
    runner = build_runner(
        snapshot,
        executor=executor,
        output_dir=output,
        repo_root=ROOT,
    )
    runs = select_runs(
        snapshot,
        task_id=TASK_ID,
        config_name=config,
        repeat_index=1,
    )
    result = __import__("asyncio").run(runner.run(runs))
    raw = json.loads((output / "raw.jsonl").read_text(encoding="utf-8").splitlines()[0])
    trace = json.loads((output / "traces.jsonl").read_text(encoding="utf-8").splitlines()[0])
    return result, raw, trace, output, provider


def test_job_ready_protocol_is_versioned_and_selects_one_live_gate():
    report = validate_job_ready_protocol()
    snapshot = load_snapshot()
    assert report["benchmark_version"] == JOB_READY_VERSION
    assert report["protocol_hash"] == snapshot.protocol_hash
    assert job_ready_config_hash(snapshot)
    assert select_single_odys_run(snapshot)[0].run_id.endswith("odys_p3::repeat-1")


def test_fake_minimal_keeps_fault_observable_and_rejected(tmp_path):
    snapshot = load_snapshot()
    counts, raw, trace, _output, provider = _run(
        tmp_path,
        snapshot,
        "minimal",
        [_response(content="Task completed.")],
    )

    assert counts == {"valid": 1, "invalid": 0}
    assert raw["validity"] == "VALIDATED_FAIL"
    assert raw["verified_completion"] is False
    assert raw["false_completion"] is True
    assert raw["recovery_attempted"] is False
    assert raw["runtime_environment"]["runtime_source"] == "minimal_factory"
    assert provider.call_records[0]["provider"] == FROZEN_PROVIDER
    assert len(provider.call_records) == 1
    event_types = [event["event_type"] for event in trace["execution_trace"]]
    assert "FAULT_INJECTED" in event_types
    assert trace["execution_trace"][-1]["metadata"]["acceptance_status"] == "REJECTED"


def test_fake_odys_repairs_through_p45_real_factory_and_external_validator(tmp_path):
    snapshot = load_snapshot()
    counts, raw, trace, _output, provider = _run(
        tmp_path,
        snapshot,
        "odys_p3",
        [
            _response(content="Task completed."),
            _repair_response(),
            _response(content="State repaired and verified."),
        ],
    )

    assert counts == {"valid": 1, "invalid": 0}
    assert raw["validity"] == "VALIDATED_PASS"
    assert raw["verified_completion"] is True
    assert raw["recovery_required"] is True
    assert raw["recovery_attempted"] is True
    assert raw["recovery_success"] is True
    assert raw["repair_scope"] == "LOCAL"
    assert raw["repair_attempts"] == 1
    assert raw["duplicate_side_effect_count"] == 0
    assert raw["runtime_environment"]["validation"]["acceptance_status"] == "REJECTED"
    assert raw["runtime_environment"]["validation"]["final_acceptance_status"] == "ACCEPTED"
    assert raw["runtime_environment"]["state_evidence"]["state_changed_after_repair"] is True
    evidence = raw["runtime_environment"]["state_evidence"]
    assert evidence["pre_repair_state_digest"] != evidence["post_repair_state_digest"]
    assert evidence["validator_observed_state_digest"] == evidence["post_repair_state_digest"]
    assert evidence["validator_observed_repaired_state"] is True
    assert raw["runtime_environment"]["execution_accounting"]["provider_calls"] <= 20
    assert len(provider.call_records) == 3

    events = trace["execution_trace"]
    event_types = [event["event_type"] for event in events]
    for event_type in (
        "TASK_STARTED",
        "FAULT_INJECTED",
        "EXECUTION_COMPLETE",
        "VALIDATION_RESULT",
        "FAILURE_DETECTED",
        "StepFailureProvenance",
        "REPAIR_STARTED",
        "REPAIR_COMPLETED",
        "STEP_VERIFIED",
        "VERIFICATION_PASSED",
    ):
        assert event_type in event_types
    validation_events = [
        event for event in events if event["event_type"] == "VALIDATION_RESULT"
    ]
    assert validation_events[0]["metadata"]["acceptance_status"] == "REJECTED"
    assert validation_events[-1]["metadata"]["acceptance_status"] == "ACCEPTED"
    assert raw["runtime_environment"]["identity"]["benchmark_version"] == JOB_READY_VERSION


def test_job_ready_uses_the_same_external_validator_contract():
    snapshot = load_snapshot()
    assert snapshot.protocol["shared_validator_id"] == "external-observable-v1"
    assert isinstance(ExternalObservableValidator(), ExternalObservableValidator)
    assert hashlib.sha256(TARGET_CONTENT.encode("utf-8")).hexdigest() == TARGET_HASH
    assert BROKEN_CONTENT != TARGET_CONTENT
