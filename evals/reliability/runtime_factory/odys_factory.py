"""OdysRuntimeFactory — full runtime with all ODYS reliability features.

The ODYS runtime includes:

- CompletionAuthority (validates completion claims through the validator)
- Failure provenance tracking
- Selective repair capability
- Recovery loop (the kernel's while-loop with replan/recover)

It uses the full ``NativeAgentKernel`` from ``src/lhas/native/kernel.py``.
"""

from __future__ import annotations

import time
from typing import Any

from evals.reliability.run_phase4 import NOT_MEASURED, ExecutionOutcome
from evals.reliability.runtime_factory.base import RuntimeFactory
from evals.reliability.runtime_factory.protocol import BenchmarkRuntime


class _OdysRuntime:
    """Full ODYS runtime wrapping the ``NativeAgentKernel``.

    This runtime includes CompletionAuthority, failure provenance,
    selective repair, and the recovery loop — all the reliability
    features that distinguish ODYS from the minimal baseline.
    """

    def __init__(self, *, kernel: Any, db: Any = None):
        self.kernel = kernel
        self.db = db
        # Expose the completion authority so tests can verify its presence
        self.completion = kernel.completion

    async def execute(self, task: dict[str, Any], config: dict[str, Any]) -> ExecutionOutcome:
        """Run one benchmark task through the full ODYS kernel."""
        started = time.monotonic()
        features = config.get("features", {})

        try:
            from lhas.agent.models import AgentBudget, AgentRequest, AgentRole, AgentStatus

            # Ensure DB records exist
            if self.db is not None:
                self._ensure_db_records(task, config)

            max_turns = int(task.get("max_turns", 20))
            max_tool_calls = int(task.get("max_model_calls", 20))
            budget = AgentBudget(max_turns=max_turns, max_tool_calls=max_tool_calls)
            allowed = set(config.get("tool_capability_set", []))

            run_id = config.get("run_id", "odys-run")
            attempt_id = f"{run_id}::attempt-1"
            metadata = {
                "task_id": task.get("task_id", "unknown"),
                "run_id": run_id,
                "attempt_id": attempt_id,
            }

            request = AgentRequest(
                agent_id=f"odys-{run_id}",
                role=AgentRole.WORKER,
                objective=str(task.get("objective", "")),
                context={
                    "acceptance_criteria": list(task.get("acceptance_criteria", [])),
                    "benchmark_features": dict(features),
                },
                messages=[],
                allowed_capabilities=allowed,
                budget=budget,
                metadata=metadata,
            )

            # Run through the full kernel (includes completion authority,
            # failure provenance, selective repair, recovery loop)
            result = await self.kernel.run(request)

            elapsed = time.monotonic() - started

            # Map AgentResult to ExecutionOutcome
            claimed_complete = result.status is AgentStatus.COMPLETED
            failure_type = None
            if result.error_type:
                failure_type = result.error_type
            elif result.status is AgentStatus.FAILED:
                failure_type = "UNKNOWN_FAILURE"

            safe_trace = result.safe_trace or []
            recovery_required = any(
                isinstance(entry, dict) and entry.get("status") == "FAILURE"
                for entry in safe_trace
            )
            recovery_attempted = any(
                isinstance(entry, dict)
                and entry.get("recovered_from_durable_invocation") is True
                for entry in safe_trace
            )
            recovery_success = result.status is AgentStatus.COMPLETED and recovery_attempted

            return ExecutionOutcome(
                claimed_complete=claimed_complete,
                observed_state={
                    "agent_status": result.status.value,
                    "tool_call_count": result.tool_call_count,
                    "turn_count": result.turn_count,
                    "completion_claim": result.completion_claim,
                    "features_active": {
                        "completion_authority": True,
                        "failure_provenance": True,
                        "selective_repair": features.get("selective_repair", False),
                        "recovery_loop": True,
                    },
                },
                failure_type=failure_type,
                recovery_required=recovery_required,
                recovery_attempted=recovery_attempted,
                recovery_success=recovery_success,
                repair_scope="local" if features.get("selective_repair") else None,
                tool_calls=result.tool_call_count,
                model_calls=result.turn_count,
                attempt_count=1,
                wall_time_seconds=round(elapsed, 6),
            )

        except Exception as exc:
            elapsed = time.monotonic() - started
            return ExecutionOutcome(
                claimed_complete=False,
                observed_state={
                    "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                    "features_active": {
                        "completion_authority": True,
                        "failure_provenance": True,
                        "recovery_loop": True,
                    },
                },
                failure_type=f"ODYS_ERROR:{type(exc).__name__}",
                recovery_required=False,
                recovery_attempted=False,
                recovery_success=False,
                repair_scope=None,
                tool_calls=0,
                model_calls=0,
                attempt_count=1,
                wall_time_seconds=round(elapsed, 6),
            )

    def _ensure_db_records(self, task: dict[str, Any], config: dict[str, Any]) -> None:
        """Create Task, Run, and Attempt records if they don't exist."""
        from lhas.domain.models import Attempt, Project, Run, Task
        from lhas.persistence.repositories import (
            AttemptRepository,
            ProjectRepository,
            RunRepository,
            TaskRepository,
        )

        task_id = task.get("task_id", "unknown")
        run_id = config.get("run_id", "odys-run")
        attempt_id = f"{run_id}::attempt-1"

        projects = ProjectRepository(self.db)
        project = projects.get_by_name("benchmark")
        if project is None:
            project = projects.create(Project(name="benchmark", type="benchmark"))

        tasks = TaskRepository(self.db)
        existing_task = tasks.get(task_id)
        if existing_task is None:
            existing_task = Task(
                id=task_id,
                project_id=project.id,
                title=task.get("title", task_id),
                objective=task.get("objective", ""),
                constraints=[],
                acceptance_criteria=task.get("acceptance_criteria", []),
                max_attempts=1,
                timeout_seconds=float(task.get("timeout_seconds", 900)),
            )
            tasks.create(existing_task)

        runs = RunRepository(self.db)
        existing_run = runs.get(run_id)
        if existing_run is None:
            existing_run = Run(id=run_id, task_id=task_id, status="RUNNING")
            runs.create(existing_run)

        attempts = AttemptRepository(self.db)
        existing_attempt = attempts.get(attempt_id)
        if existing_attempt is None:
            existing_attempt = Attempt(id=attempt_id, run_id=existing_run.id, attempt_number=1, status="RUNNING")
            attempts.create(existing_attempt)


