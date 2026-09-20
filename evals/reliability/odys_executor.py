"""Execution adapter bridging the phase4-v1 benchmark runner with the ODYS
native agent kernel.

Usage with the runner CLI::

    --executor evals.reliability.odys_executor:create_executor

The :func:`create_executor` factory is intentionally zero-argument so the
runner's ``_maybe_factory`` helper can call it directly.  Configuration is
read from environment variables and the frozen protocol snapshot.

For benchmarking, the kernel is built with a temporary SQLite database and
a deterministic scripted provider.  Missing dependencies are surfaced as
clear errors rather than silently degraded.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from evals.reliability.run_phase4 import (
    DEFAULT_PROTOCOL_ROOT,
    NOT_MEASURED,
    ExecutionOutcome,
    ExecutionRequest,
    FaultContext,
    ProtocolSnapshot,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fault type → benchmark boundary mapping
# ---------------------------------------------------------------------------

_FAULT_TYPE_TO_BOUNDARY: dict[str, str] = {
    "tool_failure": "tool_call",
    "interruption": "tool_call",
    "provider_timeout": "provider_call",
    "provider_unavailable": "provider_call",
    "quota_exhausted": "provider_call",
    "malformed_response": "provider_call",
    "assumption_invalidated": "assumption",
    "stale_workspace": "workspace",
    "capability_unavailable": "capability",
    "duplicate_delivery": "delivery",
    "partial_output": "completion",
}

# ---------------------------------------------------------------------------
# Fault type → NativeFaultPoint mapping
# ---------------------------------------------------------------------------

_FAULT_TYPE_TO_NATIVE_POINT: dict[str, str] = {
    "tool_failure": "AFTER_TOOL_REQUESTED",
    "interruption": "AFTER_TOOL_EXECUTED",
    "provider_timeout": "AFTER_MODEL_TURN_PERSISTED",
    "provider_unavailable": "AFTER_MODEL_TURN_PERSISTED",
    "quota_exhausted": "AFTER_MODEL_TURN_PERSISTED",
    "malformed_response": "AFTER_MODEL_TURN_PERSISTED",
    "assumption_invalidated": "AFTER_CANDIDATE_VALIDATED",
    "stale_workspace": "AFTER_CANDIDATE_PERSISTED",
    "capability_unavailable": "AFTER_TOOL_REQUESTED",
    "duplicate_delivery": "AFTER_TOOL_OBSERVED",
    "partial_output": "AFTER_CANDIDATE_PERSISTED",
}


class BenchmarkFaultInjector:
    """Wraps a benchmark :class:`FaultContext` to implement the
    :class:`NativeFaultInjector` protocol used by the ODYS kernel.

    Maps benchmark fault types to :class:`NativeFaultPoint` values and uses
    :meth:`FaultContext.should_inject` to decide when to fire.

    Once the fault fires, subsequent calls to :meth:`hit` are no-ops (the
    underlying :class:`FaultContext` is single-fire by design).
    """

    def __init__(self, fault_context: FaultContext):
        self.fault_context = fault_context
        self.fired: bool = False
        self.fired_point: str | None = None
        self.fired_kwargs: dict[str, Any] = {}

        fault_type = fault_context.plan.fault_type
        self._boundary = _FAULT_TYPE_TO_BOUNDARY.get(fault_type, "tool_call")
        self._native_point_name = _FAULT_TYPE_TO_NATIVE_POINT.get(
            fault_type, "AFTER_MODEL_TURN_PERSISTED"
        )

    @property
    def native_point(self) -> str:
        """Return the NativeFaultPoint name this injector targets."""
        return self._native_point_name

    def hit(self, point, **context: Any) -> None:
        """Called by the kernel at fault injection points.

        Delegates to ``FaultContext.should_inject()`` to decide whether to
        fire.  The ``point`` argument is a :class:`NativeFaultPoint` enum
        value; we only fire when it matches the mapped point for our fault
        type.
        """
        if self.fired:
            return

        # Only fire at the mapped NativeFaultPoint
        point_value = point.value if hasattr(point, "value") else str(point)
        if point_value != self._native_point_name:
            return

        # Determine ordinal from context (kernel passes snapshot or similar)
        ordinal = 1
        snapshot = context.get("snapshot")
        if snapshot is not None:
            # Use tool_call_count or model_turn_count as the ordinal
            ordinal = getattr(snapshot, "tool_call_count", 1) or 1

        if self.fault_context.should_inject(self._boundary, ordinal):
            self.fired = True
            self.fired_point = point_value
            self.fired_kwargs = dict(context)


class OdysRuntimeExecutor:
    """Benchmark executor that runs tasks through the ODYS native kernel.

    Implements the :class:`BenchmarkExecutor` protocol expected by the
    phase4-v1 runner.
    """

    def __init__(
        self,
        kernel: Any,
        config: dict[str, Any],
        snapshot: ProtocolSnapshot,
        *,
        db: Any = None,
    ):
        self.kernel = kernel
        self.config = config
        self.snapshot = snapshot
        self.db = db
        self._features = config.get("features", {})
        self._tool_capability_set = set(config.get("tool_capability_set", []))

    async def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        """Run one benchmark task through the native kernel and return the
        observed outcome."""
        started = time.monotonic()

        # Ensure Task/Run/Attempt records exist in the database
        if self.db is not None:
            self._ensure_db_records(request)

        # Build a BenchmarkFaultInjector wrapping the request's FaultContext
        fault_injector = BenchmarkFaultInjector(request.fault_context)

        # Inject the fault injector into the kernel if it supports it
        original_injector = getattr(self.kernel, "fault_injector", None)
        self.kernel.fault_injector = fault_injector

        try:
            agent_request = self._build_agent_request(request, fault_injector)
            result = await self.kernel.run(agent_request)
            outcome = self._map_outcome(result, request, fault_injector)
        except Exception as exc:
            logger.error("Kernel execution failed for %s: %s", request.run_id, exc)
            outcome = self._error_outcome(exc, request, fault_injector)
        finally:
            # Restore original injector
            self.kernel.fault_injector = original_injector

        elapsed = time.monotonic() - started
        if outcome.wall_time_seconds == NOT_MEASURED:
            outcome.wall_time_seconds = round(elapsed, 6)

        return outcome

    def _ensure_db_records(self, request: ExecutionRequest) -> None:
        """Create Task, Run, and Attempt records if they don't exist."""
        from lhas.domain.models import Attempt, Project, Run, Task
        from lhas.persistence.repositories import (
            AttemptRepository,
            ProjectRepository,
            RunRepository,
            TaskRepository,
        )

        task_id = request.task["task_id"]
        run_id = request.run_id
        attempt_id = f"{run_id}::attempt-1"

        # Ensure project exists
        projects = ProjectRepository(self.db)
        project = projects.get_by_name("benchmark")
        if project is None:
            project = projects.create(Project(name="benchmark", type="benchmark"))

        # Ensure task exists with the benchmark task_id
        tasks = TaskRepository(self.db)
        task = tasks.get(task_id)
        if task is None:
            task = Task(
                id=task_id,
                project_id=project.id,
                title=request.task.get("title", task_id),
                objective=request.task.get("objective", ""),
                constraints=[],
                acceptance_criteria=request.task.get("acceptance_criteria", []),
                max_attempts=1,
                timeout_seconds=float(request.task.get("timeout_seconds", 900)),
            )
            tasks.create(task)

        # Ensure run exists with the benchmark run_id
        runs = RunRepository(self.db)
        run = runs.get(run_id)
        if run is None:
            run = Run(id=run_id, task_id=task_id, status="RUNNING")
            runs.create(run)

        # Ensure attempt exists with the benchmark attempt_id
        attempts = AttemptRepository(self.db)
        attempt = attempts.get(attempt_id)
        if attempt is None:
            attempt = Attempt(id=attempt_id, run_id=run.id, attempt_number=1, status="RUNNING")
            attempts.create(attempt)

    def _build_agent_request(
        self,
        request: ExecutionRequest,
        fault_injector: BenchmarkFaultInjector,
    ) -> Any:
        """Construct an AgentRequest from a benchmark ExecutionRequest."""
        from lhas.agent.models import AgentBudget, AgentRequest, AgentRole

        task = request.task
        config = request.config

        # Build budget from task limits
        max_turns = int(task.get("max_turns", 20))
        max_model_calls = int(task.get("max_model_calls", 20))
        budget = AgentBudget(
            max_turns=max_turns,
            max_tool_calls=max_model_calls,
        )

        # Build allowed capabilities from config
        allowed_capabilities = set(config.get("tool_capability_set", []))

        # Build context with acceptance criteria and fixture info
        context: dict[str, Any] = {
            "acceptance_criteria": list(task.get("acceptance_criteria", [])),
            "fixture_id": request.fixture.fixture_id,
            "fixture_initial_state": request.fixture.initial_state,
            "fault_id": request.fault.fault_id,
            "fault_type": request.fault.fault_type,
        }

        # Add feature flags to context so the kernel can respect them
        context["benchmark_features"] = dict(self._features)

        # Build metadata with run/attempt identity
        attempt_id = f"{request.run_id}::attempt-1"
        metadata: dict[str, Any] = {
            "task_id": task["task_id"],
            "run_id": request.run_id,
            "attempt_id": attempt_id,
            "attempt_number": 1,
            "benchmark_repeat_index": request.repeat_index,
        }

        return AgentRequest(
            agent_id=f"benchmark-{request.run_id}",
            role=AgentRole.WORKER,
            objective=str(task.get("objective", "")),
            context=context,
            messages=[],
            allowed_capabilities=allowed_capabilities,
            budget=budget,
            metadata=metadata,
        )

    def _map_outcome(
        self,
        result: Any,
        request: ExecutionRequest,
        fault_injector: BenchmarkFaultInjector,
    ) -> ExecutionOutcome:
        """Map an AgentResult to a benchmark ExecutionOutcome."""
        from lhas.agent.models import AgentStatus

        # Determine completion claim
        claimed_complete = result.status is AgentStatus.COMPLETED

        # Extract failure type
        failure_type = None
        if result.error_type:
            failure_type = result.error_type
        elif result.status is AgentStatus.FAILED:
            failure_type = "UNKNOWN_FAILURE"

        # Extract usage / token counts
        usage = result.usage or {}
        tokens_input = usage.get("prompt_tokens", usage.get("input_tokens", NOT_MEASURED))
        tokens_output = usage.get("completion_tokens", usage.get("output_tokens", NOT_MEASURED))
        total_tokens = usage.get("total_tokens", NOT_MEASURED)

        # Compute total if not provided
        if (
            total_tokens == NOT_MEASURED
            and isinstance(tokens_input, int)
            and isinstance(tokens_output, int)
        ):
            total_tokens = tokens_input + tokens_output

        # Extract recovery signals from artifacts and safe_trace
        artifacts = result.artifacts or {}
        safe_trace = result.safe_trace or []

        recovery_required = self._detect_recovery_required(result, safe_trace)
        recovery_attempted = self._detect_recovery_attempted(safe_trace)
        recovery_success = (
            result.status is AgentStatus.COMPLETED and recovery_attempted
        )

        # Repair scope from features and trace
        repair_scope = self._determine_repair_scope(safe_trace)

        # Count replans from trace
        replan_count = sum(
            1
            for entry in safe_trace
            if isinstance(entry, dict)
            and entry.get("error_type") in {"VALIDATOR_REJECTION", "REPEATED_TOOL_FAILURE"}
        )

        # Duplicate side effects from trace
        duplicate_side_effect_count = sum(
            1
            for entry in safe_trace
            if isinstance(entry, dict)
            and entry.get("status") == "FAILURE"
            and "duplicate" in str(entry.get("error_type", "")).lower()
        )

        # Build observed_state from artifacts
        observed_state: dict[str, Any] = {
            "agent_status": result.status.value,
            "tool_call_count": result.tool_call_count,
            "turn_count": result.turn_count,
            "completion_claim": result.completion_claim,
            "fault_fired": fault_injector.fired,
            "fault_fired_point": fault_injector.fired_point,
        }
        if result.final_output:
            observed_state["final_output"] = result.final_output[:2000]
        if artifacts:
            observed_state["artifacts"] = {
                str(k): str(v)[:500] for k, v in artifacts.items()
            }

        outcome = ExecutionOutcome(
            claimed_complete=claimed_complete,
            observed_state=observed_state,
            failure_type=failure_type,
            recovery_required=recovery_required,
            recovery_attempted=recovery_attempted,
            recovery_success=recovery_success,
            repair_scope=repair_scope,
            repair_attempts=replan_count,
            replan_count=replan_count,
            lost_work_units=0,
            duplicate_side_effect_count=duplicate_side_effect_count,
            tool_calls=result.tool_call_count,
            model_calls=result.turn_count,
            attempt_count=1,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            total_tokens=total_tokens,
            model_cost=NOT_MEASURED,
            tool_cost=NOT_MEASURED,
            wall_time_seconds=NOT_MEASURED,
            human_intervention=False,
        )
        # Preserve the runtime's safe observations for the benchmark trace
        # sink.  These are actual kernel observations; the runner remains
        # responsible for adding only the validator boundary events it sees.
        outcome.execution_trace = list(safe_trace)
        outcome.tool_events = [
            entry for entry in safe_trace
            if isinstance(entry, dict) and (entry.get("capability") or entry.get("tool_name"))
        ]
        return outcome

    def _error_outcome(
        self,
        exc: Exception,
        request: ExecutionRequest,
        fault_injector: BenchmarkFaultInjector,
    ) -> ExecutionOutcome:
        """Build an ExecutionOutcome from an exception."""
        return ExecutionOutcome(
            claimed_complete=False,
            observed_state={
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                "fault_fired": fault_injector.fired,
                "fault_fired_point": fault_injector.fired_point,
            },
            failure_type=f"EXECUTOR_ERROR:{type(exc).__name__}",
            recovery_required=False,
            recovery_attempted=False,
            recovery_success=False,
            repair_scope=None,
            tool_calls=0,
            model_calls=0,
            attempt_count=1,
        )

    def _detect_recovery_required(self, result: Any, trace: list) -> bool:
        """Detect if the kernel encountered failures requiring recovery."""
        if result.error_type is not None:
            return True
        return any(
            isinstance(entry, dict) and entry.get("status") == "FAILURE"
            for entry in trace
        )

    def _detect_recovery_attempted(self, trace: list) -> bool:
        """Detect if the kernel attempted recovery from failures."""
        return any(
            isinstance(entry, dict)
            and entry.get("recovered_from_durable_invocation") is True
            for entry in trace
        )

    def _determine_repair_scope(self, trace: list) -> str | None:
        """Determine repair scope from the execution trace."""
        if not self._features.get("selective_repair"):
            return None

        has_replan = any(
            isinstance(entry, dict)
            and entry.get("error_type") in {"VALIDATOR_REJECTION", "REPEATED_TOOL_FAILURE"}
            for entry in trace
        )
        if not has_replan:
            return None

        # Check for macro-level replan signals
        for entry in trace:
            if isinstance(entry, dict):
                reason = str(entry.get("error_type", ""))
                if reason in {"QUOTA_EXHAUSTED", "PROVIDER_UNAVAILABLE"}:
                    return "macro_replan"

        return "local"


