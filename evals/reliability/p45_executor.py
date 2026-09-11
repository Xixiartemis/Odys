"""P4.5 Official Benchmark Executor.

Bridges the ``runtime_factory/`` package (MinimalRuntimeFactory, OdysRuntimeFactory)
with the ``Phase4Runner`` from ``run_phase4.py``.  The runner calls
``BenchmarkExecutor.execute(request)``; this adapter selects the correct
runtime factory based on the request's config, creates a ``BenchmarkRuntime``,
and delegates execution.

The adapter also integrates the ``FixtureRegistry`` for executable fixture
setup/reset and the ``BenchmarkFaultInjector`` for deterministic fault
injection.

Usage with the runner CLI::

    --executor evals.reliability.p45_executor:create_executor

The :func:`create_executor` factory reads configuration from environment
variables:

- ``ODYS_BENCHMARK_MODEL``: model identity (default ``SCRIPTED_P4.5``)
- ``ODYS_BENCHMARK_PROVIDER``: provider identity (default ``SCRIPTED_P4.5``)
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.reliability.run_phase4 import (
    ExecutionOutcome,
    ExecutionRequest,
    NOT_MEASURED,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Trace event helpers
# ---------------------------------------------------------------------------

_TOOL_EVENT_TYPES = frozenset({
    "TOOL_CALL",
    "TOOL_RESULT",
    "TOOL_ERROR",
    "CAPABILITY_INVOKED",
})


def _trace_event(
    event_type: str,
    task_id: str,
    attempt_id: str,
    *,
    step_id: str = "root",
    status: str | None = None,
    metadata: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a single trace event dict with required fields."""
    event: dict[str, Any] = {
        "event_type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "task_id": task_id,
        "step_id": step_id,
        "attempt_id": attempt_id,
        "status": status or event_type.casefold(),
        "metadata": dict(metadata or {}),
    }
    event["metadata"].update(extra)
    return event