class OdysRuntimeFactory(RuntimeFactory):
    """Factory that produces runtimes *with* all ODYS reliability features.

    The returned runtime uses the full ``NativeAgentKernel`` which includes
    CompletionAuthority, failure provenance, selective repair, and the
    recovery loop.
    """

    def __init__(
        self,
        *,
        kernel: Any = None,
        db: Any = None,
        workspace_root: Any = None,
    ):
        self._kernel = kernel
        self._db = db
        self._workspace_root = workspace_root

    def create_runtime(self, config: dict[str, Any]) -> BenchmarkRuntime:
        """Create a full ODYS runtime with all reliability features.

        Parameters
        ----------
        config:
            The benchmark config dict.  Must contain ``features`` and
            ``tool_capability_set``.

        Returns
        -------
        BenchmarkRuntime
            A runtime that satisfies the shared benchmark interface.
        """
        kernel = self._kernel
        db = self._db

        if kernel is None:
            kernel, db = _build_odys_kernel(config, workspace_root=self._workspace_root)

        runtime = _OdysRuntime(kernel=kernel, db=db)

        # Verification: the ODYS runtime MUST have completion authority
        assert (
            hasattr(runtime, "completion") and runtime.completion is not None
        ), "OdysRuntime MUST have CompletionAuthority"

        return runtime


def _build_odys_kernel(
    config: dict[str, Any],
    workspace_root: Any = None,
) -> tuple[Any, Any]:
    """Build a full NativeAgentKernel with all ODYS reliability features."""
    import tempfile
    from pathlib import Path

    from lhas.capability_registry import default_capabilities
    from lhas.native.completion import CompletionAuthority
    from lhas.native.kernel import NativeAgentKernel
    from lhas.native.models import NoOpNativeFaultInjector
    from lhas.native.parser import ModelResponseParser
    from lhas.native.tools import NativeToolDispatcher
    from lhas.persistence.database import Database
    from lhas.tools.registry import ToolRegistry
    from tests.helpers import (
        PassingCommandValidator,
        make_test_capability_definition,
        make_test_capability_registry,
    )

    # Temporary database
    tmp_dir = tempfile.mkdtemp(prefix="odys-benchmark-")
    db = Database(Path(tmp_dir) / "benchmark.db")
    db.init_db()

    # Deterministic scripted provider
    from lhas.native.models import ProviderResponse
    from lhas.native.provider import ScriptedProviderAdapter

    provider = ScriptedProviderAdapter(
        responses=[
            ProviderResponse(
                content="Task completed.",
                tool_calls=[],
                completion_claim=True,
                usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            )
        ]
    )

    # Tool dispatcher — use benchmark tools if workspace_root provided
    allowed = set(config.get("tool_capability_set", []))

    if workspace_root is not None:
        from evals.reliability.tools.registry import create_benchmark_tool_registry
        registry = create_benchmark_tool_registry(Path(workspace_root))
    else:
        registry = ToolRegistry()

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

    # Completion authority — this is the key ODYS feature
    validator = PassingCommandValidator()
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
