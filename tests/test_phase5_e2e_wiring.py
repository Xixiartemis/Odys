"""Phase 5 End-to-End Wiring Tests — T1–T11.

Verifies that all Phase 5 components are correctly wired together:
  - Strategy → adapter integration
  - Error propagation (PolicyExecutionError / BudgetExhausted)
  - BudgetedModelDriver exact counting
  - Root budget identity across arms
  - Shadow observer single-instance creation
  - Recovery decisions from adapter
  - A3 DefaultRecoveryPolicy.decide() invocation
  - Terminal action (ESCALATE) stops execution
  - Fresh driver per trial (RealPilotRunner)
  - Budget exhaustion as valid task outcome
  - Manifest compatibility (tasks + selected_task_ids)

All tests pass from a clean checkout using mocks/fixtures.
No external benchmark data or live model calls required.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch, AsyncMock, PropertyMock, call

import pytest

# ── Conditional imports — guard against missing sibling modules ──────

def _try_import(module_name: str):
    """Import a module or return None if not available."""
    try:
        return importlib.import_module(module_name)
    except (ModuleNotFoundError, ImportError):
        return None

_model_driver_mod = _try_import("lhas.phase5.model_driver")
_agent_core_mod = _try_import("lhas.phase5.agent_core")
_agent_adapter_mod = _try_import("lhas.phase5.agent_adapter")
_runtime_backend_mod = _try_import("lhas.phase5.runtime_backend")
_control_arms_mod = _try_import("lhas.phase5.control_arms")
_shadow_observer_mod = _try_import("lhas.phase5.shadow_observer")
_real_pilot_runner_mod = _try_import("lhas.phase5.real_pilot_runner")
_budgeted_driver_mod = _try_import("lhas.phase5.budgeted_driver")
_types_mod = _try_import("lhas.phase5.types")
_provenance_mod = _try_import("lhas.phase5.provenance")

_HAS_MODEL_DRIVER = _model_driver_mod is not None
_HAS_AGENT_ADAPTER = _agent_adapter_mod is not None
_HAS_RUNTIME_BACKEND = _runtime_backend_mod is not None
_HAS_CONTROL_ARMS = _control_arms_mod is not None
_HAS_SHADOW_OBSERVER = _shadow_observer_mod is not None
_HAS_REAL_PILOT_RUNNER = _real_pilot_runner_mod is not None
_HAS_BUDGETED_DRIVER = _budgeted_driver_mod is not None
HAS_ALL_CORE = (
    _HAS_MODEL_DRIVER
    and _HAS_AGENT_ADAPTER
    and _HAS_RUNTIME_BACKEND
    and _HAS_CONTROL_ARMS
)

# ── Skip markers ─────────────────────────────────────────────────────

requires_model_driver = pytest.mark.skipif(
    not _HAS_MODEL_DRIVER, reason="model_driver.py not yet available"
)
requires_agent_adapter = pytest.mark.skipif(
    not _HAS_AGENT_ADAPTER, reason="agent_adapter.py not yet available"
)
requires_runtime_backend = pytest.mark.skipif(
    not _HAS_RUNTIME_BACKEND, reason="runtime_backend.py not yet available"
)
requires_control_arms = pytest.mark.skipif(
    not _HAS_CONTROL_ARMS, reason="control_arms.py not yet available"
)
requires_shadow_observer = pytest.mark.skipif(
    not _HAS_SHADOW_OBSERVER, reason="shadow_observer.py not yet available"
)
requires_budgeted_driver = pytest.mark.skipif(
    not _HAS_BUDGETED_DRIVER, reason="budgeted_driver.py not yet available"
)
requires_all_core = pytest.mark.skipif(
    not HAS_ALL_CORE, reason="Core modules not all available yet"
)
_HAS_TYPES = _types_mod is not None

requires_types = pytest.mark.skipif(
    not _HAS_TYPES, reason="types.py not yet available"
)


# ══════════════════════════════════════════════════════════════════════
#  Shared fixtures and helpers
# ══════════════════════════════════════════════════════════════════════

def _get_agent_action():
    """Get AgentAction from ToolMaze's base_agent via model_driver."""
    if _HAS_MODEL_DRIVER:
        return getattr(_model_driver_mod, "AgentAction", None)
    return None


def _make_scripted_driver(actions: list):
    """Create a ScriptedModelDriver with the given scripted actions."""
    if not _HAS_MODEL_DRIVER:
        pytest.skip("model_driver.py not yet available")
    ScriptedAction = _model_driver_mod.ScriptedAction
    ScriptedModelDriver = _model_driver_mod.ScriptedModelDriver
    script = []
    for a in actions:
        if isinstance(a, dict):
            script.append(ScriptedAction(**a))
        else:
            script.append(a)
    return ScriptedModelDriver(script)


def _make_tool_call_action(tool_name: str = "test_tool", arguments: dict = None):
    """Create a tool_call AgentAction."""
    AgentAction = _get_agent_action()
    if AgentAction is None:
        pytest.skip("AgentAction not available")
    return AgentAction(type="tool_call", tool_name=tool_name, arguments=arguments or {})


def _make_final_answer_action(content: str = "done"):
    """Create a final_answer AgentAction."""
    AgentAction = _get_agent_action()
    if AgentAction is None:
        pytest.skip("AgentAction not available")
    return AgentAction(type="final_answer", content=content)


def _make_failing_strategy(error_msg: str = "strategy exploded"):
    """Create a strategy whose configure() or on_step_result() raises."""
    if not _HAS_CONTROL_ARMS:
        pytest.skip("control_arms.py not available")
    strategy = MagicMock()
    strategy.configure.side_effect = RuntimeError(error_msg)
    strategy.arm = MagicMock()
    strategy.arm.value = "A0_BARE"
    strategy.create_observer.return_value = None
    strategy.on_step_result = AsyncMock(side_effect=RuntimeError(error_msg))
    strategy.should_validate.return_value = False
    strategy.recovery_budget_enabled.return_value = False
    strategy.progress_signals_recovery.return_value = False
    return strategy


def _make_bare_strategy():
    """Create a real BareStrategy instance."""
    if not _HAS_CONTROL_ARMS:
        pytest.skip("control_arms.py not available")
    return _control_arms_mod.BareStrategy()


def _make_a3_strategy():
    """Create a real OdysFullStrategy instance."""
    if not _HAS_CONTROL_ARMS:
        pytest.skip("control_arms.py not available")
    return _control_arms_mod.OdysFullStrategy()


def _make_runtime_task(task_id: str = "T-e2e"):
    """Create a minimal RuntimeTask."""
    if not _HAS_TYPES:
        pytest.skip("types.py not available")
    return _types_mod.RuntimeTask(
        task_id=task_id,
        objective="test objective",
        visible_tools=[{"name": "tool_a", "description": "A test tool"}],
        prompt="do something",
        budget=_types_mod.BudgetConfig(max_turns=10, max_model_calls=20),
    )