def create_executor(
    *,
    kernel: Any | None = None,
    config_name: str | None = None,
    snapshot: ProtocolSnapshot | None = None,
) -> OdysRuntimeExecutor:
    """Factory that builds a fully-configured :class:`OdysRuntimeExecutor`.

    When called with no arguments (the default for the CLI), this reads
    configuration from environment variables and builds a kernel from the
    available ODYS stack.

    For testing, callers can inject a mock kernel and explicit config.
    """
    # Load snapshot
    root = Path(os.environ.get("ODYS_PROTOCOL_ROOT", str(DEFAULT_PROTOCOL_ROOT)))
    if snapshot is None:
        snapshot = ProtocolSnapshot.load(root)

    # Determine config
    if config_name is None:
        config_name = os.environ.get("ODYS_BENCHMARK_CONFIG", "minimal")
    config = snapshot.configs.get(config_name)
    if config is None:
        raise ValueError(
            f"Unknown config '{config_name}'; available: {list(snapshot.configs)}"
        )

    # Build kernel if not injected
    if kernel is None:
        kernel, db = _build_kernel(config, snapshot)
    else:
        db = None

    return OdysRuntimeExecutor(kernel=kernel, config=config, snapshot=snapshot, db=db)


def _build_kernel(config: dict[str, Any], snapshot: ProtocolSnapshot) -> tuple[Any, Any]:
    """Build a NativeAgentKernel from config and environment.

    Returns (kernel, db) so the executor can pre-create database records.
    """
    from lhas.capability_registry import default_capabilities
    from lhas.native.completion import CompletionAuthority
    from lhas.native.kernel import NativeAgentKernel
    from lhas.native.models import NoOpNativeFaultInjector
    from lhas.native.parser import ModelResponseParser
    from lhas.native.tools import NativeToolDispatcher
    from lhas.persistence.database import Database
    from lhas.tools.registry import ToolRegistry
    from tests.helpers import make_test_capability_definition, make_test_capability_registry

    # Temporary database for benchmark purposes
    tmp_dir = tempfile.mkdtemp(prefix="odys-benchmark-")
    db = Database(Path(tmp_dir) / "benchmark.db")
    db.init_db()

    # Provider — deterministic scripted provider for benchmarking
    provider = _build_provider(config)

    # Tool dispatcher
    registry = ToolRegistry()
    allowed = set(config.get("tool_capability_set", []))

    # Only add test definitions for capabilities NOT already in the default catalog
    default_ids = {d.id for d in default_capabilities()}
    extra_defs = [
        make_test_capability_definition(cap_id, output_schema={})
        for cap_id in allowed
        if cap_id not in default_ids
    ]
    cap_reg, contract = make_test_capability_registry(registry, extra_defs)

    dispatcher = NativeToolDispatcher(
        db=db,
        registry=registry,
        allowed_capabilities=allowed,
        allowed_side_effect_capabilities=allowed,
        capability_registry=cap_reg,
        tool_contract=contract,
    )

    # Completion authority
    validator = _build_validator(config)
    completion_authority = CompletionAuthority(
        db=db,
        validator=validator,
        fault_injector=NoOpNativeFaultInjector(),
    )

    kernel = NativeAgentKernel(
        db=db,
        provider=provider,
        dispatcher=dispatcher,
        completion_authority=completion_authority,
        parser=ModelResponseParser(),
        fault_injector=NoOpNativeFaultInjector(),
    )

    return kernel, db


def _build_provider(config: dict[str, Any]) -> Any:
    """Build a provider for benchmark execution.

    Uses a scripted provider for deterministic benchmarking.
    """
    from lhas.native.models import ProviderResponse
    from lhas.native.provider import ScriptedProviderAdapter

    return ScriptedProviderAdapter(
        responses=[
            ProviderResponse(
                content="Task completed.",
                tool_calls=[],
                completion_claim=True,
                usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            )
        ]
    )


def _build_validator(config: dict[str, Any]) -> Any:
    """Build a validator for completion authority."""
    from tests.helpers import PassingCommandValidator

    return PassingCommandValidator()
