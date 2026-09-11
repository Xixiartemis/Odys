"""Tests for evals.reliability.odys_executor.

Validates:
- BenchmarkFaultInjector fault type → NativeFaultPoint mapping
- OdysRuntimeExecutor with a mock kernel
- ExecutionOutcome mapping from AgentResult
- Config feature propagation
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from evals.reliability.odys_executor import (
    BenchmarkFaultInjector,
    OdysRuntimeExecutor,
    create_executor,
    _FAULT_TYPE_TO_BOUNDARY,
    _FAULT_TYPE_TO_NATIVE_POINT,
)
from evals.reliability.run_phase4 import (
    ExecutionOutcome,
    ExecutionRequest,
    FaultContext,
    FaultPlan,
    FixtureHandle,
    NOT_MEASURED,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def fault_plan_factory():
    """Factory for creating FaultPlan instances."""
    def _make(fault_id="TEST_FAULT", fault_type="tool_failure", trigger="tool_call ordinal == 1"):
        return FaultPlan(
            fault_id=fault_id,
            fault_type=fault_type,
            trigger=trigger,
            trigger_count=1,
            deterministic_seed=42,
            definition={"fault_id": fault_id, "fault_type": fault_type},
        )
    return _make


@pytest.fixture()
def fixture_handle():
    return FixtureHandle(
        fixture_id="test-fixture",
        version="1",
        initial_state="clean",
        fixture_hash="abc123",
        metadata={},
    )


@pytest.fixture()
def execution_request_factory(fixture_handle):
    """Factory for creating benchmark ExecutionRequest instances."""
    def _make(
        run_id="CI-01::minimal::repeat-1",
        task_id="CI-01",
        fault_type="tool_failure",
        fault_id="FAIL_TOOL_ON_CALL_1",
        trigger="tool_call ordinal == 1",
        config_features=None,
    ):
        task = {
            "task_id": task_id,
            "objective": "Test objective",
            "acceptance_criteria": ["criterion 1"],
            "max_turns": 20,
            "max_model_calls": 20,
        }
        config = {
            "config_id": "minimal",
            "features": config_features or {
                "completion_authority": False,
                "selective_repair": False,
                "failure_provenance": False,
            },
            "tool_capability_set": ["workspace.read", "workspace.edit"],
        }
        fault = FaultPlan(
            fault_id=fault_id,
            fault_type=fault_type,
            trigger=trigger,
            trigger_count=1,
            deterministic_seed=42,
            definition={"fault_id": fault_id, "fault_type": fault_type},
        )
        return ExecutionRequest(
            run_id=run_id,
            repeat_index=1,
            task=task,
            config=config,
            fixture=fixture_handle,
            fault=fault,
            fault_context=FaultContext(fault),
        )
    return _make


def _make_agent_result(
    status="COMPLETED",
    final_output="Done",
    completion_claim=True,
    turn_count=1,
    tool_call_count=0,
    usage=None,
    artifacts=None,
    error_type=None,
    error_message=None,
    safe_trace=None,
):
    """Create a mock AgentResult."""
    from lhas.agent.models import AgentResult, AgentStatus
    return AgentResult(
        status=AgentStatus(status),
        final_output=final_output,
        completion_claim=completion_claim,
        turn_count=turn_count,
        tool_call_count=tool_call_count,
        usage=usage or {},
        artifacts=artifacts or {},
        safe_trace=safe_trace or [],
        error_type=error_type,
        error_message=error_message,
    )


def _make_snapshot():
    """Create a minimal ProtocolSnapshot mock."""
    snapshot = MagicMock()
    snapshot.configs = {
        "minimal": {
            "config_id": "minimal",
            "features": {
                "completion_authority": False,
                "selective_repair": False,
                "failure_provenance": False,
            },
            "tool_capability_set": ["workspace.read"],
        },
        "odys_p3": {
            "config_id": "odys_p3",
            "features": {
                "completion_authority": True,
                "selective_repair": True,
                "failure_provenance": True,
            },
            "tool_capability_set": ["workspace.read", "workspace.edit", "cli.exec"],
        },
    }
    return snapshot


# ---------------------------------------------------------------------------
# BenchmarkFaultInjector tests
# ---------------------------------------------------------------------------

class TestBenchmarkFaultInjector:
    """Test fault type → NativeFaultPoint mapping and injection logic."""

    def test_tool_failure_maps_to_after_tool_requested(self, fault_plan_factory):
        plan = fault_plan_factory(fault_type="tool_failure")
        ctx = FaultContext(plan)
        injector = BenchmarkFaultInjector(ctx)
        assert injector.native_point == "AFTER_TOOL_REQUESTED"
        assert injector._boundary == "tool_call"

    def test_provider_timeout_maps_to_after_model_turn(self, fault_plan_factory):
        plan = fault_plan_factory(fault_type="provider_timeout", trigger="provider_call ordinal == 1")
        ctx = FaultContext(plan)
        injector = BenchmarkFaultInjector(ctx)
        assert injector.native_point == "AFTER_MODEL_TURN_PERSISTED"
        assert injector._boundary == "provider_call"

    def test_quota_exhausted_maps_to_after_model_turn(self, fault_plan_factory):
        plan = fault_plan_factory(fault_type="quota_exhausted", trigger="first provider request")
        ctx = FaultContext(plan)
        injector = BenchmarkFaultInjector(ctx)
        assert injector.native_point == "AFTER_MODEL_TURN_PERSISTED"
        assert injector._boundary == "provider_call"

    def test_stale_workspace_maps_to_after_candidate_persisted(self, fault_plan_factory):
        plan = fault_plan_factory(fault_type="stale_workspace", trigger="before dispatch eligibility check")
        ctx = FaultContext(plan)
        injector = BenchmarkFaultInjector(ctx)
        assert injector.native_point == "AFTER_CANDIDATE_PERSISTED"
        assert injector._boundary == "workspace"

    def test_capability_unavailable_maps_to_after_tool_requested(self, fault_plan_factory):
        plan = fault_plan_factory(fault_type="capability_unavailable", trigger="capability resolution")
        ctx = FaultContext(plan)
        injector = BenchmarkFaultInjector(ctx)
        assert injector.native_point == "AFTER_TOOL_REQUESTED"
        assert injector._boundary == "capability"

    def test_partial_output_maps_to_after_candidate_persisted(self, fault_plan_factory):
        plan = fault_plan_factory(fault_type="partial_output", trigger="completion claim before artifact")
        ctx = FaultContext(plan)
        injector = BenchmarkFaultInjector(ctx)
        assert injector.native_point == "AFTER_CANDIDATE_PERSISTED"
        assert injector._boundary == "completion"

    def test_unknown_fault_type_defaults_to_model_turn(self, fault_plan_factory):
        plan = fault_plan_factory(fault_type="unknown_type")
        ctx = FaultContext(plan)
        injector = BenchmarkFaultInjector(ctx)
        assert injector.native_point == "AFTER_MODEL_TURN_PERSISTED"
        assert injector._boundary == "tool_call"

    def test_hit_fires_at_matching_point(self, fault_plan_factory):
        from lhas.native.models import NativeFaultPoint
        plan = fault_plan_factory(fault_type="tool_failure", trigger="tool_call ordinal == 1")
        ctx = FaultContext(plan)
        injector = BenchmarkFaultInjector(ctx)
        assert not injector.fired
        injector.hit(NativeFaultPoint.AFTER_TOOL_REQUESTED)
        assert injector.fired
        assert injector.fired_point == "AFTER_TOOL_REQUESTED"

    def test_hit_ignores_non_matching_point(self, fault_plan_factory):
        from lhas.native.models import NativeFaultPoint
        plan = fault_plan_factory(fault_type="tool_failure", trigger="tool_call ordinal == 1")
        ctx = FaultContext(plan)
        injector = BenchmarkFaultInjector(ctx)
        injector.hit(NativeFaultPoint.AFTER_MODEL_TURN_PERSISTED)
        assert not injector.fired

    def test_hit_only_fires_once(self, fault_plan_factory):
        from lhas.native.models import NativeFaultPoint
        plan = fault_plan_factory(fault_type="tool_failure", trigger="tool_call ordinal == 1")
        ctx = FaultContext(plan)
        injector = BenchmarkFaultInjector(ctx)
        injector.hit(NativeFaultPoint.AFTER_TOOL_REQUESTED)
        assert injector.fired
        # Second call should be a no-op
        injector.hit(NativeFaultPoint.AFTER_TOOL_REQUESTED)
        assert injector.fired

    def test_all_fault_types_have_mappings(self):
        """Every fault type in the frozen protocol should have a mapping."""
        expected_types = {
            "tool_failure", "interruption", "provider_timeout",
            "provider_unavailable", "quota_exhausted", "malformed_response",
            "assumption_invalidated", "stale_workspace", "capability_unavailable",
            "duplicate_delivery", "partial_output",
        }
        assert set(_FAULT_TYPE_TO_BOUNDARY.keys()) == expected_types
        assert set(_FAULT_TYPE_TO_NATIVE_POINT.keys()) == expected_types


# ---------------------------------------------------------------------------
# OdysRuntimeExecutor outcome mapping tests
# ---------------------------------------------------------------------------

class TestOdysRuntimeExecutorOutcomeMapping:
    """Test ExecutionOutcome mapping from AgentResult."""

    def test_completed_result_maps_to_claimed_complete(self, execution_request_factory):
        kernel = MagicMock()
        snapshot = _make_snapshot()
        config = snapshot.configs["minimal"]
        executor = OdysRuntimeExecutor(kernel=kernel, config=config, snapshot=snapshot)

        result = _make_agent_result(status="COMPLETED", final_output="Done", turn_count=2, tool_call_count=3)
        request = execution_request_factory()
        outcome = executor._map_outcome(result, request, BenchmarkFaultInjector(request.fault_context))

        assert outcome.claimed_complete is True
        assert outcome.failure_type is None
        assert outcome.tool_calls == 3
        assert outcome.model_calls == 2
        assert outcome.observed_state["agent_status"] == "COMPLETED"

    def test_failed_result_maps_to_failure_type(self, execution_request_factory):
        kernel = MagicMock()
        snapshot = _make_snapshot()
        config = snapshot.configs["minimal"]
        executor = OdysRuntimeExecutor(kernel=kernel, config=config, snapshot=snapshot)

        result = _make_agent_result(status="FAILED", error_type="BUDGET_EXHAUSTED")
        request = execution_request_factory()
        outcome = executor._map_outcome(result, request, BenchmarkFaultInjector(request.fault_context))

        assert outcome.claimed_complete is False
        assert outcome.failure_type == "BUDGET_EXHAUSTED"
        assert outcome.observed_state["agent_status"] == "FAILED"

    def test_usage_maps_to_token_counts(self, execution_request_factory):
        kernel = MagicMock()
        snapshot = _make_snapshot()
        config = snapshot.configs["minimal"]
        executor = OdysRuntimeExecutor(kernel=kernel, config=config, snapshot=snapshot)

        usage = {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}
        result = _make_agent_result(usage=usage)
        request = execution_request_factory()
        outcome = executor._map_outcome(result, request, BenchmarkFaultInjector(request.fault_context))

        assert outcome.tokens_input == 1000
        assert outcome.tokens_output == 500
        assert outcome.total_tokens == 1500

    def test_usage_computes_total_when_missing(self, execution_request_factory):
        kernel = MagicMock()
        snapshot = _make_snapshot()
        config = snapshot.configs["minimal"]
        executor = OdysRuntimeExecutor(kernel=kernel, config=config, snapshot=snapshot)

        usage = {"prompt_tokens": 1000, "completion_tokens": 500}
        result = _make_agent_result(usage=usage)
        request = execution_request_factory()
        outcome = executor._map_outcome(result, request, BenchmarkFaultInjector(request.fault_context))

        assert outcome.total_tokens == 1500

    def test_artifacts_map_to_observed_state(self, execution_request_factory):
        kernel = MagicMock()
        snapshot = _make_snapshot()
        config = snapshot.configs["minimal"]
        executor = OdysRuntimeExecutor(kernel=kernel, config=config, snapshot=snapshot)

        artifacts = {"completion_candidate_id": "c123", "execution_snapshot_id": "s456"}
        result = _make_agent_result(artifacts=artifacts)
        request = execution_request_factory()
        outcome = executor._map_outcome(result, request, BenchmarkFaultInjector(request.fault_context))

        assert "artifacts" in outcome.observed_state
        assert outcome.observed_state["artifacts"]["completion_candidate_id"] == "c123"

    def test_recovery_detection_from_trace(self, execution_request_factory):
        kernel = MagicMock()
        snapshot = _make_snapshot()
        config = snapshot.configs["minimal"]
        executor = OdysRuntimeExecutor(kernel=kernel, config=config, snapshot=snapshot)

        safe_trace = [
            {"status": "FAILURE", "error_type": "TEST_TOOL_FAILURE"},
            {"status": "SUCCESS", "recovered_from_durable_invocation": True},
        ]
        result = _make_agent_result(status="COMPLETED", safe_trace=safe_trace)
        request = execution_request_factory()
        outcome = executor._map_outcome(result, request, BenchmarkFaultInjector(request.fault_context))

        assert outcome.recovery_required is True
        assert outcome.recovery_attempted is True
        assert outcome.recovery_success is True

    def test_error_outcome_structure(self, execution_request_factory):
        kernel = MagicMock()
        snapshot = _make_snapshot()
        config = snapshot.configs["minimal"]
        executor = OdysRuntimeExecutor(kernel=kernel, config=config, snapshot=snapshot)

        request = execution_request_factory()
        injector = BenchmarkFaultInjector(request.fault_context)
        outcome = executor._error_outcome(RuntimeError("test error"), request, injector)

        assert outcome.claimed_complete is False
        assert "EXECUTOR_ERROR:RuntimeError" in outcome.failure_type
        assert "test error" in outcome.observed_state["error"]


# ---------------------------------------------------------------------------
# Config feature tests
# ---------------------------------------------------------------------------

class TestConfigFeatures:
    """Test that config features are respected."""

    def test_selective_repair_disabled_returns_none_scope(self, execution_request_factory):
        kernel = MagicMock()
        snapshot = _make_snapshot()
        config = dict(snapshot.configs["minimal"])
        config["features"] = {"selective_repair": False}
        executor = OdysRuntimeExecutor(kernel=kernel, config=config, snapshot=snapshot)

        safe_trace = [{"error_type": "VALIDATOR_REJECTION"}]
        result = _make_agent_result(status="COMPLETED", safe_trace=safe_trace)
        request = execution_request_factory(config_features={"selective_repair": False})
        outcome = executor._map_outcome(result, request, BenchmarkFaultInjector(request.fault_context))

        assert outcome.repair_scope is None

    def test_selective_repair_enabled_determines_scope(self, execution_request_factory):
        kernel = MagicMock()
        snapshot = _make_snapshot()
        config = dict(snapshot.configs["odys_p3"])
        executor = OdysRuntimeExecutor(kernel=kernel, config=config, snapshot=snapshot)

        safe_trace = [{"error_type": "VALIDATOR_REJECTION"}]
        result = _make_agent_result(status="COMPLETED", safe_trace=safe_trace)
        request = execution_request_factory(
            config_features={"selective_repair": True, "completion_authority": True}
        )
        outcome = executor._map_outcome(result, request, BenchmarkFaultInjector(request.fault_context))

        assert outcome.repair_scope == "local"

    def test_features_propagated_to_agent_request_context(self, execution_request_factory):
        kernel = MagicMock()
        snapshot = _make_snapshot()
        config = dict(snapshot.configs["odys_p3"])
        executor = OdysRuntimeExecutor(kernel=kernel, config=config, snapshot=snapshot)

        request = execution_request_factory(
            config_features={"completion_authority": True, "selective_repair": True}
        )
        injector = BenchmarkFaultInjector(request.fault_context)
        agent_req = executor._build_agent_request(request, injector)

        assert agent_req.context["benchmark_features"]["completion_authority"] is True
        assert agent_req.context["benchmark_features"]["selective_repair"] is True


# ---------------------------------------------------------------------------
# Full integration tests with mock kernel
# ---------------------------------------------------------------------------

class TestOdysRuntimeExecutorIntegration:
    """Test OdysRuntimeExecutor with a mock kernel."""

    @pytest.mark.asyncio
    async def test_execute_maps_kernel_result_to_outcome(self, execution_request_factory):
        from lhas.agent.models import AgentResult, AgentStatus

        mock_kernel = MagicMock()
        mock_kernel.fault_injector = None

        async def mock_run(request):
            return AgentResult(
                status=AgentStatus.COMPLETED,
                final_output="Task completed successfully",
                completion_claim=True,
                turn_count=3,
                tool_call_count=5,
                usage={"prompt_tokens": 500, "completion_tokens": 200, "total_tokens": 700},
                artifacts={"completion_candidate_id": "c1"},
            )

        mock_kernel.run = mock_run

        snapshot = _make_snapshot()
        config = snapshot.configs["minimal"]
        executor = OdysRuntimeExecutor(kernel=mock_kernel, config=config, snapshot=snapshot)

        request = execution_request_factory()
        outcome = await executor.execute(request)

        assert outcome.claimed_complete is True
        assert outcome.failure_type is None
        assert outcome.tool_calls == 5
        assert outcome.model_calls == 3
        assert outcome.tokens_input == 500
        assert outcome.tokens_output == 200
        assert outcome.total_tokens == 700
        assert outcome.observed_state["agent_status"] == "COMPLETED"

    @pytest.mark.asyncio
    async def test_execute_handles_kernel_exception(self, execution_request_factory):
        mock_kernel = MagicMock()
        mock_kernel.fault_injector = None

        async def mock_run(request):
            raise RuntimeError("Kernel exploded")

        mock_kernel.run = mock_run

        snapshot = _make_snapshot()
        config = snapshot.configs["minimal"]
        executor = OdysRuntimeExecutor(kernel=mock_kernel, config=config, snapshot=snapshot)

        request = execution_request_factory()
        outcome = await executor.execute(request)

        assert outcome.claimed_complete is False
        assert "EXECUTOR_ERROR:RuntimeError" in outcome.failure_type
        assert "Kernel exploded" in outcome.observed_state["error"]

    @pytest.mark.asyncio
    async def test_execute_restores_original_injector(self, execution_request_factory):
        from lhas.agent.models import AgentResult, AgentStatus

        original_injector = MagicMock()
        mock_kernel = MagicMock()
        mock_kernel.fault_injector = original_injector

        async def mock_run(request):
            return AgentResult(status=AgentStatus.COMPLETED)

        mock_kernel.run = mock_run

        snapshot = _make_snapshot()
        config = snapshot.configs["minimal"]
        executor = OdysRuntimeExecutor(kernel=mock_kernel, config=config, snapshot=snapshot)

        request = execution_request_factory()
        await executor.execute(request)

        assert mock_kernel.fault_injector is original_injector

    @pytest.mark.asyncio
    async def test_execute_injects_benchmark_fault_injector(self, execution_request_factory):
        from lhas.agent.models import AgentResult, AgentStatus

        captured_injector = None

        async def mock_run(request):
            nonlocal captured_injector
            # The kernel should see a BenchmarkFaultInjector
            from evals.reliability.odys_executor import BenchmarkFaultInjector
            # The fault_injector is set on the kernel, not passed to run()
            return AgentResult(status=AgentStatus.COMPLETED)

        mock_kernel = MagicMock()
        mock_kernel.fault_injector = None
        mock_kernel.run = mock_run

        snapshot = _make_snapshot()
        config = snapshot.configs["minimal"]
        executor = OdysRuntimeExecutor(kernel=mock_kernel, config=config, snapshot=snapshot)

        request = execution_request_factory()
        await executor.execute(request)

        # After execution, the original injector should be restored (None)
        assert mock_kernel.fault_injector is None


# ---------------------------------------------------------------------------
# AgentRequest construction tests
# ---------------------------------------------------------------------------

class TestAgentRequestConstruction:
    """Test that AgentRequest is correctly built from ExecutionRequest."""

    def test_agent_request_has_correct_objective(self, execution_request_factory):
        kernel = MagicMock()
        snapshot = _make_snapshot()
        config = snapshot.configs["minimal"]
        executor = OdysRuntimeExecutor(kernel=kernel, config=config, snapshot=snapshot)

        request = execution_request_factory()
        injector = BenchmarkFaultInjector(request.fault_context)
        agent_req = executor._build_agent_request(request, injector)

        assert agent_req.objective == "Test objective"

    def test_agent_request_has_acceptance_criteria(self, execution_request_factory):
        kernel = MagicMock()
        snapshot = _make_snapshot()
        config = snapshot.configs["minimal"]
        executor = OdysRuntimeExecutor(kernel=kernel, config=config, snapshot=snapshot)

        request = execution_request_factory()
        injector = BenchmarkFaultInjector(request.fault_context)
        agent_req = executor._build_agent_request(request, injector)

        assert agent_req.context["acceptance_criteria"] == ["criterion 1"]

    def test_agent_request_has_budget_from_task(self, execution_request_factory):
        kernel = MagicMock()
        snapshot = _make_snapshot()
        config = snapshot.configs["minimal"]
        executor = OdysRuntimeExecutor(kernel=kernel, config=config, snapshot=snapshot)

        request = execution_request_factory()
        injector = BenchmarkFaultInjector(request.fault_context)
        agent_req = executor._build_agent_request(request, injector)

        assert agent_req.budget.max_turns == 20
        assert agent_req.budget.max_tool_calls == 20

    def test_agent_request_has_allowed_capabilities(self, execution_request_factory):
        kernel = MagicMock()
        snapshot = _make_snapshot()
        config = snapshot.configs["minimal"]
        executor = OdysRuntimeExecutor(kernel=kernel, config=config, snapshot=snapshot)

        request = execution_request_factory()
        injector = BenchmarkFaultInjector(request.fault_context)
        agent_req = executor._build_agent_request(request, injector)

        assert "workspace.read" in agent_req.allowed_capabilities
        assert "workspace.edit" in agent_req.allowed_capabilities

    def test_agent_request_has_metadata(self, execution_request_factory):
        kernel = MagicMock()
        snapshot = _make_snapshot()
        config = snapshot.configs["minimal"]
        executor = OdysRuntimeExecutor(kernel=kernel, config=config, snapshot=snapshot)

        request = execution_request_factory()
        injector = BenchmarkFaultInjector(request.fault_context)
        agent_req = executor._build_agent_request(request, injector)

        assert agent_req.metadata["task_id"] == "CI-01"
        assert agent_req.metadata["attempt_number"] == 1
        assert "benchmark_repeat_index" in agent_req.metadata
