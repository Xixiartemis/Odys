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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.reliability.run_phase4 import (
    ExecutionOutcome,
    ExecutionRequest,
    NOT_MEASURED,
    RuntimeInfrastructureError,
    RunBudgetExhausted,
)

logger = logging.getLogger(__name__)


@dataclass
class RunBudgetLedger:
    """One immutable experiment budget shared by all phases of one run.

    The frozen protocol exposes ``max_model_calls`` as the provider-call
    ceiling.  Recovery is a continuation of the same benchmark run, so it
    receives this ledger rather than a fresh per-attempt budget.
    """

    max_provider_calls: int
    max_turns: int | None = None
    max_repair_attempts: int = 1
    max_replan_attempts: int = 0
    root_provider_calls: int = 0
    nested_provider_calls: int = 0
    blocked_provider_calls: int = 0
    exhausted: bool = False
    _phases: list[str] = field(default_factory=list)

    def reserve(self, phase: str) -> None:
        """Reserve exactly one real provider call, or fail before transport."""
        if self.exhausted or self.total_provider_calls >= self.max_provider_calls:
            self.exhausted = True
            self.blocked_provider_calls += 1
            raise RunBudgetExhausted("RUN_API_BUDGET_EXHAUSTED")
        if str(phase).lower() == "initial":
            self.root_provider_calls += 1
        else:
            self.nested_provider_calls += 1
        self._phases.append(str(phase))

    @property
    def total_provider_calls(self) -> int:
        return self.root_provider_calls + self.nested_provider_calls

    def snapshot(self) -> dict[str, Any]:
        return {
            "max_provider_calls": self.max_provider_calls,
            "max_turns": self.max_turns,
            "max_repair_attempts": self.max_repair_attempts,
            "max_replan_attempts": self.max_replan_attempts,
            "root_provider_calls": self.root_provider_calls,
            "nested_provider_calls": self.nested_provider_calls,
            "provider_calls": self.total_provider_calls,
            "blocked_provider_calls": self.blocked_provider_calls,
            "exhausted": self.exhausted,
        }

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


_CONTROLLED_FAILURES: dict[str, frozenset[str]] = {
    "FAIL_TOOL_ON_CALL_1": frozenset(
        {"TOOL_ERROR", "TOOL_EXECUTION_ERROR", "EXECUTION_FAILED", "TOOL_FAILURE"}
    ),
    "FAIL_TOOL_ON_CALL_2": frozenset(
        {"TOOL_ERROR", "TOOL_EXECUTION_ERROR", "EXECUTION_FAILED", "TOOL_FAILURE"}
    ),
    "PROVIDER_TIMEOUT_ON_CALL_1": frozenset({"PROVIDER_TIMEOUT"}),
    "PROVIDER_UNAVAILABLE": frozenset({"PROVIDER_UNAVAILABLE"}),
    "QUOTA_EXHAUSTED": frozenset({"QUOTA_EXHAUSTED"}),
    "MALFORMED_RESPONSE": frozenset(
        {"PROVIDER_MALFORMED_RESPONSE", "MALFORMED_PROVIDER_RESPONSE"}
    ),
    "INTERRUPT_AFTER_EFFECT": frozenset({"PROCESS_INTERRUPTED", "INTERRUPTED"}),
    "CAPABILITY_UNAVAILABLE": frozenset(
        {"CAPABILITY_UNAVAILABLE", "UNKNOWN_CAPABILITY", "TOOL_NOT_FOUND"}
    ),
    "STALE_WORKSPACE_BEFORE_DISPATCH": frozenset(
        {"STALE_WORKSPACE", "STALE_PLAN", "RUNTIME_TARGET_DIVERGENCE", "PRECONDITION_FAILED"}
    ),
    "INVALIDATE_ASSUMPTION": frozenset(
        {"ASSUMPTION_INVALID", "WRONG_ASSUMPTION", "REPEATED_TOOL_FAILURE"}
    ),
    "DUPLICATE_DELIVERY_ATTEMPT": frozenset(
        {"DUPLICATE_DELIVERY_ATTEMPT", "DUPLICATE_DELIVERY"}
    ),
}


