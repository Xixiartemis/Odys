"""Provider-free adversarial coverage for MiMo multi-tool batches."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lhas.phase5.agent_core import Phase5AgentCore
from lhas.phase5.control_arms import RecoveryActionKind, RecoveryDecision
from lhas.phase5.substrate.evidence import EvidenceLedger
from lhas.phase5.live_driver import LiveModelDriver, ProviderExecutionError
from lhas.phase5.model_driver import ModelAction, ModelToolCall
from lhas.phase5.provider_wire import serialize_messages
from lhas.phase5.trial_executor import execute_trial
from lhas.phase5.control_arms import ControlArm
from lhas.phase5.types import BudgetConfig


ROOT = Path(__file__).resolve().parents[1]
TOOLMAZE_ROOT = ROOT / "experiments" / "phase5" / "benchmarks" / "toolmaze"
TASK_PATH = TOOLMAZE_ROOT / "data" / "perturbed_tasks" / "c1" / "C1_task_082_P0.json"


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


def _response(tool_calls=None, *, reasoning="batch reasoning", content="done"):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["reasoning_content"] = reasoning
        message["tool_calls"] = tool_calls
    return {
        "model": "mimo-v2.5",
        "choices": [{"message": message}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
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
        request_parallel_tool_calls_hint=False,
        provider_may_return_multiple_tool_calls=True,
        client_factory=lambda **_: fake_client,
    )


def test_driver_accepts_two_and_three_calls_in_provider_order():
    calls = [
        _call("call-A", "first_tool", {"n": 1}),
        _call("call-B", "second_tool", {"n": 2}),
        _call("call-C", "third_tool", {"n": 3}),
    ]
    action = _driver(_FakeClient([_response(calls)])).next_action([], [])
    assert action.type == "tool_call"
    assert [call.tool_name for call in action.tool_calls] == [
        "first_tool", "second_tool", "third_tool"
    ]
    assert [call.provider_tool_call_id for call in action.tool_calls] == [
        "call-A", "call-B", "call-C"
    ]


@pytest.mark.parametrize(
    "calls,match",
    [
        ([_call("same", "a", {}), _call("same", "b", {})], "duplicate"),
        ([{"type": "function", "function": {"name": "a", "arguments": "{}"}}], "missing"),
        ([{"id": "a", "function": {"name": "a", "arguments": "not-json"}}], "valid JSON"),
        ([_call("a", "a", {}), {"id": "b", "function": {"name": "b", "arguments": "[]"}}], "JSON object"),
    ],
)
def test_invalid_call_in_batch_fails_before_any_action_is_returned(calls, match):
    response = _response(calls)
    with pytest.raises(ProviderExecutionError, match=match):
        _driver(_FakeClient([response])).next_action([], [])


def test_multi_tool_wire_history_preserves_reasoning_order_and_ids():
    wire = serialize_messages(
        [
            {"role": "user", "content": "task"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"name": "first_tool", "arguments": {"n": 1}, "provider_tool_call_id": "call-A"},
                    {"name": "second_tool", "arguments": {"n": 2}, "provider_tool_call_id": "call-B"},
                ],
                "_provider_transport": {"reasoning_content": "exact\nreasoning"},
            },
            {"role": "tool", "provider_tool_call_id": "call-A", "content": {"ok": "A"}},
            {"role": "tool", "provider_tool_call_id": "call-B", "content": {"ok": "B"}},
        ]
    )
    assert wire[1]["reasoning_content"] == "exact\nreasoning"
    assert [call["id"] for call in wire[1]["tool_calls"]] == ["call-A", "call-B"]
    assert [message["tool_call_id"] for message in wire[2:]] == ["call-A", "call-B"]


class _SequenceDriver:
    def __init__(self, actions):
        self.actions = list(actions)
        self.calls = 0

    def next_action(self, messages, tool_definitions, generation_config=None):
        self.calls += 1
        return self.actions.pop(0)

    def record_tool_result(self, tool_name, result):
        pass

    def get_token_usage(self):
        return type("Usage", (), {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})()

    def get_total_tokens(self):
        return 0

    def reset(self):
        pass


def test_core_maps_batch_indices_and_evidence_without_last_call_aliasing():
    driver = _SequenceDriver(
        [
            ModelAction(
                type="tool_call",
                tool_calls=[
                    ModelToolCall("first_tool", {"n": 1}, provider_tool_call_id="call-A"),
                    ModelToolCall("second_tool", {"n": 2}, provider_tool_call_id="call-B"),
                ],
                provider_reasoning_content="exact batch reasoning",
            ),
            ModelAction(type="final_answer", content="done"),
        ]
    )
    core = Phase5AgentCore(driver)
    ledger = EvidenceLedger(run_id="multi-tool")
    core.set_evidence_ledger(ledger)
    core.initialize("task", [])

    action = core.next_model_action()
    assert len(action.tool_calls) == 2
    assert action.tool_calls[0].tool_call_id != action.tool_calls[1].tool_call_id

    with pytest.raises(RuntimeError, match="all tool results"):
        core.next_model_action()

    core.receive_tool_result("second_tool", {"status": "B"}, tool_call_index=1)
    core.receive_tool_result("first_tool", {"status": "A"}, tool_call_index=0)
    final = core.next_model_action()

    assert final.type == "final_answer"
    observations = core.get_public_tool_observations()
    assert len(observations) == 2
    assert {obs.provider_tool_call_id for obs in observations} == {"call-A", "call-B"}
    assert {obs.tool_call_index for obs in observations} == {0, 1}
    assert {event.payload["provider_tool_call_id"] for event in ledger.all_events()} == {"call-A", "call-B"}
    assistant = core.get_conversation_history()[1]
    assert len(assistant["tool_calls"]) == 2
    assert assistant["_provider_transport"]["reasoning_content"] == "exact batch reasoning"
    assert driver.calls == 2


class _FirstDecisionWinsStrategy:
    arm = ControlArm.A3_ODYS_FULL

    def configure(self, **kwargs):
        return {}

    def create_observer(self):
        return None

    def should_validate(self):
        return False

    async def on_step_result(self, *, step, result, observer):
        return RecoveryDecision(
            action=RecoveryActionKind.RETRY if result.get("which") == "A" else RecoveryActionKind.ESCALATE,
            reason=result.get("which", "unknown"),
        )


def test_first_non_none_recovery_decision_wins_within_batch():
    driver = _SequenceDriver([
        ModelAction(
            type="tool_call",
            tool_calls=[
                ModelToolCall("first_tool", {}, provider_tool_call_id="call-A"),
                ModelToolCall("second_tool", {}, provider_tool_call_id="call-B"),
            ],
        ),
        ModelAction(type="final_answer", content="done"),
    ])
    core = Phase5AgentCore(driver, strategy=_FirstDecisionWinsStrategy())
    core.initialize("task", [])
    core.next_model_action()
    core.receive_tool_result("first_tool", {"which": "A"}, tool_call_index=0)
    core.receive_tool_result("second_tool", {"which": "B"}, tool_call_index=1)
    assert core._pending_recovery.action is RecoveryActionKind.RETRY
    assert any(item.get("suppressed") for item in core.get_recovery_decisions())


def test_fake_mimo_multi_tool_real_toolmaze_e2e_is_provider_free():
    task = json.loads(TASK_PATH.read_text(encoding="utf-8"))
    first_calls = [
        _call("provider-A", "get_stock_level", {"product_id": "p_apple_airpods"}),
        _call("provider-B", "stringify_data", {"data": {"product_id": "p_apple_airpods"}}),
    ]
    fake = _FakeClient([_response(first_calls), _response(None, content="done")])
    driver = _driver(fake)
    result = execute_trial(
        arm=ControlArm.A0_BARE,
        task_json=task,
        model_driver=driver,
        budget=BudgetConfig(max_turns=3, max_model_calls=3, token_budget=1000),
        experiment_id="phase5-fake-mimo-multi-tool",
        task_id="C1_task_082_P0",
    )
    second_request = fake.chat.completions.requests[1]["messages"]
    assert driver.logical_provider_call_count == 2
    assert len(first_calls) == 2
    assert len(result.public_tool_observations) == 2
    assert len(result.evidence_events) == 2
    assert second_request[1]["tool_calls"] == [
        {"id": "provider-A", "type": "function", "function": {"name": "get_stock_level", "arguments": '{"product_id":"p_apple_airpods"}'}},
        {"id": "provider-B", "type": "function", "function": {"name": "stringify_data", "arguments": '{"data":{"product_id":"p_apple_airpods"}}'}},
    ]
    assert second_request[1]["reasoning_content"] == "batch reasoning"
    assert [message["tool_call_id"] for message in second_request[2:4]] == ["provider-A", "provider-B"]
    assert result.firewall_report["firewall"]["violation_count"] == "0"
    assert result.provider_usage["provider_request_count"] == 2
