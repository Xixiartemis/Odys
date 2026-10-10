"""Provider-free tests for the frozen MiMo/OpenAI wire and fake E2E path."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from lhas.phase5.control_arms import ControlArm
from lhas.phase5.live_driver import LiveModelDriver, ProviderExecutionError
from lhas.phase5.provider_wire import canonical_tool_schemas, serialize_messages, tool_schema_hash
from lhas.phase5.trial_executor import (
    _classify_exception,
    _provider_failure_termination_reason,
    execute_trial,
)
from lhas.phase5.types import BudgetConfig


ROOT = Path(__file__).resolve().parents[1]
TOOLMAZE_ROOT = ROOT / "experiments" / "phase5" / "benchmarks" / "toolmaze"
TASK_PATH = TOOLMAZE_ROOT / "data" / "perturbed_tasks" / "c1" / "C1_task_082_P0.json"
if str(TOOLMAZE_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLMAZE_ROOT))


class _FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.chat = type("Chat", (), {})()
        self.chat.completions = _FakeCompletions(responses)
        self.base_url = "https://token-plan-cn.xiaomimimo.com/v1"


def _engine_tools():
    from evaluation.core.sandbox import ExecutionEngine

    task = json.loads(TASK_PATH.read_text(encoding="utf-8"))
    engine = ExecutionEngine(task_json=task, agent=object(), tools_dir=str(TOOLMAZE_ROOT / "tools"))
    return task, engine.tool_definitions


def _response(*, tool_call=None, content="", prompt=11, completion=7):
    message = {"role": "assistant", "content": content}
    if tool_call is not None:
        message["reasoning_content"] = "reasoning\nwith exact whitespace"
        message["tool_calls"] = [tool_call]
    return {
        "model": "mimo-v2.5",
        "choices": [{"message": message}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }


def _driver(fake_client):
    return LiveModelDriver(
        provider="mimo",
        model_id="mimo-v2.5",
        base_url="https://token-plan-cn.xiaomimimo.com/v1",
        api_key="dummy-never-sent",
        thinking_enabled=True,
        effective_temperature=1.0,
        effective_top_p=0.95,
        sampling_parameters_sent=False,
        max_completion_tokens=4096,
        sdk_max_retries=0,
        supports_tool_choice=False,
        supports_parallel_tool_calls=False,
        client_factory=lambda **_: fake_client,
    )


def test_wire_serializer_strips_internal_fields_and_roundtrips_provider_ids():
    messages = [
        {"role": "user", "content": "task", "metadata": {"secret": "hidden"}},
        {
            "role": "assistant",
            "content": "",
            "thought": "Odys-only thought",
            "metadata": {"invocation_id": "odys-inv-1"},
            "_provider_transport": {"reasoning_content": "reasoning\nexact"},
            "tool_call": {
                "id": "provider-call-1",
                "provider_tool_call_id": "provider-call-1",
                "invocation_id": "odys-inv-1",
                "name": "get_stock_level",
                "arguments": {"product_id": "p_apple_airpods"},
            },
        },
        {
            "role": "tool",
            "tool_call_id": "odys-inv-1",
            "provider_tool_call_id": "provider-call-1",
            "content": {"status": "success", "stock": 50},
            "result_digest": "internal",
        },
    ]

    wire = serialize_messages(messages)

    assert wire == [
        {"role": "user", "content": "task"},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "reasoning\nexact",
            "tool_calls": [
                {
                    "id": "provider-call-1",
                    "type": "function",
                    "function": {
                        "name": "get_stock_level",
                        "arguments": '{"product_id":"p_apple_airpods"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "provider-call-1",
            "content": '{"status":"success","stock":50}',
        },
    ]
    assert "thought" not in json.dumps(wire)
    assert "invocation_id" not in json.dumps(wire)


def test_live_driver_uses_correct_mimo_request_contract_and_reasoning_roundtrip():
    task, tool_definitions = _engine_tools()
    first_call = {
        "id": "provider-call-1",
        "type": "function",
        "function": {
            "name": "get_stock_level",
            "arguments": '{"product_id":"p_apple_airpods"}',
        },
    }
    fake = _FakeClient(
        [_response(tool_call=first_call), _response(content="done", prompt=13, completion=3)]
    )
    driver = _driver(fake)

    first = driver.next_action(
        [{"role": "user", "content": task["user_input"]["query"]}],
        tool_definitions,
    )
    second_messages = [
        {"role": "user", "content": task["user_input"]["query"]},
        {
            "role": "assistant",
            "content": "",
            "tool_call": {
                "id": "provider-call-1",
                "provider_tool_call_id": "provider-call-1",
                "invocation_id": "odys-inv-1",
                "name": first.tool_name,
                "arguments": first.arguments,
            },
            "_provider_transport": {"reasoning_content": first.provider_reasoning_content},
        },
        {
            "role": "tool",
            "tool_call_id": "odys-inv-1",
            "provider_tool_call_id": "provider-call-1",
            "content": '{"status":"success"}',
        },
    ]
    final = driver.next_action(second_messages, tool_definitions)

    assert first.provider_tool_call_id == "provider-call-1"
    assert first.provider_reasoning_content == "reasoning\nwith exact whitespace"
    assert final.content == "done"
    assert driver.provider_request_count == 2
    assert driver.logical_provider_call_count == 2
    assert driver.get_total_tokens() == 34
    request = fake.chat.completions.requests[0]
    assert request["max_completion_tokens"] == 4096
    assert request["parallel_tool_calls"] is False
    assert request["extra_body"] == {"thinking": {"type": "enabled"}}
    assert "temperature" not in request
    assert "top_p" not in request
    assert "max_tokens" not in request
    assert request["tools"] == sorted(
        request["tools"], key=lambda item: item["function"]["name"]
    )
    second_wire = fake.chat.completions.requests[1]["messages"]
    assert second_wire[1]["tool_calls"][0]["id"] == "provider-call-1"
    assert second_wire[1]["reasoning_content"] == "reasoning\nwith exact whitespace"
    assert second_wire[2]["tool_call_id"] == "provider-call-1"


@pytest.mark.parametrize(
    "response,match",
    [
        ({"model": "mimo-v2.5", "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}, "choices"),
        ({"model": "other", "choices": [{"message": {"role": "assistant", "content": "x"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}, "model identity"),
        ({"model": "mimo-v2.5", "choices": [{"message": {"role": "assistant", "content": "x"}}]}, "usage"),
    ],
)
def test_live_driver_rejects_invalid_provider_contract(response, match):
    _, tools = _engine_tools()
    driver = _driver(_FakeClient([response]))
    with pytest.raises(ProviderExecutionError, match=match):
        driver.next_action([{"role": "user", "content": "task"}], tools)


def test_live_driver_rejects_missing_reasoning_but_preserves_multi_tool_batch():
    _, tools = _engine_tools()
    missing_reasoning = {
        "model": "mimo-v2.5",
        "choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "a", "function": {"name": "x", "arguments": "{}"}},]}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    with pytest.raises(ProviderExecutionError, match="reasoning_content"):
        _driver(_FakeClient([missing_reasoning])).next_action([{"role": "user", "content": "task"}], tools)

    parallel = {
        "model": "mimo-v2.5",
        "choices": [{"message": {"role": "assistant", "reasoning_content": "r", "tool_calls": [
            {"id": "a", "function": {"name": "x", "arguments": "{}"}},
            {"id": "b", "function": {"name": "y", "arguments": "{}"}},
        ]}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    action = _driver(_FakeClient([parallel])).next_action(
        [{"role": "user", "content": "task"}], tools
    )
    assert [call.tool_name for call in action.tool_calls] == ["x", "y"]
    assert [call.provider_tool_call_id for call in action.tool_calls] == ["a", "b"]


@pytest.mark.parametrize(
    "failure_class,expected",
    [
        ("PROVIDER_PROTOCOL_FAILURE", "provider_protocol_failure"),
        ("CONNECTION_RESET", "provider_transport_failure"),
    ],
)
def test_provider_failure_classification_preserves_protocol_transport_split(
    failure_class, expected
):
    error = ProviderExecutionError("classified failure", failure_class=failure_class)
    assert _provider_failure_termination_reason(error) == expected


def test_fake_openai_e2e_uses_canonical_execute_trial_without_provider_requests():
    task = json.loads(TASK_PATH.read_text(encoding="utf-8"))
    first_call = {
        "id": "provider-e2e-1",
        "type": "function",
        "function": {"name": "get_stock_level", "arguments": '{"product_id":"p_apple_airpods"}'},
    }
    fake = _FakeClient([_response(tool_call=first_call), _response(content="done")])
    driver = _driver(fake)
    result = execute_trial(
        arm=ControlArm.A0_BARE,
        task_json=task,
        model_driver=driver,
        budget=BudgetConfig(max_turns=3, max_model_calls=3, token_budget=1000),
        experiment_id="phase5-fake-e2e",
        task_id="C1_task_082_P0",
    )

    assert result.validity != "INVALID_INFRA"
    assert result.provider_usage["provider_request_count"] == 2
    assert result.provider_usage["logical_provider_call_count"] == 2
    assert result.runtime_tool_schema
    assert result.to_dict()["runtime_tool_schema_hash"] == tool_schema_hash(result.runtime_tool_schema)
    assert len(fake.chat.completions.requests) == 2
    assert result.firewall_report["firewall"]["violation_count"] == "0"
    assert result.firewall_report["firewall"]["runtime_terminated"] == "True"


def test_exception_artifact_diagnostics_redact_api_key(monkeypatch):
    secret = "super-secret-key-for-adversarial-test"
    monkeypatch.setenv("ODYS_AGENT_API_KEY", secret)
    diagnostics = _classify_exception(RuntimeError(f"provider leaked {secret}"))
    assert secret not in json.dumps(diagnostics)
    assert "[REDACTED]" in diagnostics["exception_message"]


def test_tool_schema_hash_is_order_invariant_and_parameters_are_nonempty():
    _, tools = _engine_tools()
    canonical = canonical_tool_schemas(tools)
    reversed_raw = list(reversed(tools))
    reversed_canonical = canonical_tool_schemas(reversed_raw)
    assert canonical
    assert tool_schema_hash(canonical) == tool_schema_hash(reversed_canonical)
    assert all(item["function"]["parameters"].get("type") == "object" for item in canonical)
