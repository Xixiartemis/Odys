"""Tests for P4.5 benchmark executor (P45).

Validates:
- P45BenchmarkExecutor implements BenchmarkExecutor protocol
- Smoke run of 1 task x 2 configs produces valid ExecutionOutcome
- Fixture setup/reset lifecycle works through the executor
- Both minimal and odys_p3 configs produce correct outcomes
"""
import asyncio
import pytest
import shutil
import tempfile
from pathlib import Path

from evals.reliability.run_phase4 import ExecutionOutcome, ExecutionRequest, ProtocolSnapshot, RunSpec


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def snapshot() -> ProtocolSnapshot:
    return ProtocolSnapshot.load(Path("evals/reliability/phase4_v1"))


@pytest.fixture()
def executor():
    from evals.reliability.p45_executor import P45BenchmarkExecutor
    from evals.reliability.fixture_packages.registry import FixtureRegistry
    return P45BenchmarkExecutor(fixture_registry=FixtureRegistry(), factory_type="scripted")


def _make_request(snapshot: ProtocolSnapshot, task_id: str, config_name: str) -> ExecutionRequest:
    """Build an ExecutionRequest for a given task and config."""
    task = next(t for t in snapshot.tasks if t["task_id"] == task_id)
    config = snapshot.configs[config_name]
    spec = RunSpec(task=task, config=config, repeat_index=1)
    from evals.reliability.run_phase4 import FaultInjector, FaultContext, FixtureHandle
    injector = FaultInjector(snapshot)
    fault = injector.plan_for(task)
    fixture = FixtureHandle(
        fixture_id=task["fixture_id"],
        version=task["fixture_version"],
        initial_state="clean fixture workspace",
        fixture_hash="test-hash",
        metadata={},
    )
    return ExecutionRequest(
        run_id=spec.run_id,
        repeat_index=1,
        task=task,
        config=config,
        fixture=fixture,
        fault=fault,
        fault_context=FaultContext(fault),
    )


# ---------------------------------------------------------------------------
# 1. Protocol compliance
# ---------------------------------------------------------------------------

class TestP45ExecutorProtocol:
    def test_has_execute_method(self, executor):
        assert hasattr(executor, "execute")
        assert callable(executor.execute)

    def test_implements_benchmark_executor(self, executor):
        from evals.reliability.run_phase4 import BenchmarkExecutor
        # BenchmarkExecutor is a Protocol; check structural compatibility
        assert hasattr(executor, "execute")


# ---------------------------------------------------------------------------
# 2. Smoke run — minimal config
# ---------------------------------------------------------------------------

class TestP45SmokeMinimal:
    @pytest.mark.asyncio
    async def test_ci01_minimal_returns_outcome(self, executor, snapshot):
        request = _make_request(snapshot, "CI-01", "minimal")
        outcome = await executor.execute(request)
        assert isinstance(outcome, ExecutionOutcome)

    @pytest.mark.asyncio
    async def test_ci01_minimal_has_required_fields(self, executor, snapshot):
        request = _make_request(snapshot, "CI-01", "minimal")
        outcome = await executor.execute(request)
        assert hasattr(outcome, "claimed_complete")
        assert hasattr(outcome, "observed_state")
        assert hasattr(outcome, "failure_type")
        assert hasattr(outcome, "tool_calls")
        assert hasattr(outcome, "model_calls")
        assert isinstance(outcome.observed_state, dict)


# ---------------------------------------------------------------------------
# 3. Smoke run — odys_p3 config
# ---------------------------------------------------------------------------

class TestP45SmokeOdys:
    @pytest.mark.asyncio
    async def test_ci01_odys_returns_outcome(self, executor, snapshot):
        request = _make_request(snapshot, "CI-01", "odys_p3")
        outcome = await executor.execute(request)
        assert isinstance(outcome, ExecutionOutcome)

    @pytest.mark.asyncio
    async def test_ci01_odys_has_required_fields(self, executor, snapshot):
        request = _make_request(snapshot, "CI-01", "odys_p3")
        outcome = await executor.execute(request)
        assert hasattr(outcome, "claimed_complete")
        assert hasattr(outcome, "observed_state")
        assert isinstance(outcome.observed_state, dict)


