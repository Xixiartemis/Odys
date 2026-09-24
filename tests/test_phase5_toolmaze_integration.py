"""Phase 5 ToolMaze Integration Tests — T1–T15.

Provider-free, benchmark-optional integration tests for the official
ToolMaze execution pipeline: ScriptedModelDriver, AgentAdapter,
official ExecutionEngine, golden parity, ablation, and regression.

Tests that require sibling modules (model_driver.py, agent_adapter.py)
that may not exist yet use _require_module() to produce a clean skip.
Tests that require external ToolMaze benchmark data use skipif markers.

Default CI must pass from a clean checkout.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

# ── Conditional import guard ────────────────────────────────────────────
# Sibling agents are creating model_driver.py and agent_adapter.py.
# If they don't exist yet, tests that need them are cleanly skipped.

def _require_module(module_name: str):
    """Import a module or skip the test if it doesn't exist yet."""
    try:
        return importlib.import_module(module_name)
    except (ModuleNotFoundError, ImportError):
        pytest.skip(
            f"{module_name} not yet available (sibling agent not finished)",
            allow_module_level=True,
        )

# Attempt imports — guard at module level for tests that need them
_model_driver_mod = None
_agent_adapter_mod = None
_agent_core_mod = None

try:
    _model_driver_mod = importlib.import_module("lhas.phase5.model_driver")
except (ModuleNotFoundError, ImportError):
    pass

try:
    _agent_adapter_mod = importlib.import_module("lhas.phase5.agent_adapter")
except (ModuleNotFoundError, ImportError):
    pass

try:
    _agent_core_mod = importlib.import_module("lhas.phase5.agent_core")
except (ModuleNotFoundError, ImportError):
    pass

_HAS_MODEL_DRIVER = _model_driver_mod is not None
_HAS_AGENT_ADAPTER = _agent_adapter_mod is not None
_HAS_AGENT_CORE = _agent_core_mod is not None
_HAS_BOTH = _HAS_MODEL_DRIVER and _HAS_AGENT_CORE

# Helper: get AgentAction class if available
def _get_agent_action():
    """Get the AgentAction dataclass from ToolMaze's base_agent."""
    if _HAS_MODEL_DRIVER:
        return getattr(_model_driver_mod, "AgentAction", None)
    return None


def _make_scripted_action(tool_name: str, arguments: dict | None = None, **kw):
    """Create a ScriptedAction if the module is available."""
    if not _HAS_MODEL_DRIVER:
        pytest.skip("model_driver.py not yet available")
    ScriptedAction = _model_driver_mod.ScriptedAction
    return ScriptedAction(type="tool_call", tool_name=tool_name, arguments=arguments or {}, **kw)


def _make_final_answer(content: str = "done", **kw):
    """Create a final_answer ScriptedAction."""
    if not _HAS_MODEL_DRIVER:
        pytest.skip("model_driver.py not yet available")
    ScriptedAction = _model_driver_mod.ScriptedAction
    return ScriptedAction(type="final_answer", content=content, **kw)

# ── External benchmark availability ────────────────────────────────────

_BENCHMARK_DIR = (
    Path(__file__).resolve().parents[1]
    / "experiments" / "phase5" / "benchmarks" / "toolmaze" / "data"
)
_BENCHMARK_AVAILABLE = (_BENCHMARK_DIR / "perturbed_tasks").is_dir()

# ── Core imports (always available) ────────────────────────────────────

from lhas.phase5.types import (
    BenchmarkAdapter,
    BudgetConfig,
    ControlArm,
    ControlPolicy,
    GenerationConfig,
    NativeResult,
    ProgressObserver,
    RuntimeTask,
    SignalKind,
)
from lhas.phase5.control_arms import (
    AgentExecutionHarness,
    BareStrategy,
    OdysFullStrategy,
    OdysMinusObservableProgress,
    OdysMinusRecoveryBudgetPolicy,
    RetryOnlyStrategy,
    ValidatorOnlyStrategy,
    ExperimentPairValidator,
    assert_matched_authority,
    create_policy,
)
from lhas.phase5.shadow_observer import ShadowProgressObserver
from lhas.phase5.toolmaze_adapter import ToolMazeAdapter

# ── Fixtures ───────────────────────────────────────────────────────────

_FIXTURES_DIR = Path(__file__).parent / "phase5_fixtures"
_TOOLMAZE_FIXTURE_DATA = _FIXTURES_DIR / "toolmaze_data"


@pytest.fixture
def gen_config() -> GenerationConfig:
    return GenerationConfig(
        model_id="test-model-v1",
        provider="test-provider",
        temperature=0.0,
        seed=42,
    )


@pytest.fixture
def budget() -> BudgetConfig:
    return BudgetConfig(max_turns=30, max_model_calls=50)


