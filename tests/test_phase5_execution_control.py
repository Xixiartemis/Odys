"""Deterministic tests for the Phase5 execution-control loop (P0/P1/P2).

Covers:
- booking_ready=false → confirm tool executions == 0
- satisfied preconditions → normal tool sequences continue
- UNKNOWN / FAILURE → dependent side-effect operations blocked
- not-ready state persists across model turns
- no-progress retries (same args / same result / no state change) rejected
  or escalated; NOT_RETRYABLE failures reject the first identical repeat
- single tool call, normal multi-tool sequences and budget logic unchanged
- original tool-call ids pair real and synthetic responses correctly
- blocked calls only enter control events, never real execution records
- ShadowProgressObserver stays non-interfering
- A0 / A3 share the base execution safety gate without mixing recovery

No live model calls anywhere.  Fake engines reproduce the frozen
ExecutionEngine loop contract (one ``receive_tool_result`` per executed
call) without executing real tools, except the single canary e2e test
which runs the real frozen engine with a scripted provider client.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lhas.phase5.agent_adapter import create_toolmaze_agent_adapter
from lhas.phase5.agent_core import Phase5AgentCore
from lhas.phase5.control_arms import (
    BareStrategy,
    ControlArm,
    RecoveryActionKind,
    RecoveryDecision,
)
from lhas.phase5.execution_control import (
    ControlEventKind,
    ExecutionAuthorizationGate,
    Retryability,
    ToolExecStatus,
    ToolReadyStatus,
    ToolResultNormalizer,
    ToolSemanticsRegistry,
)
from lhas.phase5.model_driver import ModelAction, ModelToolCall
from lhas.phase5.provider_wire import serialize_messages
from lhas.phase5.recovery_budget import RecoveryBudgetGate
from lhas.phase5.shadow_observer import ShadowProgressObserver
from lhas.phase5.substrate.evidence import EvidenceLedger


ROOT = Path(__file__).resolve().parents[1]
TOOLMAZE_ROOT = ROOT / "experiments" / "phase5" / "benchmarks" / "toolmaze"
TASK_PATH = TOOLMAZE_ROOT / "data" / "perturbed_tasks" / "c1" / "C1_task_036_P4.json"


# ══════════════════════════════════════════════════════════════════════
# Harness
# ══════════════════════════════════════════════════════════════════════

class SequenceDriver:
    """Deterministic driver replaying scripted ModelAction entries."""

    def __init__(self, actions):
        self.actions = list(actions)
        self.calls = 0

    def next_action(self, messages, tool_definitions, generation_config=None):
        self.calls += 1
        if not self.actions:
            return ModelAction(type="final_answer", content="script exhausted")
        return self.actions.pop(0)

    def record_tool_result(self, tool_name, result):
        pass

    def get_token_usage(self):
        return type("Usage", (), {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})()

    def get_total_tokens(self):
        return 0

    def reset(self):
        self.actions = []
        self.calls = 0


def batch_action(calls):
    """ModelAction carrying one provider batch of tool calls.

    ``calls``: list of (tool_name, arguments, provider_tool_call_id).
    """
    return ModelAction(
        type="tool_call",
        tool_calls=[
            ModelToolCall(
                tool_name=name,
                arguments=dict(args),
                tool_call_id=None,
                provider_tool_call_id=provider_id,
            )
            for name, args, provider_id in calls
        ],
    )


def run_engine_loop(adapter, results, *, task="task", tools=None, max_rounds=30):
    """Reproduce the frozen ExecutionEngine.run() loop contract.

    ``results``: dict tool_name -> result dict, or callable(name, args, n).
    Returns (executions, final_action) where executions records every call
    that actually reached the execution engine.
    """
    executions = []
    adapter.initialize(task, list(tools or []))
    message = task
    for round_num in range(max_rounds):
        action = adapter.step(user_message=message if round_num == 0 else None)
        message = None
        if action.type == "final_answer":
            return executions, action
        # Per-call delivery: the engine executes exactly one call.
        assert action.tool_calls is None or len(action.tool_calls) <= 1, (
            "adapter must deliver at most one tool call per step"
        )
        name = action.tool_name
        args = action.arguments or {}
        if callable(results):
            result = results(name, args, len(executions))
        else:
            result = results[name]
        executions.append((name, dict(args)))
        adapter.receive_tool_result(name, result)
    raise AssertionError("engine loop did not terminate")


def make_core_adapter(actions, *, strategy=None, gate=None, observer=None, ledger=None):
    core = Phase5AgentCore(SequenceDriver(actions), strategy=strategy)
    if observer is not None:
        core.set_shadow_observer(observer)
    if ledger is not None:
        core.set_evidence_ledger(ledger)
    adapter = create_toolmaze_agent_adapter(core, execution_gate=gate)
    return core, adapter, adapter._gate


def events_of_kind(gate, kind):
    return [e for e in gate.control_events if e["kind"] == kind.value]


# ══════════════════════════════════════════════════════════════════════
# Normalized result semantics
# ══════════════════════════════════════════════════════════════════════

class TestNormalizedResultSemantics:
    def test_booking_ready_false_is_not_ready(self):
        normalized = ToolResultNormalizer().normalize(
            "build_flight_booking_request",
            {"flight_number": "UNKNOWN", "booking_ready": False},
        )
        assert normalized.ready_status is ToolReadyStatus.NOT_READY
        assert normalized.affects_followups

    def test_booking_ready_true_is_ready(self):
        normalized = ToolResultNormalizer().normalize(
            "build_flight_booking_request",
            {"flight_number": "CA1234", "booking_ready": True},
        )
        assert normalized.ready_status is ToolReadyStatus.READY
        assert not normalized.affects_followups

    def test_confirmed_status_is_success_but_not_readiness_evidence(self):
        normalized = ToolResultNormalizer().normalize(
            "confirm_held_recommendation_order",
            {"order_id": "ORD-1", "status": "confirmed"},
        )
        assert normalized.exec_status is ToolExecStatus.SUCCEEDED
        assert normalized.confirmation_receipt
        assert normalized.ready_status is ToolReadyStatus.NO_EVIDENCE

    def test_missing_evidence_is_never_default_success(self):
        normalized = ToolResultNormalizer().normalize("tool_a", {})
        assert normalized.exec_status is ToolExecStatus.UNKNOWN
        assert normalized.ready_status is ToolReadyStatus.NO_EVIDENCE
        assert normalized.affects_followups

    def test_explicit_retryable_markers(self):
        normalizer = ToolResultNormalizer()
        assert normalizer.normalize("t", {"status": "error", "retryable": True}).retryable is Retryability.RETRYABLE
        assert normalizer.normalize("t", {"status": "error", "retryable": False}).retryable is Retryability.NOT_RETRYABLE
        assert normalizer.normalize("t", {"status": "timeout"}).retryable is Retryability.RETRYABLE
        assert normalizer.normalize("t", {"status": "error"}).retryable is Retryability.UNKNOWN

    def test_original_result_preserved_verbatim(self):
        raw = {"booking_ready": False, "note": "keep me"}
        normalized = ToolResultNormalizer().normalize("build_x", raw)
        assert normalized.raw == raw


# ══════════════════════════════════════════════════════════════════════
# P0-1: per-call authorization
# ══════════════════════════════════════════════════════════════════════

class TestPerCallAuthorization:
    def test_booking_ready_false_blocks_confirm_execution(self):
        """booking_ready=false 后，确认工具执行次数为 0。"""
        core, adapter, gate = make_core_adapter([
            batch_action([
                ("build_flight_booking_request", {"flight": "CA1234"}, "call-1"),
                ("confirm_held_recommendation_order", {"order": "ORD-1"}, "call-2"),
            ]),
            ModelAction(type="final_answer", content="done"),
        ])
        executions, final = run_engine_loop(adapter, {
            "build_flight_booking_request": {"flight_number": "UNKNOWN", "booking_ready": False},
            "confirm_held_recommendation_order": {"status": "confirmed"},
        })
        confirm_executions = [e for e in executions if e[0] == "confirm_held_recommendation_order"]
        assert confirm_executions == []
        assert [e[0] for e in executions] == ["build_flight_booking_request"]
        blocked = events_of_kind(gate, ControlEventKind.BLOCKED)
        assert [e["tool_name"] for e in blocked] == ["confirm_held_recommendation_order"]
        assert blocked[0]["rule"] == "DEPENDENCY_PRECONDITION_UNRESOLVED"
        # blocked call never becomes a real observation
        assert [o.tool_name for o in core.get_public_tool_observations()] == [
            "build_flight_booking_request"
        ]

    def test_precondition_satisfied_normal_sequence_continues(self):
        """前置条件确实满足时，正常工具序列可以继续。"""
        core, adapter, gate = make_core_adapter([
            batch_action([
                ("build_flight_booking_request", {"flight": "CA1234"}, "call-1"),
                ("confirm_held_recommendation_order", {"order": "ORD-1"}, "call-2"),
            ]),
            ModelAction(type="final_answer", content="done"),
        ])
        executions, _ = run_engine_loop(adapter, {
            "build_flight_booking_request": {"flight_number": "CA1234", "booking_ready": True},
            "confirm_held_recommendation_order": {"status": "confirmed"},
        })
        assert [e[0] for e in executions] == [
            "build_flight_booking_request",
            "confirm_held_recommendation_order",
        ]
        assert events_of_kind(gate, ControlEventKind.BLOCKED) == []

    @pytest.mark.parametrize(
        "first_result",
        [
            {"status": "error"},          # FAILURE
            {},                            # UNKNOWN (no reliable evidence)
            {"status": "timeout"},         # UNKNOWN-ish transient failure
        ],
    )
    def test_unknown_or_failure_blocks_dependent_side_effect(self, first_result):
        """UNKNOWN / FAILURE 时，依赖该结果的副作用操作被拦截。"""
        core, adapter, gate = make_core_adapter([
            batch_action([
                ("lookup_booking", {"q": "x"}, "call-1"),
                ("confirm_held_recommendation_order", {"order": "ORD-1"}, "call-2"),
            ]),
            ModelAction(type="final_answer", content="done"),
        ])
        executions, _ = run_engine_loop(adapter, {
            "lookup_booking": first_result,
            "confirm_held_recommendation_order": {"status": "confirmed"},
        })
        assert [e[0] for e in executions] == ["lookup_booking"]
        blocked = events_of_kind(gate, ControlEventKind.BLOCKED)
        assert [e["tool_name"] for e in blocked] == ["confirm_held_recommendation_order"]

    def test_not_ready_state_persists_across_model_turns(self):
        """未就绪状态在新的模型轮次仍然有效。"""
        core, adapter, gate = make_core_adapter([
            batch_action([("build_flight_booking_request", {"flight": "A"}, "call-1")]),
            batch_action([("confirm_held_recommendation_order", {"order": "O"}, "call-2")]),
            ModelAction(type="final_answer", content="done"),
        ])
        executions, _ = run_engine_loop(adapter, {
            "build_flight_booking_request": {"booking_ready": False},
            "confirm_held_recommendation_order": {"status": "confirmed"},
        })
        # turn 1 executed the build; turn 2's confirm is still blocked
        assert [e[0] for e in executions] == ["build_flight_booking_request"]
        blocked = events_of_kind(gate, ControlEventKind.BLOCKED)
        assert len(blocked) == 1
        assert blocked[0]["tool_name"] == "confirm_held_recommendation_order"

    def test_read_only_tools_still_available_while_not_ready(self):
        """未就绪状态下只读工具不受阻（模型可以继续取证）。"""
        core, adapter, gate = make_core_adapter([
            batch_action([("build_flight_booking_request", {"flight": "A"}, "call-1")]),
            batch_action([("get_flight_info", {"flight": "A"}, "call-2")]),
            ModelAction(type="final_answer", content="done"),
        ])
        executions, _ = run_engine_loop(adapter, {
            "build_flight_booking_request": {"booking_ready": False},
            "get_flight_info": {"status": "success"},
        })
        assert [e[0] for e in executions] == [
            "build_flight_booking_request",
            "get_flight_info",
        ]


# ══════════════════════════════════════════════════════════════════════
# P1: no-progress recovery
# ══════════════════════════════════════════════════════════════════════

class AlwaysRetryStrategy:
    """A1-style strategy: retry every failed call."""

    arm = ControlArm.A1_RETRY_ONLY

    def __init__(self):
        self.recoveries = 0

    def configure(self, **kwargs):
        return {}

    def create_observer(self):
        return None

    def should_validate(self):
        return False

    def recovery_budget_enabled(self):
        return False

    async def on_step_result(self, *, step, result, observer):
        if result.get("status") in {"error", "failure"} or result.get("retryable") is False:
            self.recoveries += 1
            return RecoveryDecision(
                action=RecoveryActionKind.RETRY,
                reason=f"retry after failed result #{self.recoveries}",
                signal="TOOL_ERROR",
            )
        return RecoveryDecision()


class TestNoProgressRecovery:
    def test_identical_repeated_failures_terminate_as_no_progress(self):
        """相同参数、相同失败结果、没有状态变化时，无进展重试被拒绝并停止。"""
        core, adapter, gate = make_core_adapter([
            batch_action([("tool_a", {"n": 1}, "call-1")]),
            batch_action([("tool_a", {"n": 1}, "call-2")]),
            batch_action([("tool_a", {"n": 1}, "call-3")]),
            ModelAction(type="final_answer", content="never reached"),
        ])
        executions, final = run_engine_loop(adapter, {
            "tool_a": {"status": "error", "detail": "boom"},
        })
        # one bounded verification retry executed, the third identical
        # proposal is rejected and the run stops explicitly
        assert [e[0] for e in executions] == ["tool_a", "tool_a"]
        assert final.type == "final_answer"
        assert final.content.startswith("TERMINATED")
        assert gate.is_terminal
        blocked = events_of_kind(gate, ControlEventKind.BLOCKED)
        assert blocked and blocked[0]["rule"] == "NO_PROGRESS_RETRY"

    def test_not_retryable_failure_rejects_first_identical_repeat(self):
        core, adapter, gate = make_core_adapter([
            batch_action([("tool_a", {"n": 1}, "call-1")]),
            batch_action([("tool_a", {"n": 1}, "call-2")]),
            ModelAction(type="final_answer", content="never reached"),
        ])
        executions, final = run_engine_loop(adapter, {
            "tool_a": {"status": "error", "retryable": False},
        })
        assert len(executions) == 1
        assert final.content.startswith("TERMINATED")
        blocked = events_of_kind(gate, ControlEventKind.BLOCKED)
        assert blocked[0]["rule"] == "NO_PROGRESS_RETRY"

    def test_changed_arguments_are_progress_and_allowed(self):
        core, adapter, gate = make_core_adapter([
            batch_action([("tool_a", {"n": 1}, "call-1")]),
            batch_action([("tool_a", {"n": 2}, "call-2")]),
            batch_action([("tool_a", {"n": 3}, "call-3")]),
            ModelAction(type="final_answer", content="done"),
        ])
        executions, final = run_engine_loop(adapter, {
            "tool_a": {"status": "error"},
        })
        assert len(executions) == 3
        assert final.content == "done"

    def test_recovery_retry_no_progress_converted_to_explicit_stop(self):
        """恢复重试与预算是两个独立判断；无进展重试转为明确停止。"""
        strategy = AlwaysRetryStrategy()
        budget_gate = RecoveryBudgetGate(max_recovery_attempts=3)
        core = Phase5AgentCore(
            SequenceDriver([
                batch_action([("tool_a", {"n": 1}, "call-1")]),
                ModelAction(type="final_answer", content="never reached"),
            ]),
            strategy=strategy,
        )
        core.set_recovery_budget_gate(budget_gate)
        adapter = create_toolmaze_agent_adapter(core)
        gate = adapter._gate

        executions, final = run_engine_loop(adapter, {"tool_a": {"status": "error"}})

        # call-1 fails → RETRY #1 justified (verification) → replay executes
        # → identical failure → RETRY #2 unjustified → converted to STOP
        assert len(executions) == 2
        assert final.content.startswith("TERMINATED")
        assert strategy.recoveries == 2

        recovery_events = events_of_kind(gate, ControlEventKind.RECOVERY)
        assert recovery_events[0]["retry_justified"] is True
        assert recovery_events[0]["budget_decision"] == "ALLOW"
        rejected = [e for e in recovery_events if e.get("retry_justified") is False]
        assert rejected, "expected a rejected no-progress retry event"
        assert rejected[0]["action"] == "stop"
        assert rejected[0]["budget_decision"] == "NOT_CONSUMED"

        # budget ledger shows only the single justified intervention
        assert len(budget_gate.ledger) == 1

    def test_transient_basis_keeps_retry_justified(self):
        """明确的瞬态重试依据不被无进展规则误杀。"""
        normalizer_gate = ExecutionAuthorizationGate()
        normalizer_gate.observe(
            step=1, tool_name="tool_a", arguments={"n": 1},
            result={"status": "timeout"},
        )
        decision = normalizer_gate.is_retry_justified(
            step=2, tool_name="tool_a", arguments={"n": 1}
        )
        assert decision.authorized
        assert decision.rule == "RETRY_JUSTIFIED"


# ══════════════════════════════════════════════════════════════════════
# Regression guards: single call / normal sequences / budget
# ══════════════════════════════════════════════════════════════════════

class TestNoRegression:
    def test_single_tool_call_executes_normally(self):
        core, adapter, gate = make_core_adapter([
            batch_action([("get_flight_info", {"flight": "CA1234"}, "call-1")]),
            ModelAction(type="final_answer", content="done"),
        ])
        executions, final = run_engine_loop(adapter, {
            "get_flight_info": {"status": "success"},
        })
        assert len(executions) == 1
        assert final.content == "done"
        assert events_of_kind(gate, ControlEventKind.BLOCKED) == []

    def test_normal_multi_tool_sequence_in_proposed_order(self):
        core, adapter, gate = make_core_adapter([
            batch_action([
                ("get_flight_info", {"flight": "CA1234"}, "call-1"),
                ("hold_inventory", {"item": "p1"}, "call-2"),
                ("create_document", {"title": "note"}, "call-3"),
            ]),
            ModelAction(type="final_answer", content="done"),
        ])
        executions, _ = run_engine_loop(adapter, {
            "get_flight_info": {"status": "success"},
            "hold_inventory": {"status": "success", "reservation_id": "R1"},
            "create_document": {"status": "success", "note_id": "N1"},
        })
        assert [e[0] for e in executions] == [
            "get_flight_info", "hold_inventory", "create_document",
        ]

    def test_recovery_budget_gate_still_enforced(self):
        """现有预算逻辑不发生回归：预算耗尽仍然升级。"""
        from lhas.phase5.recovery_budget import BudgetDecision

        budget_gate = RecoveryBudgetGate(max_recovery_attempts=1)
        decisions = [
            budget_gate.authorize(step=1, candidate_action="retry"),
            budget_gate.authorize(step=2, candidate_action="retry"),
        ]
        assert decisions == [BudgetDecision.ALLOW, BudgetDecision.ESCALATE]
        assert len(budget_gate.ledger) == 2

    def test_side_effect_success_repeat_requires_idempotency(self):
        """副作用工具默认不安全重复执行（幂等性需明确证据）。"""
        core, adapter, gate = make_core_adapter([
            batch_action([("hold_inventory", {"item": "p1"}, "call-1")]),
            batch_action([("hold_inventory", {"item": "p1"}, "call-2")]),
            ModelAction(type="final_answer", content="done"),
        ])
        executions, _ = run_engine_loop(adapter, {
            "hold_inventory": {"status": "success", "reservation_id": "R1"},
        })
        assert len(executions) == 1
        blocked = events_of_kind(gate, ControlEventKind.BLOCKED)
        assert blocked[0]["rule"] == "SIDE_EFFECT_REPEAT_NOT_IDEMPOTENT"


# ══════════════════════════════════════════════════════════════════════
# Audit boundaries: ids, synthetic responses, execution records
# ══════════════════════════════════════════════════════════════════════

class TestAuditBoundaries:
    def test_original_ids_pair_real_and_synthetic_responses(self):
        """原始工具调用 ID 与真实/合成响应能够正确对应。"""
        core, adapter, gate = make_core_adapter([
            batch_action([
                ("build_flight_booking_request", {"flight": "A"}, "call-A"),
                ("confirm_held_recommendation_order", {"order": "O"}, "call-B"),
            ]),
            ModelAction(type="final_answer", content="done"),
        ])
        run_engine_loop(adapter, {
            "build_flight_booking_request": {"booking_ready": False},
            "confirm_held_recommendation_order": {"status": "confirmed"},
        })

        history = core.get_conversation_history()
        assistant = next(m for m in history if m.get("role") == "assistant" and m.get("tool_calls"))
        assert [c["provider_tool_call_id"] for c in assistant["tool_calls"]] == ["call-A", "call-B"]

        tool_msgs = [m for m in history if m.get("role") == "tool"]
        assert [m.get("provider_tool_call_id") for m in tool_msgs] == ["call-A", "call-B"]
        real, synthetic = tool_msgs
        assert not real.get("_synthetic_response")
        assert synthetic.get("_synthetic_response") is True
        assert synthetic["synthetic_rule"] == "DEPENDENCY_PRECONDITION_UNRESOLVED"
        assert synthetic["content"].startswith("[ODYS-CONTROL][SYNTHETIC RESPONSE")

        # wire pairing is preserved for both ids
        wire = serialize_messages(history)
        wire_tool = [m for m in wire if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in wire_tool] == ["call-A", "call-B"]

    def test_blocked_calls_only_in_control_events_not_execution_records(self):
        """被拦截的调用只进入控制事件，不进入真实执行记录。"""
        ledger = EvidenceLedger(run_id="audit")
        core, adapter, gate = make_core_adapter([
            batch_action([
                ("build_flight_booking_request", {"flight": "A"}, "call-A"),
                ("confirm_held_recommendation_order", {"order": "O"}, "call-B"),
            ]),
            ModelAction(type="final_answer", content="done"),
        ], ledger=ledger)
        executions, _ = run_engine_loop(adapter, {
            "build_flight_booking_request": {"booking_ready": False},
            "confirm_held_recommendation_order": {"status": "confirmed"},
        })

        # real execution records: executed calls only
        assert [e[0] for e in executions] == ["build_flight_booking_request"]
        assert [o.tool_name for o in core.get_public_tool_observations()] == [
            "build_flight_booking_request"
        ]
        ledger_tools = [
            e.payload.get("tool_name") for e in ledger.all_events()
        ]
        assert "confirm_held_recommendation_order" not in ledger_tools

        # control events carry the blocked call
        blocked = events_of_kind(gate, ControlEventKind.BLOCKED)
        assert [e["tool_name"] for e in blocked] == ["confirm_held_recommendation_order"]
        assert blocked[0]["tool_call_id"]  # original invocation id preserved

    def test_control_events_cover_full_lifecycle(self):
        core, adapter, gate = make_core_adapter([
            batch_action([
                ("build_flight_booking_request", {"flight": "A"}, "call-A"),
                ("confirm_held_recommendation_order", {"order": "O"}, "call-B"),
            ]),
            ModelAction(type="final_answer", content="done"),
        ])
        run_engine_loop(adapter, {
            "build_flight_booking_request": {"booking_ready": False},
            "confirm_held_recommendation_order": {"status": "confirmed"},
        })
        kinds = {e["kind"] for e in gate.control_events}
        assert {"PROPOSED", "AUTHORIZED", "EXECUTED", "OBSERVED", "BLOCKED"} <= kinds

    def test_lineage_uses_real_identifiers(self):
        """P2: lineage 使用真实任务标识，不是任务描述前缀。"""
        ledger = EvidenceLedger(run_id="lineage")
        core = Phase5AgentCore(SequenceDriver([
            batch_action([("tool_a", {"n": 1}, "call-1")]),
            ModelAction(type="final_answer", content="done"),
        ]))
        core.set_evidence_ledger(ledger)
        core.set_execution_lineage(task_id="C1_task_036_P4", trial_id="trial-001")
        core.initialize("a very long task description that must not be used as an identifier", [])
        core.next_model_action()
        core.receive_tool_result("tool_a", {"status": "success"})
        events = ledger.all_events()
        assert events
        assert all(e.task_id == "C1_task_036_P4" for e in events)
        assert all(e.payload.get("trial_id") == "trial-001" for e in events)


# ══════════════════════════════════════════════════════════════════════
# Observer non-interference + arm consistency
# ══════════════════════════════════════════════════════════════════════

class TestObserverAndArms:
    def test_shadow_observer_is_non_interfering(self):
        """Shadow Observer 仍然是非干预式的。"""
        scenario = [
            batch_action([
                ("build_flight_booking_request", {"flight": "A"}, "call-1"),
                ("confirm_held_recommendation_order", {"order": "O"}, "call-2"),
            ]),
            ModelAction(type="final_answer", content="done"),
        ]
        results = {
            "build_flight_booking_request": {"booking_ready": False},
            "confirm_held_recommendation_order": {"status": "confirmed"},
        }
        core_plain, adapter_plain, gate_plain = make_core_adapter(list(scenario))
        exec_plain, _ = run_engine_loop(adapter_plain, results)

        observer = ShadowProgressObserver()
        core_obs, adapter_obs, gate_obs = make_core_adapter(list(scenario), observer=observer)
        exec_obs, _ = run_engine_loop(adapter_obs, results)

        # identical decisions with and without the observer
        assert exec_plain == exec_obs
        blocked_plain = [e["tool_name"] for e in events_of_kind(gate_plain, ControlEventKind.BLOCKED)]
        blocked_obs = [e["tool_name"] for e in events_of_kind(gate_obs, ControlEventKind.BLOCKED)]
        assert blocked_plain == blocked_obs == ["confirm_held_recommendation_order"]

        # the observer only observed the executed call
        records = observer.get_records()
        assert len(records) == 1
        assert "build_flight_booking_request" in records[0].observable_features["action_identity"]

    def test_a0_and_a3_share_base_gate_without_mixing_recovery(self):
        """A0 / A3 的基础执行安全门一致，实验性恢复能力不被意外混合。"""
        scenario = [
            batch_action([
                ("build_flight_booking_request", {"flight": "A"}, "call-1"),
                ("confirm_held_recommendation_order", {"order": "O"}, "call-2"),
            ]),
            ModelAction(type="final_answer", content="done"),
        ]
        results = {
            "build_flight_booking_request": {"booking_ready": False},
            "confirm_held_recommendation_order": {"status": "confirmed"},
        }

        core_a0, adapter_a0, gate_a0 = make_core_adapter(
            list(scenario), strategy=BareStrategy()
        )
        exec_a0, _ = run_engine_loop(adapter_a0, results)

        class _A3Like(AlwaysRetryStrategy):
            arm = ControlArm.A3_ODYS_FULL

        core_a3, adapter_a3, gate_a3 = make_core_adapter(
            list(scenario), strategy=_A3Like()
        )
        exec_a3, _ = run_engine_loop(adapter_a3, results)

        # base safety gate identical across arms
        assert type(gate_a0) is type(gate_a3) is ExecutionAuthorizationGate
        assert exec_a0 == exec_a3 == [("build_flight_booking_request", {"flight": "A"})]
        blocked_a0 = [(e["tool_name"], e["rule"]) for e in events_of_kind(gate_a0, ControlEventKind.BLOCKED)]
        blocked_a3 = [(e["tool_name"], e["rule"]) for e in events_of_kind(gate_a3, ControlEventKind.BLOCKED)]
        assert blocked_a0 == blocked_a3

        # experimental recovery is NOT mixed: A0 has none, A3 consults its policy
        assert events_of_kind(gate_a0, ControlEventKind.RECOVERY) == []
        assert core_a3.get_recovery_decisions() == [] or events_of_kind(gate_a3, ControlEventKind.RECOVERY)


# ══════════════════════════════════════════════════════════════════════
# Canary e2e: the C1_task_036_P4 defect through the real frozen engine
# ══════════════════════════════════════════════════════════════════════

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


def _response(tool_calls=None, *, content="done"):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["reasoning_content"] = "batch reasoning"
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


class TestCanary036E2E:
    def test_canary_036_p4_confirm_never_executes(self):
        """修复后的 C1_task_036_P4：booking_ready=false 后确认工具执行 0 次。"""
        pytest.importorskip("evaluation.core.sandbox")
        if not TASK_PATH.exists():
            pytest.skip("frozen ToolMaze task data not available")

        from lhas.phase5.live_driver import LiveModelDriver
        from lhas.phase5.trial_executor import execute_trial
        from lhas.phase5.types import BudgetConfig

        task = json.loads(TASK_PATH.read_text(encoding="utf-8"))
        fake = _FakeClient([
            _response([
                _call("provider-1", "build_flight_booking_request", {"flight_number": "CA1234"}),
                _call("provider-2", "confirm_held_recommendation_order", {"reservation_id": "RESV-1"}),
            ]),
            _response(None, content="done"),
        ])
        driver = LiveModelDriver(
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
            client_factory=lambda **_: fake,
        )
        result = execute_trial(
            arm=ControlArm.A0_BARE,
            task_json=task,
            model_driver=driver,
            budget=BudgetConfig(max_turns=5, max_model_calls=5, token_budget=5000),
            experiment_id="phase5-exec-control-canary-036",
            task_id="C1_task_036_P4",
        )

        # the official execution trace contains executed calls only
        executed_tools = [
            m.get("tool_call", {}).get("name")
            for m in result.official_trace.get("messages", [])
            if m.get("type") == "tool_call"
        ]
        assert "confirm_held_recommendation_order" not in executed_tools
        assert executed_tools.count("build_flight_booking_request") == 1

        # real observations carry only the executed call
        assert [o["tool_name"] for o in result.public_tool_observations] == [
            "build_flight_booking_request"
        ]

        # the blocked call is a control event, not an execution record
        blocked = [e for e in result.control_events if e.get("kind") == "BLOCKED"]
        assert [e["tool_name"] for e in blocked] == ["confirm_held_recommendation_order"]
        assert result.to_dict()["blocked_calls_count"] == 1