# ---------------------------------------------------------------------------
# 4. Both configs produce ExecutionOutcome
# ---------------------------------------------------------------------------

class TestP45ConfigParity:
    @pytest.mark.parametrize("config_name", ["minimal", "odys_p3"])
    @pytest.mark.asyncio
    async def test_both_configs_return_outcome(self, executor, snapshot, config_name):
        request = _make_request(snapshot, "CI-01", config_name)
        outcome = await executor.execute(request)
        assert isinstance(outcome, ExecutionOutcome)
        assert isinstance(outcome.observed_state, dict)

    @pytest.mark.parametrize("config_name", ["minimal", "odys_p3"])
    @pytest.mark.asyncio
    async def test_both_configs_have_fixture_observations(self, executor, snapshot, config_name):
        request = _make_request(snapshot, "CI-01", config_name)
        outcome = await executor.execute(request)
        # Fixture observations should be present in observed_state
        assert "fixture_observations" in outcome.observed_state


# ---------------------------------------------------------------------------
# 5. Unknown config fails closed
# ---------------------------------------------------------------------------

class TestP45FailClosed:
    @pytest.mark.asyncio
    async def test_unknown_config_returns_error(self, executor, snapshot):
        task = next(t for t in snapshot.tasks if t["task_id"] == "CI-01")
        config = {"config_id": "unknown_config", "features": {}}
        from evals.reliability.run_phase4 import FaultInjector, FaultContext, FixtureHandle
        injector = FaultInjector(snapshot)
        fault = injector.plan_for(task)
        fixture = FixtureHandle(
            fixture_id=task["fixture_id"],
            version=task["fixture_version"],
            initial_state="clean fixture workspace",
            fixture_hash="test-hash",
            metadata={},
        )
        request = ExecutionRequest(
            run_id="test-unknown::minimal::repeat-1",
            repeat_index=1,
            task=task,
            config=config,
            fixture=fixture,
            fault=fault,
            fault_context=FaultContext(fault),
        )
        outcome = await executor.execute(request)
        assert outcome.claimed_complete is False
        assert outcome.failure_type is not None
        assert "UNKNOWN_CONFIG" in outcome.failure_type


# ---------------------------------------------------------------------------
# 6. Fixture cleanup
# ---------------------------------------------------------------------------

class TestP45FixtureCleanup:
    @pytest.mark.asyncio
    async def test_workspace_cleaned_after_execution(self, executor, snapshot):
        request = _make_request(snapshot, "CI-01", "minimal")
        await executor.execute(request)
        # Official validation/recovery owns the boundary after execute;
        # explicit cleanup releases the workspace once that boundary closes.
        executor.cleanup(request)
        assert len(executor._workspace_dirs) == 0


# ---------------------------------------------------------------------------
# 7. Multiple tasks
# ---------------------------------------------------------------------------

class TestP45MultipleTasks:
    @pytest.mark.parametrize("task_id", ["CI-01", "ESR-01", "CWR-01"])
    @pytest.mark.asyncio
    async def test_different_families(self, executor, snapshot, task_id):
        request = _make_request(snapshot, task_id, "minimal")
        outcome = await executor.execute(request)
        assert isinstance(outcome, ExecutionOutcome)


# ---------------------------------------------------------------------------
# 8. Trace propagation
# ---------------------------------------------------------------------------

