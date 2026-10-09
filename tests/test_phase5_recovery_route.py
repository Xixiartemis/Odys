"""Deterministic tests for the recovery-route layer and experiment
attribution (route-v1).

Coverage (Phase E):
- route state machine: CONTINUE / RETRY_SAME / RETRY_CHANGED / REPLAN /
  ESCALATE / STOP with reason codes
- honest StateChange evidence (UNKNOWN is never fabricated)
- REPLAN never bypasses the execution authorization gate
- ESCALATE and STOP are distinct states with distinct events
- budget insufficiency blocks retries; rejected retries consume no budget
- legitimate polling is not misjudged as no-progress
- successful side-effect repeats require idempotency
- attribution: call ids ↔ real/synthetic responses, event ordering,
  AUTHORIZED≠EXECUTED≠final success, decision_id linkage, JSON
  round-trip and old-schema compatibility, manifest version identity
- integration: C1_task_036_P4 control behaviour on the frozen ToolMaze,
  root budget authority unchanged, no unauthorized provider calls

No live model calls anywhere.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lhas.phase5.agent_adapter import create_toolmaze_agent_adapter
from lhas.phase5.agent_core import Phase5AgentCore
from lhas.phase5.control_arms import (
    ControlArm,
    RecoveryActionKind,
    RecoveryDecision,
)
from lhas.phase5.execution_control import (
    EVENT_SCHEMA_VERSION,
    EXECUTION_CONTROL_VERSION,
    ControlEventKind,
    ExecutionAuthorizationGate,
)
from lhas.phase5.model_driver import BudgetExhausted, BudgetedModelDriver, ModelAction, ModelToolCall
from lhas.phase5.recovery_budget import RecoveryBudgetGate
from lhas.phase5.recovery_route import (
    ROUTE_POLICY_VERSION,
    AttemptEvidence,
    EvidenceGrade,
    RecoveryRoute,
    RecoveryRouteDecider,
    RouteDecisionInputs,
    StateChange,
    evaluate_state_change,
    map_route_to_recovery_action,
    route_policy_fingerprint,
)


ROOT = Path(__file__).resolve().parents[1]
TOOLMAZE_ROOT = ROOT / "experiments" / "phase5" / "benchmarks" / "toolmaze"
TASK_PATH = TOOLMAZE_ROOT / "data" / "perturbed_tasks" / "c1" / "C1_task_036_P4.json"


# ══════════════════════════════════════════════════════════════════════
# Harness (mirrors the frozen ExecutionEngine loop contract)
# ══════════════════════════════════════════════════════════════════════

class SequenceDriver:
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
    return ModelAction(
        type="tool_call",
        tool_calls=[
            ModelToolCall(tool_name=n, arguments=dict(a), provider_tool_call_id=p)
            for n, a, p in calls
        ],
    )


def run_engine_loop(adapter, results, *, task="task", tools=None, max_rounds=30):
    executions = []
    adapter.initialize(task, list(tools or []))
    message = task
    for round_num in range(max_rounds):
        action = adapter.step(user_message=message if round_num == 0 else None)
        message = None
        if action.type == "final_answer":
            return executions, action
        name = action.tool_name
        args = action.arguments or {}
        result = results(name, args, len(executions)) if callable(results) else results[name]
        executions.append((name, dict(args)))
        adapter.receive_tool_result(name, result)
    raise AssertionError("engine loop did not terminate")


class Strategy:
    """Scripted recovery strategy with a fixed per-result policy."""

    def __init__(self, arm=ControlArm.A3_ODYS_FULL, on_failure=None):
        self.arm = arm
        self.on_failure = on_failure
        self.calls = []

    def configure(self, **kwargs):
        return {}

    def create_observer(self):
        return None

    def should_validate(self):
        return False

    def recovery_budget_enabled(self):
        return True

    async def on_step_result(self, *, step, result, observer):
        status = str(result.get("status", "")).lower()
        failed = bool(result.get("error")) or status in {
            "error", "failure", "failed", "timeout", "transient_error",
        }
        if failed and self.on_failure is not None:
            self.calls.append(result)
            return self.on_failure
        return RecoveryDecision()


def events_of(gate, kind):
    return [e for e in gate.control_events if e["kind"] == kind.value]


def attempt(step, result_digest, state_digest, *, affects=True, state_evidence=True,
            exec_status="FAILED", retryable="UNKNOWN"):
    return AttemptEvidence(
        step=step,
        result_digest=result_digest,
        state_digest=state_digest,
        exec_status=exec_status,
        ready_status="NO_EVIDENCE",
        retryable=retryable,
        affects_followups=affects,
        state_evidence_available=state_evidence,
    )


# ══════════════════════════════════════════════════════════════════════
# Route state machine (pure decisions)
# ══════════════════════════════════════════════════════════════════════

class TestRouteStateMachine:
    def test_normal_progress_selects_continue(self):
        decider = RecoveryRouteDecider()
        decision = decider.decide(RouteDecisionInputs(step=1, proposed_action="none"))
        assert decision.route is RecoveryRoute.CONTINUE
        assert decision.reason_code == "STRATEGY_NONE"
        assert decision.mapped_recovery_action == "none"

    def test_transient_evidence_selects_retry_same(self):
        decider = RecoveryRouteDecider()
        decision = decider.decide(RouteDecisionInputs(
            step=1, proposed_action="retry",
            last_exec_status="FAILED", last_retryable="RETRYABLE",
            transient_basis=True,
        ))
        assert decision.route is RecoveryRoute.RETRY_SAME
        assert decision.reason_code == "TRANSIENT_BASIS"
        assert decision.mapped_recovery_action == "retry"

    def test_not_retryable_error_does_not_repeat_action(self):
        """不可重试错误不重复执行 —— 首次进入 REPLAN。"""
        decider = RecoveryRouteDecider()
        inputs = RouteDecisionInputs(
            step=1, proposed_action="retry",
            last_exec_status="FAILED", last_retryable="NOT_RETRYABLE",
        )
        first = decider.decide(inputs)
        assert first.route is RecoveryRoute.REPLAN
        assert first.reason_code == "PERMANENT_FAILURE_REPLAN"
        assert first.mapped_recovery_action == "retry_with_context"
        second = decider.decide(inputs)
        assert second.route is RecoveryRoute.ESCALATE
        assert second.reason_code == "REPLAN_EXHAUSTED"

    def test_identical_failure_loop_is_detected(self):
        decider = RecoveryRouteDecider()
        decision = decider.decide(RouteDecisionInputs(
            step=3, proposed_action="retry",
            last_exec_status="FAILED",
            attempts=[attempt(1, "X", "S"), attempt(2, "X", "S")],
        ))
        assert decision.route is RecoveryRoute.STOP
        assert decision.reason_code == "NO_PROGRESS_LOOP"

    def test_unknown_environment_state_is_not_fabricated(self):
        """环境状态未知时不伪造无进展证据：StateChange 如实为 UNKNOWN。"""
        attempts = [
            attempt(1, "X", "S", state_evidence=False),
            attempt(2, "X", "S", state_evidence=False),
        ]
        change, grade = evaluate_state_change(attempts)
        assert change is StateChange.UNKNOWN
        assert grade is EvidenceGrade.UNKNOWN

        decider = RecoveryRouteDecider()
        decision = decider.decide(RouteDecisionInputs(
            step=3, proposed_action="retry",
            last_exec_status="FAILED",
            attempts=attempts,
            state_change=change, state_change_grade=grade,
        ))
        # the loop is still bounded, but the claim is "no new evidence",
        # never "the environment did not change"
        assert decision.route is RecoveryRoute.STOP
        assert decision.state_change == "UNKNOWN"

    def test_real_state_change_allows_reevaluation(self):
        attempts = [
            attempt(1, "X", "S1"),
            attempt(2, "X", "S2"),  # observed state change between attempts
        ]
        change, grade = evaluate_state_change(attempts)
        assert change is StateChange.OBSERVED_CHANGE
        assert grade is EvidenceGrade.DIRECT

        decider = RecoveryRouteDecider()
        decision = decider.decide(RouteDecisionInputs(
            step=3, proposed_action="retry",
            last_exec_status="FAILED",
            attempts=attempts,
            state_change=change, state_change_grade=grade,
        ))
        assert decision.route is RecoveryRoute.RETRY_CHANGED
        assert decision.reason_code == "STATE_CHANGED"
        assert decision.mapped_recovery_action == "retry_with_context"

    def test_no_observed_change_with_state_evidence(self):
        attempts = [attempt(1, "X", "S"), attempt(2, "X", "S")]
        change, grade = evaluate_state_change(attempts)
        assert change is StateChange.NO_OBSERVED_CHANGE
        assert grade is EvidenceGrade.DIRECT

    def test_escalate_and_stop_are_distinct(self):
        decider = RecoveryRouteDecider()
        stop = decider.decide(RouteDecisionInputs(step=1, proposed_action="stop"))
        escalate = decider.decide(RouteDecisionInputs(step=2, proposed_action="escalate"))
        assert stop.route is RecoveryRoute.STOP
        assert escalate.route is RecoveryRoute.ESCALATE
        assert stop.reason_code == "STRATEGY_STOP"
        assert escalate.reason_code == "STRATEGY_ESCALATE"
        assert stop.mapped_recovery_action != escalate.mapped_recovery_action

    def test_route_policy_fingerprint_is_deterministic(self):
        assert route_policy_fingerprint() == route_policy_fingerprint()
        assert ROUTE_POLICY_VERSION == "recovery-route-v1"


# ══════════════════════════════════════════════════════════════════════
# Route integration through core + adapter
# ══════════════════════════════════════════════════════════════════════

class TestRouteIntegration:
    def test_retry_same_replays_after_transient_failure(self):
        strategy = Strategy(on_failure=RecoveryDecision(
            action=RecoveryActionKind.RETRY, reason="retry transient", signal="TOOL_ERROR",
        ))
        core = Phase5AgentCore(SequenceDriver([
            batch_action([("tool_a", {"n": 1}, "call-1")]),
            ModelAction(type="final_answer", content="done"),
        ]), strategy=strategy)
        budget_gate = RecoveryBudgetGate(max_recovery_attempts=3)
        core.set_recovery_budget_gate(budget_gate)
        adapter = create_toolmaze_agent_adapter(core)
        gate = adapter._gate

        executions, final = _run_with_sequence(adapter, [
            {"status": "timeout"},                      # transient failure
            {"status": "success"},                      # verification succeeds
        ])
        assert len(executions) == 2
        assert final.content == "done"
        recovery = events_of(gate, ControlEventKind.RECOVERY)
        assert recovery[0]["route"] == "RETRY_SAME"
        assert recovery[0]["reason_code"] == "TRANSIENT_BASIS"
        assert recovery[0]["action"] == "retry"
        assert recovery[0]["budget_decision"] == "ALLOW"
        assert recovery[0]["recovery_budget_before"] == 3
        assert recovery[0]["recovery_budget_after"] == 2

    def test_permanent_failure_routes_to_replan_not_repeat(self):
        """不可重试错误 → REPLAN：同动作不重复执行，模型改走新动作且经授权门。"""
        strategy = Strategy(on_failure=RecoveryDecision(
            action=RecoveryActionKind.RETRY, reason="strategy wants retry", signal="TOOL_ERROR",
        ))
        core = Phase5AgentCore(SequenceDriver([
            batch_action([("tool_a", {"n": 1}, "call-1")]),
            batch_action([("tool_b", {"n": 2}, "call-2")]),   # re-planned action
            ModelAction(type="final_answer", content="done"),
        ]), strategy=strategy)
        adapter = create_toolmaze_agent_adapter(core)
        gate = adapter._gate

        executions, final = _run_with_sequence(adapter, [
            {"status": "error", "retryable": False},   # permanent failure
            {"status": "success"},
        ])

        # the failed identical call is NEVER executed twice
        assert [e[0] for e in executions] == ["tool_a", "tool_b"]
        recovery = events_of(gate, ControlEventKind.RECOVERY)
        assert recovery[0]["route"] == "REPLAN"
        assert recovery[0]["reason_code"] == "PERMANENT_FAILURE_REPLAN"
        assert recovery[0]["action"] == "retry_with_context"
        assert final.content == "done"

    def test_replan_then_repeated_action_is_blocked_by_gate(self):
        """REPLAN 后模型重复同一永久失败动作 → 授权门拒绝再次执行。"""
        strategy = Strategy(on_failure=RecoveryDecision(
            action=RecoveryActionKind.RETRY, reason="strategy wants retry", signal="TOOL_ERROR",
        ))
        core = Phase5AgentCore(SequenceDriver([
            batch_action([("tool_a", {"n": 1}, "call-1")]),
            batch_action([("tool_a", {"n": 1}, "call-2")]),   # model repeats despite replan
            ModelAction(type="final_answer", content="never"),
        ]), strategy=strategy)
        adapter = create_toolmaze_agent_adapter(core)
        gate = adapter._gate

        executions, final = _run_with_sequence(adapter, [
            {"status": "error", "retryable": False},
            {"status": "error", "retryable": False},
        ])
        # the permanent-failure action executed exactly once
        assert [e[0] for e in executions] == ["tool_a"]
        recovery = events_of(gate, ControlEventKind.RECOVERY)
        assert recovery[0]["route"] == "REPLAN"
        assert recovery[0]["reason_code"] == "PERMANENT_FAILURE_REPLAN"
        blocked = events_of(gate, ControlEventKind.BLOCKED)
        assert blocked and blocked[0]["rule"] == "NO_PROGRESS_RETRY"
        assert final.content.startswith("TERMINATED")
        assert core.termination_kind == "STOP"

    def test_strategy_escalate_keeps_distinct_escalation_state(self):
        strategy = Strategy(on_failure=RecoveryDecision(
            action=RecoveryActionKind.ESCALATE, reason="critical failure",
            signal="TOOL_ERROR",
        ))
        core = Phase5AgentCore(SequenceDriver([
            batch_action([("tool_a", {"n": 1}, "call-1")]),
        ]), strategy=strategy)
        adapter = create_toolmaze_agent_adapter(core)
        gate = adapter._gate
        _run_with_sequence(adapter, [{"status": "error"}])
        recovery = events_of(gate, ControlEventKind.RECOVERY)
        assert recovery[0]["route"] == "ESCALATE"
        assert recovery[0]["reason_code"] == "STRATEGY_ESCALATE"
        assert recovery[0]["reason"] == "critical failure"  # strategy evidence kept
        assert core.termination_kind == "ESCALATION"
        assert "critical failure" in core.escalation_reason

    def test_stop_and_escalation_events_are_distinct(self):
        strategy = Strategy(on_failure=RecoveryDecision(
            action=RecoveryActionKind.STOP, reason="policy stop", signal="TOOL_ERROR",
        ))
        core = Phase5AgentCore(SequenceDriver([
            batch_action([("tool_a", {"n": 1}, "call-1")]),
        ]), strategy=strategy)
        adapter = create_toolmaze_agent_adapter(core)
        gate = adapter._gate
        _run_with_sequence(adapter, [{"status": "error"}])
        recovery = events_of(gate, ControlEventKind.RECOVERY)
        assert recovery[0]["route"] == "STOP"
        assert recovery[0]["reason_code"] == "STRATEGY_STOP"
        assert core.termination_kind == "STOP"

    def test_budget_insufficiency_blocks_retry_without_disguise(self):
        """预算不足不执行重试，且不能被伪装成"重试不合理"。"""
        strategy = Strategy(on_failure=RecoveryDecision(
            action=RecoveryActionKind.RETRY, reason="retry", signal="TOOL_ERROR",
        ))
        budget_gate = RecoveryBudgetGate(max_recovery_attempts=0)  # no budget at all
        core = Phase5AgentCore(SequenceDriver([
            batch_action([("tool_a", {"n": 1}, "call-1")]),
        ]), strategy=strategy)
        core.set_recovery_budget_gate(budget_gate)
        adapter = create_toolmaze_agent_adapter(core)
        gate = adapter._gate

        executions, final = _run_with_sequence(adapter, [{"status": "error"}])
        assert len(executions) == 1          # the retry never executed
        recovery = events_of(gate, ControlEventKind.RECOVERY)
        assert recovery[0]["route"] == "RETRY_SAME"          # route WAS reasonable
        assert recovery[0]["budget_decision"] == "ESCALATE"  # budget said no
        assert recovery[0]["authorized"] is False
        assert recovery[0]["route_evidence"]["last_retryable"] == "UNKNOWN"
        assert core.termination_kind == "BUDGET_EXHAUSTION"
        assert final.content.startswith("TERMINATED")

    def test_rejected_retry_consumes_no_recovery_budget(self):
        strategy = Strategy(on_failure=RecoveryDecision(
            action=RecoveryActionKind.RETRY, reason="retry", signal="TOOL_ERROR",
        ))
        budget_gate = RecoveryBudgetGate(max_recovery_attempts=5)
        core = Phase5AgentCore(SequenceDriver([
            batch_action([("tool_a", {"n": 1}, "call-1")]),
            ModelAction(type="final_answer", content="never"),
        ]), strategy=strategy)
        core.set_recovery_budget_gate(budget_gate)
        adapter = create_toolmaze_agent_adapter(core)
        gate = adapter._gate

        _run_with_sequence(adapter, [
            {"status": "error"},   # → bounded verification retry (consumes 1)
            {"status": "error"},   # → loop stop (consumes 0)
        ])
        recovery = events_of(gate, ControlEventKind.RECOVERY)
        assert recovery[0]["budget_decision"] == "ALLOW"
        assert recovery[0]["recovery_budget_before"] == 5
        assert recovery[0]["recovery_budget_after"] == 4
        assert recovery[1]["route"] == "STOP"
        assert recovery[1]["budget_decision"] == "NOT_CONSUMED"
        assert recovery[1]["recovery_budget_before"] == 4
        assert recovery[1]["recovery_budget_after"] == 4
        assert len(budget_gate.ledger) == 1   # only the justified intervention

    def test_legitimate_polling_is_not_killed(self):
        """合法轮询不会因单纯重复返回内容就被误杀。"""
        core = Phase5AgentCore(SequenceDriver([
            batch_action([("get_status", {"q": "x"}, "call-1")]),
            batch_action([("get_status", {"q": "x"}, "call-2")]),
            batch_action([("get_status", {"q": "x"}, "call-3")]),
            batch_action([("get_status", {"q": "x"}, "call-4")]),
            ModelAction(type="final_answer", content="done"),
        ]))
        adapter = create_toolmaze_agent_adapter(core)
        executions, final = _run_with_sequence(adapter, {"status": "success"})
        assert len(executions) == 4
        assert final.content == "done"
        assert events_of(adapter._gate, ControlEventKind.BLOCKED) == []

    def test_successful_side_effect_repeat_requires_idempotency(self):
        core = Phase5AgentCore(SequenceDriver([
            batch_action([("hold_inventory", {"item": "p1"}, "call-1")]),
            batch_action([("hold_inventory", {"item": "p1"}, "call-2")]),
            ModelAction(type="final_answer", content="done"),
        ]))
        adapter = create_toolmaze_agent_adapter(core)
        executions, _ = _run_with_sequence(adapter, {
            "status": "success", "reservation_id": "R1",
        })
        assert len(executions) == 1
        blocked = events_of(adapter._gate, ControlEventKind.BLOCKED)
        assert blocked[0]["rule"] == "SIDE_EFFECT_REPEAT_NOT_IDEMPOTENT"

    def test_replan_output_still_passes_authorization_gate(self):
        """REPLAN 不绕过执行授权门：重规划后的依赖调用照样被拦。"""
        strategy = Strategy(on_failure=RecoveryDecision(
            action=RecoveryActionKind.RETRY, reason="retry", signal="TOOL_ERROR",
        ))
        core = Phase5AgentCore(SequenceDriver([
            batch_action([("tool_a", {"n": 1}, "call-1")]),           # permanent failure
            batch_action([
                ("build_flight_booking_request", {"f": "A"}, "call-2"),
                ("confirm_held_recommendation_order", {"o": "O"}, "call-3"),
            ]),  # re-planned batch whose confirm depends on readiness
            ModelAction(type="final_answer", content="done"),
        ]), strategy=strategy)
        adapter = create_toolmaze_agent_adapter(core)
        gate = adapter._gate

        executions, _ = _run_with_sequence(adapter, lambda name, args, n: (
            {"status": "error", "retryable": False} if name == "tool_a"
            else {"booking_ready": False} if name == "build_flight_booking_request"
            else {"status": "confirmed"}
        ))
        executed_names = [e[0] for e in executions]
        assert "confirm_held_recommendation_order" not in executed_names
        blocked = events_of(gate, ControlEventKind.BLOCKED)
        assert blocked[0]["tool_name"] == "confirm_held_recommendation_order"

    def test_root_budget_authority_unchanged(self):
        driver = BudgetedModelDriver(
            SequenceDriver([]), max_model_calls=1, max_turns=1,
        )
        core = Phase5AgentCore(driver)
        core.initialize("task", [])
        core.next_model_action()
        with pytest.raises(BudgetExhausted):
            core.next_model_action()


def _run_with_sequence(adapter, results, *, max_rounds=30):
    """run_engine_loop with per-call results supplied in order."""
    if isinstance(results, dict) or callable(results):
        payload = results
    else:
        payload = list(results)

    def resolver(name, args, index):
        if callable(payload):
            return payload(name, args, index)
        if isinstance(payload, list):
            return payload[index]
        return payload

    return run_engine_loop(adapter, resolver, max_rounds=max_rounds)


# ══════════════════════════════════════════════════════════════════════
# Events & attribution
# ══════════════════════════════════════════════════════════════════════

class TestAttribution:
    def test_recovery_event_links_call_identity_and_decision(self):
        strategy = Strategy(on_failure=RecoveryDecision(
            action=RecoveryActionKind.RETRY, reason="retry", signal="TOOL_ERROR",
        ))
        core = Phase5AgentCore(SequenceDriver([
            batch_action([("tool_a", {"n": 1}, "provider-77")]),
            ModelAction(type="final_answer", content="done"),
        ]), strategy=strategy)
        adapter = create_toolmaze_agent_adapter(core)
        gate = adapter._gate
        gate.bind_identity(run_id="trial-R1", trial_id="T1")
        core.set_execution_lineage(task_id="C1_task_036_P4", trial_id="T1")

        _run_with_sequence(adapter, [
            {"status": "timeout"},
            {"status": "success"},
        ])
        recovery = events_of(gate, ControlEventKind.RECOVERY)
        event = recovery[0]
        # call identity + experiment identity + decision identity
        assert event["provider_tool_call_id"] == "provider-77"
        assert event["tool_call_id"]
        assert event["action_signature"]
        assert event["run_id"] == "trial-R1"
        assert event["trial_id"] == "T1"
        assert event["decision_id"].startswith("route-")
        assert event["route_policy_version"] == ROUTE_POLICY_VERSION
        assert event["event_schema_version"] == EVENT_SCHEMA_VERSION

        # decision_id links the control event to the recovery_decisions entry
        entries = core.get_recovery_decisions()
        assert entries[0]["decision_id"] == event["decision_id"]
        assert entries[0]["route"] == event["route"]

    def test_event_order_matches_state_transitions(self):
        core = Phase5AgentCore(SequenceDriver([
            batch_action([
                ("build_flight_booking_request", {"f": "A"}, "call-1"),
                ("confirm_held_recommendation_order", {"o": "O"}, "call-2"),
            ]),
            ModelAction(type="final_answer", content="done"),
        ]))
        adapter = create_toolmaze_agent_adapter(core)
        _run_with_sequence(adapter, {"booking_ready": False})

        kinds = [e["kind"] for e in adapter._gate.control_events]
        # propose → authorize → execute → observe for the real call,
        # propose → block for the intercepted call
        assert kinds.index("PROPOSED") < kinds.index("AUTHORIZED")
        assert kinds.index("AUTHORIZED") < kinds.index("EXECUTED")
        assert kinds.index("EXECUTED") < kinds.index("OBSERVED")
        assert "BLOCKED" in kinds
        blocked_index = kinds.index("BLOCKED")
        assert blocked_index > kinds.index("OBSERVED")  # batch priority: first call observed first

    def test_synthetic_never_counted_as_executed_or_observed(self):
        core = Phase5AgentCore(SequenceDriver([
            batch_action([
                ("build_flight_booking_request", {"f": "A"}, "call-1"),
                ("confirm_held_recommendation_order", {"o": "O"}, "call-2"),
            ]),
            ModelAction(type="final_answer", content="done"),
        ]))
        adapter = create_toolmaze_agent_adapter(core)
        gate = adapter._gate
        _run_with_sequence(adapter, {"booking_ready": False})

        executed_tools = [e["tool_name"] for e in events_of(gate, ControlEventKind.EXECUTED)]
        observed_tools = [e["tool_name"] for e in events_of(gate, ControlEventKind.OBSERVED)]
        assert "confirm_held_recommendation_order" not in executed_tools
        assert "confirm_held_recommendation_order" not in observed_tools
        # AUTHORIZED ≠ success: the authorized call's observation is its own event
        authorized_tools = [e["tool_name"] for e in events_of(gate, ControlEventKind.AUTHORIZED)]
        assert authorized_tools == ["build_flight_booking_request"]

    def test_events_json_round_trip_and_old_schema_compat(self):
        gate = ExecutionAuthorizationGate()
        gate.bind_identity(run_id="trial-R", trial_id="T")
        gate.observe(step=1, tool_name="tool_a", arguments={"n": 1},
                     result={"status": "error"})
        payload = json.dumps(gate.control_events)
        restored = json.loads(payload)
        assert restored[0]["event_schema_version"] == EVENT_SCHEMA_VERSION
        assert restored[0]["execution_control_version"] == EXECUTION_CONTROL_VERSION

        # old-schema events (no version/identity fields) still load and merge
        old_event = {"event_id": 1, "kind": "EXECUTED", "tool_name": "legacy"}
        merged = json.loads(json.dumps([old_event] + restored))
        assert merged[0].get("event_schema_version") is None  # marked as legacy
        assert merged[1]["run_id"] == "trial-R"

    def test_execution_provenance_identifies_versions(self):
        from lhas.phase5.trial_executor import _execution_provenance
        prov = _execution_provenance(engine_round_guard=20)
        assert prov["execution_path"] == "CANONICAL_TRIAL_EXECUTOR"
        assert prov["included_in_canonical_analysis"] is True
        assert prov["execution_control_version"] == EXECUTION_CONTROL_VERSION
        assert prov["event_schema_version"] == EVENT_SCHEMA_VERSION
        assert prov["route_policy_version"] == ROUTE_POLICY_VERSION
        assert prov["route_policy_fingerprint"] == route_policy_fingerprint()
        assert prov["engine_round_guard"] == 20
        assert "BudgetedModelDriver" in prov["engine_round_guard_semantics"]

    def test_non_canonical_path_is_excluded_from_analysis(self):
        from lhas.phase5.runtime_backend import ToolMazeRuntimeBackend
        source = Path(ToolMazeRuntimeBackend.__module__.replace(".", "/") + ".py")
        # the exclusion marker must exist in the non-canonical path source
        module_source = (
            ROOT / "src" / "lhas" / "phase5" / "runtime_backend.py"
        ).read_text(encoding="utf-8")
        assert "NON_CANONICAL_RUNTIME_BACKEND" in module_source
        assert "included_in_canonical_analysis" in module_source
        del source

    def test_tool_failure_vs_infra_failure_events_differ(self):
        """工具失败产生 EXECUTED+OBSERVED(FAILED)；基础设施错误不伪造恢复事件。"""
        core = Phase5AgentCore(SequenceDriver([
            batch_action([("tool_a", {"n": 1}, "call-1")]),
        ]))
        adapter = create_toolmaze_agent_adapter(core)
        gate = adapter._gate

        class ExplodingStrategy(Strategy):
            async def on_step_result(self, *, step, result, observer):
                raise RuntimeError("policy blew up")

        core2 = Phase5AgentCore(SequenceDriver([
            batch_action([("tool_a", {"n": 1}, "call-1")]),
        ]), strategy=ExplodingStrategy())
        adapter2 = create_toolmaze_agent_adapter(core2)

        _run_with_sequence(adapter, {"status": "error"})
        with pytest.raises(Exception):
            _run_with_sequence(adapter2, {"status": "error"})

        # tool failure: normal observed failure, no fabricated recovery claim
        tool_events = events_of(gate, ControlEventKind.OBSERVED)
        assert tool_events and tool_events[0]["exec_status"] == "FAILED"
        # infra failure: no RECOVERY decision was ever recorded
        assert events_of(adapter2._gate, ControlEventKind.RECOVERY) == []


# ══════════════════════════════════════════════════════════════════════
# Integration on the frozen ToolMaze (C1_task_036_P4)
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


def _call(call_id, name, arguments):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class TestFrozenToolMazeIntegration:
    def test_canary_036_control_behavior_with_attribution(self):
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
            experiment_id="phase5-route-attribution-canary-036",
            task_id="C1_task_036_P4",
        )

        # booking_ready=false → confirm executions == 0 (frozen ToolMaze)
        executed_tools = [
            m.get("tool_call", {}).get("name")
            for m in result.official_trace.get("messages", [])
            if m.get("type") == "tool_call"
        ]
        assert executed_tools.count("confirm_held_recommendation_order") == 0
        assert executed_tools.count("build_flight_booking_request") == 1

        # no unauthorized provider calls: one batch turn + one final turn
        assert len(fake.chat.completions.requests) == 2

        # attribution: provenance + event identity stamped on the canonical path
        prov = result.execution_provenance
        assert prov["execution_path"] == "CANONICAL_TRIAL_EXECUTOR"
        assert prov["route_policy_version"] == ROUTE_POLICY_VERSION
        assert result.control_events
        assert all(e.get("run_id") == f"trial-{result.trial_id}"
                   for e in result.control_events)
        assert all(e.get("trial_id") == result.trial_id
                   for e in result.control_events)
        blocked = [e for e in result.control_events if e["kind"] == "BLOCKED"]
        assert blocked and blocked[0]["tool_name"] == "confirm_held_recommendation_order"
        # AUTHORIZED is not success — only the judge decides completion
        assert result.validity in {"VALID", "INVALID_INFRA"}