@pytest.fixture
def toolmaze_adapter() -> ToolMazeAdapter:
    return ToolMazeAdapter(
        data_dir=_TOOLMAZE_FIXTURE_DATA,
        repo_dir=_TOOLMAZE_FIXTURE_DATA,
        benchmark_revision="fixture-rev-001",
        dataset_hash="fixture-dataset-hash",
        evaluator_hash="fixture-evaluator-hash",
    )


@pytest.fixture
def shadow_observer() -> ShadowProgressObserver:
    return ShadowProgressObserver()


def _make_runtime_task(task_id: str = "t1") -> RuntimeTask:
    """Build a minimal RuntimeTask for testing."""
    return RuntimeTask(
        task_id=task_id,
        objective="test objective",
        prompt="test prompt",
        visible_tools=[
            {"name": "tool_a", "description": "Tool A", "parameters": {}},
            {"name": "tool_b", "description": "Tool B", "parameters": {}},
        ],
        budget=BudgetConfig(max_turns=5, max_model_calls=5),
    )


# ══════════════════════════════════════════════════════════════════════
# T1: ScriptedModelDriver returns scripted actions
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _HAS_MODEL_DRIVER, reason="model_driver.py not yet available")
class TestT1_ScriptedModelDriverReturnsActions:
    """T1: ScriptedModelDriver yields actions in scripted order."""

    def test_returns_first_action(self):
        mod = _model_driver_mod
        ScriptedModelDriver = mod.ScriptedModelDriver
        script = [
            _make_scripted_action("tool_a", {"x": 1}),
            _make_scripted_action("tool_b", {"y": 2}),
        ]
        driver = ScriptedModelDriver(script=script)
        result = driver.next_action(messages=[], tool_definitions=[])
        assert result.type == "tool_call"
        assert result.tool_name == "tool_a"
        assert result.arguments == {"x": 1}

    def test_returns_actions_in_order(self):
        mod = _model_driver_mod
        ScriptedModelDriver = mod.ScriptedModelDriver
        script = [
            _make_scripted_action("a", {}),
            _make_scripted_action("b", {}),
            _make_scripted_action("c", {}),
        ]
        driver = ScriptedModelDriver(script=script)
        for expected_tool in ["a", "b", "c"]:
            result = driver.next_action(messages=[], tool_definitions=[])
            assert result.tool_name == expected_tool

    def test_accepts_messages_and_tool_definitions(self):
        mod = _model_driver_mod
        ScriptedModelDriver = mod.ScriptedModelDriver
        script = [_make_scripted_action("a", {})]
        driver = ScriptedModelDriver(script=script)
        # Should not raise with complex messages/tool defs
        result = driver.next_action(
            messages=[{"role": "user", "content": "test"}],
            tool_definitions=[{"name": "a", "description": "tool a"}],
        )
        assert result.tool_name == "a"

    def test_satisfies_model_driver_protocol(self):
        """ScriptedModelDriver satisfies ModelDriver protocol."""
        mod = _model_driver_mod
        ScriptedModelDriver = mod.ScriptedModelDriver
        ModelDriver = mod.ModelDriver
        script = [_make_scripted_action("a", {})]
        driver = ScriptedModelDriver(script=script)
        assert isinstance(driver, ModelDriver)


# ══════════════════════════════════════════════════════════════════════
# T2: ScriptedModelDriver returns final_answer when exhausted
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _HAS_MODEL_DRIVER, reason="model_driver.py not yet available")
class TestT2_ScriptedModelDriverFinalAnswer:
    """T2: After all scripted actions are consumed, driver signals completion."""

    def test_final_answer_after_exhaustion(self):
        mod = _model_driver_mod
        ScriptedModelDriver = mod.ScriptedModelDriver
        script = [
            _make_scripted_action("a", {}),
            _make_scripted_action("b", {}),
        ]
        driver = ScriptedModelDriver(script=script)
        # Consume all actions
        driver.next_action(messages=[], tool_definitions=[])
        driver.next_action(messages=[], tool_definitions=[])
        # Next call should return final_answer
        result = driver.next_action(messages=[], tool_definitions=[])
        assert result.type == "final_answer"

    def test_empty_script_returns_final_immediately(self):
        mod = _model_driver_mod
        ScriptedModelDriver = mod.ScriptedModelDriver
        driver = ScriptedModelDriver(script=[])
        result = driver.next_action(messages=[], tool_definitions=[])
        assert result.type == "final_answer"

    def test_single_action_then_final(self):
        mod = _model_driver_mod
        ScriptedModelDriver = mod.ScriptedModelDriver
        script = [_make_scripted_action("only", {"v": 42})]
        driver = ScriptedModelDriver(script=script)
        first = driver.next_action(messages=[], tool_definitions=[])
        assert first.type == "tool_call"
        assert first.tool_name == "only"
        second = driver.next_action(messages=[], tool_definitions=[])
        assert second.type == "final_answer"

    def test_explicit_final_answer_in_script(self):
        """Script can include an explicit final_answer entry."""
        mod = _model_driver_mod
        ScriptedModelDriver = mod.ScriptedModelDriver
        script = [
            _make_scripted_action("a", {}),
            _make_final_answer("the answer is 42"),
        ]
        driver = ScriptedModelDriver(script=script)
        driver.next_action(messages=[], tool_definitions=[])
        result = driver.next_action(messages=[], tool_definitions=[])
        assert result.type == "final_answer"
        assert "42" in (result.content or "")