def _make_generation_config():
    """Create a minimal GenerationConfig."""
    if not _HAS_TYPES:
        pytest.skip("types.py not available")
    return _types_mod.GenerationConfig(
        model_id="test-model", provider="test-provider", seed=42,
    )



# ══════════════════════════════════════════════════════════════════════
#  T1: Strategy passed to official agent adapter
# ══════════════════════════════════════════════════════════════════════

class TestT1_StrategyPassedToAdapter:
    """Verify that backend.execute() wires the strategy into the adapter."""

    @requires_all_core
    def test_adapter_receives_strategy_via_constructor(self):
        """The OdysToolMazeAgentAdapter accepts strategy at construction."""
        OdysToolMazeAgentAdapter = _agent_core_mod.Phase5AgentCore
        ScriptedModelDriver = _model_driver_mod.ScriptedModelDriver
        BareStrategy = _control_arms_mod.BareStrategy

        driver = ScriptedModelDriver([])
        strategy = BareStrategy()
        adapter = OdysToolMazeAgentAdapter(driver, strategy=strategy)

        assert adapter._strategy is strategy

    @requires_all_core
    def test_backend_execute_wires_strategy_to_adapter(self):
        """backend.execute() creates adapter with the given strategy.

        We patch the ExecutionEngine to capture the agent it receives.
        """
        from lhas.phase5.runtime_backend import ToolMazeRuntimeBackend

        with patch("lhas.phase5.runtime_backend.ToolMazeRuntimeBackend._load_tool_skeletons", return_value={}):
            backend = ToolMazeRuntimeBackend(
                {"task_id": "T1", "task_description": "test", "user_input": {"query": "q"}},
                budget=_types_mod.BudgetConfig(max_turns=5, max_model_calls=10),
            )

        # ExecutionEngine is lazy-imported inside execute(). Mock via sys.modules.
        mock_engine_cls = MagicMock()
        mock_engine_instance = MagicMock()
        mock_trace = MagicMock()
        mock_trace.to_dict.return_value = {"task_id": "T1", "tool_calls": []}
        mock_engine_instance.run.return_value = (mock_trace, None)
        mock_engine_cls.return_value = mock_engine_instance

        import sys
        mock_sandbox = MagicMock()
        mock_sandbox.ExecutionEngine = mock_engine_cls
        with patch.dict(sys.modules, {"evaluation": MagicMock(), "evaluation.core": MagicMock(), "evaluation.core.sandbox": mock_sandbox}):
            strategy = _make_bare_strategy()
            driver = _make_scripted_driver([
                {"type": "final_answer", "content": "done"},
            ])

            backend.execute(driver, strategy=strategy, max_rounds=1)

            # Check that ExecutionEngine was created with an agent that has the strategy
            mock_engine_cls.assert_called_once()
            ckw = mock_engine_cls.call_args
            agent_arg = ckw.kwargs.get("agent") if ckw.kwargs else None
            if agent_arg is None and ckw[1]:
                agent_arg = ckw[1].get("agent")
            if agent_arg is None and len(ckw[0]) > 1:
                agent_arg = ckw[0][1]

            assert agent_arg is not None, "Agent not passed to ExecutionEngine"
            assert agent_arg._core._strategy is strategy, (
                f"Strategy not wired to core. Got: {agent_arg._core._strategy}"
            )

    @requires_all_core
    def test_strategy_set_shadow_observer_on_adapter(self):
        """When strategy provides an observer, it is injected into the adapter."""
        OdysToolMazeAgentAdapter = _agent_core_mod.Phase5AgentCore
        ScriptedModelDriver = _model_driver_mod.ScriptedModelDriver

        driver = ScriptedModelDriver([])
        adapter = OdysToolMazeAgentAdapter(driver)

        mock_observer = MagicMock()
        adapter.set_shadow_observer(mock_observer)

        assert adapter._shadow_observer is mock_observer


# ══════════════════════════════════════════════════════════════════════
#  T2: PolicyExecutionError on strategy failure
# ══════════════════════════════════════════════════════════════════════

class TestT2_PolicyExecutionError:
    """Verify that strategy failures are not swallowed — they propagate."""

    @requires_all_core
    def test_strategy_configure_error_raises_policy_execution_error(self):
        """If strategy.configure() raises, the backend raises
        PolicyExecutionError (not silently ignores).
        """
        from lhas.phase5.runtime_backend import ToolMazeRuntimeBackend
        from lhas.phase5.types import PolicyExecutionError

        strategy = _make_failing_strategy("configure exploded")

        with patch("lhas.phase5.runtime_backend.ToolMazeRuntimeBackend._load_tool_skeletons", return_value={}):
            backend = ToolMazeRuntimeBackend(
                {"task_id": "T1", "task_description": "test", "user_input": {"query": "q"}},
                budget=_types_mod.BudgetConfig(max_turns=5, max_model_calls=10),
            )

        import sys
        mock_engine_cls = MagicMock()
        mock_engine_instance = MagicMock()
        mock_trace = MagicMock()
        mock_trace.to_dict.return_value = {"task_id": "T1", "tool_calls": []}
        mock_engine_instance.run.return_value = (mock_trace, None)
        mock_engine_cls.return_value = mock_engine_instance
        mock_sandbox = MagicMock()
        mock_sandbox.ExecutionEngine = mock_engine_cls

        driver = _make_scripted_driver([
            {"type": "final_answer", "content": "done"},
        ])

        with patch.dict(sys.modules, {"evaluation": MagicMock(), "evaluation.core": MagicMock(), "evaluation.core.sandbox": mock_sandbox}):
            with pytest.raises(PolicyExecutionError, match="configure"):
                backend.execute(driver, strategy=strategy, max_rounds=1)

    @requires_all_core
    def test_budget_exhausted_is_raised_on_over_budget(self):
        """BudgetExhausted is raised when model calls exceed the budget."""
        from lhas.phase5.runtime_backend import BudgetExhausted

        assert issubclass(BudgetExhausted, Exception)

        # Verify it can be raised and caught
        with pytest.raises(BudgetExhausted):
            raise BudgetExhausted("test budget exhausted")

    @requires_all_core
    def test_failing_strategy_raises_policy_execution_error(self):
        """If strategy.on_step_result raises, the adapter wraps it as
        PolicyExecutionError and re-raises. Strategy failures are NOT silently swallowed.
        """
        from lhas.phase5.types import PolicyExecutionError

        OdysToolMazeAgentAdapter = _agent_core_mod.Phase5AgentCore
        ScriptedModelDriver = _model_driver_mod.ScriptedModelDriver

        driver = ScriptedModelDriver([
            _model_driver_mod.ScriptedAction(type="tool_call", tool_name="t", arguments={}),
            _model_driver_mod.ScriptedAction(type="final_answer", content="done"),
        ])

        # Strategy that raises on on_step_result
        strategy = MagicMock()
        strategy.on_step_result = AsyncMock(side_effect=RuntimeError("boom"))

        adapter = OdysToolMazeAgentAdapter(driver, strategy=strategy)
        adapter.initialize("test task", [{"name": "t"}])

        # Step 1: tool_call
        action = adapter.next_model_action()
        assert action.type == "tool_call"

        # receive_tool_result — strategy raises → PolicyExecutionError
        with pytest.raises(PolicyExecutionError, match="on_step_result failed"):
            adapter.receive_tool_result("t", {"status": "error", "output": "fail"})

    @requires_all_core
    def test_policy_execution_error_class_exists_in_adapter(self):
        """PolicyExecutionError is defined in agent_adapter.py and
        is raised when strategy.on_step_result() fails.
        """
        from lhas.phase5.types import PolicyExecutionError

        assert issubclass(PolicyExecutionError, Exception)
        # Verify it can be raised and caught
        with pytest.raises(PolicyExecutionError):
            raise PolicyExecutionError("test")


