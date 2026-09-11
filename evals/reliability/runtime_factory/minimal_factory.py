"""MinimalRuntimeFactory — baseline runtime without ODYS reliability features.

The minimal baseline is deliberately stripped of:

- CompletionAuthority (no completion claim validation)
- Failure provenance tracking
- Selective repair
- Recovery loop

It uses a simple executor that calls the provider once, returns whatever
the model produces, and maps the result to the common ``ExecutionOutcome``
shape.  This establishes the *no-reliability-features* baseline against
which ODYS improvements are measured.
"""

from __future__ import annotations

import time
from typing import Any

from evals.reliability.run_phase4 import NOT_MEASURED, ExecutionOutcome
from evals.reliability.runtime_factory.base import RuntimeFactory
from evals.reliability.runtime_factory.protocol import BenchmarkRuntime


class _MinimalRuntime:
    """A simple runtime that calls the provider once and returns the result.

    This runtime deliberately omits CompletionAuthority, failure provenance,
    selective repair, and the recovery loop.  It represents the simplest
    possible agent execution path for benchmarking.
    """

    def __init__(
        self,
        *,
        provider: Any,
        dispatcher: Any,
        db: Any = None,
    ):
        self.provider = provider
        self.dispatcher = dispatcher
        self.db = db
        # Deliberately NO completion authority, no recovery loop, no
        # failure provenance, no selective repair.
        self.completion = None

    async def execute(self, task: dict[str, Any], config: dict[str, Any]) -> ExecutionOutcome:
        """Run one benchmark task through the minimal execution path."""
        started = time.monotonic()
        features = config.get("features", {})

        try:
            # Build a minimal agent request
            from lhas.agent.models import AgentBudget, AgentRequest, AgentRole

            max_turns = int(task.get("max_turns", 5))
            budget = AgentBudget(max_turns=max_turns, max_tool_calls=max_turns)
            allowed = set(config.get("tool_capability_set", []))

            metadata = {
                "task_id": task.get("task_id", "unknown"),
                "run_id": config.get("run_id", "minimal-run"),
                "attempt_id": f"{config.get('run_id', 'minimal-run')}::attempt-1",
            }

            request = AgentRequest(
                agent_id=f"minimal-{metadata['run_id']}",
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

            # Generate a single response from the provider — no loop,
            # no completion authority, no recovery.
            from lhas.native.context import NativeContextAssembler

            assembler = NativeContextAssembler()
            context = assembler.build(
                request,
                _MinimalSnapshot(metadata),
                validation_failures=[],
                replan_signals=[],
            )

            raw = await self.provider.generate(
                context=context,
                tools=self.dispatcher.tool_schemas() if self.dispatcher else [],
                timeout_seconds=30.0,
            )

            # Parse the response
            from lhas.native.parser import ModelResponseParser

            parser = ModelResponseParser()
            response = parser.parse(raw)

            # Execute any tool calls (one pass only, no recovery)
            tool_outcomes = []
            if response.tool_calls:
                from lhas.native.models import ExecutionSnapshot

                snapshot = _MinimalSnapshot(metadata)
                for call in response.tool_calls:
                    observation = await self.dispatcher.dispatch(call, request, snapshot)
                    tool_outcomes.append(observation)

            elapsed = time.monotonic() - started

            return ExecutionOutcome(
                claimed_complete=bool(response.completion_claim),
                observed_state={
                    "agent_status": "COMPLETED" if response.completion_claim else "CONTINUED",
                    "turn_count": 1,
                    "tool_call_count": len(tool_outcomes),
                    "completion_claim": response.completion_claim,
                    "features_active": {},
                },
                failure_type=None,
                recovery_required=False,
                recovery_attempted=False,
                recovery_success=False,
                repair_scope=None,
                tool_calls=len(tool_outcomes),
                model_calls=1,
                attempt_count=1,
                wall_time_seconds=round(elapsed, 6),
            )

        except Exception as exc:
            elapsed = time.monotonic() - started
            return ExecutionOutcome(
                claimed_complete=False,
                observed_state={
                    "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                    "features_active": {},
                },
                failure_type=f"MINIMAL_ERROR:{type(exc).__name__}",
                recovery_required=False,
                recovery_attempted=False,
                recovery_success=False,
                repair_scope=None,
                tool_calls=0,
                model_calls=0,
                attempt_count=1,
                wall_time_seconds=round(elapsed, 6),
            )


class _MinimalSnapshot:
    """Lightweight snapshot stand-in for the minimal runtime.

    Satisfies the interface that ``NativeContextAssembler.build`` and
    ``NativeToolDispatcher.dispatch`` expect without requiring a real
    database-backed ``ExecutionSnapshot``.
    """

    def __init__(self, metadata: dict[str, Any]):
        self.task_id = metadata.get("task_id", "")
        self.run_id = metadata.get("run_id", "")
        self.attempt_id = metadata.get("attempt_id", "")
        self.model_turn_count = 0
        self.tool_call_count = 0
        self.recent_tool_outcomes: list[dict] = []
        self.repeated_failure_state: dict = {}
        self.verification_state: dict = {}
        self.workspace_mutation_version = 0
        self.current_failure: dict = {}
        self.phase = type("Phase", (), {"value": "CONTINUE"})()
        self.taskgraph_position = None
        self.completed_nodes: list = []
        self.pending_nodes: list = []
        self.consumed_delivery_tokens: list = []
        self.delegation_dependencies: dict = {}
        self.goal = ""
        self.configured_target = None
        self.effective_target = None
        self.fallback_reason = None
        self.target_event_id = None
        self.completion_candidate_id = None


class MinimalRuntimeFactory(RuntimeFactory):
    """Factory that produces runtimes *without* ODYS reliability features.

    The returned runtime has ``completion = None`` and does not use
    CompletionAuthority, failure provenance, selective repair, or the
    recovery loop.
    """

    def __init__(
        self,
        *,
        provider: Any = None,
        dispatcher: Any = None,
        db: Any = None,
        workspace_root: Any = None,
    ):
        self._provider = provider
        self._dispatcher = dispatcher
        self._db = db
        self._workspace_root = workspace_root

    def create_runtime(self, config: dict[str, Any]) -> BenchmarkRuntime:
        """Create a minimal runtime without ODYS reliability features.

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
        provider = self._provider
        dispatcher = self._dispatcher
        db = self._db

        # If provider/dispatcher not injected, build them from config
        if provider is None or dispatcher is None:
            provider, dispatcher, db = _build_minimal_components(
                config, workspace_root=self._workspace_root
            )

        runtime = _MinimalRuntime(provider=provider, dispatcher=dispatcher, db=db)

        # Verification: the minimal runtime must NOT have completion authority
        assert (
            not hasattr(runtime, "completion") or runtime.completion is None
        ), "MinimalRuntime must NOT have CompletionAuthority"

        return runtime


def _build_minimal_components(
    config: dict[str, Any],
    workspace_root: Any = None,
) -> tuple[Any, Any, Any]:
    """Build provider, dispatcher, and db for the minimal runtime."""
    import tempfile
    from pathlib import Path

    from lhas.capability_registry import default_capabilities
    from lhas.native.models import NoOpNativeFaultInjector
    from lhas.native.tools import NativeToolDispatcher
    from lhas.persistence.database import Database
    from lhas.tools.registry import ToolRegistry
    from tests.helpers import make_test_capability_definition, make_test_capability_registry

    # Temporary database
    tmp_dir = tempfile.mkdtemp(prefix="odys-minimal-benchmark-")
    db = Database(Path(tmp_dir) / "minimal-benchmark.db")
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

    return provider, dispatcher, db