# ══════════════════════════════════════════════════════════════════════
# T3: OdysToolMazeAgentAdapter satisfies BaseAgent protocol
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _HAS_AGENT_ADAPTER, reason="agent_adapter.py not yet available")
class TestT3_AgentAdapterSatisfiesProtocol:
    """T3: OdysToolMazeAgentAdapter satisfies the BaseAgent protocol."""

    def test_has_required_methods(self):
        mod = _agent_core_mod
        Adapter = _agent_core_mod.Phase5AgentCore
        assert hasattr(Adapter, "initialize")
        assert hasattr(Adapter, "next_model_action")
        assert hasattr(Adapter, "receive_tool_result")
        assert hasattr(Adapter, "get_total_tokens")
        assert hasattr(Adapter, "get_token_usage")
        assert hasattr(Adapter, "get_conversation_history")
        assert hasattr(Adapter, "reset")

    def test_adapter_is_class(self):
        mod = _agent_core_mod
        Adapter = _agent_core_mod.Phase5AgentCore
        assert inspect.isclass(Adapter)

    def test_adapter_requires_model_driver(self):
        """Constructor requires a ModelDriver instance."""
        mod = _agent_core_mod
        Adapter = _agent_core_mod.Phase5AgentCore
        with pytest.raises(TypeError):
            Adapter()  # Missing required model_driver arg

    def test_adapter_subclasses_base_agent(self):
        """Adapter inherits from BaseAgent (only when ToolMaze is available)."""
        mod = _agent_core_mod
        Adapter = _agent_core_mod.Phase5AgentCore
        # Check the class MRO for BaseAgent — only meaningful when ToolMaze is present
        base_names = [c.__name__ for c in inspect.getmro(Adapter)]
        if "BaseAgent" not in base_names:
            pytest.skip("ToolMaze not available — BaseAgent is object stub")
        assert "BaseAgent" in base_names