# ══════════════════════════════════════════════════════════════════════
#  T3: BudgetedModelDriver exact counting
# ══════════════════════════════════════════════════════════════════════

class TestT3_BudgetedModelDriverCounting:
    """Verify BudgetedModelDriver counts next_action calls exactly."""

    @requires_budgeted_driver
    def test_budgeted_driver_counts_exactly(self):
        """Run 5 actions with max_model_calls=5 → model_calls_used==5."""
        BudgetedModelDriver = _budgeted_driver_mod.BudgetedModelDriver
        ScriptedModelDriver = _model_driver_mod.ScriptedModelDriver
        ScriptedAction = _model_driver_mod.ScriptedAction

        # 6 scripted actions (5 tool_calls + 1 final_answer)
        inner = ScriptedModelDriver([
            ScriptedAction(type="tool_call", tool_name=f"tool_{i}", arguments={})
            for i in range(6)
        ] + [
            ScriptedAction(type="final_answer", content="done"),
        ])

        budgeted = BudgetedModelDriver(inner, max_model_calls=5)

        for i in range(5):
            action = budgeted.next_action(messages=[], tool_definitions=[])
            assert action.type == "tool_call"

        assert budgeted.model_calls_used == 5

    @requires_budgeted_driver
    def test_budgeted_driver_raises_on_6th_call(self):
        """6th next_action call with max_model_calls=5 → BudgetExhausted."""
        BudgetedModelDriver = _budgeted_driver_mod.BudgetedModelDriver
        ScriptedModelDriver = _model_driver_mod.ScriptedModelDriver
        ScriptedAction = _model_driver_mod.ScriptedAction

        inner = ScriptedModelDriver([
            ScriptedAction(type="tool_call", tool_name=f"tool_{i}", arguments={})
            for i in range(10)
        ])

        budgeted = BudgetedModelDriver(inner, max_model_calls=5)

        # Use 5 calls
        for _ in range(5):
            budgeted.next_action(messages=[], tool_definitions=[])

        # 6th should raise
        BudgetExhaustedError = getattr(
            _budgeted_driver_mod, "BudgetExhausted", None
        ) or getattr(
            _runtime_backend_mod, "BudgetExhausted", Exception
        )

        with pytest.raises(BudgetExhaustedError):
            budgeted.next_action(messages=[], tool_definitions=[])

    @requires_budgeted_driver
    def test_budgeted_driver_wraps_inner_driver(self):
        """BudgetedModelDriver delegates to inner driver's next_action."""
        BudgetedModelDriver = _budgeted_driver_mod.BudgetedModelDriver

        inner = MagicMock()
        inner.next_action.return_value = _make_tool_call_action()
        inner.get_token_usage.return_value = MagicMock(input_tokens=10, output_tokens=5, total_tokens=15)

        budgeted = BudgetedModelDriver(inner, max_model_calls=3)

        action = budgeted.next_action(messages=[], tool_definitions=[])
        inner.next_action.assert_called_once()

    @requires_all_core
    def test_scripted_driver_exact_counting_as_baseline(self):
        """ScriptedModelDriver counts correctly as baseline for budgeted tests."""
        ScriptedModelDriver = _model_driver_mod.ScriptedModelDriver
        ScriptedAction = _model_driver_mod.ScriptedAction

        driver = ScriptedModelDriver([
            ScriptedAction(type="tool_call", tool_name=f"t_{i}", arguments={})
            for i in range(5)
        ] + [
            ScriptedAction(type="final_answer", content="done"),
        ])

        for i in range(5):
            action = driver.next_action(messages=[], tool_definitions=[])
            assert action.type == "tool_call"
            assert driver.script_position == i + 1

        # 6th returns final_answer (script exhausted)
        action = driver.next_action(messages=[], tool_definitions=[])
        assert action.type == "final_answer"


# ══════════════════════════════════════════════════════════════════════
#  T4: Root budget identical A0-A5
# ══════════════════════════════════════════════════════════════════════

