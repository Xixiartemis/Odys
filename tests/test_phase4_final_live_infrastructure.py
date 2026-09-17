from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from openai import APITimeoutError

from evals.reliability.p46_provider import (
    CHEAP_MODEL,
    FROZEN_ENDPOINT,
    FROZEN_PROVIDER,
    RealLLMProvider,
    provider_identity,
)
from evals.reliability.run_phase4 import ExecutionOutcome, Phase4Runner, ProtocolSnapshot, select_runs
from evals.reliability.runtime_factory.recovery import repair_thresholds_from_task
from scripts.phase4_live_no_progress_final import (
    ARMS,
    RECOVERY_THRESHOLDS,
    _execute,
    _preflight,
    _task,
    main,
)


class _FakeCompletions:
    def __init__(self, mode: str = "success") -> None:
        self.mode = mode
        self.calls = 0
        self.request = httpx.Request("POST", FROZEN_ENDPOINT + "/chat/completions")

    async def create(self, **kwargs):
        del kwargs
        self.calls += 1
        if self.mode == "timeout":
            raise APITimeoutError(request=self.request)
        if self.mode == "connect_timeout":
            raise httpx.ConnectTimeout("connect timeout", request=self.request)
        if self.mode == "pending":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise APITimeoutError(request=self.request)
        return {
            "id": "offline-provider-response",
            "model": CHEAP_MODEL,
            "choices": [{"message": {"content": "done", "tool_calls": []}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }


class _FakeClient:
    base_url = FROZEN_ENDPOINT

    def __init__(self, mode: str = "success") -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions(mode))


def _provider(client: _FakeClient) -> RealLLMProvider:
    return RealLLMProvider(
        model=CHEAP_MODEL,
        expected_model=CHEAP_MODEL,
        provider_id=FROZEN_PROVIDER,
        api_key="test-secret-never-persisted",
        base_url=FROZEN_ENDPOINT,
        client=client,
    )


def test_final_task_thresholds_are_one_shared_projection():
    snapshot = ProtocolSnapshot.load()
    task = _task(snapshot)

    assert repair_thresholds_from_task(task) == {
        key.removeprefix("repair_"): value
        for key, value in RECOVERY_THRESHOLDS.items()
    }
    assert task["timeout_seconds"] == 900.0
    assert task["task_id"]


def test_real_provider_uses_one_transport_attempt_and_secret_free_identity():
    client = _FakeClient()
    provider = _provider(client)

    result = asyncio.run(
        provider.generate(
            context=SimpleNamespace(messages=[]),
            tools=[],
            timeout_seconds=5,
        )
    )

    assert result["model"] == CHEAP_MODEL
    assert client.chat.completions.calls == 1
    assert provider._inner.max_retries == 0
    assert provider_identity(provider, expected_model=CHEAP_MODEL)["sdk_max_retries"] == 0


def test_real_odys_adapter_stack_consumes_timeout_without_orphans(tmp_path):
    from evals.reliability.p46_provider import RealLLMOdysRuntimeFactory

    task = {
        "task_id": "P4-INFRA-TIMEOUT",
        "title": "provider timeout",
        "objective": "complete a provider timeout probe",
        "acceptance_criteria": [],
        "required_capabilities": ["workspace.edit", "workspace.edit_lines"],
        "expected_observable_effects": {},
        "max_turns": 1,
        "max_model_calls": 1,
        "timeout_seconds": 1,
    }

    for mode in ("timeout", "connect_timeout", "pending"):
        client = _FakeClient(mode)
        provider = _provider(client)
        control = None
        if mode == "pending":
            from lhas.execution_control import ExecutionControlToken

            control = ExecutionControlToken("p4-infra-race", timeout_seconds=2.0)
        workspace = tmp_path / mode
        workspace.mkdir()
        runtime = RealLLMOdysRuntimeFactory(
            provider=provider,
            workspace_root=workspace,
        ).create_runtime({})
        config = {
            "config_id": "odys_p3",
            "run_id": f"p4-infra-{mode}",
            "features": {
                "durable_workflow_recovery": True,
                "failure_provenance": True,
                "selective_repair": True,
            },
            "tool_capability_set": ["workspace.edit", "workspace.edit_lines"],
            "_execution_control": control,
        }
        errors: list[dict] = []
        loop = asyncio.new_event_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: errors.append(context))
        try:
            outcome = loop.run_until_complete(runtime.execute(task, config))
            loop.run_until_complete(asyncio.sleep(0))
        finally:
            loop.set_exception_handler(previous)
            loop.close()

        assert errors == []
        assert client.chat.completions.calls == 1, outcome.observed_state
        assert outcome.failure_type in {
            "PROVIDER_TIMEOUT",
            "PROVIDER_UNAVAILABLE",
            "UNKNOWN_FAILURE",
            "ROOT_DEADLINE_EXCEEDED",
        }