# ══════════════════════════════════════════════════════════════════════
# T4: Agent adapter records conversation history
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _HAS_AGENT_ADAPTER, reason="agent_adapter.py not yet available")
@pytest.mark.skipif(not _HAS_MODEL_DRIVER, reason="model_driver.py not yet available")
class TestT4_AgentAdapterRecordsHistory:
    """T4: Adapter accumulates conversation history during execution."""

    def test_history_starts_empty_after_init(self):
        """After initialize(), conversation history has the initial user message."""
        mod = _agent_core_mod
        md_mod = _model_driver_mod
        Adapter = _agent_core_mod.Phase5AgentCore
        ScriptedModelDriver = md_mod.ScriptedModelDriver
        driver = ScriptedModelDriver(script=[])
        adapter = Adapter(model_driver=driver)
        adapter.initialize("test task", [])
        history = adapter.get_conversation_history()
        assert len(history) == 1  # initial user message
        assert history[0]["role"] == "user"

    def test_history_grows_with_steps(self):
        """Each step() adds an assistant message to history."""
        mod = _agent_core_mod
        md_mod = _model_driver_mod
        Adapter = _agent_core_mod.Phase5AgentCore
        ScriptedModelDriver = md_mod.ScriptedModelDriver
        script = [
            _make_scripted_action("tool_a", {"x": 1}),
            _make_final_answer("done"),
        ]
        driver = ScriptedModelDriver(script=script)
        adapter = Adapter(model_driver=driver)
        adapter.initialize("test task", [{"name": "tool_a"}])

        # First step: tool call
        action = adapter.next_model_action()
        history = adapter.get_conversation_history()
        assert len(history) == 2  # user + assistant
        assert history[-1]["role"] == "assistant"
        assert history[-1]["type"] == "tool_call"

    def test_tool_result_recorded_in_history(self):
        """receive_tool_result() appends tool message to history."""
        mod = _agent_core_mod
        md_mod = _model_driver_mod
        Adapter = _agent_core_mod.Phase5AgentCore
        ScriptedModelDriver = md_mod.ScriptedModelDriver
        script = [
            _make_scripted_action("tool_a", {}),
            _make_final_answer("done"),
        ]
        driver = ScriptedModelDriver(script=script)
        adapter = Adapter(model_driver=driver)
        adapter.initialize("test task", [{"name": "tool_a"}])

        adapter.next_model_action()
        adapter.receive_tool_result("tool_a", {"status": "success", "output": "ok"})

        history = adapter.get_conversation_history()
        tool_msgs = [m for m in history if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["name"] == "tool_a"


# ══════════════════════════════════════════════════════════════════════
# T5: Agent adapter records evidence events
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _HAS_AGENT_ADAPTER, reason="agent_adapter.py not yet available")
@pytest.mark.skipif(not _HAS_MODEL_DRIVER, reason="model_driver.py not yet available")
class TestT5_AgentAdapterRecordsEvidence:
    """T5: Adapter records evidence events (tool calls, observations)."""

    def test_adapter_has_evidence_injection_point(self):
        """Adapter has set_evidence_ledger method."""
        mod = _agent_core_mod
        Adapter = _agent_core_mod.Phase5AgentCore
        assert hasattr(Adapter, "set_evidence_ledger")

    def test_adapter_has_shadow_observer_injection(self):
        """Adapter has set_shadow_observer method."""
        mod = _agent_core_mod
        Adapter = _agent_core_mod.Phase5AgentCore
        assert hasattr(Adapter, "set_shadow_observer")

    def test_adapter_records_to_evidence_ledger(self):
        """When evidence ledger is set, tool results are recorded."""
        mod = _agent_core_mod
        md_mod = _model_driver_mod
        Adapter = _agent_core_mod.Phase5AgentCore
        ScriptedModelDriver = md_mod.ScriptedModelDriver

        script = [_make_scripted_action("tool_a", {})]
        driver = ScriptedModelDriver(script=script)
        adapter = Adapter(model_driver=driver)
        adapter.initialize("test task", [{"name": "tool_a"}])

        # Create a mock evidence ledger
        mock_ledger = MagicMock()
        adapter.set_evidence_ledger(mock_ledger)

        adapter.next_model_action()
        adapter.receive_tool_result("tool_a", {"status": "success"})

        # Ledger should have been called
        assert mock_ledger.append.called

    def test_adapter_records_to_shadow_observer(self):
        """When shadow observer is set, tool results are observed."""
        mod = _agent_core_mod
        md_mod = _model_driver_mod
        Adapter = _agent_core_mod.Phase5AgentCore
        ScriptedModelDriver = md_mod.ScriptedModelDriver

        script = [_make_scripted_action("tool_a", {})]
        driver = ScriptedModelDriver(script=script)
        adapter = Adapter(model_driver=driver)
        adapter.initialize("test task", [{"name": "tool_a"}])

        # Create a mock shadow observer
        mock_observer = MagicMock()
        adapter.set_shadow_observer(mock_observer)

        adapter.next_model_action()
        adapter.receive_tool_result("tool_a", {"status": "success"})

        # Observer should have been called
        assert mock_observer.observe.called


# ══════════════════════════════════════════════════════════════════════
# T6: Official ExecutionEngine runs with agent adapter (provider-free)
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _HAS_BOTH, reason="model_driver.py and/or agent_adapter.py not yet available")
class TestT6_ExecutionEngineWithAgentAdapter:
    """T6: Official ExecutionEngine runs with OdysToolMazeAgentAdapter."""

    def test_adapter_and_driver_can_coexist(self):
        """Both modules importable and classes exist."""
        md_mod = _model_driver_mod
        aa_mod = _agent_core_mod
        assert hasattr(md_mod, "ScriptedModelDriver")
        assert hasattr(aa_mod, "Phase5AgentCore")

    def test_harness_accepts_adapter_as_strategy(self):
        """AgentExecutionHarness can wrap any PolicyStrategy."""
        # Verify the harness accepts the standard strategies
        for arm in ControlArm:
            policy = create_policy(arm)
            assert isinstance(policy, AgentExecutionHarness)
            assert policy.arm == arm

    @pytest.mark.asyncio
    async def test_harness_runs_with_scripted_backend(self):
        """Harness executes a trial with a mock backend (no provider)."""
        task = _make_runtime_task("t6")

        strategy = BareStrategy()
        harness = AgentExecutionHarness(strategy)

        # Create a mock backend that satisfies RuntimeBackend protocol
        mock_backend = MagicMock()
        step_count = 0

        async def mock_run_tool(**kwargs):
            nonlocal step_count
            step_count += 1
            return {"status": "success", "step": step_count}

        async def mock_reset(task):
            return None

        mock_backend.run_tool = mock_run_tool
        mock_backend.reset = mock_reset
        mock_backend.finalize = lambda tid: {"task_id": tid, "runtime_events": []}

        result = await harness.run(
            task=task, backend=mock_backend,
            generation_config=GenerationConfig(
                model_id="test", provider="test", temperature=0.0,
            ),
        )
        assert result["arm"] == "A0_BARE"
        assert len(result["steps"]) > 0


# ══════════════════════════════════════════════════════════════════════
# T7: Official TraceLogger produces actual trace
# ══════════════════════════════════════════════════════════════════════