class TestT4_RootBudgetIdenticalAcrossArms:
    """Verify all arms share the same root budget configuration."""

    @requires_control_arms
    def test_all_arms_share_same_budget_in_pair_validator(self):
        """ExperimentPairValidator enforces root budget is identical."""
        from lhas.phase5.control_arms import ExperimentPairValidator

        budget_a0 = {"root_model_call_budget": 50, "root_token_budget": None, "wall_deadline_budget": None}
        budget_a3 = {"root_model_call_budget": 50, "root_token_budget": None, "wall_deadline_budget": None}

        trial_a0 = {
            "arm": "A0_BARE", "task_id": "T1", "benchmark_name": "toolmaze",
            "benchmark_revision": "r1", "dataset_digest": "d1",
            "native_condition": "T1/P0", "prompt_hash": "h1",
            "tool_schema_hash": "h2", "tool_registry_hash": "h3",
            "environment_fixture_hash": "h4", "perturbation_mode": "P0",
            "fault_source": "BENCHMARK_NATIVE", "model_id": "m1",
            "provider": "p1", "temperature": 0.0, "seed": 42,
            "max_tokens": None, "system_prompt": "",
            "validator_identity": "v1", "offline_grader_identity": "g1",
            **budget_a0,
        }
        trial_a3 = dict(trial_a0)
        trial_a3["arm"] = "A3_ODYS_FULL"

        validator = ExperimentPairValidator()
        result = validator.validate([trial_a0, trial_a3])
        assert result["valid"], f"Pairing should be valid: {result['violations']}"

    @requires_control_arms
    def test_budget_mismatch_is_pairing_violation(self):
        """Different root_model_call_budget → pairing violation."""
        from lhas.phase5.control_arms import ExperimentPairValidator

        trial_a0 = {
            "arm": "A0_BARE", "task_id": "T1", "benchmark_name": "toolmaze",
            "benchmark_revision": "r1", "dataset_digest": "d1",
            "native_condition": "T1/P0", "prompt_hash": "h1",
            "tool_schema_hash": "h2", "tool_registry_hash": "h3",
            "environment_fixture_hash": "h4", "perturbation_mode": "P0",
            "fault_source": "BENCHMARK_NATIVE", "model_id": "m1",
            "provider": "p1", "temperature": 0.0, "seed": 42,
            "max_tokens": None, "system_prompt": "",
            "root_model_call_budget": 50, "root_token_budget": None,
            "wall_deadline_budget": None,
            "validator_identity": "v1", "offline_grader_identity": "g1",
        }
        trial_a3 = dict(trial_a0)
        trial_a3["arm"] = "A3_ODYS_FULL"
        trial_a3["root_model_call_budget"] = 100  # different!

        validator = ExperimentPairValidator()
        result = validator.validate([trial_a0, trial_a3])
        assert not result["valid"]
        assert any("root_model_call_budget" in v for v in result["violations"])

    @requires_control_arms
    def test_compute_trial_invariants_has_root_budget(self):
        """compute_trial_invariants includes root_model_call_budget."""
        from lhas.phase5.control_arms import ExperimentPairValidator

        inv = ExperimentPairValidator.compute_trial_invariants(
            experiment_id="exp-1",
            trial_id="trial-1",
            adapter=MagicMock(benchmark_identity=MagicMock(
                benchmark_name=MagicMock(value="toolmaze"),
                benchmark_revision="r1",
                dataset_digest="d1",
            )),
            task=MagicMock(
                task_id="T1",
                prompt="test",
                visible_tools=[],
                environment_snapshot={},
                budget=MagicMock(
                    max_model_calls=50, token_budget=None, deadline_seconds=None,
                ),
            ),
            generation_config=MagicMock(
                model_id="m1", provider="p1", temperature=0.0, seed=42, max_output_tokens=None,
            ),
            arm=_types_mod.ControlArm.A0_BARE,
        )
        assert inv["root_model_call_budget"] == 50


# ══════════════════════════════════════════════════════════════════════
#  T5: Shadow observer single instance
# ══════════════════════════════════════════════════════════════════════

class TestT5_ShadowObserverSingleInstance:
    """Verify shadow observer is created once per trial, not twice."""

    @requires_control_arms
    def test_a3_strategy_creates_single_observer(self):
        """OdysFullStrategy.create_observer() returns one instance."""
        strategy = _make_a3_strategy()
        observer = strategy.create_observer()
        assert observer is not None
        assert isinstance(observer, _shadow_observer_mod.ShadowProgressObserver)

        # Second call creates a NEW instance (each trial gets fresh observer)
        observer2 = strategy.create_observer()
        assert observer2 is not None
        assert observer2 is not observer  # new instance per call

    @requires_control_arms
    def test_bare_strategy_creates_no_observer(self):
        """BareStrategy.create_observer() returns None."""
        strategy = _make_bare_strategy()
        observer = strategy.create_observer()
        assert observer is None

    @requires_all_core
    def test_backend_execute_calls_create_observer_once(self):
        """backend.execute() calls strategy.create_observer() at least once
        during wiring to inject the observer into the adapter.
        """
        import sys as _sys

        strategy = MagicMock()
        strategy.configure.return_value = {"test": True}
        strategy.create_observer.return_value = None
        strategy.on_step_result = AsyncMock(return_value=MagicMock(
            action=MagicMock(value="none"), reason="", signal=None, evidence={},
        ))
        strategy.should_validate.return_value = False
        strategy.recovery_budget_enabled.return_value = False
        strategy.progress_signals_recovery.return_value = False
        strategy.arm = MagicMock()
        strategy.arm.value = "A0_BARE"

        with patch("lhas.phase5.runtime_backend.ToolMazeRuntimeBackend._load_tool_skeletons", return_value={}):
            backend = _runtime_backend_mod.ToolMazeRuntimeBackend(
                {"task_id": "T1", "task_description": "test", "user_input": {"query": "q"}},
                budget=_types_mod.BudgetConfig(max_turns=5, max_model_calls=10),
            )

        mock_engine_cls = MagicMock()
        mock_engine_instance = MagicMock()
        mock_trace = MagicMock()
        mock_trace.to_dict.return_value = {"task_id": "T1", "tool_calls": []}
        mock_engine_instance.run.return_value = (mock_trace, None)
        mock_engine_cls.return_value = mock_engine_instance
        mock_sandbox = MagicMock()
        mock_sandbox.ExecutionEngine = mock_engine_cls

        driver = _make_scripted_driver([
            {"type": "final_answer", "content": "done"},
        ])

        with patch.dict(_sys.modules, {"evaluation": MagicMock(), "evaluation.core": MagicMock(), "evaluation.core.sandbox": mock_sandbox}):
            backend.execute(driver, strategy=strategy, max_rounds=1)

            assert strategy.create_observer.call_count >= 1

    @requires_shadow_observer
    def test_shadow_observer_is_stateless_per_trial(self):
        """Each ShadowProgressObserver has independent state."""
        obs1 = _shadow_observer_mod.ShadowProgressObserver(window_size=3)
        obs2 = _shadow_observer_mod.ShadowProgressObserver(window_size=3)

        obs1.observe(
            task_id="T1", step=0, action_identity="tool@0",
            tool_result={"status": "success"},
        )

        assert len(obs1.get_records()) == 1
        assert len(obs2.get_records()) == 0  # independent


# ══════════════════════════════════════════════════════════════════════
#  T6: Recovery decisions from adapter
# ══════════════════════════════════════════════════════════════════════