class TestP45TracePropagation:
    """Verify execution_trace, tool_events, and runtime_source in observed_state."""

    @pytest.mark.asyncio
    async def test_execution_trace_present(self, executor, snapshot):
        request = _make_request(snapshot, "CI-01", "minimal")
        outcome = await executor.execute(request)
        assert "execution_trace" in outcome.observed_state
        assert isinstance(outcome.observed_state["execution_trace"], list)

    @pytest.mark.asyncio
    async def test_tool_events_present(self, executor, snapshot):
        request = _make_request(snapshot, "CI-01", "minimal")
        outcome = await executor.execute(request)
        assert "tool_events" in outcome.observed_state
        assert isinstance(outcome.observed_state["tool_events"], list)

    @pytest.mark.asyncio
    async def test_runtime_source_present(self, executor, snapshot):
        request = _make_request(snapshot, "CI-01", "minimal")
        outcome = await executor.execute(request)
        assert "runtime_source" in outcome.observed_state
        assert outcome.observed_state["runtime_source"] in ("minimal_factory", "odys_factory")

    @pytest.mark.asyncio
    async def test_trace_has_required_event_types(self, executor, snapshot):
        request = _make_request(snapshot, "CI-01", "minimal")
        outcome = await executor.execute(request)
        trace = outcome.observed_state["execution_trace"]
        event_types = [ev["event_type"] for ev in trace]
        assert "TASK_STARTED" in event_types
        assert "FIXTURE_SETUP" in event_types
        assert "RUNTIME_CREATED" in event_types
        assert "EXECUTION_COMPLETE" in event_types
        assert "FIXTURE_OBSERVATIONS" in event_types
        assert "VALIDATION_RESULT" in event_types

    @pytest.mark.asyncio
    async def test_trace_event_has_required_fields(self, executor, snapshot):
        request = _make_request(snapshot, "CI-01", "minimal")
        outcome = await executor.execute(request)
        trace = outcome.observed_state["execution_trace"]
        for ev in trace:
            assert "event_type" in ev, f"Missing event_type in {ev}"
            assert "timestamp" in ev, f"Missing timestamp in {ev}"
            assert "step_id" in ev, f"Missing step_id in {ev}"
            assert "attempt_id" in ev, f"Missing attempt_id in {ev}"

    @pytest.mark.asyncio
    async def test_trace_timestamps_are_utc_iso8601(self, executor, snapshot):
        request = _make_request(snapshot, "CI-01", "minimal")
        outcome = await executor.execute(request)
        trace = outcome.observed_state["execution_trace"]
        from datetime import datetime, timezone
        for ev in trace:
            ts = datetime.fromisoformat(ev["timestamp"])
            assert ts.tzinfo is not None, f"Timestamp missing timezone: {ev['timestamp']}"

    @pytest.mark.asyncio
    async def test_runtime_source_minimal(self, executor, snapshot):
        request = _make_request(snapshot, "CI-01", "minimal")
        outcome = await executor.execute(request)
        assert outcome.observed_state["runtime_source"] == "minimal_factory"

    @pytest.mark.asyncio
    async def test_runtime_source_odys(self, executor, snapshot):
        request = _make_request(snapshot, "CI-01", "odys_p3")
        outcome = await executor.execute(request)
        assert outcome.observed_state["runtime_source"] == "odys_factory"

    @pytest.mark.asyncio
    async def test_unknown_config_has_trace(self, executor, snapshot):
        task = next(t for t in snapshot.tasks if t["task_id"] == "CI-01")
        config = {"config_id": "unknown_config", "features": {}}
        from evals.reliability.run_phase4 import FaultInjector, FaultContext, FixtureHandle
        injector = FaultInjector(snapshot)
        fault = injector.plan_for(task)
        fixture = FixtureHandle(
            fixture_id=task["fixture_id"],
            version=task["fixture_version"],
            initial_state="clean fixture workspace",
            fixture_hash="test-hash",
            metadata={},
        )
        request = ExecutionRequest(
            run_id="test-unknown::minimal::repeat-1",
            repeat_index=1,
            task=task,
            config=config,
            fixture=fixture,
            fault=fault,
            fault_context=FaultContext(fault),
        )
        outcome = await executor.execute(request)
        assert "execution_trace" in outcome.observed_state
        assert "tool_events" in outcome.observed_state