def _is_infrastructure_failure(outcome: ExecutionOutcome, fault_id: str | None) -> bool:
    """Classify runtime integrity failures without hiding controlled faults."""
    if outcome.infrastructure_failure:
        return True
    failure_type = str(outcome.failure_type or "").upper()
    if not failure_type:
        return False
    if failure_type.startswith(("MINIMAL_ERROR:", "ODYS_ERROR:", "EXECUTOR_ERROR:")):
        return True
    if failure_type in {
        "AUTH_INVALID",
        "BILLING_OR_CREDIT_EXHAUSTED",
        "PROVIDER_TIMEOUT",
        "PROVIDER_UNAVAILABLE",
        "QUOTA_EXHAUSTED",
        "MALFORMED_PROVIDER_RESPONSE",
        "UNKNOWN_PROVIDER_FAILURE",
        "MODEL_IDENTITY_MISMATCH",
        "PROVIDER_ENDPOINT_MISMATCH",
        "PROVIDER_IDENTITY_MISMATCH",
        "CREDENTIAL_REQUIRED_BEFORE_RUN",
    }:
        return failure_type not in _CONTROLLED_FAILURES.get(fault_id or "", frozenset())
    if fault_id is None:
        return True
    expected = _CONTROLLED_FAILURES.get(fault_id)
    return expected is not None and failure_type not in expected and failure_type.startswith(
        ("PROVIDER_", "MINIMAL_", "ODYS_", "EXECUTOR_")
    )


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
        expected_model: str | None = None,
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
        # Runtime objects are retained only until the runner asks for a
        # validation-rejection recovery through the explicit runtime
        # recovery contract.
        self._active_runtimes: dict[str, Any] = {}
        self._provider_call_offsets: dict[str, int] = {}
        self._run_budgets: dict[str, RunBudgetLedger] = {}
        self._frozen_budgets: dict[str, Any] = {}
        self._expected_model = expected_model or (
            self._provider_identity.get("model")
            if self._provider_identity
            else None
        )
        logger.info("P45BenchmarkExecutor created with factory_type=%s", factory_type)

    def configure_frozen_budget(self, budgets: dict[str, Any] | None) -> None:
        """Resolve the frozen protocol budget without starting execution.

        The runner calls this during construction.  It is intentionally a
        configuration-only hook so a provider cannot be contacted before the
        first selected run.
        """
        values = dict(budgets or {})
        max_calls = values.get("max_model_calls")
        if max_calls is None:
            raise RuntimeInfrastructureError("FROZEN_PROVIDER_BUDGET_MISSING")
        self._frozen_budgets = {
            "max_provider_calls": int(max_calls),
            "max_turns": int(values["max_turns"]) if values.get("max_turns") is not None else None,
            # The official bridge already bounds these values; recording
            # them here prevents a recovery coordinator from inventing a new
            # budget at the provider boundary.
            "max_repair_attempts": 1,
            "max_replan_attempts": 0,
        }

    def _run_budget(self, run_id: str) -> RunBudgetLedger:
        ledger = self._run_budgets.get(run_id)
        if ledger is None:
            values = self._frozen_budgets
            if not values:
                # Direct unit use of P45 predates the official runner hook;
                # leave that path unlimited while official runs are always
                # configured from the frozen protocol.
                values = {
                    "max_provider_calls": 2**31 - 1,
                    "max_turns": None,
                    "max_repair_attempts": 1,
                    "max_replan_attempts": 0,
                }
            ledger = RunBudgetLedger(**values)
            self._run_budgets[run_id] = ledger
        return ledger

    @property
    def provider_identity(self) -> dict[str, Any] | None:
        """Secret-free provider identity used by an official run."""
        return dict(self._provider_identity) if self._provider_identity else None

    def persist_provider_identity(self, path: Path) -> dict[str, Any]:
        """Persist identity evidence into the current output bundle."""
        if self._provider is None:
            raise RuntimeError("PROVIDER_IDENTITY_UNAVAILABLE")
        from evals.reliability.p46_provider import validate_and_persist_provider_identity

        identity = validate_and_persist_provider_identity(
            self._provider,
            path=path,
            expected_model=self._expected_model or getattr(
                self._provider, "model", "mimo-v2.5-pro"
            ),
        )
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
                FROZEN_MODEL,
            )

            self._expected_model = self._expected_model or FROZEN_MODEL
            self._provider = create_real_provider(expected_model=self._expected_model)
            self._provider_identity = validate_and_persist_provider_identity(
                self._provider,
                expected_model=self._expected_model,
            )

        config_id = request.config.get("config_id", "unknown")
        task = request.task
        task_id = task.get("task_id", "unknown")
        run_id = request.run_id
        self._run_budget(run_id)
        attempt_id = f"{run_id}::attempt-{request.repeat_index}"
        self._provider_call_offsets.setdefault(
            run_id,
            len(getattr(self._provider, "call_records", ()) or ()),
        )
        self._bind_provider_context(
            run_id=run_id,
            task_id=str(task_id),
            attempt_id=attempt_id,
            phase="initial",
        )

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
                                              validator_execution_status="NOT_EXECUTED",
                                              acceptance_status="NOT_EVALUATED",
                                              reason=f"Unknown config: {config_id}"))
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
                                              validator_execution_status="NOT_EXECUTED",
                                              acceptance_status="NOT_EVALUATED",
                                              reason=f"Unknown config: {config_id}"))
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
            self._active_runtimes[request.run_id] = runtime

            # ---- RUNTIME_CREATED ----
            trace.append(_trace_event("RUNTIME_CREATED", task_id, attempt_id,
                                      runtime_source=runtime_source))

            # Execute through the runtime
            runtime_config = dict(request.config)
            # These execution-local bindings are not benchmark inputs.  They
            # make the durable native Attempt and the recovery coordinator
            # use the same run identity as the official Phase 4 request.
            runtime_config["run_id"] = request.run_id
            runtime_config["_workspace_root"] = str(workspace_dir)
            outcome = await runtime.execute(task, runtime_config)
            self._attach_provider_accounting(outcome, run_id)

            if _is_infrastructure_failure(outcome, fault_id):
                outcome.infrastructure_failure = True
                runtime_failure = outcome.observed_state.get("runtime_failure")
                detail = (
                    runtime_failure.get("error_message")
                    if isinstance(runtime_failure, dict)
                    else None
                )
                outcome.infrastructure_error = (
                    outcome.infrastructure_error
                    or (
                        f"UNEXPECTED_RUNTIME_FAILURE:{outcome.failure_type}"
                        + (f": {detail}" if detail else "")
                    )
                )

            # ---- EXECUTION_COMPLETE ----
            trace.append(_trace_event("EXECUTION_COMPLETE", task_id, attempt_id,
                                      claimed_complete=outcome.claimed_complete,
                                      failure_type=outcome.failure_type))

            # Observe fixture state
            if self._fixture_registry is not None:
                try:
                    fixture = self._fixture_registry.get(task_id)
                    observed = fixture.observe(workspace_dir)
                    if isinstance(observed, dict):
                        # Fixture observations are the authoritative external
                        # state used by the shared validator. Keep the
                        # nested copy for audit consumers while exposing the
                        # same fields at the validator boundary for
                        # pre/post repair comparison.
                        outcome.observed_state.update(observed)
                        outcome.observed_state["fixture_observations"] = observed
                    # ---- FIXTURE_OBSERVATIONS ----
                    trace.append(_trace_event("FIXTURE_OBSERVATIONS", task_id, attempt_id,
                                              observation_keys=list(observed.keys()) if isinstance(observed, dict) else []))
                except KeyError:
                    trace.append(_trace_event("FIXTURE_OBSERVATIONS", task_id, attempt_id,
                                              observation_keys=[], note="no fixture"))

            # The shared Phase4Runner owns the actual external validator
            # boundary.  Keep this event as a non-authoritative placeholder;
            # the runner replaces it with explicit execution/acceptance
            # statuses after validation.
            trace.append(_trace_event("VALIDATION_RESULT", task_id, attempt_id,
                                      validator_execution_status="NOT_EXECUTED",
                                      acceptance_status="NOT_EVALUATED",
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
                                      validator_execution_status="NOT_EXECUTED",
                                      acceptance_status="NOT_EVALUATED",
                                      failure_type=f"EXECUTOR_ERROR:{type(exc).__name__}"))
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
        # The fixture workspace must survive until the runner has performed
        # external validation and (for Odys) recovery.  Cleanup is performed
        # by ``cleanup`` after that boundary; resetting here would make the
        # recovery path observe a different/empty workspace.

    async def recover_after_validation(
        self,
        request: ExecutionRequest,
        outcome: ExecutionOutcome,
        validation: Any,
    ) -> ExecutionOutcome | dict[str, Any] | None:
        """Delegate rejection recovery through the explicit runtime contract."""
        from evals.reliability.runtime_factory.protocol import RecoverableBenchmarkRuntime

        runtime = self._active_runtimes.get(request.run_id)
        try:
            if not isinstance(runtime, RecoverableBenchmarkRuntime):
                if request.config.get("config_id") == "odys_p3":
                    raise RuntimeInfrastructureError(
                        "OFFICIAL_RECOVERY_AUTHORITY_UNAVAILABLE"
                    )
                return None
            self._bind_provider_context(
                run_id=request.run_id,
                task_id=str(request.task.get("task_id", "unknown")),
                attempt_id=str(outcome.repair_attempt_id or "recovery"),
                phase="recovery",
            )
            result = await runtime.recover_after_validation(
                request,
                outcome,
                validation,
            )
            if result is None:
                return None
            repaired = ExecutionOutcome.from_value(result)
            self._attach_provider_accounting(repaired, request.run_id)
            workspace_dir = self._workspace_dirs.get(request.run_id)
            if workspace_dir is not None and self._fixture_registry is not None:
                fixture = self._fixture_registry.get(request.task["task_id"])
                observed = fixture.observe(workspace_dir)
                repaired.observed_state.update(observed)
                repaired.observed_state["fixture_observations"] = observed
            return repaired
        finally:
            self._active_runtimes.pop(request.run_id, None)
            self._reset_fixture(request)
            self._run_budgets.pop(request.run_id, None)

    def cleanup(self, request: ExecutionRequest) -> None:
        """Release one run's workspace after validation/recovery is complete."""
        self._active_runtimes.pop(request.run_id, None)
        self._reset_fixture(request)
        self._run_budgets.pop(request.run_id, None)
        self._provider_call_offsets.pop(request.run_id, None)

    def _bind_provider_context(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt_id: str,
        phase: str,
    ) -> None:
        binder = getattr(self._provider, "bind_execution_context", None)
        if callable(binder):
            binder(
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                phase=phase,
            )
        ledger = self._run_budgets.get(run_id)
        if ledger is not None:
            self._bind_provider_budget(ledger)

    def _bind_provider_budget(self, ledger: RunBudgetLedger) -> None:
        binder = getattr(self._provider, "bind_run_budget", None)
        if callable(binder):
            binder(ledger)

    def _attach_provider_accounting(
        self,
        outcome: ExecutionOutcome,
        run_id: str,
    ) -> None:
        records = getattr(self._provider, "call_records", None)
        if not isinstance(records, list):
            return
        start = self._provider_call_offsets.get(run_id, 0)
        run_records = [dict(item) for item in records[start:] if isinstance(item, dict)]
        actual_records = [
            item for item in run_records
            if item.get("provider_call", True) is not False
        ]
        outcome.provider_call_records = run_records
        outcome.provider_calls = len(actual_records)
        # A provider turn is the authoritative count when the adapter can
        # observe it.  Keep test/dry adapters' existing count if they expose
        # no call records at all.
        outcome.model_calls = len(actual_records)

        attempt_ids = {
            str(item["attempt_id"])
            for item in actual_records
            if item.get("attempt_id")
        }
        outcome.provider_attempt_count = len(attempt_ids)
        outcome.root_attempt_count = 1 if actual_records else 0
        outcome.nested_attempt_count = max(0, len(attempt_ids) - 1)

        ledger = self._run_budgets.get(run_id)
        if ledger is not None:
            if ledger.exhausted:
                # A root-scoped budget exhaustion is a controlled benchmark
                # outcome.  It must not be reclassified as an infrastructure
                # exception or trigger a fresh recovery budget.
                outcome.budget_exhausted = True
                outcome.failure_type = "BUDGET_EXHAUSTED"
                outcome.recovery_required = False
            outcome.provider_calls = ledger.total_provider_calls
            outcome.root_attempt_count = 1 if ledger.total_provider_calls else 0
            outcome.nested_attempt_count = max(
                outcome.nested_attempt_count,
                1 if ledger.nested_provider_calls else 0,
            )

        def _sum_known(field: str) -> int | str:
            values = [item.get(field) for item in actual_records]
            if not values or any(not isinstance(value, int) for value in values):
                return NOT_MEASURED
            return sum(values)

        outcome.tokens_input = _sum_known("input_tokens")
        outcome.tokens_output = _sum_known("output_tokens")
        outcome.total_tokens = _sum_known("total_tokens")

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
        expected_model=provider.model,
    )


def create_cheap_executor() -> P45BenchmarkExecutor:
    """Factory for the isolated ``phase4-v1-cheap-model`` profile."""
    from evals.reliability.fixture_packages.registry import FixtureRegistry
    from evals.reliability.p46_provider import (
        CHEAP_MODEL,
        create_cheap_model_provider,
        provider_identity,
    )

    fixture_registry = FixtureRegistry()
    logger.info("Loaded %d fixtures", len(fixture_registry))
    provider = create_cheap_model_provider()
    identity = provider_identity(provider, expected_model=CHEAP_MODEL)

    return P45BenchmarkExecutor(
        fixture_registry=fixture_registry,
        factory_type="real",
        provider=provider,
        provider_identity=identity,
        expected_model=CHEAP_MODEL,
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
    "create_cheap_executor",
    "create_scripted_executor",
]