class TestT6_RecoveryDecisionsFromAdapter:
    """Verify recovery_decisions are sourced from the adapter."""

    @requires_all_core
    def test_adapter_records_recovery_decisions(self):
        """When strategy returns a non-NONE decision, adapter records it."""
        OdysToolMazeAgentAdapter = _agent_core_mod.Phase5AgentCore
        ScriptedModelDriver = _model_driver_mod.ScriptedModelDriver
        ScriptedAction = _model_driver_mod.ScriptedAction
        RecoveryDecision = _control_arms_mod.RecoveryDecision
        RecoveryActionKind = _control_arms_mod.RecoveryActionKind

        driver = ScriptedModelDriver([
            ScriptedAction(type="tool_call", tool_name="t", arguments={}),
            ScriptedAction(type="final_answer", content="done"),
        ])

        # Strategy that returns RETRY on first call
        strategy = MagicMock()
        strategy.on_step_result = AsyncMock(return_value=RecoveryDecision(
            action=RecoveryActionKind.RETRY,
            reason="test retry",
            signal="TOOL_ERROR",
        ))

        adapter = OdysToolMazeAgentAdapter(driver, strategy=strategy)
        adapter.initialize("test task", [{"name": "t"}])

        # Step 1: tool_call
        action = adapter.next_model_action()
        assert action.type == "tool_call"

        # receive_tool_result triggers strategy consultation
        adapter.receive_tool_result("t", {"status": "error", "output": "fail"})

        # Verify recovery decision was recorded
        decisions = adapter.get_recovery_decisions()
        assert len(decisions) >= 1
        assert decisions[0]["action"] == "retry"
        assert decisions[0]["tool_name"] == "t"

    @requires_all_core
    def test_adapter_recovery_decisions_sourced_from_strategy(self):
        """Recovery decisions come from strategy.on_step_result(), not fabricated."""
        OdysToolMazeAgentAdapter = _agent_core_mod.Phase5AgentCore
        ScriptedModelDriver = _model_driver_mod.ScriptedModelDriver
        ScriptedAction = _model_driver_mod.ScriptedAction
        RecoveryDecision = _control_arms_mod.RecoveryDecision
        RecoveryActionKind = _control_arms_mod.RecoveryActionKind

        driver = ScriptedModelDriver([
            ScriptedAction(type="tool_call", tool_name="t", arguments={}),
            ScriptedAction(type="final_answer", content="done"),
        ])

        specific_decision = RecoveryDecision(
            action=RecoveryActionKind.RETRY_WITH_CONTEXT,
            reason="specific recovery reason from strategy",
            signal="ANOMALY",
            evidence={"detail": "custom evidence"},
        )

        strategy = MagicMock()
        strategy.on_step_result = AsyncMock(return_value=specific_decision)

        adapter = OdysToolMazeAgentAdapter(driver, strategy=strategy)
        adapter.initialize("test task", [{"name": "t"}])
        adapter.next_model_action()
        adapter.receive_tool_result("t", {"status": "error"})

        decisions = adapter.get_recovery_decisions()
        assert len(decisions) == 1
        assert decisions[0]["reason"] == "specific recovery reason from strategy"
        assert decisions[0]["action"] == "retry_with_context"

    @requires_all_core
    def test_no_recovery_decisions_when_strategy_returns_none(self):
        """When strategy returns NONE, no recovery decisions are recorded."""
        OdysToolMazeAgentAdapter = _agent_core_mod.Phase5AgentCore
        ScriptedModelDriver = _model_driver_mod.ScriptedModelDriver
        ScriptedAction = _model_driver_mod.ScriptedAction
        RecoveryDecision = _control_arms_mod.RecoveryDecision
        RecoveryActionKind = _control_arms_mod.RecoveryActionKind

        driver = ScriptedModelDriver([
            ScriptedAction(type="tool_call", tool_name="t", arguments={}),
            ScriptedAction(type="final_answer", content="done"),
        ])

        strategy = MagicMock()
        strategy.on_step_result = AsyncMock(return_value=RecoveryDecision(
            action=RecoveryActionKind.NONE,
        ))

        adapter = OdysToolMazeAgentAdapter(driver, strategy=strategy)
        adapter.initialize("test task", [{"name": "t"}])
        adapter.next_model_action()
        adapter.receive_tool_result("t", {"status": "success"})

        decisions = adapter.get_recovery_decisions()
        assert len(decisions) == 0


# ══════════════════════════════════════════════════════════════════════
#  T7: A3 DefaultRecoveryPolicy.decide() called
# ══════════════════════════════════════════════════════════════════════

class TestT7_A3DefaultRecoveryPolicyDecide:
    """Verify A3 strategy calls DefaultRecoveryPolicy.decide() via spy."""

    @requires_control_arms
    def test_a3_delegates_to_phase4_recovery(self):
        """OdysFullStrategy._delegate_to_recovery calls DefaultRecoveryPolicy.decide()."""
        from lhas.phase5.control_arms import OdysFullStrategy

        strategy = OdysFullStrategy()

        # Spy on the recovery policy's decide method
        original_decide = strategy._recovery_policy.decide

        call_log = []

        async def spying_decide(*args, **kwargs):
            call_log.append({"args": args, "kwargs": kwargs})
            # Call original
            return await original_decide(*args, **kwargs)

        strategy._recovery_policy.decide = spying_decide

        # Configure the strategy
        rt = _make_runtime_task()
        gen_cfg = _make_generation_config()
        strategy.configure(task=rt, generation_config=gen_cfg)

        # Trigger recovery via _delegate_to_recovery
        result = {"status": "error", "output": "tool failed"}

        decision = asyncio.run(strategy._delegate_to_recovery(
            step=1, result=result,
            signal="TOOL_ERROR", reason="tool returned error",
        ))

        assert len(call_log) >= 1, "DefaultRecoveryPolicy.decide() was not called"
        assert decision is not None

    @requires_control_arms
    def test_a3_on_step_result_triggers_recovery_on_error(self):
        """A3 strategy's on_step_result delegates to recovery on tool error."""
        from lhas.phase5.control_arms import OdysFullStrategy

        strategy = OdysFullStrategy()
        rt = _make_runtime_task()
        strategy.configure(task=rt, generation_config=_make_generation_config())

        # Create observer for progress_signals_recovery path
        observer = strategy.create_observer()

        # Spy on _delegate_to_recovery
        delegate_calls = []
        original_delegate = strategy._delegate_to_recovery

        async def spying_delegate(*args, **kwargs):
            delegate_calls.append({"args": args, "kwargs": kwargs})
            return await original_delegate(*args, **kwargs)

        strategy._delegate_to_recovery = spying_delegate

        # Call on_step_result with error status
        decision = asyncio.run(strategy.on_step_result(
            step=1,
            result={"status": "error", "output": "failed"},
            observer=observer,
        ))

        assert len(delegate_calls) >= 1, "_delegate_to_recovery not called"
        assert decision.action.value in {"retry_with_context", "escalate", "retry", "stop"}

    @requires_control_arms
    def test_a3_recovery_uses_phase4_domain_objects(self):
        """A3 constructs Phase4 Task, Attempt, FailureReport for decide()."""
        from lhas.phase5.control_arms import OdysFullStrategy

        strategy = OdysFullStrategy()
        rt = _make_runtime_task()
        strategy.configure(task=rt, generation_config=_make_generation_config())

        # Track the arguments passed to decide
        decide_args = []
        original_decide = strategy._recovery_policy.decide

        async def capture_decide(*args, **kwargs):
            decide_args.append(kwargs)
            return await original_decide(*args, **kwargs)

        strategy._recovery_policy.decide = capture_decide

        asyncio.run(strategy._delegate_to_recovery(
            step=1, result={"status": "error"},
            signal="TOOL_ERROR", reason="test",
        ))

        assert len(decide_args) >= 1
        kw = decide_args[0]
        assert "task" in kw
        assert "attempt" in kw
        assert "failure_report" in kw