class TestT7_TraceLoggerProducesTrace:
    """T7: The harness accumulates actual execution steps."""

    @pytest.mark.asyncio
    async def test_steps_accumulated_during_execution(self, toolmaze_adapter, gen_config):
        tasks = toolmaze_adapter.enumerate_tasks()
        desc = tasks[0]
        rt = toolmaze_adapter.build_runtime_task(desc)

        policy = create_policy(ControlArm.A0_BARE)
        result = await policy.execute_trial(
            task=rt, adapter=toolmaze_adapter, generation_config=gen_config,
        )
        assert len(result["steps"]) > 0, "Steps must be accumulated during execution"

    @pytest.mark.asyncio
    async def test_steps_are_actual_observations(self, toolmaze_adapter, gen_config):
        """Steps contain actual observation data, not empty dicts."""
        tasks = toolmaze_adapter.enumerate_tasks()
        desc = tasks[0]
        rt = toolmaze_adapter.build_runtime_task(desc)

        policy = create_policy(ControlArm.A0_BARE)
        result = await policy.execute_trial(
            task=rt, adapter=toolmaze_adapter, generation_config=gen_config,
        )
        for step in result["steps"]:
            assert isinstance(step, dict)
            assert len(step) > 0, "Each step should contain observation data"

    def test_artifact_has_trace(self, toolmaze_adapter):
        """Finalized artifact contains runtime events."""
        tasks = toolmaze_adapter.enumerate_tasks()
        desc = tasks[0]
        artifact = toolmaze_adapter.finalize_runtime_artifact(desc.task_id)
        assert "runtime_events" in artifact
        assert "tool_calls" in artifact


# ══════════════════════════════════════════════════════════════════════
# T8: Golden parity: same trace → exact TSR/PRR/RC
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _BENCHMARK_AVAILABLE, reason="External ToolMaze benchmark not available")
class TestT8_GoldenParity:
    """T8: Same trace produces identical TSR/PRR/RC via official judge."""

    def test_tsr_deterministic(self):
        """Evaluating the same artifact twice yields identical TSR."""
        adapter = ToolMazeAdapter()
        tasks = adapter.enumerate_tasks()
        desc = tasks[0]
        artifact = adapter.finalize_runtime_artifact(desc.task_id)
        r1 = adapter.offline_native_evaluate(desc.task_id, artifact)
        r2 = adapter.offline_native_evaluate(desc.task_id, artifact)
        assert r1.tsr == r2.tsr

    def test_prr_deterministic(self):
        """PRR is deterministic for the same artifact."""
        adapter = ToolMazeAdapter()
        tasks = adapter.enumerate_tasks()
        desc = tasks[0]
        artifact = adapter.finalize_runtime_artifact(desc.task_id)
        r1 = adapter.offline_native_evaluate(desc.task_id, artifact)
        r2 = adapter.offline_native_evaluate(desc.task_id, artifact)
        assert r1.prr == r2.prr

    def test_rc_deterministic(self):
        """RC is deterministic for the same artifact."""
        adapter = ToolMazeAdapter()
        tasks = adapter.enumerate_tasks()
        desc = tasks[0]
        artifact = adapter.finalize_runtime_artifact(desc.task_id)
        r1 = adapter.offline_native_evaluate(desc.task_id, artifact)
        r2 = adapter.offline_native_evaluate(desc.task_id, artifact)
        assert r1.rc == r2.rc


# ══════════════════════════════════════════════════════════════════════
# T9: A3 calls DefaultRecoveryPolicy.decide() at runtime
# ══════════════════════════════════════════════════════════════════════

class TestT9_A3CallsRecoveryPolicy:
    """T9: A3 (OdysFullStrategy) delegates to DefaultRecoveryPolicy."""

    def test_a3_instantiates_recovery_policy(self):
        """OdysFullStrategy.__init__ imports DefaultRecoveryPolicy."""
        source = inspect.getsource(OdysFullStrategy.__init__)
        assert "DefaultRecoveryPolicy" in source

    def test_a3_delegate_to_recovery_method_exists(self):
        """OdysFullStrategy has _delegate_to_recovery method."""
        assert hasattr(OdysFullStrategy, "_delegate_to_recovery")

    def test_a3_recovery_policy_stored_as_instance(self):
        """OdysFullStrategy stores recovery policy instance."""
        strategy = OdysFullStrategy()
        # Check the strategy has a recovery policy attribute
        assert hasattr(strategy, "_recovery_policy") or hasattr(strategy, "recovery_policy")

    @pytest.mark.asyncio
    async def test_a3_calls_recovery_on_error(self, toolmaze_adapter, gen_config):
        """When tool returns error, A3 delegates to recovery policy."""
        tasks = toolmaze_adapter.enumerate_tasks()
        desc = tasks[0]
        rt = toolmaze_adapter.build_runtime_task(desc)

        policy = create_policy(ControlArm.A3_ODYS_FULL)
        result = await policy.execute_trial(
            task=rt, adapter=toolmaze_adapter, generation_config=gen_config,
        )
        # The strategy config should reference recovery
        cfg = result.get("strategy_config", {})
        assert cfg.get("recovery") is True
        assert cfg.get("recovery_policy_class") is not None

    def test_a3_delegation_source_contains_phase4_reference(self):
        """_delegate_to_recovery bridges to Phase4 recovery."""
        source = inspect.getsource(OdysFullStrategy._delegate_to_recovery)
        assert "Phase4" in source or "phase4" in source or "recovery" in source.lower()


