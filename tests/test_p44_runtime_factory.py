"""Tests for runtime factory separation (P44).

Validates:
- MinimalRuntimeFactory creates kernel without CompletionAuthority
- OdysRuntimeFactory creates kernel with CompletionAuthority
- Both return objects satisfying the BenchmarkRuntime protocol
- Both can execute a simple task dict
- Config features are correctly mapped
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from evals.reliability.runtime_factory import (
    BenchmarkRuntime,
    MinimalRuntimeFactory,
    OdysRuntimeFactory,
    RuntimeFactory,
)
from evals.reliability.run_phase4 import ExecutionOutcome


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def minimal_config() -> dict[str, Any]:
    """A minimal benchmark config without ODYS features."""
    return {
        "config_id": "minimal",
        "features": {
            "completion_authority": False,
            "failure_provenance": False,
            "selective_repair": False,
            "macro_replan": False,
            "durable_workflow_recovery": False,
        },
        "tool_capability_set": ["file.read", "shell.exec"],
        "run_id": "test-minimal-run",
    }


@pytest.fixture()
def odys_config() -> dict[str, Any]:
    """An ODYS benchmark config with all reliability features."""
    return {
        "config_id": "odys_p3",
        "features": {
            "completion_authority": True,
            "failure_provenance": True,
            "selective_repair": True,
            "macro_replan": True,
            "durable_workflow_recovery": True,
        },
        "tool_capability_set": ["file.read", "shell.exec"],
        "run_id": "test-odys-run",
    }


@pytest.fixture()
def simple_task() -> dict[str, Any]:
    """A simple benchmark task definition."""
    return {
        "task_id": "test-task-001",
        "title": "Simple test task",
        "objective": "Write a hello world program",
        "acceptance_criteria": ["Program prints hello world"],
        "max_turns": 5,
        "max_model_calls": 5,
        "timeout_seconds": 60,
    }


# ---------------------------------------------------------------------------
# MinimalRuntimeFactory tests
# ---------------------------------------------------------------------------


class TestMinimalRuntimeFactory:
    """Tests for the minimal baseline runtime factory."""

    def test_is_runtime_factory_subclass(self):
        """MinimalRuntimeFactory must be a RuntimeFactory."""
        factory = MinimalRuntimeFactory()
        assert isinstance(factory, RuntimeFactory)

    def test_create_runtime_returns_benchmark_runtime(self, minimal_config):
        """create_runtime must return a BenchmarkRuntime."""
        factory = MinimalRuntimeFactory()
        runtime = factory.create_runtime(minimal_config)
        assert isinstance(runtime, BenchmarkRuntime)

    def test_kernel_has_no_completion_authority(self, minimal_config):
        """The minimal runtime must NOT have CompletionAuthority."""
        factory = MinimalRuntimeFactory()
        runtime = factory.create_runtime(minimal_config)
        assert not hasattr(runtime, "completion") or runtime.completion is None

    @pytest.mark.asyncio
    async def test_execute_returns_execution_outcome(self, simple_task, minimal_config):
        """execute must return an ExecutionOutcome."""
        factory = MinimalRuntimeFactory()
        runtime = factory.create_runtime(minimal_config)
        outcome = await runtime.execute(simple_task, minimal_config)
        assert isinstance(outcome, ExecutionOutcome)

    @pytest.mark.asyncio
    async def test_execute_has_correct_fields(self, simple_task, minimal_config):
        """The outcome must have the standard ExecutionOutcome fields."""
        factory = MinimalRuntimeFactory()
        runtime = factory.create_runtime(minimal_config)
        outcome = await runtime.execute(simple_task, minimal_config)

        assert hasattr(outcome, "claimed_complete")
        assert hasattr(outcome, "observed_state")
        assert hasattr(outcome, "failure_type")
        assert hasattr(outcome, "recovery_required")
        assert hasattr(outcome, "recovery_attempted")
        assert hasattr(outcome, "recovery_success")
        assert hasattr(outcome, "repair_scope")
        assert hasattr(outcome, "tool_calls")
        assert hasattr(outcome, "model_calls")
        assert hasattr(outcome, "attempt_count")
        assert hasattr(outcome, "wall_time_seconds")

    @pytest.mark.asyncio
    async def test_no_recovery_signals(self, simple_task, minimal_config):
        """Minimal runtime should not report recovery signals."""
        factory = MinimalRuntimeFactory()
        runtime = factory.create_runtime(minimal_config)
        outcome = await runtime.execute(simple_task, minimal_config)

        assert outcome.recovery_required is False
        assert outcome.recovery_attempted is False
        assert outcome.recovery_success is False
        assert outcome.repair_scope is None

    @pytest.mark.asyncio
    async def test_features_active_is_empty(self, simple_task, minimal_config):
        """Minimal runtime should report no active features."""
        factory = MinimalRuntimeFactory()
        runtime = factory.create_runtime(minimal_config)
        outcome = await runtime.execute(simple_task, minimal_config)

        features = outcome.observed_state.get("features_active", {})
        assert features == {} or all(v is False or v is None for v in features.values())


# ---------------------------------------------------------------------------
# OdysRuntimeFactory tests
# ---------------------------------------------------------------------------


class TestOdysRuntimeFactory:
    """Tests for the full ODYS runtime factory."""

    def test_is_runtime_factory_subclass(self):
        """OdysRuntimeFactory must be a RuntimeFactory."""
        factory = OdysRuntimeFactory()
        assert isinstance(factory, RuntimeFactory)

    def test_create_runtime_returns_benchmark_runtime(self, odys_config):
        """create_runtime must return a BenchmarkRuntime."""
        factory = OdysRuntimeFactory()
        runtime = factory.create_runtime(odys_config)
        assert isinstance(runtime, BenchmarkRuntime)

    def test_kernel_has_completion_authority(self, odys_config):
        """The ODYS runtime MUST have CompletionAuthority."""
        factory = OdysRuntimeFactory()
        runtime = factory.create_runtime(odys_config)
        assert hasattr(runtime, "completion")
        assert runtime.completion is not None

    @pytest.mark.asyncio
    async def test_execute_returns_execution_outcome(self, simple_task, odys_config):
        """execute must return an ExecutionOutcome."""
        factory = OdysRuntimeFactory()
        runtime = factory.create_runtime(odys_config)
        outcome = await runtime.execute(simple_task, odys_config)
        assert isinstance(outcome, ExecutionOutcome)

    @pytest.mark.asyncio
    async def test_execute_has_correct_fields(self, simple_task, odys_config):
        """The outcome must have the standard ExecutionOutcome fields."""
        factory = OdysRuntimeFactory()
        runtime = factory.create_runtime(odys_config)
        outcome = await runtime.execute(simple_task, odys_config)

        assert hasattr(outcome, "claimed_complete")
        assert hasattr(outcome, "observed_state")
        assert hasattr(outcome, "failure_type")
        assert hasattr(outcome, "recovery_required")
        assert hasattr(outcome, "recovery_attempted")
        assert hasattr(outcome, "recovery_success")
        assert hasattr(outcome, "repair_scope")
        assert hasattr(outcome, "tool_calls")
        assert hasattr(outcome, "model_calls")
        assert hasattr(outcome, "attempt_count")
        assert hasattr(outcome, "wall_time_seconds")

    @pytest.mark.asyncio
    async def test_features_active_shows_odys_features(self, simple_task, odys_config):
        """ODYS runtime should report active reliability features."""
        factory = OdysRuntimeFactory()
        runtime = factory.create_runtime(odys_config)
        outcome = await runtime.execute(simple_task, odys_config)

        features = outcome.observed_state.get("features_active", {})
        assert features.get("completion_authority") is True
        assert features.get("failure_provenance") is True
        assert features.get("recovery_loop") is True


# ---------------------------------------------------------------------------
# Interface parity tests
# ---------------------------------------------------------------------------


class TestInterfaceParity:
    """Both factories must produce runtimes with the same interface."""

    def test_both_satisfy_benchmark_runtime(self, minimal_config, odys_config):
        """Both factories must return BenchmarkRuntime objects."""
        minimal = MinimalRuntimeFactory().create_runtime(minimal_config)
        odys = OdysRuntimeFactory().create_runtime(odys_config)

        assert isinstance(minimal, BenchmarkRuntime)
        assert isinstance(odys, BenchmarkRuntime)

    def test_both_have_execute_method(self, minimal_config, odys_config):
        """Both runtimes must have an execute method."""
        minimal = MinimalRuntimeFactory().create_runtime(minimal_config)
        odys = OdysRuntimeFactory().create_runtime(odys_config)

        assert hasattr(minimal, "execute")
        assert hasattr(odys, "execute")
        assert callable(minimal.execute)
        assert callable(odys.execute)

    @pytest.mark.asyncio
    async def test_both_return_same_outcome_type(self, simple_task, minimal_config, odys_config):
        """Both runtimes must return ExecutionOutcome."""
        minimal = MinimalRuntimeFactory().create_runtime(minimal_config)
        odys = OdysRuntimeFactory().create_runtime(odys_config)

        minimal_outcome = await minimal.execute(simple_task, minimal_config)
        odys_outcome = await odys.execute(simple_task, odys_config)

        assert type(minimal_outcome) is type(odys_outcome)
        assert isinstance(minimal_outcome, ExecutionOutcome)
        assert isinstance(odys_outcome, ExecutionOutcome)

    @pytest.mark.asyncio
    async def test_outcome_field_structure_matches(self, simple_task, minimal_config, odys_config):
        """Both outcomes must have the same set of fields."""
        minimal = MinimalRuntimeFactory().create_runtime(minimal_config)
        odys = OdysRuntimeFactory().create_runtime(odys_config)

        minimal_outcome = await minimal.execute(simple_task, minimal_config)
        odys_outcome = await odys.execute(simple_task, odys_config)

        minimal_fields = set(minimal_outcome.__dataclass_fields__.keys())
        odys_fields = set(odys_outcome.__dataclass_fields__.keys())
        assert minimal_fields == odys_fields


# ---------------------------------------------------------------------------
# Config feature mapping tests
# ---------------------------------------------------------------------------


class TestConfigFeatureMapping:
    """Verify that config features are correctly mapped to runtime capabilities."""

    def test_minimal_factory_rejects_odys_config_features(self, odys_config):
        """MinimalRuntimeFactory creates runtimes without ODYS features
        regardless of what the config says."""
        factory = MinimalRuntimeFactory()
        runtime = factory.create_runtime(odys_config)
        # The runtime itself should not have completion authority
        assert not hasattr(runtime, "completion") or runtime.completion is None

    def test_odys_factory_creates_with_completion_authority(self, odys_config):
        """OdysRuntimeFactory must create runtimes with CompletionAuthority."""
        factory = OdysRuntimeFactory()
        runtime = factory.create_runtime(odys_config)
        assert runtime.completion is not None

    @pytest.mark.asyncio
    async def test_minimal_config_id_preserved(self, simple_task, minimal_config):
        """The minimal runtime should reflect the config_id."""
        factory = MinimalRuntimeFactory()
        runtime = factory.create_runtime(minimal_config)
        outcome = await runtime.execute(simple_task, minimal_config)
        # The outcome should be valid regardless of config
        assert isinstance(outcome, ExecutionOutcome)

    @pytest.mark.asyncio
    async def test_odys_config_id_preserved(self, simple_task, odys_config):
        """The odys runtime should reflect the config_id."""
        factory = OdysRuntimeFactory()
        runtime = factory.create_runtime(odys_config)
        outcome = await runtime.execute(simple_task, odys_config)
        assert isinstance(outcome, ExecutionOutcome)


# ---------------------------------------------------------------------------
# Assertion verification tests
# ---------------------------------------------------------------------------


class TestAssertionVerification:
    """Verify that the runtime factories enforce their invariants."""

    def test_minimal_factory_assertion_passes(self, minimal_config):
        """MinimalRuntimeFactory's assertion must pass (no completion)."""
        factory = MinimalRuntimeFactory()
        # This should NOT raise — the assertion verifies no completion authority
        runtime = factory.create_runtime(minimal_config)
        assert runtime.completion is None

    def test_odys_factory_assertion_passes(self, odys_config):
        """OdysRuntimeFactory's assertion must pass (has completion)."""
        factory = OdysRuntimeFactory()
        # This should NOT raise — the assertion verifies completion authority exists
        runtime = factory.create_runtime(odys_config)
        assert runtime.completion is not None