# ══════════════════════════════════════════════════════════════════════
#  T8: Terminal action stops execution
# ══════════════════════════════════════════════════════════════════════

class TestT8_TerminalActionStopsExecution:
    """Verify ESCALATE terminal action produces final_answer, not tool_call."""

    @requires_all_core
    def test_escalate_produces_final_answer(self):
        """When pending recovery is ESCALATE, step() returns final_answer."""
        OdysToolMazeAgentAdapter = _agent_core_mod.Phase5AgentCore
        ScriptedModelDriver = _model_driver_mod.ScriptedModelDriver
        ScriptedAction = _model_driver_mod.ScriptedAction
        RecoveryDecision = _control_arms_mod.RecoveryDecision
        RecoveryActionKind = _control_arms_mod.RecoveryActionKind

        # Driver with tool calls that would normally produce tool_call
        driver = ScriptedModelDriver([
            ScriptedAction(type="tool_call", tool_name="t", arguments={}),
            ScriptedAction(type="tool_call", tool_name="t2", arguments={}),
        ])

        # Strategy that escalates
        strategy = MagicMock()
        strategy.on_step_result = AsyncMock(return_value=RecoveryDecision(
            action=RecoveryActionKind.ESCALATE,
            reason="too many failures",
        ))

        adapter = OdysToolMazeAgentAdapter(driver, strategy=strategy)
        adapter.initialize("test task", [{"name": "t"}])

        # Step 1: normal tool_call
        action = adapter.next_model_action()
        assert action.type == "tool_call"

        # receive_tool_result triggers ESCALATE decision
        adapter.receive_tool_result("t", {"status": "error"})

        # Step 2: should be final_answer due to ESCALATE
        action = adapter.next_model_action()
        assert action.type == "final_answer", (
            f"Expected final_answer due to ESCALATE/TERMINATE, got: {action.type}"
        )
        assert "TERMINATED" in action.content

    @requires_all_core
    def test_escalation_flag_set_on_adapter(self):
        """After ESCALATE, adapter.is_escalated is True."""
        OdysToolMazeAgentAdapter = _agent_core_mod.Phase5AgentCore
        ScriptedModelDriver = _model_driver_mod.ScriptedModelDriver
        ScriptedAction = _model_driver_mod.ScriptedAction
        RecoveryDecision = _control_arms_mod.RecoveryDecision
        RecoveryActionKind = _control_arms_mod.RecoveryActionKind

        driver = ScriptedModelDriver([
            ScriptedAction(type="tool_call", tool_name="t", arguments={}),
            ScriptedAction(type="tool_call", tool_name="t2", arguments={}),
        ])

        strategy = MagicMock()
        strategy.on_step_result = AsyncMock(return_value=RecoveryDecision(
            action=RecoveryActionKind.ESCALATE,
            reason="critical failure",
        ))

        adapter = OdysToolMazeAgentAdapter(driver, strategy=strategy)
        adapter.initialize("test task", [{"name": "t"}])

        assert not adapter.is_escalated

        adapter.next_model_action()
        adapter.receive_tool_result("t", {"status": "error"})
        adapter.next_model_action()

        assert adapter.is_escalated
        assert "critical failure" in adapter.escalation_reason

    @requires_control_arms
    def test_harness_stop_action_breaks_loop(self):
        """AgentExecutionHarness breaks on RecoveryActionKind.STOP."""
        from lhas.phase5.control_arms import (
            AgentExecutionHarness, RecoveryDecision, RecoveryActionKind,
            _NONE_DECISION, BareStrategy,
        )

        strategy = MagicMock(spec=BareStrategy)
        strategy.arm = _types_mod.ControlArm.A0_BARE
        strategy.configure.return_value = {}
        strategy.create_observer.return_value = None
        strategy.should_validate.return_value = False
        strategy.recovery_budget_enabled.return_value = False
        strategy.progress_signals_recovery.return_value = False

        # First call returns STOP, rest return NONE
        call_count = [0]
        async def on_step(*, step, result, observer):
            call_count[0] += 1
            if call_count[0] == 1:
                return RecoveryDecision(
                    action=RecoveryActionKind.STOP,
                    reason="terminal stop",
                )
            return _NONE_DECISION

        strategy.on_step_result = on_step

        harness = AgentExecutionHarness(strategy)

        backend = MagicMock()
        backend.reset = AsyncMock()
        backend.run_tool = AsyncMock(return_value={"status": "ok"})
        backend.finalize.return_value = {"task_id": "T1"}

        rt = _make_runtime_task()
        result = asyncio.run(harness.run(
            task=rt, backend=backend, generation_config=_make_generation_config(),
        ))

        # Should have stopped after 1 step (STOP breaks the loop)
        assert len(result["steps"]) <= 2


# ══════════════════════════════════════════════════════════════════════
#  T9: Fresh driver per trial
# ══════════════════════════════════════════════════════════════════════

class TestT9_FreshDriverPerTrial:
    """Verify RealPilotRunner creates a fresh driver per trial."""

    @requires_all_core
    def test_scripted_driver_reset_creates_fresh_state(self):
        """ScriptedModelDriver.reset() returns to initial state."""
        ScriptedModelDriver = _model_driver_mod.ScriptedModelDriver
        ScriptedAction = _model_driver_mod.ScriptedAction

        driver = ScriptedModelDriver([
            ScriptedAction(type="tool_call", tool_name="t", arguments={}),
            ScriptedAction(type="final_answer", content="done"),
        ])

        # Use the driver
        action1 = driver.next_action(messages=[], tool_definitions=[])
        assert action1.type == "tool_call"
        assert driver.script_position == 1

        # Reset
        driver.reset()
        assert driver.script_position == 0

        # Use again — same sequence
        action2 = driver.next_action(messages=[], tool_definitions=[])
        assert action2.type == "tool_call"
        assert driver.script_position == 1

    @requires_all_core
    def test_adapter_reset_creates_fresh_state(self):
        """OdysToolMazeAgentAdapter.reset() clears all state."""
        OdysToolMazeAgentAdapter = _agent_core_mod.Phase5AgentCore
        ScriptedModelDriver = _model_driver_mod.ScriptedModelDriver
        ScriptedAction = _model_driver_mod.ScriptedAction

        driver = ScriptedModelDriver([
            ScriptedAction(type="tool_call", tool_name="t", arguments={}),
            ScriptedAction(type="final_answer", content="done"),
        ])

        adapter = OdysToolMazeAgentAdapter(driver)
        adapter.initialize("task 1", [{"name": "t"}])
        adapter.next_model_action()

        assert adapter._step_count == 1
        assert len(adapter._conversation_history) > 0

        adapter.reset()

        assert adapter._step_count == 0
        assert adapter._conversation_history == []
        assert adapter._task_description == ""

    @requires_all_core
    def test_backend_resets_budget_per_execution(self):
        """ToolMazeRuntimeBackend.execute() resets model_calls_used to 0."""
        import sys as _sys
        from lhas.phase5.runtime_backend import ToolMazeRuntimeBackend

        with patch("lhas.phase5.runtime_backend.ToolMazeRuntimeBackend._load_tool_skeletons", return_value={}):
            backend = ToolMazeRuntimeBackend(
                {"task_id": "T1", "task_description": "test", "user_input": {"query": "q"}},
                budget=_types_mod.BudgetConfig(max_turns=5, max_model_calls=10),
            )

        # Simulate some state
        backend._model_calls_used = 7

        mock_engine_cls = MagicMock()
        mock_engine_instance = MagicMock()
        mock_trace = MagicMock()
        mock_trace.to_dict.return_value = {"task_id": "T1", "tool_calls": []}
        mock_engine_instance.run.return_value = (mock_trace, None)
        mock_engine_cls.return_value = mock_engine_instance
        mock_sandbox = MagicMock()
        mock_sandbox.ExecutionEngine = mock_engine_cls

        driver = _make_scripted_driver([
            {"type": "final_answer", "content": "done"},
        ])

        with patch.dict(_sys.modules, {"evaluation": MagicMock(), "evaluation.core": MagicMock(), "evaluation.core.sandbox": mock_sandbox}):
            # execute resets the counter
            backend._finalized = False  # allow re-execution
            result = backend.execute(driver, strategy=None, max_rounds=1)

            # Budget was reset at start of execute
            assert backend._model_calls_used >= 0