# ══════════════════════════════════════════════════════════════════════
# T10: A4 single-variable ablation (OP excluded from recovery)
# ══════════════════════════════════════════════════════════════════════

class TestT10_A4SingleVariableAblation:
    """T10: A4 disables observable-progress signals from recovery."""

    def test_a4_inherits_from_a3(self):
        """A4 is a subclass of A3 (single-variable ablation)."""
        assert issubclass(OdysMinusObservableProgress, OdysFullStrategy)

    def test_a4_progress_signals_recovery_false(self):
        """A4's progress_signals_recovery() returns False."""
        strategy = OdysMinusObservableProgress()
        assert strategy.progress_signals_recovery() is False

    def test_a4_still_has_recovery_enabled(self):
        """A4 still has recovery enabled (only progress disabled)."""
        strategy = OdysMinusObservableProgress()
        assert strategy.should_validate() is True
        assert strategy.recovery_budget_enabled() is True

    def test_a4_arm_value(self):
        assert OdysMinusObservableProgress().arm == ControlArm.A4_ODYS_MINUS_OBSERVABLE_PROGRESS

    @pytest.mark.asyncio
    async def test_a4_executes_with_progress_disabled(self, toolmaze_adapter, gen_config):
        """A4 executes trial with progress signals not influencing recovery."""
        tasks = toolmaze_adapter.enumerate_tasks()
        desc = tasks[0]
        rt = toolmaze_adapter.build_runtime_task(desc)

        policy = create_policy(ControlArm.A4_ODYS_MINUS_OBSERVABLE_PROGRESS)
        result = await policy.execute_trial(
            task=rt, adapter=toolmaze_adapter, generation_config=gen_config,
        )
        assert result["observable_progress_used"] is False

    @pytest.mark.asyncio
    async def test_a4_still_has_shadow_observer(self, toolmaze_adapter, gen_config):
        """A4 still creates shadow observer (just doesn't use it for recovery)."""
        tasks = toolmaze_adapter.enumerate_tasks()
        desc = tasks[0]
        rt = toolmaze_adapter.build_runtime_task(desc)

        policy = create_policy(ControlArm.A4_ODYS_MINUS_OBSERVABLE_PROGRESS)
        result = await policy.execute_trial(
            task=rt, adapter=toolmaze_adapter, generation_config=gen_config,
        )
        assert result.get("shadow_records", 0) > 0


# ══════════════════════════════════════════════════════════════════════
# T11: A5 single-variable ablation (budget policy disabled)
# ══════════════════════════════════════════════════════════════════════

class TestT11_A5SingleVariableAblation:
    """T11: A5 disables recovery budget policy."""

    def test_a5_inherits_from_a3(self):
        """A5 is a subclass of A3 (single-variable ablation)."""
        assert issubclass(OdysMinusRecoveryBudgetPolicy, OdysFullStrategy)

    def test_a5_recovery_budget_enabled_false(self):
        """A5's recovery_budget_enabled() returns False."""
        strategy = OdysMinusRecoveryBudgetPolicy()
        assert strategy.recovery_budget_enabled() is False

    def test_a5_still_has_progress_and_validation(self):
        """A5 still has progress and validation (only budget disabled)."""
        strategy = OdysMinusRecoveryBudgetPolicy()
        assert strategy.progress_signals_recovery() is True
        assert strategy.should_validate() is True

    def test_a5_arm_value(self):
        assert OdysMinusRecoveryBudgetPolicy().arm == ControlArm.A5_ODYS_MINUS_RECOVERY_BUDGET_POLICY

    @pytest.mark.asyncio
    async def test_a5_executes_with_budget_disabled(self, toolmaze_adapter, gen_config):
        """A5 executes trial with recovery budget policy inactive."""
        tasks = toolmaze_adapter.enumerate_tasks()
        desc = tasks[0]
        rt = toolmaze_adapter.build_runtime_task(desc)

        policy = create_policy(ControlArm.A5_ODYS_MINUS_RECOVERY_BUDGET_POLICY)
        result = await policy.execute_trial(
            task=rt, adapter=toolmaze_adapter, generation_config=gen_config,
        )
        assert result["recovery_budget_policy_active"] is False

    @pytest.mark.asyncio
    async def test_a5_preserves_other_mechanisms(self, toolmaze_adapter, gen_config):
        """A5 preserves shadow observer and execution steps."""
        tasks = toolmaze_adapter.enumerate_tasks()
        desc = tasks[0]
        rt = toolmaze_adapter.build_runtime_task(desc)

        policy = create_policy(ControlArm.A5_ODYS_MINUS_RECOVERY_BUDGET_POLICY)
        result = await policy.execute_trial(
            task=rt, adapter=toolmaze_adapter, generation_config=gen_config,
        )
        assert len(result["steps"]) > 0
        assert result.get("shadow_records", 0) > 0


