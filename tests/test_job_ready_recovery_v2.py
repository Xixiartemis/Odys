"""Deterministic proof of the V2 attempt boundary and cross-attempt repair."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from evals.reliability.job_ready_recovery_v2 import (
    BROKEN_CONTENT, JOB_READY_VERSION, TARGET_CONTENT, TARGET_HASH, TASK_ID,
    JobReadyFixtureRegistry, build_runner, job_ready_config_hash, load_snapshot,
    select_single_odys_run, validate_job_ready_protocol,
)
from evals.reliability.p45_executor import P45BenchmarkExecutor
from evals.reliability.p46_provider import CHEAP_MODEL, FROZEN_ENDPOINT, FROZEN_PROVIDER, RealLLMProvider
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
    return {"id": "job-ready-v2-offline", "model": CHEAP_MODEL, "choices": [{"message": {"content": content, "tool_calls": tool_calls or []}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


def _initial_tool_response() -> dict:
    return _response(content="Applying the requested state change.", tool_calls=[{"id": "job-ready-initial-call", "type": "function", "function": {"name": "workspace.edit", "arguments": json.dumps({"path": "state.json", "content": TARGET_CONTENT})}}])


def _repair_response() -> dict:
    return _response(content="Repairing the state document.", tool_calls=[{"id": "job-ready-repair-call", "type": "function", "function": {"name": "workspace.edit", "arguments": json.dumps({"path": "state.json", "content": TARGET_CONTENT})}}])


def _provider(responses):
    client = _FakeClient(responses)
    provider = RealLLMProvider(model=CHEAP_MODEL, api_key="offline-job-ready-v2-test-secret", base_url=FROZEN_ENDPOINT, provider_id=FROZEN_PROVIDER, client=client, expected_model=CHEAP_MODEL)
    provider.offline_requests = client.chat.completions.calls
    return provider


def _run(tmp_path, snapshot, config: str, responses):
    output = tmp_path / config
    provider = _provider(responses)
    executor = P45BenchmarkExecutor(fixture_registry=JobReadyFixtureRegistry(), factory_type="real", provider=provider, expected_model=CHEAP_MODEL)
    runner = build_runner(snapshot, executor=executor, output_dir=output, repo_root=ROOT)
    runs = select_runs(snapshot, task_id=TASK_ID, config_name=config, repeat_index=1)
    result = asyncio.run(runner.run(runs))
    raw = json.loads((output / "raw.jsonl").read_text(encoding="utf-8").splitlines()[0])
    trace = json.loads((output / "traces.jsonl").read_text(encoding="utf-8").splitlines()[0])
    return result, raw, trace, output, provider


def test_v2_protocol_and_selection_are_versioned():
    report = validate_job_ready_protocol()
    snapshot = load_snapshot()
    assert report["benchmark_version"] == JOB_READY_VERSION
    assert report["protocol_hash"] == snapshot.protocol_hash
    assert job_ready_config_hash(snapshot)
    assert select_single_odys_run(snapshot)[0].run_id.endswith("odys_p3::repeat-1")


def test_v2_minimal_terminal_failure_has_budget_headroom_and_no_recovery(tmp_path):
    snapshot = load_snapshot()
    counts, raw, trace, _output, provider = _run(tmp_path, snapshot, "minimal", [_initial_tool_response()])
    assert counts == {"valid": 1, "invalid": 0}
    assert raw["validity"] == "VALIDATED_FAIL"
    assert raw["verified_completion"] is False
    assert raw["recovery_attempted"] is False
    assert raw["runtime_environment"]["execution_accounting"]["provider_calls"] == 1
    assert raw["runtime_environment"]["execution_accounting"]["provider_calls"] < 20
    assert len(provider.call_records) == 1
    assert raw["runtime_environment"]["runtime_source"] == "minimal_factory"
    assert any(e["event_type"] == "FAULT_INJECTED" for e in trace["execution_trace"])
    terminal = next(e for e in trace["execution_trace"] if e["event_type"] == "EXECUTION_COMPLETE")
    assert terminal["metadata"]["attempt_terminal"] is True
    assert terminal["metadata"]["terminal_failure_type"] == "ATTEMPT_TERMINAL"


def test_v2_odys_repairs_after_typed_terminal_failure(tmp_path):
    snapshot = load_snapshot()
    counts, raw, trace, _output, provider = _run(tmp_path, snapshot, "odys_p3", [_initial_tool_response(), _repair_response(), _response(content="State repaired and verified.")])
    assert counts == {"valid": 1, "invalid": 0}
    assert raw["validity"] == "VALIDATED_PASS"
    assert raw["verified_completion"] is True
    assert raw["recovery_required"] is True
    assert raw["recovery_attempted"] is True
    assert raw["recovery_success"] is True
    assert raw["repair_attempts"] == 1
    assert raw["runtime_environment"]["execution_accounting"]["provider_calls"] <= 20
    evidence = raw["runtime_environment"]["state_evidence"]
    assert evidence["pre_repair_state_digest"] != evidence["post_repair_state_digest"]
    assert evidence["validator_observed_state_digest"] == evidence["post_repair_state_digest"]
    assert len(provider.call_records) == 3
    events = trace["execution_trace"]
    types = [event["event_type"] for event in events]
    required = ["TASK_STARTED", "FAULT_INJECTED", "EXECUTION_COMPLETE", "VALIDATION_RESULT", "FAILURE_DETECTED", "StepFailureProvenance", "REPAIR_STARTED", "REPAIR_COMPLETED", "STEP_VERIFIED", "VERIFICATION_PASSED"]
    assert all(item in types for item in required)
    assert types.index("FAULT_INJECTED") < types.index("EXECUTION_COMPLETE") < types.index("FAILURE_DETECTED") < types.index("REPAIR_STARTED") < types.index("REPAIR_COMPLETED") < types.index("VERIFICATION_PASSED")
    validations = [event for event in events if event["event_type"] == "VALIDATION_RESULT"]
    assert validations[0]["metadata"]["acceptance_status"] == "REJECTED"
    assert validations[-1]["metadata"]["acceptance_status"] == "ACCEPTED"
    attempt_ids = {event["attempt_id"] for event in events if event["event_type"] in {"EXECUTION_COMPLETE", "REPAIR_STARTED", "REPAIR_COMPLETED", "STEP_VERIFIED"}}
    assert len(attempt_ids) >= 2


def test_v2_uses_shared_external_validator_and_canonical_target():
    snapshot = load_snapshot()
    assert snapshot.protocol["shared_validator_id"] == "external-observable-v1"
    assert isinstance(ExternalObservableValidator(), ExternalObservableValidator)
    assert hashlib.sha256(TARGET_CONTENT.encode("utf-8")).hexdigest() == TARGET_HASH
    assert BROKEN_CONTENT != TARGET_CONTENT


def test_v2_recovery_is_bounded_and_projects_real_execution_evidence(tmp_path):
    snapshot = load_snapshot()
    counts, raw, trace, _output, provider = _run(
        tmp_path,
        snapshot,
        "odys_p3",
        [_initial_tool_response(), _repair_response(), _response(content="State repaired and verified.")],
    )

    assert counts == {"valid": 1, "invalid": 0}
    accounting = raw["runtime_environment"]["execution_accounting"]
    assert accounting["provider_calls"] == 3
    assert accounting["model_calls"] == 3
    assert accounting["provider_call_reservations"] == 3
    assert accounting["unrecorded_provider_reservations"] == 0
    assert accounting["root_timeout_seconds"] == 900.0
    assert accounting["provider_timeout_seconds"] == 300.0
    assert raw["tool_calls"] == 1

    actual_repair_records = [
        item for item in provider.call_records if item.get("phase") == "repair"
    ]
    assert len(actual_repair_records) == 2
    assert len({item["attempt_id"] for item in actual_repair_records}) == 1
    assert accounting["nested_attempt_count"] == 1
    assert accounting["provider_attempt_count"] == 2

    event_types = [event["event_type"] for event in trace["execution_trace"]]
    assert "REPLAN_REJECTED" not in event_types
    assert event_types.index("REPAIR_STARTED") < event_types.index("REPAIR_COMPLETED")

    repair_messages = [
        message.get("content", "")
        for call in provider.offline_requests[1:]
        for message in call.get("messages", [])
    ]
    assert any("repair_context" in message for message in repair_messages)
    assert any("failure_provenance" in message for message in repair_messages)
    assert any("expected_observable_effects" in message for message in repair_messages)
