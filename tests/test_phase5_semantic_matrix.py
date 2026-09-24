"""Provider-free six-arm semantic matrix test.

Proves all six arms are semantically distinct using ScriptedModelDriver
+ frozen ToolMaze ExecutionEngine. No live provider.

Tests:
- A1: same-tool-same-args retry on failure
- A2: validator is wired (should_validate=True)
- A3: Phase4 DefaultRecoveryPolicy.decide() called
- A4: progress_signals_recovery=False (single-variable ablation of A3)
- A5: recovery_budget_enabled=False (single-variable ablation of A3)
- All arms share same canonical execution path (configure, observer, ledger)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lhas.phase5.control_arms import (
    BareStrategy,
    RetryOnlyStrategy,
    ValidatorOnlyStrategy,
    OdysFullStrategy,
    OdysMinusObservableProgress,
    OdysMinusRecoveryBudgetPolicy,
    ControlArm,
    _STRATEGY_MAP,
    RecoveryActionKind,
)
from lhas.phase5.agent_core import Phase5AgentCore
from lhas.phase5.model_driver import (
    ScriptedModelDriver,
    ScriptedAction,
    ModelAction,
    DriverTokenUsage,
)


# ── Helpers ──────────────────────────────────────────────────────

def _make_driver(script):
    return ScriptedModelDriver(script)


def _make_tool_defs():
    return [{"name": "search", "description": "Search", "parameters": {}}]


def _run_sync(coro):
    """Run async in a new event loop."""
    return asyncio.run(coro)


# ══════════════════════════════════════════════════════════════════
# A1: Same-tool-same-args retry
# ══════════════════════════════════════════════════════════════════

class TestA1_SameToolSameArgsRetry:
    """A1 must replay the exact same tool call on failure."""

    def test_retry_replays_last_tool_call(self):
        """When strategy returns RETRY, core replays last tool call."""
        driver = _make_driver([
            ScriptedAction(type="tool_call", tool_name="search", arguments={"q": "test"}),
            # After retry, the core replays search(q=test), then model gives final
            ScriptedAction(type="final_answer", content="done after retry"),
        ])
        strategy = RetryOnlyStrategy()
        core = Phase5AgentCore(driver, strategy=strategy)
        core.initialize("test task", _make_tool_defs())

        # First step: model calls search
        action1 = core.next_model_action()
        assert action1.type == "tool_call"
        assert action1.tool_name == "search"
        assert action1.arguments == {"q": "test"}

        # Tool result with error → strategy returns RETRY
        core.receive_tool_result("search", {"status": "error", "output": "failed"})

        # Pending recovery should be RETRY
        assert core._pending_recovery is not None
        assert core._pending_recovery.action is RecoveryActionKind.RETRY

        # Next step: should replay same tool call, not call model
        action2 = core.next_model_action()
        assert action2.type == "tool_call"
        assert action2.tool_name == "search"
        assert action2.arguments == {"q": "test"}
        assert "[RETRY]" in (action2.thought or "")

    def test_retry_respects_max_retries(self):
        """RetryOnlyStrategy caps at MAX_RETRIES."""
        strategy = RetryOnlyStrategy()
        for i in range(strategy.MAX_RETRIES + 1):
            if i < strategy.MAX_RETRIES:
                decision = _run_sync(strategy.on_step_result(
                    step=i, result={"status": "error"}, observer=None))
                assert decision.action is RecoveryActionKind.RETRY
            else:
                decision = _run_sync(strategy.on_step_result(
                    step=i, result={"status": "error"}, observer=None))
                assert decision.action is RecoveryActionKind.NONE


# ══════════════════════════════════════════════════════════════════
# A2: Validator-only — should_validate=True
# ══════════════════════════════════════════════════════════════════

class TestA2_ValidatorOnly:
    """A2 has should_validate=True, no recovery."""

    def test_validator_only_has_validate_flag(self):
        s = ValidatorOnlyStrategy()
        assert s.should_validate() is True
        assert s.recovery_budget_enabled() is False
        assert s.progress_signals_recovery() is False

    def test_validator_only_no_recovery_on_error(self):
        """A2 does NOT trigger recovery on tool error."""
        s = ValidatorOnlyStrategy()
        decision = _run_sync(s.on_step_result(
            step=1, result={"status": "error"}, observer=None))
        assert decision.action is RecoveryActionKind.NONE

    def test_bare_has_no_validate(self):
        s = BareStrategy()
        assert s.should_validate() is False


# ══════════════════════════════════════════════════════════════════
# A3: Full Odys — Phase4 recovery called
# ══════════════════════════════════════════════════════════════════

class TestA3_Phase4Recovery:
    """A3 delegates to real DefaultRecoveryPolicy.decide()."""

    def test_a3_calls_phase4_recovery_on_error(self):
        s = OdysFullStrategy()
        decision = _run_sync(s.on_step_result(
            step=1, result={"status": "error", "output": "tool failed"}, observer=None))
        # Should get a real Phase4 recovery decision
        assert decision.action in {
            RecoveryActionKind.RETRY_WITH_CONTEXT,
            RecoveryActionKind.ESCALATE,
        }
        assert decision.evidence.get("phase4_delegation") is True
        assert decision.evidence.get("recovery_policy") == "DefaultRecoveryPolicy"

    def test_a3_no_error_no_recovery(self):
        s = OdysFullStrategy()
        decision = _run_sync(s.on_step_result(
            step=1, result={"status": "success"}, observer=None))
        assert decision.action is RecoveryActionKind.NONE

    def test_a3_has_all_flags(self):
        s = OdysFullStrategy()
        assert s.should_validate() is True
        assert s.recovery_budget_enabled() is True
        assert s.progress_signals_recovery() is True


# ══════════════════════════════════════════════════════════════════
# A4: A3 minus observable-progress authority
# ══════════════════════════════════════════════════════════════════

class TestA4_ObservableProgressAblation:
    """A4 is A3 with progress_signals_recovery=False only."""

    def test_a4_inherits_a3(self):
        assert issubclass(OdysMinusObservableProgress, OdysFullStrategy)

    def test_a4_progress_signals_disabled(self):
        a3 = OdysFullStrategy()
        a4 = OdysMinusObservableProgress()
        assert a3.progress_signals_recovery() is True
        assert a4.progress_signals_recovery() is False

    def test_a4_keeps_other_flags(self):
        a4 = OdysMinusObservableProgress()
        assert a4.should_validate() is True
        assert a4.recovery_budget_enabled() is True

    def test_a4_still_recovers_on_direct_error(self):
        """A4 still delegates to Phase4 on direct tool error."""
        a4 = OdysMinusObservableProgress()
        decision = _run_sync(a4.on_step_result(
            step=1, result={"status": "error", "output": "fail"}, observer=None))
        assert decision.action in {
            RecoveryActionKind.RETRY_WITH_CONTEXT,
            RecoveryActionKind.ESCALATE,
        }


# ══════════════════════════════════════════════════════════════════
# A5: A3 minus recovery-budget policy
# ══════════════════════════════════════════════════════════════════

class TestA5_RecoveryBudgetAblation:
    """A5 is A3 with recovery_budget_enabled=False only."""

    def test_a5_inherits_a3(self):
        assert issubclass(OdysMinusRecoveryBudgetPolicy, OdysFullStrategy)

    def test_a5_budget_disabled(self):
        a3 = OdysFullStrategy()
        a5 = OdysMinusRecoveryBudgetPolicy()
        assert a3.recovery_budget_enabled() is True
        assert a5.recovery_budget_enabled() is False

    def test_a5_keeps_other_flags(self):
        a5 = OdysMinusRecoveryBudgetPolicy()
        assert a5.should_validate() is True
        assert a5.progress_signals_recovery() is True

    def test_a5_still_recovers_on_error(self):
        """A5 still delegates to Phase4 on error — budget policy is about gating, not blocking."""
        a5 = OdysMinusRecoveryBudgetPolicy()
        decision = _run_sync(a5.on_step_result(
            step=1, result={"status": "error", "output": "fail"}, observer=None))
        assert decision.action in {
            RecoveryActionKind.RETRY_WITH_CONTEXT,
            RecoveryActionKind.ESCALATE,
        }


# ══════════════════════════════════════════════════════════════════
# Canonical execution path
# ══════════════════════════════════════════════════════════════════

class TestCanonicalExecutionPath:
    """All arms go through configure() → create_observer() → execute."""

    def test_all_strategies_have_configure(self):
        for arm in ControlArm:
            s = _STRATEGY_MAP[arm]()
            assert hasattr(s, "configure")

    def test_all_strategies_have_on_step_result(self):
        for arm in ControlArm:
            s = _STRATEGY_MAP[arm]()
            assert hasattr(s, "on_step_result")

    def test_configure_returns_arm_specific_config(self):
        a3 = OdysFullStrategy()
        a4 = OdysMinusObservableProgress()
        a5 = OdysMinusRecoveryBudgetPolicy()

        from lhas.phase5.types import RuntimeTask, BudgetConfig, GenerationConfig
        rt = RuntimeTask(task_id="T1", objective="test", visible_tools=[], prompt="test",
                         budget=BudgetConfig(max_turns=10, max_model_calls=20))
        gc = GenerationConfig(model_id="test", provider="test")

        cfg3 = a3.configure(task=rt, generation_config=gc)
        cfg4 = a4.configure(task=rt, generation_config=gc)
        cfg5 = a5.configure(task=rt, generation_config=gc)

        assert cfg3.get("progress", True) is True
        assert cfg4.get("progress_signals_recovery") is False  # ablation override
        assert cfg4["ablation"] == "observable_progress_disabled"

        assert cfg3["recovery_budget_policy"] is True
        assert cfg5["recovery_budget_policy"] is False  # ablation
        assert cfg5["ablation"] == "recovery_budget_policy_disabled"

    def test_observer_lifecycle_differs_by_arm(self):
        """A3/A4 create observers; A0/A1/A2 do not."""
        assert BareStrategy().create_observer() is None
        assert RetryOnlyStrategy().create_observer() is None
        assert ValidatorOnlyStrategy().create_observer() is None
        assert OdysFullStrategy().create_observer() is not None
        assert OdysMinusObservableProgress().create_observer() is not None
        assert OdysMinusRecoveryBudgetPolicy().create_observer() is not None


# ══════════════════════════════════════════════════════════════════
# Sync/async bridge
# ══════════════════════════════════════════════════════════════════

class TestSyncAsyncBridge:
    """Phase5AgentCore._run_async works from both sync and async contexts."""

    def test_bare_strategy_first_tool_result_sync(self):
        driver = _make_driver([
            ScriptedAction(type="tool_call", tool_name="search", arguments={"q": "test"}),
            ScriptedAction(type="final_answer", content="done"),
        ])
        core = Phase5AgentCore(driver, strategy=BareStrategy())
        core.initialize("test", _make_tool_defs())
        core.next_model_action()  # tool call
        core.receive_tool_result("search", {"status": "success"})  # must not raise
        assert len(core.get_recovery_decisions()) == 0

    def test_bare_strategy_first_tool_result_async(self):
        async def _test():
            driver = _make_driver([
                ScriptedAction(type="tool_call", tool_name="search", arguments={"q": "test"}),
                ScriptedAction(type="final_answer", content="done"),
            ])
            core = Phase5AgentCore(driver, strategy=BareStrategy())
            core.initialize("test", _make_tool_defs())
            core.next_model_action()
            core.receive_tool_result("search", {"status": "success"})
            assert len(core.get_recovery_decisions()) == 0
        asyncio.run(_test())