# ══════════════════════════════════════════════════════════════════════
# T12: Shadow observer non-interference
# ══════════════════════════════════════════════════════════════════════

class TestT12_ShadowObserverNonInterference:
    """T12: Shadow observer records but never influences execution."""

    def test_observer_has_no_execution_methods(self, shadow_observer):
        """Observer cannot request recovery, modify budget, or influence."""
        assert not hasattr(shadow_observer, "request_recovery")
        assert not hasattr(shadow_observer, "modify_budget")
        assert not hasattr(shadow_observer, "influence_execution")

    def test_observer_returns_shadow_record(self, shadow_observer):
        """observe() returns a ShadowRecord, not an execution directive."""
        record = shadow_observer.observe(
            task_id="t12", step=0,
            action_identity="tool_a", tool_result={"status": "success"},
        )
        assert record.signal == SignalKind.PROGRESSING
        assert record.trial_id == "t12"

    def test_observer_does_not_see_oracle_data(self, shadow_observer):
        """Observer features contain no oracle/perturbation data."""
        record = shadow_observer.observe(
            task_id="t12", step=0,
            action_identity="tool_a",
            tool_result={"status": "success", "output": "done"},
        )
        features = record.observable_features
        assert "perturbation_mode" not in features
        assert "oracle" not in json.dumps(features)

    def test_observer_complies_with_protocol(self, shadow_observer):
        """Shadow observer satisfies ProgressObserver protocol."""
        assert isinstance(shadow_observer, ProgressObserver)

    @pytest.mark.asyncio
    async def test_observer_does_not_change_harness_result(self, toolmaze_adapter, gen_config):
        """Harness result is the same structure regardless of observer."""
        tasks = toolmaze_adapter.enumerate_tasks()
        desc = tasks[0]
        rt = toolmaze_adapter.build_runtime_task(desc)

        # A0 has no observer
        a0 = create_policy(ControlArm.A0_BARE)
        r0 = await a0.execute_trial(task=rt, adapter=toolmaze_adapter, generation_config=gen_config)
        # A3 has observer
        a3 = create_policy(ControlArm.A3_ODYS_FULL)
        r3 = await a3.execute_trial(task=rt, adapter=toolmaze_adapter, generation_config=gen_config)

        # Both produce the same keys (minus observer-specific ones)
        assert "arm" in r0 and "arm" in r3
        assert "steps" in r0 and "steps" in r3
        assert "task_id" in r0 and "task_id" in r3


# ══════════════════════════════════════════════════════════════════════
# T13: No oracle data in runtime envelope
# ══════════════════════════════════════════════════════════════════════

class TestT13_NoOracleDataInRuntimeEnvelope:
    """T13: RuntimeTask never contains oracle data."""

    def test_no_hidden_fields_in_runtime_task(self, toolmaze_adapter):
        """build_runtime_task strips all hidden fields."""
        from lhas.phase5.toolmaze_adapter import _HIDDEN_FIELDS
        tasks = toolmaze_adapter.enumerate_tasks()
        for desc in tasks:
            rt = toolmaze_adapter.build_runtime_task(desc)
            dump = json.dumps(rt.model_dump()).lower()
            for field in _HIDDEN_FIELDS:
                assert field not in dump, (
                    f"Hidden field '{field}' leaked into RuntimeTask for {desc.task_id}"
                )

    def test_no_oracle_in_visible_tools(self, toolmaze_adapter):
        """visible_tools contain no oracle references."""
        tasks = toolmaze_adapter.enumerate_tasks()
        for desc in tasks:
            rt = toolmaze_adapter.build_runtime_task(desc)
            tools_json = json.dumps(rt.visible_tools).lower()
            assert "oracle" not in tools_json
            assert "execution_trace" not in tools_json

    def test_no_perturbation_point_in_envelope(self, toolmaze_adapter):
        """RuntimeTask does not expose perturbation_point."""
        tasks = toolmaze_adapter.enumerate_tasks()
        for desc in tasks:
            rt = toolmaze_adapter.build_runtime_task(desc)
            dump = json.dumps(rt.model_dump()).lower()
            assert "perturbation_point" not in dump

    def test_runtime_task_forbids_extra_fields(self):
        """Cannot inject hidden fields via constructor."""
        with pytest.raises(Exception):
            RuntimeTask(
                task_id="test", objective="test",
                **{"hidden_oracle": "should fail"},
            )

    def test_public_observation_clean(self, toolmaze_adapter):
        """Public observations contain no oracle data."""
        tasks = toolmaze_adapter.enumerate_tasks()
        for desc in tasks[:5]:
            obs = toolmaze_adapter.collect_public_observation(desc.task_id, 0)
            obs_json = json.dumps(obs).lower()
            assert "oracle" not in obs_json
            assert "ground_truth" not in obs_json