def test_offline_execute_uses_final_wrapper_and_writes_complete_bundle(tmp_path):
    output = tmp_path / "offline-final"
    assert main(["--offline-execute", "--output", str(output)]) == 0

    expected = {
        "experiment_manifest.json",
        "benchmark_identity.json",
        "provider_identity.json",
        "raw.jsonl",
        "invalid.jsonl",
        "aggregation-input.jsonl",
        "traces.jsonl",
        "effect-policy-evidence.jsonl",
        "summary.json",
    }
    assert expected.issubset({item.name for item in output.iterdir()})
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["planned_runs"] == 6
    assert summary["total_runs"] == 6
    assert summary["valid_runs"] == 6
    assert summary["invalid_runs"] == 0
    assert summary["provider_executed"] is True
    assert summary["offline_provider"] is True
    assert len((output / "raw.jsonl").read_text(encoding="utf-8").splitlines()) == 6
    assert len((output / "traces.jsonl").read_text(encoding="utf-8").splitlines()) == 6
    assert len((output / "effect-policy-evidence.jsonl").read_text(encoding="utf-8").splitlines()) == 6
    assert (output / "invalid.jsonl").read_text(encoding="utf-8") == ""
    manifest = json.loads((output / "experiment_manifest.json").read_text(encoding="utf-8"))
    assert manifest["provider_executed"] is True
    assert manifest["run_order"] == [
        "P4E02D-CWR-NP-01::odys_p3::baseline::repeat-1",
        "P4E02D-CWR-NP-01::odys_p3::v2::repeat-1",
        "P4E02D-CWR-NP-01::odys_p3::baseline::repeat-2",
        "P4E02D-CWR-NP-01::odys_p3::v2::repeat-2",
        "P4E02D-CWR-NP-01::odys_p3::baseline::repeat-3",
        "P4E02D-CWR-NP-01::odys_p3::v2::repeat-3",
    ]


def test_offline_execute_resume_is_immutable_and_runs_only_missing_ids(tmp_path, monkeypatch):
    import scripts.phase4_live_no_progress_final as final

    output = tmp_path / "offline-resume"
    original = final._build_runner
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("simulated interruption")
        return original(*args, **kwargs)

    monkeypatch.setattr(final, "_build_runner", interrupted)
    try:
        asyncio.run(_execute(output, offline=True))
    except RuntimeError as exc:
        assert str(exc) == "simulated interruption"
    else:  # pragma: no cover
        raise AssertionError("expected interruption")

    first_raw = (output / "raw.jsonl").read_text(encoding="utf-8")
    assert len(first_raw.splitlines()) == 2
    monkeypatch.setattr(final, "_build_runner", original)
    report = asyncio.run(_execute(output, resume=True, offline=True))

    assert report["total_runs"] == 6
    assert report["valid_runs"] == 6
    run_ids = [json.loads(line)["benchmark_run_id"] for line in (output / "raw.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(run_ids) == len(set(run_ids)) == 6
    assert (output / "raw.jsonl").read_text(encoding="utf-8").splitlines()[:2] == first_raw.splitlines()


class _InfrastructureAfterProvider:
    async def execute(self, request):
        return ExecutionOutcome(
            observed_state={},
            infrastructure_failure=True,
            infrastructure_error="post-dispatch crash",
            provider_calls=1,
            model_calls=1,
            provider_call_reservations=1,
            provider_call_records=[
                {"call_index": 1, "status": "SUCCESS", "total_tokens": 5}
            ],
        )


def test_invalid_projection_preserves_observed_provider_accounting(tmp_path):
    snapshot = ProtocolSnapshot.load()
    run = select_runs(snapshot, task_id="CI-01", config_name="minimal", repeat_index=1)
    runner = Phase4Runner(
        snapshot,
        output_dir=tmp_path,
        executor=_InfrastructureAfterProvider(),
        repo_root=Path(__file__).resolve().parents[1],
    )
    assert asyncio.run(runner.run(run)) == {"valid": 0, "invalid": 1}
    invalid = json.loads((tmp_path / "invalid.jsonl").read_text(encoding="utf-8"))
    accounting = invalid["runtime_environment"]["execution_accounting"]
    assert accounting["provider_calls"] == 1
    assert accounting["model_calls"] == 1
    assert accounting["provider_call_records"][0]["total_tokens"] == 5
