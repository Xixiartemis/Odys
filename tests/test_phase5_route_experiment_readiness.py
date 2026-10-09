"""Readiness-gate tests: B0/B1 unique difference surface (Experiment B).

Verifies, in provider-free / fake-client environments:

1. B1 (route-v1, default) behaviour is unchanged (frozen route rules).
2. B0 (baseline) disables EXACTLY the Q3a/Q3b/Q3d enhancement rules and
   passes the strategy proposal through — and nothing else changes:
   the execution authorization gate, normalizer, budget gate, task input,
   Judge inputs and event schema are shared and identical.
3. The strategy switch actually takes effect and is recorded in provenance.
4. No default-value override, no configuration leakage, no bypass path
   around the execution authorization gate in either mode.

No real model calls anywhere.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lhas.phase5.agent_adapter import create_toolmaze_agent_adapter
from lhas.phase5.agent_core import Phase5AgentCore
from lhas.phase5.control_arms import ControlArm, RecoveryActionKind, RecoveryDecision
from lhas.phase5.execution_control import ControlEventKind, ExecutionAuthorizationGate
from lhas.phase5.model_driver import ModelAction, ModelToolCall
from lhas.phase5.recovery_budget import RecoveryBudgetGate
from lhas.phase5.recovery_route import (
    ROUTE_MODE_BASELINE,
    ROUTE_MODE_V1,
    RecoveryRoute,
    RecoveryRouteDecider,
    RouteDecisionInputs,
)


ROOT = Path(__file__).resolve().parents[1]
TOOLMAZE_ROOT = ROOT / "experiments" / "phase5" / "benchmarks" / "toolmaze"
TASK_PATH = TOOLMAZE_ROOT / "data" / "perturbed_tasks" / "c1" / "C1_task_036_P4.json"


# ══════════════════════════════════════════════════════════════════════
# Harness
# ══════════════════════════════════════════════════════════════════════

class SequenceDriver:
    def __init__(self, actions):
        self.actions = list(actions)

    def next_action(self, messages, tool_definitions, generation_config=None):
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
        pass


def batch_action(calls):
    return ModelAction(
        type="tool_call",
        tool_calls=[
            ModelToolCall(tool_name=n, arguments=dict(a), provider_tool_call_id=p)
            for n, a, p in calls
        ],
    )


class RetryStrategy:
    arm = ControlArm.A3_ODYS_FULL

    def __init__(self, action=RecoveryActionKind.RETRY, reason="strategy retry"):
        self._action = action
        self._reason = reason

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
        if result.get("error") or status in {"error", "failure", "failed", "timeout"}:
            return RecoveryDecision(
                action=self._action, reason=self._reason, signal="TOOL_ERROR",
            )
        return RecoveryDecision()


def run_loop(adapter, results, *, max_rounds=30):
    executions = []
    adapter.initialize("task", [])
    message = "task"
    for round_num in range(max_rounds):
        action = adapter.step(user_message=message if round_num == 0 else None)
        message = None
        if action.type == "final_answer":
            return executions, action
        name = action.tool_name
        result = results(name, action.arguments or {}, len(executions))
        executions.append((name, dict(action.arguments or {})))
        adapter.receive_tool_result(name, result)
    raise AssertionError("loop did not terminate")


def make_core(actions, strategy, *, route_mode):
    core = Phase5AgentCore(SequenceDriver(actions), strategy=strategy)
    core.set_route_mode(route_mode)
    adapter = create_toolmaze_agent_adapter(core)
    return core, adapter


def events_of(gate, kind):
    return [e for e in gate.control_events if e["kind"] == kind.value]


# ══════════════════════════════════════════════════════════════════════
# 1. Route-mode semantics
# ══════════════════════════════════════════════════════════════════════

class TestRouteModes:
    def test_default_mode_is_frozen_route_v1(self):
        decider = RecoveryRouteDecider()
        assert decider.mode == ROUTE_MODE_V1 == "route-v1"
        decision = decider.decide(RouteDecisionInputs(
            step=1, proposed_action="retry",
            last_exec_status="FAILED", last_retryable="NOT_RETRYABLE",
        ))
        assert decision.route is RecoveryRoute.REPLAN  # B1 enhancement Q3a

    def test_b0_disables_q3a_permanent_failure_replan(self):
        decider = RecoveryRouteDecider(mode=ROUTE_MODE_BASELINE)
        decision = decider.decide(RouteDecisionInputs(
            step=1, proposed_action="retry",
            last_exec_status="FAILED", last_retryable="NOT_RETRYABLE",
        ))
        assert decision.route is RecoveryRoute.RETRY_SAME
        assert decision.reason_code == "STRATEGY_PASSTHROUGH"
        assert decision.mapped_recovery_action == "retry"

    def test_b0_disables_q3b_loop_stop(self):
        from lhas.phase5.recovery_route import AttemptEvidence

        attempts = [
            AttemptEvidence(step=1, result_digest="X", state_digest="S",
                            exec_status="FAILED", ready_status="NO_EVIDENCE",
                            retryable="UNKNOWN", affects_followups=True,
                            state_evidence_available=True),
            AttemptEvidence(step=2, result_digest="X", state_digest="S",
                            exec_status="FAILED", ready_status="NO_EVIDENCE",
                            retryable="UNKNOWN", affects_followups=True,
                            state_evidence_available=True),
        ]
        baseline = RecoveryRouteDecider(mode=ROUTE_MODE_BASELINE).decide(
            RouteDecisionInputs(step=3, proposed_action="retry",
                                last_exec_status="FAILED", attempts=attempts))
        v1 = RecoveryRouteDecider().decide(
            RouteDecisionInputs(step=3, proposed_action="retry",
                                last_exec_status="FAILED", attempts=attempts))
        assert baseline.route is RecoveryRoute.RETRY_SAME   # passthrough
        assert v1.route is RecoveryRoute.STOP               # Q3b enhancement
        assert v1.reason_code == "NO_PROGRESS_LOOP"

    def test_b0_disables_q3d_state_change_reroute(self):
        from lhas.phase5.recovery_route import AttemptEvidence, StateChange

        attempts = [
            AttemptEvidence(step=1, result_digest="X", state_digest="S1",
                            exec_status="FAILED", ready_status="NO_EVIDENCE",
                            retryable="UNKNOWN", affects_followups=True,
                            state_evidence_available=True),
            AttemptEvidence(step=2, result_digest="X", state_digest="S2",
                            exec_status="FAILED", ready_status="NO_EVIDENCE",
                            retryable="UNKNOWN", affects_followups=True,
                            state_evidence_available=True),
        ]
        baseline = RecoveryRouteDecider(mode=ROUTE_MODE_BASELINE).decide(
            RouteDecisionInputs(step=3, proposed_action="retry",
                                last_exec_status="FAILED", attempts=attempts,
                                state_change=StateChange.OBSERVED_CHANGE))
        v1 = RecoveryRouteDecider().decide(
            RouteDecisionInputs(step=3, proposed_action="retry",
                                last_exec_status="FAILED", attempts=attempts,
                                state_change=StateChange.OBSERVED_CHANGE))
        assert baseline.route is RecoveryRoute.RETRY_SAME
        assert v1.route is RecoveryRoute.RETRY_CHANGED
        assert v1.reason_code == "STATE_CHANGED"

    def test_b0_context_change_proposal_passes_through(self):
        decider = RecoveryRouteDecider(mode=ROUTE_MODE_BASELINE)
        decision = decider.decide(RouteDecisionInputs(
            step=1, proposed_action="retry_with_context",
        ))
        assert decision.route is RecoveryRoute.RETRY_CHANGED
        assert decision.reason_code == "STRATEGY_CONTEXT_CHANGE"

    def test_b0_keeps_terminal_safety_and_strategy_terminal(self):
        baseline = RecoveryRouteDecider(mode=ROUTE_MODE_BASELINE)
        terminal = baseline.decide(RouteDecisionInputs(
            step=1, proposed_action="retry", terminal=True))
        assert terminal.route is RecoveryRoute.STOP
        assert terminal.reason_code == "TERMINAL_STATE"
        escalate = baseline.decide(RouteDecisionInputs(
            step=2, proposed_action="escalate"))
        assert escalate.route is RecoveryRoute.ESCALATE
        stop = baseline.decide(RouteDecisionInputs(step=3, proposed_action="stop"))
        assert stop.route is RecoveryRoute.STOP

    def test_unknown_route_mode_rejected(self):
        with pytest.raises(ValueError):
            RecoveryRouteDecider(mode="experimental-v2")


# ══════════════════════════════════════════════════════════════════════
# 2. Unique difference surface through core + adapter
# ══════════════════════════════════════════════════════════════════════

class TestUniqueDifferenceSurface:
    def _shared_scenario(self, route_mode):
        """One transient failure + one recovery retry — behaviour shared."""
        core, adapter = make_core(
            [
                batch_action([("tool_a", {"n": 1}, "call-1")]),
                ModelAction(type="final_answer", content="done"),
            ],
            RetryStrategy(),
            route_mode=route_mode,
        )
        budget_gate = RecoveryBudgetGate(max_recovery_attempts=3)
        core.set_recovery_budget_gate(budget_gate)
        executions, final = run_loop(adapter, lambda n, a, i: (
            {"status": "error"} if i == 0 else {"status": "success"}
        ))
        return core, adapter._gate, budget_gate, executions, final

    def test_only_route_fields_differ_between_modes(self):
        core_b1, gate_b1, budget_b1, exec_b1, final_b1 = self._shared_scenario(ROUTE_MODE_V1)
        core_b0, gate_b0, budget_b0, exec_b0, final_b0 = self._shared_scenario(ROUTE_MODE_BASELINE)

        # shared execution behaviour (same calls, same order, same results)
        assert exec_b1 == exec_b0 == [("tool_a", {"n": 1}), ("tool_a", {"n": 1})]
        assert final_b1.content == final_b0.content == "done"

        # shared infrastructure identity
        assert type(gate_b1) is type(gate_b0) is ExecutionAuthorizationGate
        assert type(budget_b1) is type(budget_b0) is RecoveryBudgetGate
        assert budget_b1.ledger == budget_b0.ledger  # same budget accounting

        # non-RECOVERY control events are identical
        def strip(events, kind):
            return [
                {k: v for k, v in e.items()
                 if k not in {"event_id", "route", "reason_code", "rationale",
                              "route_evidence", "evidence_grade", "state_change",
                              "state_summary", "action", "reason", "signal",
                              "retry_justified", "retry_justification_rule",
                              "budget_decision", "recovery_budget_before",
                              "recovery_budget_after", "model_calls_used",
                              "authorized", "decision_id", "route_policy_version"}}
                for e in events if e["kind"] == kind
            ]
        for kind in ("PROPOSED", "AUTHORIZED", "EXECUTED", "OBSERVED", "BLOCKED"):
            assert strip(gate_b1.control_events, kind) == strip(gate_b0.control_events, kind), kind

        # the ONLY difference is the declared route surface
        rec_b1 = events_of(gate_b1, ControlEventKind.RECOVERY)[0]
        rec_b0 = events_of(gate_b0, ControlEventKind.RECOVERY)[0]
        assert rec_b1["reason_code"] == "BOUNDED_VERIFICATION"
        assert rec_b0["reason_code"] == "STRATEGY_PASSTHROUGH"
        assert rec_b1["route"] == rec_b0["route"] == "RETRY_SAME"

    def test_b1_replans_where_b0_passes_through(self):
        """差异面即实验变量：永久失败时 B1 重规划、B0 直通（共享门兜底）。"""
        def scenario(route_mode):
            core, adapter = make_core(
                [
                    batch_action([("tool_a", {"n": 1}, "call-1")]),
                    batch_action([("tool_b", {"n": 2}, "call-2")]),
                    ModelAction(type="final_answer", content="done"),
                ],
                RetryStrategy(),
                route_mode=route_mode,
            )
            executions, final = run_loop(adapter, lambda n, a, i: (
                {"status": "error", "retryable": False} if n == "tool_a"
                else {"status": "success"}
            ))
            return core, adapter._gate, executions, final

        core_b1, gate_b1, exec_b1, final_b1 = scenario(ROUTE_MODE_V1)
        core_b0, gate_b0, exec_b0, final_b0 = scenario(ROUTE_MODE_BASELINE)

        # B1: REPLAN → model re-plans with a new action (tool_b) via the gate
        rec_b1 = events_of(gate_b1, ControlEventKind.RECOVERY)
        assert rec_b1[0]["route"] == "REPLAN"
        assert rec_b1[0]["reason_code"] == "PERMANENT_FAILURE_REPLAN"
        assert [e[0] for e in exec_b1] == ["tool_a", "tool_b"]
        assert final_b1.content == "done"

        # B0: passthrough retry → the SHARED gate refuses the identical
        # permanent-failure repeat (no bypass) and the run stops
        rec_b0 = events_of(gate_b0, ControlEventKind.RECOVERY)
        assert rec_b0[0]["route"] == "RETRY_SAME"
        assert rec_b0[0]["reason_code"] == "STRATEGY_PASSTHROUGH"
        assert [e[0] for e in exec_b0] == ["tool_a"]
        blocked = events_of(gate_b0, ControlEventKind.BLOCKED)
        assert blocked[0]["rule"] == "NO_PROGRESS_RETRY"
        assert final_b0.content.startswith("TERMINATED")

    def test_authorization_gate_identical_in_both_modes(self):
        """授权安全门在两种模式下行为完全一致（booking 场景）。"""
        def scenario(route_mode):
            core, adapter = make_core(
                [
                    batch_action([
                        ("build_flight_booking_request", {"f": "A"}, "call-1"),
                        ("confirm_held_recommendation_order", {"o": "O"}, "call-2"),
                    ]),
                    ModelAction(type="final_answer", content="done"),
                ],
                RetryStrategy(),
                route_mode=route_mode,
            )
            executions, _ = run_loop(adapter, lambda n, a, i: {"booking_ready": False})
            return executions, adapter._gate

        exec_b1, gate_b1 = scenario(ROUTE_MODE_V1)
        exec_b0, gate_b0 = scenario(ROUTE_MODE_BASELINE)
        assert exec_b1 == exec_b0 == [("build_flight_booking_request", {"f": "A"})]
        blocked_b1 = [(e["tool_name"], e["rule"]) for e in events_of(gate_b1, ControlEventKind.BLOCKED)]
        blocked_b0 = [(e["tool_name"], e["rule"]) for e in events_of(gate_b0, ControlEventKind.BLOCKED)]
        assert blocked_b1 == blocked_b0 == [("confirm_held_recommendation_order",
                                             "DEPENDENCY_PRECONDITION_UNRESOLVED")]

    def test_replan_output_still_gated_in_b1(self):
        core, adapter = make_core(
            [
                batch_action([("tool_a", {"n": 1}, "call-1")]),
                batch_action([
                    ("build_flight_booking_request", {"f": "A"}, "call-2"),
                    ("confirm_held_recommendation_order", {"o": "O"}, "call-3"),
                ]),
                ModelAction(type="final_answer", content="done"),
            ],
            RetryStrategy(),
            route_mode=ROUTE_MODE_V1,
        )
        executions, _ = run_loop(adapter, lambda n, a, i: (
            {"status": "error", "retryable": False} if n == "tool_a"
            else {"booking_ready": False}
        ))
        assert "confirm_held_recommendation_order" not in [e[0] for e in executions]
        assert events_of(adapter._gate, ControlEventKind.BLOCKED)


# ══════════════════════════════════════════════════════════════════════
# 3. Switch effectiveness at the canonical executor (fake client)
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


class TestCanonicalExecutorParity:
    def _run_trial(self, route_mode):
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
            experiment_id=f"phase5-readiness-{route_mode}",
            task_id="C1_task_036_P4",
            route_mode=route_mode,
        )
        return result, fake

    def test_route_mode_recorded_and_runs_are_identical_except_mode(self):
        pytest.importorskip("evaluation.core.sandbox")
        if not TASK_PATH.exists():
            pytest.skip("frozen ToolMaze task data not available")

        result_b1, fake_b1 = self._run_trial(ROUTE_MODE_V1)
        result_b0, fake_b0 = self._run_trial(ROUTE_MODE_BASELINE)

        # switch takes effect and is recorded
        assert result_b1.execution_provenance["route_mode"] == "route-v1"
        assert result_b0.execution_provenance["route_mode"] == "baseline"

        # no config leakage: everything else is identical
        assert (result_b1.derived_view == result_b0.derived_view)
        assert result_b1.validity == result_b0.validity
        assert (result_b1.execution_provenance["route_policy_fingerprint"]
                == result_b0.execution_provenance["route_policy_fingerprint"])
        assert (result_b1.execution_provenance["engine_round_guard"]
                == result_b0.execution_provenance["engine_round_guard"])
        assert (result_b1.grader_result.get("metrics_summary")
                == result_b0.grader_result.get("metrics_summary"))
        assert (result_b1.provider_usage["provider_request_count"]
                == result_b0.provider_usage["provider_request_count"] == 2)

        # identical control streams except the per-trial identity stamp
        def normalize(events):
            return [
                {k: v for k, v in e.items() if k not in {"run_id", "trial_id"}}
                for e in events
            ]
        assert normalize(result_b1.control_events) == normalize(result_b0.control_events)

        # no unauthorized provider calls in either arm
        assert len(fake_b1.chat.completions.requests) == 2
        assert len(fake_b0.chat.completions.requests) == 2