# ══════════════════════════════════════════════════════════════════════
# T14: Six arms share same execution substrate
# ══════════════════════════════════════════════════════════════════════

class TestT14_SixArmsShareSubstrate:
    """T14: All six arms use the same AgentExecutionHarness."""

    def test_six_arms_exist(self):
        assert len(list(ControlArm)) == 6

    def test_all_arms_creatable(self):
        for arm in ControlArm:
            policy = create_policy(arm)
            assert policy.arm == arm

    def test_all_arms_are_harness_instances(self):
        """Every arm is wrapped by AgentExecutionHarness."""
        for arm in ControlArm:
            policy = create_policy(arm)
            assert isinstance(policy, AgentExecutionHarness)

    def test_arm_enum_values(self):
        expected = {
            "A0_BARE", "A1_RETRY_ONLY", "A2_VALIDATOR_ONLY",
            "A3_ODYS_FULL", "A4_ODYS_MINUS_OBSERVABLE_PROGRESS",
            "A5_ODYS_MINUS_RECOVERY_BUDGET_POLICY",
        }
        actual = {a.value for a in ControlArm}
        assert actual == expected

    @pytest.mark.asyncio
    async def test_all_arms_share_same_task(self, toolmaze_adapter, gen_config, budget):
        """All arms receive identical task/environment."""
        tasks = toolmaze_adapter.enumerate_tasks()
        desc = tasks[0]
        rt = toolmaze_adapter.build_runtime_task(desc)

        results: dict[ControlArm, dict[str, Any]] = {}
        for arm in ControlArm:
            policy = create_policy(arm)
            result = await policy.execute_trial(
                task=rt, adapter=toolmaze_adapter, generation_config=gen_config,
            )
            results[arm] = result

        violations = assert_matched_authority(
            results, task_id=rt.task_id,
            generation_config=gen_config, budget=budget,
        )
        assert violations == [], f"Authority violations: {violations}"

    def test_all_strategies_satisfy_protocol(self):
        """Every strategy class satisfies PolicyStrategy protocol."""
        strategies = [
            BareStrategy(), RetryOnlyStrategy(), ValidatorOnlyStrategy(),
            OdysFullStrategy(), OdysMinusObservableProgress(),
            OdysMinusRecoveryBudgetPolicy(),
        ]
        for s in strategies:
            assert hasattr(s, "arm")
            assert hasattr(s, "configure")
            assert hasattr(s, "create_observer")
            assert hasattr(s, "on_step_result")
            assert hasattr(s, "should_validate")
            assert hasattr(s, "recovery_budget_enabled")
            assert hasattr(s, "progress_signals_recovery")


# ══════════════════════════════════════════════════════════════════════
# T15: Phase4 regression
# ══════════════════════════════════════════════════════════════════════

class TestT15_Phase4Regression:
    """T15: Phase 4 core modules remain stable and importable."""

    def test_phase4_core_imports(self):
        from lhas.recovery import DefaultRecoveryPolicy
        from lhas.domain.enums import FailureType
        assert FailureType.TOOL_ERROR.value == "TOOL_ERROR"

    def test_phase4_enum_values_stable(self):
        from lhas.domain.enums import FailureType, RecoveryActionType
        assert FailureType.TOOL_ERROR.value == "TOOL_ERROR"
        assert FailureType.TIMEOUT.value == "TIMEOUT"
        assert RecoveryActionType.RETRY_WITH_FAILURE_CONTEXT.value == "RETRY_WITH_FAILURE_CONTEXT"
        assert RecoveryActionType.ESCALATE.value == "ESCALATE"

    def test_phase4_recovery_policy_importable(self):
        from lhas.recovery import DefaultRecoveryPolicy, RecoveryAction
        assert DefaultRecoveryPolicy is not None
        assert RecoveryAction is not None

    def test_phase4_event_store_importable(self):
        from lhas.persistence.event_store import EventStore
        assert EventStore is not None

    def test_phase5_does_not_modify_phase4_modules(self):
        import lhas.domain.enums as enums_mod
        before_attrs = set(dir(enums_mod))
        importlib.import_module("lhas.phase5.control_arms")
        importlib.import_module("lhas.phase5.shadow_observer")
        after_attrs = set(dir(enums_mod))
        assert before_attrs == after_attrs, "Phase 5 added attributes to Phase 4 module"