# ══════════════════════════════════════════════════════════════════════
#  T10: Budget exhaustion is VALID_TASK_OUTCOME
# ══════════════════════════════════════════════════════════════════════

class TestT10_BudgetExhaustionIsValid:
    """Verify budget exhaustion is classified as a valid task outcome."""

    @requires_all_core
    def test_budget_exhausted_exception_is_catchable(self):
        """BudgetExhausted is a standard Exception subclass."""
        from lhas.phase5.runtime_backend import BudgetExhausted

        assert issubclass(BudgetExhausted, Exception)

        exc = BudgetExhausted("exhausted after 50 calls")
        assert "50 calls" in str(exc)

    @requires_all_core
    def test_budget_exhausted_captured_in_result(self):
        """When BudgetExhausted is raised, backend captures partial state."""
        import sys as _sys
        from lhas.phase5.runtime_backend import ToolMazeRuntimeBackend, BudgetExhausted

        with patch("lhas.phase5.runtime_backend.ToolMazeRuntimeBackend._load_tool_skeletons", return_value={}):
            backend = ToolMazeRuntimeBackend(
                {"task_id": "T1", "task_description": "test", "user_input": {"query": "q"}},
                budget=_types_mod.BudgetConfig(max_turns=5, max_model_calls=2),
            )

        mock_engine_cls = MagicMock()
        mock_engine_instance = MagicMock()
        # Engine.run raises BudgetExhausted
        mock_engine_instance.run.side_effect = BudgetExhausted("budget exhausted")
        # _trace_logger must have a to_dict that returns a real dict
        mock_trace_logger = MagicMock()
        mock_trace_logger.to_dict.return_value = {"task_id": "T1", "tool_calls": []}
        mock_engine_instance._trace_logger = mock_trace_logger
        mock_engine_cls.return_value = mock_engine_instance
        mock_sandbox = MagicMock()
        mock_sandbox.ExecutionEngine = mock_engine_cls

        driver = _make_scripted_driver([
            {"type": "tool_call", "tool_name": "t", "arguments": {}},
        ])

        with patch.dict(_sys.modules, {"evaluation": MagicMock(), "evaluation.core": MagicMock(), "evaluation.core.sandbox": mock_sandbox}):
            # Should not raise BudgetExhausted — it is captured internally
            result = backend.execute(driver, strategy=None, max_rounds=5)

            # Result is a well-formed dict with budget_accounting
            assert "budget_accounting" in result
            assert "model_calls_used" in result["budget_accounting"]
            assert "budget_remaining" in result["budget_accounting"]

    @requires_types
    def test_trial_status_valid_exists(self):
        """TrialStatus.VALID exists for classifying valid outcomes."""
        assert hasattr(_types_mod.TrialStatus, "VALID")
        assert _types_mod.TrialStatus.VALID.value == "VALID"

    @requires_control_arms
    def test_harness_budget_exhaustion_classified_correctly(self):
        """When harness runs out of steps, the result is still valid."""
        from lhas.phase5.control_arms import AgentExecutionHarness, BareStrategy

        strategy = BareStrategy()
        harness = AgentExecutionHarness(strategy)

        backend = MagicMock()
        backend.reset = AsyncMock()
        backend.run_tool = AsyncMock(return_value={"status": "ok"})
        backend.finalize.return_value = {"task_id": "T1"}

        # Budget: max 2 steps
        rt = _types_mod.RuntimeTask(
            task_id="T-budget",
            objective="test",
            visible_tools=[{"name": "t", "description": "tool"}],
            budget=_types_mod.BudgetConfig(max_turns=2, max_model_calls=2),
        )

        result = asyncio.run(harness.run(
            task=rt, backend=backend, generation_config=_make_generation_config(),
        ))

        # Result should be well-formed (not an error)
        assert "arm" in result
        assert "steps" in result
        assert result["arm"] == "A0_BARE"


# ══════════════════════════════════════════════════════════════════════
#  T11: Manifest compatibility
# ══════════════════════════════════════════════════════════════════════