def _filter_tool_events(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the subset of *trace* events that are tool-related."""
    return [
        ev for ev in trace
        if ev.get("event_type", "").upper() in _TOOL_EVENT_TYPES
        or "capability" in ev.get("event_type", "").lower()
    ]


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class P45BenchmarkExecutor:
    """Benchmark executor that delegates to runtime factories.

    When *factory_type* is ``'real'`` (default) the executor uses
    :class:`RealLLMMinimalRuntimeFactory` / :class:`RealLLMOdysRuntimeFactory`
    from :mod:`evals.reliability.p46_provider` which talk to a real
    OpenAI-compatible API.

    When *factory_type* is ``'scripted'`` the executor falls back to
    :class:`MinimalRuntimeFactory` / :class:`OdysRuntimeFactory` which use
    :class:`ScriptedProviderAdapter` (canned responses — for testing).
    """

    def __init__(
        self,
        *,
        fixture_registry: Any | None = None,
        factory_type: str = "real",
        trace_file: Path | None = None,
        provider: Any | None = None,
        provider_identity: dict[str, Any] | None = None,
    ):
        if factory_type not in ("real", "scripted"):
            raise ValueError(
                f"factory_type must be 'real' or 'scripted', got {factory_type!r}"
            )
        self._fixture_registry = fixture_registry
        self._workspace_dirs: dict[str, Path] = {}
        self._factory_type = factory_type
        self._trace_file = trace_file
        self._provider = provider
        self._provider_identity = dict(provider_identity or {}) or None
        logger.info("P45BenchmarkExecutor created with factory_type=%s", factory_type)

    @property
    def provider_identity(self) -> dict[str, Any] | None:
        """Secret-free provider identity used by an official run."""
        return dict(self._provider_identity) if self._provider_identity else None

    def persist_provider_identity(self, path: Path) -> dict[str, Any]:
        """Persist identity evidence into the current output bundle."""
        if self._provider is None:
            raise RuntimeError("PROVIDER_IDENTITY_UNAVAILABLE")
        from evals.reliability.p46_provider import validate_and_persist_provider_identity

        identity = validate_and_persist_provider_identity(self._provider, path=path)
        self._provider_identity = dict(identity)
        return identity

    async def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        """Run one benchmark task through the appropriate runtime."""
        if self._factory_type == "real" and self._provider is None:
            # This validation happens before fixture setup or model execution.
            # A direct executor construction therefore cannot accidentally turn
            # a missing credential into benchmark evidence.
            from evals.reliability.p46_provider import (
                create_real_provider,
                validate_and_persist_provider_identity,
            )

            self._provider = create_real_provider()
            self._provider_identity = validate_and_persist_provider_identity(self._provider)

        config_id = request.config.get("config_id", "unknown")
        task = request.task
        task_id = task.get("task_id", "unknown")
        run_id = request.run_id
        attempt_id = f"{run_id}::attempt-{request.repeat_index}"

        # Mutable trace list accumulated throughout the execution lifecycle.
        trace: list[dict[str, Any]] = []

        # ---- TASK_STARTED ----
        trace.append(_trace_event("TASK_STARTED", task_id, attempt_id))

        # Setup fixture workspace
        workspace_dir = self._setup_fixture(request)

        # ---- FIXTURE_SETUP ----
        trace.append(_trace_event("FIXTURE_SETUP", task_id, attempt_id,
                                  workspace=str(workspace_dir)))

        # ---- FAULT_INJECTION ----
        fault_id = task.get("fault_injection")
        fault = request.fault
        if fault_id is None or fault is None:
            trace.append(_trace_event("FAULT_INJECTION_FAILED", task_id, attempt_id,
                                      error="MISSING_FAULT_BINDING",
                                      fault_id=fault_id,
                                      has_fault=fault is not None))
            return ExecutionOutcome(
                claimed_complete=False,
                observed_state={
                    "error": "MISSING_FAULT_BINDING",
                    "task_id": task_id,
                    "config_id": config_id,
                    "execution_trace": trace,
                    "tool_events": _filter_tool_events(trace),
                    "runtime_source": f"{config_id}_factory",
                },
                failure_type="MISSING_FAULT_BINDING",
            )

        trace.append(_trace_event("FAULT_INJECTION_STARTED", task_id, attempt_id,
                                  fault_id=fault_id))
        try:
            fixture = self._fixture_registry.get(task_id) if self._fixture_registry is not None else None
            if fixture is not None:
                fixture.inject_fault(workspace_dir, fault_id)
            trace.append(_trace_event("FAULT_INJECTED", task_id, attempt_id,
                                      fault_id=fault_id))
        except Exception as exc:
            logger.error("Fault injection failed for %s (fault=%s): %s", task_id, fault_id, exc)
            trace.append(_trace_event("FAULT_INJECTION_FAILED", task_id, attempt_id,
                                      fault_id=fault_id, error=f"{type(exc).__name__}: {str(exc)[:500]}"))

        try:
            # Create factory with workspace root for this task
            if self._factory_type == "real":
                from evals.reliability.p46_provider import (
                    RealLLMMinimalRuntimeFactory,
                    RealLLMOdysRuntimeFactory,
                )

                if config_id == "minimal":
                    factory = RealLLMMinimalRuntimeFactory(
                        provider=self._provider,
                        workspace_root=workspace_dir,
                    )
                    runtime_source = "minimal_factory"
                elif config_id == "odys_p3":
                    factory = RealLLMOdysRuntimeFactory(
                        provider=self._provider,
                        workspace_root=workspace_dir,
                    )
                    runtime_source = "odys_factory"
                else:
                    trace.append(_trace_event("VALIDATION_RESULT", task_id, attempt_id,
                                              result="fail", reason=f"Unknown config: {config_id}"))
                    return ExecutionOutcome(
                        claimed_complete=False,
                        observed_state={
                            "error": f"Unknown config: {config_id}",
                            "execution_trace": trace,
                            "tool_events": _filter_tool_events(trace),
                            "runtime_source": "unknown",
                        },
                        failure_type=f"UNKNOWN_CONFIG:{config_id}",
                    )
            else:
                from evals.reliability.runtime_factory import (
                    MinimalRuntimeFactory,
                    OdysRuntimeFactory,
                )

                if config_id == "minimal":
                    factory = MinimalRuntimeFactory(workspace_root=workspace_dir)
                    runtime_source = "minimal_factory"
                elif config_id == "odys_p3":
                    factory = OdysRuntimeFactory(workspace_root=workspace_dir)
                    runtime_source = "odys_factory"
                else:
                    trace.append(_trace_event("VALIDATION_RESULT", task_id, attempt_id,
                                              result="fail", reason=f"Unknown config: {config_id}"))
                    return ExecutionOutcome(
                        claimed_complete=False,
                        observed_state={
                            "error": f"Unknown config: {config_id}",
                            "execution_trace": trace,
                            "tool_events": _filter_tool_events(trace),
                            "runtime_source": "unknown",
                        },
                        failure_type=f"UNKNOWN_CONFIG:{config_id}",
                    )

            runtime = factory.create_runtime(request.config)

            # ---- RUNTIME_CREATED ----
            trace.append(_trace_event("RUNTIME_CREATED", task_id, attempt_id,
                                      runtime_source=runtime_source))

            # Execute through the runtime
            outcome = await runtime.execute(task, request.config)

            # ---- EXECUTION_COMPLETE ----
            trace.append(_trace_event("EXECUTION_COMPLETE", task_id, attempt_id,
                                      claimed_complete=outcome.claimed_complete,
                                      failure_type=outcome.failure_type))

            # Observe fixture state
            if self._fixture_registry is not None:
                try:
                    fixture = self._fixture_registry.get(task_id)
                    observed = fixture.observe(workspace_dir)
                    if outcome.observed_state:
                        outcome.observed_state["fixture_observations"] = observed
                    # ---- FIXTURE_OBSERVATIONS ----
                    trace.append(_trace_event("FIXTURE_OBSERVATIONS", task_id, attempt_id,
                                              observation_keys=list(observed.keys()) if isinstance(observed, dict) else []))
                except KeyError:
                    trace.append(_trace_event("FIXTURE_OBSERVATIONS", task_id, attempt_id,
                                              observation_keys=[], note="no fixture"))

            # ---- VALIDATION_RESULT ----
            validation_passed = outcome.claimed_complete and outcome.failure_type is None
            trace.append(_trace_event("VALIDATION_RESULT", task_id, attempt_id,
                                      result="pass" if validation_passed else "fail",
                                      failure_type=outcome.failure_type))

            # ---- Propagate trace into observed_state ----
            if outcome.observed_state is None:
                outcome.observed_state = {}
            outcome.observed_state["execution_trace"] = trace
            outcome.observed_state["tool_events"] = _filter_tool_events(trace)
            outcome.observed_state["runtime_source"] = runtime_source

            # ---- Persist trace to file ----
            self._write_trace(run_id, task_id, config_id, trace)

            return outcome

        except Exception as exc:
            from evals.reliability.p46_provider import ProviderIdentityError

            if isinstance(exc, ProviderIdentityError):
                # Identity drift is infrastructure invalidity, not a task
                # outcome.  Let Phase4Runner place it in invalid.jsonl.
                raise
            logger.error("Execution failed for %s: %s", request.run_id, exc)
            trace.append(_trace_event("EXECUTION_COMPLETE", task_id, attempt_id,
                                      error=f"{type(exc).__name__}: {str(exc)[:500]}"))
            trace.append(_trace_event("VALIDATION_RESULT", task_id, attempt_id,
                                      result="fail", failure_type=f"EXECUTOR_ERROR:{type(exc).__name__}"))
            return ExecutionOutcome(
                claimed_complete=False,
                observed_state={
                    "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                    "task_id": task_id,
                    "config_id": config_id,
                    "execution_trace": trace,
                    "tool_events": _filter_tool_events(trace),
                    "runtime_source": f"{config_id}_factory",
                },
                failure_type=f"EXECUTOR_ERROR:{type(exc).__name__}",
            )
        finally:
            self._reset_fixture(request)

    def _setup_fixture(self, request: ExecutionRequest) -> Path:
        """Setup fixture workspace for a task."""
        task_id = request.task.get("task_id", "unknown")
        run_id = request.run_id

        # Create temporary workspace
        workspace_dir = Path(tempfile.mkdtemp(prefix=f"p45-{task_id}-"))
        self._workspace_dirs[run_id] = workspace_dir

        # Setup fixture if registry available
        if self._fixture_registry is not None:
            try:
                fixture = self._fixture_registry.get(task_id)
                fixture.setup(workspace_dir)
            except KeyError:
                logger.debug("No fixture for task %s", task_id)
            except Exception as exc:
                logger.error("Fixture setup failed for %s: %s", task_id, exc)

        return workspace_dir

    def _reset_fixture(self, request: ExecutionRequest) -> None:
        """Reset fixture workspace after execution."""
        run_id = request.run_id
        workspace_dir = self._workspace_dirs.pop(run_id, None)

        if workspace_dir is None:
            return

        task_id = request.task.get("task_id", "unknown")
        if self._fixture_registry is not None:
            try:
                fixture = self._fixture_registry.get(task_id)
                fixture.reset(workspace_dir)
            except (KeyError, Exception):
                pass

        shutil.rmtree(workspace_dir, ignore_errors=True)

    def _write_trace(
        self,
        run_id: str,
        task_id: str,
        config_id: str,
        trace: list[dict[str, Any]],
    ) -> None:
        """Persist execution trace to a separate traces.jsonl file."""
        if self._trace_file is None:
            return
        import json as _json

        record = {
            "run_id": run_id,
            "task_id": task_id,
            "config": config_id,
            "execution_trace": trace,
            "event_count": len(trace),
        }
        try:
            self._trace_file.parent.mkdir(parents=True, exist_ok=True)
            with self._trace_file.open("a", encoding="utf-8", newline="\n") as f:
                f.write(_json.dumps(record, ensure_ascii=False, sort_keys=True))
                f.write("\n")
                f.flush()
        except Exception as exc:
            logger.debug("Trace write failed for %s: %s", run_id, exc)


def create_executor() -> P45BenchmarkExecutor:
    """Factory that builds a fully-configured P45BenchmarkExecutor.

    Called with no arguments by the CLI's ``_maybe_factory`` helper.
    Uses RealLLM factories by default for official benchmark runs.
    """
    from evals.reliability.fixture_packages.registry import FixtureRegistry
    from evals.reliability.p46_provider import (
        create_real_provider,
        provider_identity,
    )

    fixture_registry = FixtureRegistry()
    logger.info("Loaded %d fixtures", len(fixture_registry))
    provider = create_real_provider()
    identity = provider_identity(provider)

    return P45BenchmarkExecutor(
        fixture_registry=fixture_registry,
        factory_type="real",
        provider=provider,
        provider_identity=identity,
    )


def create_scripted_executor() -> P45BenchmarkExecutor:
    """Factory that builds a scripted (testing) P45BenchmarkExecutor.

    Uses MinimalRuntimeFactory / OdysRuntimeFactory with
    ScriptedProviderAdapter for deterministic test runs.
    """
    from evals.reliability.fixture_packages.registry import FixtureRegistry

    fixture_registry = FixtureRegistry()
    logger.info("Loaded %d fixtures (scripted mode)", len(fixture_registry))

    return P45BenchmarkExecutor(
        fixture_registry=fixture_registry,
        factory_type="scripted",
    )


__all__ = [
    "P45BenchmarkExecutor",
    "create_executor",
    "create_scripted_executor",
]
