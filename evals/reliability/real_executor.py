"""Real Odys runtime adapter for the frozen Phase 4 runner.

The adapter owns no benchmark semantics.  It selects one of two explicitly
provided Odys runtime factories, passes the same benchmark task/fixture/fault
to that runtime, and normalizes the runtime's safe observations into the
common validator shape.  A caller supplies factories because provider,
workspace, database, and credential construction are environment concerns;
the adapter must never silently replace them with a dry-run implementation.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from evals.reliability.run_phase4 import (
    ExecutionOutcome,
    ExecutionRequest,
    NOT_MEASURED,
)


P3_FEATURES = (
    "completion_authority",
    "workflow_verifier",
    "typed_taskgraph",
    "failure_provenance",
    "selective_repair",
    "macro_replan",
    "durable_workflow_recovery",
)


class RealExecutorConfigurationError(ValueError):
    """Raised only for an invalid adapter/configuration contract."""


class RuntimeSession(Protocol):
    async def execute(self, request: Any) -> Any: ...


RuntimeFactory = Callable[[ExecutionRequest], RuntimeSession | Awaitable[RuntimeSession]]
StateReader = Callable[[ExecutionRequest, Any], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]
TraceSink = Callable[[ExecutionRequest, Mapping[str, Any]], Any | Awaitable[Any]]


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        dumped = dump(mode="json")
        if isinstance(dumped, Mapping):
            return dict(dumped)
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return dict(attributes)
    return {}


def _status_value(value: Any) -> str | None:
    status = getattr(value, "status", None)
    if status is None and isinstance(value, Mapping):
        status = value.get("status")
    return getattr(status, "value", status)


def _safe_trace(runtime_result: Any) -> list[dict[str, Any]]:
    raw = _as_mapping(runtime_result)
    nested = _as_mapping(raw.get("raw"))
    trace = raw.get("execution_trace") or raw.get("safe_trace") or raw.get("trace") or nested.get("safe_trace") or []
    return [dict(item) for item in trace if isinstance(item, Mapping)][-100:]


def _tool_events(trace: list[dict[str, Any]], runtime_result: Any) -> list[dict[str, Any]]:
    raw = _as_mapping(runtime_result)
    explicit = raw.get("tool_events")
    if isinstance(explicit, list):
        return [dict(item) for item in explicit if isinstance(item, Mapping)][-100:]
    return [
        item for item in trace
        if str(item.get("event", "")).casefold().startswith("tool_")
        or "capability" in item
    ][-100:]


def _usage(runtime_result: Any) -> dict[str, Any]:
    raw = _as_mapping(runtime_result)
    usage = raw.get("usage")
    if isinstance(usage, Mapping):
        return dict(usage)
    nested = _as_mapping(raw.get("raw"))
    usage = nested.get("usage")
    return dict(usage) if isinstance(usage, Mapping) else {}


class RealExecutorAdapter:
    """Execute one benchmark case through a real Odys runtime.

    ``minimal_runtime_factory`` must construct the ordinary Odys worker path
    without P3 authority.  ``odys_runtime_factory`` must construct the P3
    verified/recovery path.  Keeping these factories separate makes it
    impossible for the adapter to accidentally add P3 services to Minimal.
    """

    def __init__(
        self,
        *,
        minimal_runtime_factory: RuntimeFactory | None = None,
        odys_runtime_factory: RuntimeFactory | None = None,
        state_reader: StateReader | None = None,
        trace_sink: TraceSink | None = None,
    ):
        self.minimal_runtime_factory = minimal_runtime_factory
        self.odys_runtime_factory = odys_runtime_factory
        self.state_reader = state_reader
        self.trace_sink = trace_sink

    @property
    def ready(self) -> bool:
        return self.minimal_runtime_factory is not None and self.odys_runtime_factory is not None

    @staticmethod
    def _validate_config(config: Mapping[str, Any]) -> str:
        config_id = str(config.get("config_id", ""))
        features = config.get("features")
        if not isinstance(features, Mapping):
            raise RealExecutorConfigurationError("CONFIG_FEATURES_MISSING")
        if config_id == "minimal":
            if any(bool(features.get(name)) for name in P3_FEATURES):
                raise RealExecutorConfigurationError("MINIMAL_P3_AUTHORITY_FORBIDDEN")
            return "minimal"
        if config_id == "odys_p3":
            if not all(bool(features.get(name)) for name in P3_FEATURES):
                raise RealExecutorConfigurationError("ODYS_P3_FEATURES_INCOMPLETE")
            return "odys_p3"
        raise RealExecutorConfigurationError(f"UNSUPPORTED_REAL_CONFIG:{config_id}")

    async def _make_runtime(self, request: ExecutionRequest, mode: str) -> RuntimeSession:
        factory = self.minimal_runtime_factory if mode == "minimal" else self.odys_runtime_factory
        if factory is None:
            raise RealExecutorConfigurationError(f"REAL_RUNTIME_FACTORY_MISSING:{mode}")
        runtime = factory(request)
        if inspect.isawaitable(runtime):
            runtime = await runtime
        if not hasattr(runtime, "execute"):
            raise RealExecutorConfigurationError(f"REAL_RUNTIME_INVALID:{mode}")
        return runtime

    async def _read_state(self, request: ExecutionRequest, runtime_result: Any) -> dict[str, Any]:
        if self.state_reader is not None:
            state = self.state_reader(request, runtime_result)
            if inspect.isawaitable(state):
                state = await state
            return dict(state)
        raw = _as_mapping(runtime_result)
        state = raw.get("final_state")
        return dict(state) if isinstance(state, Mapping) else {}

    @staticmethod
    def _failure_type(runtime_result: Any) -> str | None:
        raw = _as_mapping(runtime_result)
        nested = _as_mapping(raw.get("raw"))
        value = raw.get("failure_type") or raw.get("error_type") or nested.get("error_type")
        if value:
            return str(value)
        if str(_status_value(runtime_result) or "").upper() in {"FAILURE", "FAILED", "ERROR"}:
            return "EXECUTION_FAILED"
        return None

    async def execute(self, request: ExecutionRequest) -> dict[str, Any]:
        """Run and normalize one case; runtime failures become valid failures."""
        mode = self._validate_config(request.config)
        trace: list[dict[str, Any]] = []
        tool_events: list[dict[str, Any]] = []
        final_state: dict[str, Any] = {}
        runtime_result: Any = None
        try:
            runtime = await self._make_runtime(request, mode)
            runtime_result = await runtime.execute(request)
            trace = _safe_trace(runtime_result)
            tool_events = _tool_events(trace, runtime_result)
            final_state = await self._read_state(request, runtime_result)
            raw = _as_mapping(runtime_result)
            nested = _as_mapping(raw.get("raw"))
            usage = _usage(runtime_result)
            status = str(_status_value(runtime_result) or "").upper()
            claimed_complete = bool(raw.get("claimed_complete", raw.get("completion_claim", nested.get("completion_claim", status == "SUCCESS"))))
            failure_type = self._failure_type(runtime_result)
            if status in {"FAILURE", "FAILED", "ERROR"}:
                claimed_complete = False
        except Exception as exc:
            # A provider/tool/runtime failure is a benchmark observation.  It
            # must reach the shared validator as a failed run, not abort the
            # runner and disappear from the raw result set.
            failure_type = str(getattr(exc, "error_type", None) or type(exc).__name__)
            trace = [{"event": "EXECUTION_FAILED", "error_type": failure_type}]
            tool_events = []
            final_state = {}
            usage = {}
            claimed_complete = False
            runtime_result = None
            status = "FAILURE"
            raw = {}
            nested = {}

        validator_input = {
            "final_state": final_state,
            "tool_events": tool_events,
        }
        usage = locals().get("usage", {})
        normalized = {
            # Adapter contract fields consumed by P43 evidence and the shared
            # external-observable validator.
            "run_id": request.run_id,
            "task_id": request.task["task_id"],
            "config": request.config["config_id"],
            "execution_trace": trace,
            "tool_events": tool_events,
            "final_state": final_state,
            "validator_input": validator_input,
            # Phase 4 runner normalization fields.
            "claimed_complete": claimed_complete,
            "observed_state": final_state,
            "failure_type": failure_type,
            "recovery_required": bool(raw.get("recovery_required", False)),
            "recovery_attempted": bool(raw.get("recovery_attempted", False)),
            "recovery_success": bool(raw.get("recovery_success", False)),
            "repair_scope": raw.get("repair_scope"),
            "repair_attempts": int(raw.get("repair_attempts", 0) or 0),
            "replan_count": int(raw.get("replan_count", 0) or 0),
            "lost_work_units": raw.get("lost_work_units", NOT_MEASURED),
            "duplicate_side_effect_count": int(raw.get("duplicate_side_effect_count", 0) or 0),
            "tool_calls": int(raw.get("tool_calls", nested.get("tool_call_count", len(tool_events))) or 0),
            "model_calls": int(raw.get("model_calls", nested.get("turn_count", 0)) or 0),
            "attempt_count": int(raw.get("attempt_count", 1) or 1),
            "tokens_input": usage.get("input_tokens", usage.get("prompt_tokens", NOT_MEASURED)),
            "tokens_output": usage.get("output_tokens", usage.get("completion_tokens", NOT_MEASURED)),
            "total_tokens": usage.get("total_tokens", NOT_MEASURED),
            "model_cost": raw.get("model_cost", NOT_MEASURED),
            "tool_cost": raw.get("tool_cost", NOT_MEASURED),
            "wall_time_seconds": raw.get("wall_time_seconds", NOT_MEASURED),
            "human_intervention": bool(raw.get("human_intervention", False)),
        }
        if self.trace_sink is not None:
            observed = self.trace_sink(request, normalized)
            if inspect.isawaitable(observed):
                await observed
        return normalized


__all__ = ["P3_FEATURES", "RealExecutorAdapter", "RealExecutorConfigurationError"]