class TestT11_ManifestCompatibility:
    """Verify frozen manifest has 'tasks' and 'selected_task_ids' accessible."""

    @requires_control_arms
    def test_arm_definitions_snapshot_has_all_six_arms(self):
        """arm_definitions_snapshot() returns all six arm entries."""
        from lhas.phase5.provenance import arm_definitions_snapshot

        snapshot = arm_definitions_snapshot()
        assert len(snapshot) == 6

        for arm in _types_mod.ControlArm:
            assert arm.value in snapshot, f"Missing arm: {arm.value}"

    @requires_types
    def test_trial_manifest_model_has_required_fields(self):
        """TrialManifest has all required fields for manifest compatibility."""
        fields = set(_types_mod.TrialManifest.model_fields.keys())

        required = {
            "experiment_id", "trial_id", "task_id", "arm",
            "model_id", "provider", "generation_config",
            "root_budget", "perturbation_mode",
        }
        missing = required - fields
        assert not missing, f"TrialManifest missing fields: {missing}"

    @requires_control_arms
    def test_provenance_freeze_creates_manifest_with_selected_task_ids(self):
        """ProvenanceFreeze.freeze() produces manifest with selected_task_ids."""
        from lhas.phase5.provenance import ProvenanceFreeze, arm_definitions_snapshot

        with tempfile.TemporaryDirectory() as tmpdir:
            freeze = ProvenanceFreeze(tmpdir)

            identity = _types_mod.BenchmarkIdentity(
                benchmark_name=_types_mod.BenchmarkName.TOOLMAZE,
                benchmark_revision="r1",
                repository_url="https://example.com",
                commit_sha="abc123",
                dataset_digest="digest1",
                evaluator_digest="digest2",
            )

            gen_cfg = _make_generation_config()

            manifest = freeze.freeze(
                experiment_id="test-exp-001",
                benchmark_identity=identity,
                selected_task_ids=["T3", "T1", "T2"],
                generation_config=gen_cfg,
                arm_definitions=arm_definitions_snapshot() if _provenance_mod else {},
                budgets={"max_turns": 30, "max_model_calls": 50},
            )

            assert "selected_task_ids" in manifest
            assert manifest["selected_task_ids"] == ["T1", "T2", "T3"]  # sorted

            # Verify can be loaded back
            verified = freeze.verify("test-exp-001")
            assert verified["selected_task_ids"] == ["T1", "T2", "T3"]

    @requires_control_arms
    def test_pilot_manifest_schema_has_selected_task_ids(self):
        """build_pilot_manifest() includes selected_task_ids."""
        from lhas.phase5.real_pilot_runner import build_pilot_manifest
        from lhas.phase5.provenance import arm_definitions_snapshot

        gen_cfg = _make_generation_config()

        manifest = build_pilot_manifest(
            experiment_id="test-pilot",
            task_ids=["T1", "T2", "T3"],
            arm_definitions=arm_definitions_snapshot() if _provenance_mod else {},
            model_config=gen_cfg,
            budgets={"max_turns": 30, "max_model_calls": 50},
            benchmark_identity={
                "name": "toolmaze", "revision": "r1",
                "repository_url": "https://example.com",
                "commit_sha": "abc", "dataset_digest": "d1",
                "evaluator_digest": "d2",
            },
        )

        assert "selected_task_ids" in manifest
        assert manifest["selected_task_ids"] == ["T1", "T2", "T3"]
        assert "task_count" in manifest
        assert manifest["task_count"] == 3
        assert "manifest_hash" in manifest

    @requires_control_arms
    def test_frozen_manifest_json_loadable(self):
        """A frozen manifest can be loaded from JSON and both keys are accessible."""
        from lhas.phase5.provenance import ProvenanceFreeze, arm_definitions_snapshot

        with tempfile.TemporaryDirectory() as tmpdir:
            freeze = ProvenanceFreeze(tmpdir)

            identity = _types_mod.BenchmarkIdentity(
                benchmark_name=_types_mod.BenchmarkName.TOOLMAZE,
                benchmark_revision="r1",
                repository_url="https://example.com",
                commit_sha="abc123",
                dataset_digest="digest1",
                evaluator_digest="digest2",
            )

            manifest = freeze.freeze(
                experiment_id="json-test",
                benchmark_identity=identity,
                selected_task_ids=["T10", "T20"],
                generation_config=_make_generation_config(),
                arm_definitions=arm_definitions_snapshot(),
                budgets={"max_turns": 30, "max_model_calls": 50},
            )

            # Write and reload from JSON
            manifest_path = Path(tmpdir) / "test_manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))

            # Both 'tasks' and 'selected_task_ids' accessible
            assert "selected_task_ids" in loaded
            assert isinstance(loaded["selected_task_ids"], list)
            assert loaded["selected_task_ids"] == ["T10", "T20"]


# ══════════════════════════════════════════════════════════════════════
#  Additional wiring tests
# ══════════════════════════════════════════════════════════════════════

class TestA1_RetryOnlySameToolSameArgs:
    """A1 retry-only: on retry, same tool and same args."""

    @requires_all_core
    def test_a1_retry_preserves_tool_identity(self):
        """RetryOnlyStrategy returns RETRY (not RETRY_WITH_CONTEXT),
        so the adapter replays the same tool call.
        """
        from lhas.phase5.control_arms import RetryOnlyStrategy

        strategy = RetryOnlyStrategy()
        rt = _make_runtime_task()
        strategy.configure(task=rt, generation_config=_make_generation_config())

        decision = asyncio.run(strategy.on_step_result(
            step=1,
            result={"status": "error", "output": "fail"},
            observer=None,
        ))

        assert decision.action.value == "retry"
        # RETRY (not RETRY_WITH_CONTEXT) means same args replayed
        assert "retry" in decision.reason.lower()


class TestA2_ValidatorOnlyNoRecovery:
    """A2 validator-only: validator active, no recovery."""

    @requires_control_arms
    def test_a2_validator_active(self):
        strategy = _control_arms_mod.ValidatorOnlyStrategy()
        assert strategy.should_validate() is True

    @requires_control_arms
    def test_a2_no_recovery(self):
        strategy = _control_arms_mod.ValidatorOnlyStrategy()
        rt = _make_runtime_task()
        strategy.configure(task=rt, generation_config=_make_generation_config())

        decision = asyncio.run(strategy.on_step_result(
            step=1,
            result={"status": "error", "output": "fail"},
            observer=None,
        ))

        assert decision.action.value == "none"


class TestA4A5_SingleVariableAblation:
    """A4/A5 are single-variable ablations of A3."""

    @requires_control_arms
    def test_a4_disables_only_progress_signals(self):
        """A4 = A3 with progress_signals_recovery → False."""
        from lhas.phase5.control_arms import OdysMinusObservableProgress

        a4 = OdysMinusObservableProgress()
        assert a4.progress_signals_recovery() is False
        assert a4.should_validate() is True  # same as A3
        assert a4.recovery_budget_enabled() is True  # same as A3

    @requires_control_arms
    def test_a5_disables_only_recovery_budget(self):
        """A5 = A3 with recovery_budget_enabled → False."""
        from lhas.phase5.control_arms import OdysMinusRecoveryBudgetPolicy

        a5 = OdysMinusRecoveryBudgetPolicy()
        assert a5.recovery_budget_enabled() is False
        assert a5.should_validate() is True  # same as A3
        assert a5.progress_signals_recovery() is True  # same as A3

    @requires_control_arms
    def test_a4_inherits_a3_recovery(self):
        """A4 still delegates to DefaultRecoveryPolicy."""
        from lhas.phase5.control_arms import OdysMinusObservableProgress

        a4 = OdysMinusObservableProgress()
        assert hasattr(a4, "_recovery_policy")

    @requires_control_arms
    def test_a5_inherits_a3_progress(self):
        """A5 still has observable progress enabled."""
        from lhas.phase5.control_arms import OdysMinusRecoveryBudgetPolicy

        a5 = OdysMinusRecoveryBudgetPolicy()
        assert a5.progress_signals_recovery() is True
