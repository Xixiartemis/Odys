"""Execution harness for the frozen ``phase4-v1`` benchmark.

This module is deliberately an execution layer.  The benchmark inputs are
loaded from :mod:`evals.reliability.phase4_v1`; this runner never edits them
or derives a second task/fault/metric definition.  A real harness is supplied
through :class:`BenchmarkExecutor`, which keeps the protocol independent from
the model/provider implementation used by a run.

The default executor fails closed.  Producing a plausible result without a
real execution adapter would turn a calibration placeholder into benchmark
evidence, so the failed run is written to ``invalid.jsonl`` instead.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import inspect
import json
import os
import platform
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Protocol

from evals.reliability.phase4_v1.validate import (
    ProtocolError,
    canonical_json,
    protocol_hash,
    validate_protocol,
    validate_raw_result,
)


NOT_MEASURED = "NOT_MEASURED"
PROTOCOL_NAME = "phase4-v1"
DEFAULT_PROTOCOL_ROOT = Path(__file__).parent / "phase4_v1"


class RunnerConfigurationError(RuntimeError):
    """Raised when an execution adapter is not configured safely."""


class RunSelectionError(ValueError):
    """Raised for a run selection that is outside the frozen protocol."""


class ResumeIntegrityError(RuntimeError):
    """Raised when an existing result is not compatible with this protocol."""


RESUME_IDENTITY_FIELDS = (
    "benchmark_version",
    "protocol_hash",
    "manifest_hash",
    "fault_set_hash",
    "validator_hash",
    "fixture_set_hash",
    "budget_identity",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _document_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class ProtocolSnapshot:
    """All frozen inputs and derived identities for one protocol version."""

    root: Path
    protocol: dict[str, Any]
    manifest: dict[str, Any]
    faults: dict[str, Any]
    validators: dict[str, Any]
    fixtures: dict[str, Any]
    ablation: dict[str, Any]
    configs: dict[str, dict[str, Any]]
    protocol_hash: str
    manifest_hash: str
    fault_set_hash: str
    validator_hash: str
    fixture_set_hash: str
    budget_identity: str

    @classmethod
    def load(cls, root: Path = DEFAULT_PROTOCOL_ROOT) -> "ProtocolSnapshot":
        root = Path(root).resolve()
        report = validate_protocol(root)
        protocol = _read_json(root / "protocol.json")
        manifest = _read_json(root / "manifest.json")
        faults = _read_json(root / "faults.json")
        validators = _read_json(root / "validators.json")
        fixtures = _read_json(root / "fixtures" / "catalog.json")
        ablation = _read_json(root / "ablation.json")
        configs = {
            path.stem: _read_json(path)
            for path in sorted((root / "configs").glob("*.json"))
        }
        if report["benchmark_version"] != PROTOCOL_NAME:
            raise ProtocolError("unsupported protocol version")
        return cls(
            root=root,
            protocol=protocol,
            manifest=manifest,
            faults=faults,
            validators=validators,
            fixtures=fixtures,
            ablation=ablation,
            configs=configs,
            protocol_hash=protocol_hash(root),
            manifest_hash=_document_hash(manifest),
            fault_set_hash=_document_hash(faults),
            validator_hash=_document_hash(validators),
            fixture_set_hash=_document_hash(fixtures),
            budget_identity=_document_hash(protocol["budgets"]),
        )

    @property
    def tasks(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.manifest["tasks"])

    @property
    def fault_by_id(self) -> dict[str, dict[str, Any]]:
        return {item["fault_id"]: item for item in self.faults["faults"]}

    def fairness_identity(self, *, model: str, provider: str) -> dict[str, str]:
        return {
            "benchmark_version": self.protocol["benchmark_version"],
            "protocol_hash": self.protocol_hash,
            "manifest_hash": self.manifest_hash,
            "fault_set_hash": self.fault_set_hash,
            "validator_hash": self.validator_hash,
            "fixture_set_hash": self.fixture_set_hash,
            "model_identity": model,
            "provider_identity": provider,
            "budget_identity": self.budget_identity,
        }


class ConfigLoader:
    """Loads only the frozen configuration files."""

    def __init__(self, snapshot: ProtocolSnapshot):
        self.snapshot = snapshot

    def load(self, config_name: str) -> dict[str, Any]:
        try:
            config = self.snapshot.configs[config_name]
        except KeyError as exc:
            raise RunSelectionError(f"unknown frozen configuration: {config_name}") from exc
        if config.get("config_id") != config_name:
            raise ProtocolError(f"configuration identity drift: {config_name}")
        return config


@dataclass(frozen=True)
class FixtureHandle:
    fixture_id: str
    version: str
    initial_state: str
    fixture_hash: str
    metadata: dict[str, Any]


class FixtureManager:
    """Provides a resettable identity for a frozen catalog fixture.

    The catalog intentionally contains fixture metadata, not a hidden task
    implementation.  A harness may subclass this manager or supply a reset
    hook; the default manager is still deterministic and safe for dry runs.
    """

    def __init__(
        self,
        snapshot: ProtocolSnapshot,
        reset_hook: Callable[[FixtureHandle], None | Awaitable[None]] | None = None,
    ):
        self.snapshot = snapshot
        self.reset_hook = reset_hook

    def prepare(self, task: Mapping[str, Any]) -> FixtureHandle:
        fixture_id = str(task["fixture_id"])
        try:
            entry = self.snapshot.fixtures["fixtures"][fixture_id]
        except KeyError as exc:
            raise ProtocolError(f"unknown fixture: {fixture_id}") from exc
        if str(entry["version"]) != str(task["fixture_version"]):
            raise ProtocolError(f"fixture version drift for {fixture_id}")
        if task["fixture_hash_source"] != f"fixtures/catalog.json#{fixture_id}":
            raise ProtocolError(f"fixture hash source drift for {fixture_id}")
        fixture_hash = _document_hash({"fixture_id": fixture_id, **entry})
        return FixtureHandle(
            fixture_id=fixture_id,
            version=str(entry["version"]),
            initial_state=str(entry["initial_state"]),
            fixture_hash=fixture_hash,
            metadata=dict(entry),
        )

    async def reset(self, fixture: FixtureHandle) -> None:
        if self.reset_hook is None:
            return
        result = self.reset_hook(fixture)
        if inspect.isawaitable(result):
            await result


@dataclass(frozen=True)
class FaultPlan:
    fault_id: str
    fault_type: str
    trigger: str
    trigger_count: int
    deterministic_seed: int
    definition: dict[str, Any]


class FaultContext:
    """Deterministic boundary controller passed to an execution adapter."""

    def __init__(self, plan: FaultPlan):
        self.plan = plan
        self._fired = False

    def should_inject(self, boundary: str, ordinal: int = 1) -> bool:
        if self._fired or ordinal < 1:
            return False
        trigger = self.plan.trigger.casefold()
        boundary = boundary.casefold()
        if "provider" in trigger and boundary != "provider_call":
            return False
        if "tool_call" in trigger and boundary != "tool_call":
            return False
        if "delivery" in trigger and boundary != "delivery":
            return False
        if "completion" in trigger and boundary != "completion":
            return False
        if "assumption" in trigger and boundary != "assumption":
            return False
        if "workspace" in trigger and boundary != "workspace":
            return False
        if "capability" in trigger and boundary != "capability":
            return False
        if "effect" in trigger and boundary != "effect":
            return False
        if "ordinal ==" in trigger:
            expected = int(trigger.rsplit("ordinal ==", 1)[1].strip().split()[0])
            if ordinal != expected:
                return False
        elif "first " in trigger or "after first " in trigger:
            if ordinal != 1:
                return False
        self._fired = True
        return True


class FaultInjector:
    """Loads the frozen fault and exposes the same plan to every config."""

    def __init__(self, snapshot: ProtocolSnapshot):
        self.snapshot = snapshot
        self._faults = snapshot.fault_by_id

    def plan_for(self, task: Mapping[str, Any]) -> FaultPlan:
        fault_id = str(task["fault_injection"])
        try:
            definition = self._faults[fault_id]
        except KeyError as exc:
            raise ProtocolError(f"unknown fault: {fault_id}") from exc
        return FaultPlan(
            fault_id=fault_id,
            fault_type=str(definition["fault_type"]),
            trigger=str(definition["trigger"]),
            trigger_count=int(definition["trigger_count"]),
            deterministic_seed=int(definition["deterministic_seed"]),
            definition=dict(definition),
        )

    def context_for(self, task: Mapping[str, Any]) -> FaultContext:
        return FaultContext(self.plan_for(task))


@dataclass(frozen=True)
class RunSpec:
    task: dict[str, Any]
    config: dict[str, Any]
    repeat_index: int

    @property
    def run_id(self) -> str:
        return f"{self.task['task_id']}::{self.config['config_id']}::repeat-{self.repeat_index}"


def select_runs(
    snapshot: ProtocolSnapshot,
    *,
    headline: bool = False,
    ablation: bool = False,
    task_id: str | None = None,
    config_name: str | None = None,
    repeat_index: int | None = None,
) -> tuple[RunSpec, ...]:
    """Build a run plan without altering any frozen input."""
    modes = sum((headline, ablation, task_id is not None))
    if modes != 1:
        raise RunSelectionError("choose exactly one of --headline, --ablation, or --task")
    if task_id is not None and repeat_index is None:
        raise RunSelectionError("--task requires --repeat")
    if (headline or ablation) and (config_name or repeat_index is not None):
        raise RunSelectionError("batch selection cannot override frozen configs or repeats")

    tasks = list(snapshot.tasks)
    if headline:
        config_names = list(snapshot.protocol["headline"]["configs"])
        repeats = range(1, int(snapshot.protocol["headline"]["repeats"]) + 1)
    elif ablation:
        ids = set(snapshot.ablation["task_ids"])
        tasks = [task for task in tasks if task["task_id"] in ids]
        config_names = list(snapshot.ablation["configs"])
        repeats = range(1, int(snapshot.ablation["repeats"]) + 1)
    else:
        matching = [task for task in tasks if task["task_id"] == task_id]
        if not matching:
            raise RunSelectionError(f"unknown task: {task_id}")
        tasks = matching
        config_names = [config_name or "minimal"]
        repeats = (int(repeat_index),)

    loader = ConfigLoader(snapshot)
    configs = [loader.load(name) for name in config_names]
    return tuple(
        RunSpec(task=task, config=config, repeat_index=repeat)
        for task in tasks
        for config in configs
        for repeat in repeats
    )


@dataclass(frozen=True)
class ExecutionRequest:
    run_id: str
    repeat_index: int
    task: dict[str, Any]
    config: dict[str, Any]
    fixture: FixtureHandle
    fault: FaultPlan
    fault_context: FaultContext


@dataclass
class ExecutionOutcome:
    """Harness-neutral observations returned by a real execution adapter."""

    claimed_complete: bool = False
    observed_state: dict[str, Any] = field(default_factory=dict)
    failure_type: str | None = None
    recovery_required: bool = False
    recovery_attempted: bool = False
    recovery_success: bool = False
    repair_scope: str | None = None
    repair_attempts: int = 0
    replan_count: int = 0
    lost_work_units: int | str = NOT_MEASURED
    duplicate_side_effect_count: int = 0
    tool_calls: int = 0
    model_calls: int = 0
    attempt_count: int = 0
    tokens_input: int | str = NOT_MEASURED
    tokens_output: int | str = NOT_MEASURED
    total_tokens: int | str = NOT_MEASURED
    model_cost: float | str = NOT_MEASURED
    tool_cost: float | str = NOT_MEASURED
    wall_time_seconds: float | str = NOT_MEASURED
    human_intervention: bool = False
    # These fields are execution-layer observations.  They are intentionally
    # not promoted into the frozen result schema; official trace references
    # are carried by the free-form runtime_environment object below.
    execution_trace: list[dict[str, Any]] = field(default_factory=list)
    runtime_source: str | None = None

    @classmethod
    def from_value(cls, value: "ExecutionOutcome | Mapping[str, Any]") -> "ExecutionOutcome":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("execution adapter must return ExecutionOutcome or mapping")
        allowed = {field_name for field_name in cls.__dataclass_fields__}
        return cls(**{key: value[key] for key in allowed if key in value})


class BenchmarkExecutor(Protocol):
    async def execute(self, request: ExecutionRequest) -> ExecutionOutcome | Mapping[str, Any]: ...


class UnconfiguredExecutor:
    async def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        raise RunnerConfigurationError(
            "no benchmark execution adapter configured; use --executor module:factory"
        )


@dataclass(frozen=True)
class ValidationOutcome:
    verified_completion: bool
    validity: str
    failure_type: str | None = None


class ValidatorAdapter(Protocol):
    def validate(
        self,
        task: Mapping[str, Any],
        fixture: FixtureHandle,
        outcome: ExecutionOutcome,
    ) -> ValidationOutcome: ...


def _observed_matches(expected: Any, observed: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(observed, Mapping) and all(
            key in observed and _observed_matches(value, observed[key])
            for key, value in expected.items()
        )
    if isinstance(expected, str) and expected in {"present", "pass", "passed", "successful", "true"}:
        return observed is True or str(observed).casefold() in {str(expected).casefold(), "true", "pass", "passed", "present", "successful"}
    if isinstance(expected, str) and expected in {"not present", "false", "failed"}:
        return observed is False or str(observed).casefold() in {str(expected).casefold(), "false", "failed"}
    return expected == observed


class ExternalObservableValidator:
    """Shared validator that only consumes declared external observations."""

    def validate(
        self,
        task: Mapping[str, Any],
        fixture: FixtureHandle,
        outcome: ExecutionOutcome,
    ) -> ValidationOutcome:
        del fixture  # fixture identity is carried by the result; state is external to this adapter.
        expected = task["expected_observable_effects"]
        verified = all(
            key in outcome.observed_state and _observed_matches(value, outcome.observed_state[key])
            for key, value in expected.items()
        )
        return ValidationOutcome(
            verified_completion=verified,
            validity="VALIDATED_PASS" if verified else "VALIDATED_FAIL",
            failure_type=outcome.failure_type,
        )


def _maybe_factory(value: Any) -> Any:
    if inspect.isclass(value):
        return value()
    if callable(value):
        return value()
    return value


def load_executor(spec: str | None) -> BenchmarkExecutor:
    if not spec:
        return UnconfiguredExecutor()
    if ":" not in spec:
        raise RunnerConfigurationError("--executor must use module:factory syntax")
    module_name, attribute = spec.split(":", 1)
    target = getattr(importlib.import_module(module_name), attribute)
    executor = _maybe_factory(target)
    if not hasattr(executor, "execute"):
        raise RunnerConfigurationError("execution adapter has no execute method")
    return executor


def _repo_sha(repo_root: Path) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "UNKNOWN"


def _runtime_environment(
    snapshot: ProtocolSnapshot,
    *,
    model: str,
    provider: str,
    runtime_source: str | None = None,
    execution_trace_ref: str | None = None,
    trace_event_count: int | None = None,
) -> dict[str, Any]:
    environment: dict[str, Any] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "runner": "phase4-v1",
        "identity": snapshot.fairness_identity(model=model, provider=provider),
    }
    # result.schema.json is frozen and rejects new top-level keys.  The
    # runtime_environment object is intentionally extensible, so the trace
    # contract is recorded here without changing protocol_hash.
    if runtime_source is not None:
        environment["runtime_source"] = runtime_source
    if execution_trace_ref is not None:
        environment["execution_trace_ref"] = execution_trace_ref
    if trace_event_count is not None:
        environment["trace_event_count"] = trace_event_count
    return environment


def _invalid_outcome(exc: BaseException) -> dict[str, Any]:
    return {
        "reason": f"{type(exc).__name__}: {exc}",
        "exception_type": type(exc).__name__,
    }


class ResultWriter:
    """Append-only raw/invalid writer with derived aggregation input."""

    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.raw_path = self.output_dir / "raw.jsonl"
        self.invalid_path = self.output_dir / "invalid.jsonl"
        self.aggregation_path = self.output_dir / "aggregation-input.jsonl"
        self._repair_trailing_partial(self.raw_path)
        self._repair_trailing_partial(self.invalid_path)
        self._raw = self._load(self.raw_path)
        self._invalid = self._load(self.invalid_path)
        self._ids = {record["benchmark_run_id"] for record in (*self._raw, *self._invalid)}
        self._records = {
            record["benchmark_run_id"]: record
            for record in (*self._raw, *self._invalid)
        }

    @staticmethod
    def _repair_trailing_partial(path: Path) -> None:
        if not path.exists():
            return
        data = path.read_bytes()
        if not data or data.endswith(b"\n"):
            return
        last_newline = data.rfind(b"\n")
        with path.open("r+b") as handle:
            handle.truncate(max(last_newline + 1, 0))

    @staticmethod
    def _load(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
        return records

    def has_run(self, run_id: str) -> bool:
        return run_id in self._ids

    def assert_resume_compatible(self, run_id: str, current_identity: Mapping[str, str]) -> None:
        """Refuse to reuse a result from a different frozen experiment.

        The protocol hash is required at the top level so a legacy or
        hand-written record cannot appear compatible merely because a nested
        environment blob happens to contain a hash.  The remaining identity
        fields are emitted in ``runtime_environment.identity`` by this
        runner, while accepting top-level fields keeps the check useful for
        equivalent result writers.
        """
        record = self._records.get(run_id)
        if record is None:
            raise ResumeIntegrityError(f"RESUME_RESULT_MISSING: {run_id}")
        stored_protocol_hash = record.get("protocol_hash")
        if not stored_protocol_hash or stored_protocol_hash != current_identity.get("protocol_hash"):
            raise ResumeIntegrityError("PROTOCOL_HASH_MISMATCH")
        nested_identity = (record.get("runtime_environment") or {}).get("identity") or {}
        for field_name in RESUME_IDENTITY_FIELDS:
            stored_value = record.get(field_name, nested_identity.get(field_name))
            expected_value = current_identity.get(field_name)
            if stored_value is None or expected_value is None or stored_value != expected_value:
                raise ResumeIntegrityError(f"EXPERIMENT_IDENTITY_MISMATCH: {field_name}")

    def _append(self, path: Path, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def write_raw(self, record: dict[str, Any]) -> None:
        run_id = record["benchmark_run_id"]
        if self.has_run(run_id):
            if self._records[run_id] != record:
                raise ValueError(f"immutable result collision for {run_id}")
            return
        self._append(self.raw_path, record)
        self._raw.append(record)
        self._ids.add(run_id)
        self._records[run_id] = record
        self._rewrite_aggregation()

    def write_invalid(self, record: dict[str, Any]) -> None:
        run_id = record["benchmark_run_id"]
        if self.has_run(run_id):
            if self._records[run_id] != record:
                raise ValueError(f"immutable result collision for {run_id}")
            return
        self._append(self.invalid_path, record)
        self._invalid.append(record)
        self._ids.add(run_id)
        self._records[run_id] = record

    def _rewrite_aggregation(self) -> None:
        fd, name = tempfile.mkstemp(prefix="aggregation-", suffix=".jsonl", dir=self.output_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                for record in self._raw:
                    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                    handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.aggregation_path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @property
    def counts(self) -> dict[str, int]:
        return {"valid": len(self._raw), "invalid": len(self._invalid)}


TRACE_EVENT_FIELDS = frozenset(
    {"timestamp", "event_type", "task_id", "step_id", "attempt_id", "status", "metadata"}
)


class TraceOutputError(RuntimeError):
    """Raised when a valid run cannot be represented by the trace contract."""


class TraceWriter:
    """Append-only trace sidecar writer for official execution output."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, tuple[str, int]] = {}
        self._line_count = 0
        if self.path.exists():
            for line_number, line in enumerate(
                self.path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                self._line_count = line_number
                if not line.strip():
                    continue
                record = json.loads(line)
                run_id = record.get("run_id")
                if not isinstance(run_id, str):
                    raise TraceOutputError("TRACE_RUN_ID_MISSING")
                event_count = record.get("trace_event_count")
                if not isinstance(event_count, int) or event_count < 1:
                    raise TraceOutputError("TRACE_SCHEMA_INVALID")
                self._records[run_id] = (f"{self.path.name}#L{line_number}", event_count)

    @staticmethod
    def _validate_events(events: Any) -> list[dict[str, Any]]:
        if not isinstance(events, list) or not events:
            raise TraceOutputError("TRACE_EVENTS_MISSING")
        normalized: list[dict[str, Any]] = []
        for event in events:
            if not isinstance(event, Mapping) or not TRACE_EVENT_FIELDS.issubset(event):
                raise TraceOutputError("TRACE_SCHEMA_INVALID")
            if not isinstance(event["metadata"], Mapping):
                raise TraceOutputError("TRACE_SCHEMA_INVALID")
            normalized.append(dict(event))
        return normalized

    def assert_present(self, run_id: str) -> None:
        if run_id not in self._records:
            raise TraceOutputError(f"TRACE_RESUME_RECORD_MISSING: {run_id}")

    def append(
        self,
        *,
        run_id: str,
        task_id: str,
        config: str,
        repeat: int,
        runtime_source: str,
        model_identity: str,
        protocol_hash: str,
        events: Any,
    ) -> tuple[str, int]:
        normalized = self._validate_events(events)
        existing = self._records.get(run_id)
        if existing is not None:
            return existing
        record = {
            "run_id": run_id,
            "task_id": task_id,
            "config": config,
            "repeat": repeat,
            "runtime_source": runtime_source,
            "model_identity": model_identity,
            "protocol_hash": protocol_hash,
            "execution_trace": normalized,
            "trace_event_count": len(normalized),
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        self._line_count += 1
        reference = f"{self.path.name}#L{self._line_count}"
        self._records[run_id] = (reference, len(normalized))
        return reference, len(normalized)


class Phase4Runner:
    """Runs selected frozen cases and writes schema-validated raw evidence."""

    def __init__(
        self,
        snapshot: ProtocolSnapshot,
        *,
        output_dir: Path,
        executor: BenchmarkExecutor | None = None,
        validator: ValidatorAdapter | None = None,
        fixture_manager: FixtureManager | None = None,
        model: str = "FROZEN_BY_P4.2",
        provider: str = "FROZEN_BY_P4.2",
        repo_root: Path | None = None,
        trace_path: Path | None = None,
        require_trace: bool = False,
    ):
        if require_trace and trace_path is None:
            raise RunnerConfigurationError(
                "TRACE_OUTPUT_REQUIRED: provide trace_path for official runs"
            )
        self.snapshot = snapshot
        self.output = ResultWriter(output_dir)
        self.executor = executor or UnconfiguredExecutor()
        self.validator = validator or ExternalObservableValidator()
        self.fixtures = fixture_manager or FixtureManager(snapshot)
        self.injector = FaultInjector(snapshot)
        self.model = model
        self.provider = provider
        self.repo_root = Path(repo_root or Path.cwd()).resolve()
        self.repo_sha = _repo_sha(self.repo_root)
        self.require_trace = require_trace
        self.trace_output = TraceWriter(trace_path) if trace_path is not None else None

    async def run(self, runs: Iterable[RunSpec]) -> dict[str, int]:
        for spec in runs:
            if self.output.has_run(spec.run_id):
                self.output.assert_resume_compatible(
                    spec.run_id,
                    self.snapshot.fairness_identity(model=self.model, provider=self.provider),
                )
                if self.require_trace:
                    assert self.trace_output is not None
                    self.trace_output.assert_present(spec.run_id)
                continue
            await self._run_one(spec)
        self.output._rewrite_aggregation()
        return self.output.counts

    async def _run_one(self, spec: RunSpec) -> None:
        started = _utc_now()
        started_clock = time.perf_counter()
        fixture: FixtureHandle | None = None
        fault = self.injector.plan_for(spec.task)
        try:
            fixture = self.fixtures.prepare(spec.task)
            record: dict[str, Any] | None = None
            execution_error: BaseException | None = None
            try:
                request = ExecutionRequest(
                    run_id=spec.run_id,
                    repeat_index=spec.repeat_index,
                    task=spec.task,
                    config=spec.config,
                    fixture=fixture,
                    fault=fault,
                    fault_context=FaultContext(fault),
                )
                result = self.executor.execute(request)
                if inspect.isawaitable(result):
                    result = await result
                outcome = ExecutionOutcome.from_value(result)
                if outcome.wall_time_seconds == NOT_MEASURED:
                    outcome.wall_time_seconds = round(time.perf_counter() - started_clock, 6)
                validation = self.validator.validate(spec.task, fixture, outcome)
                finished = _utc_now()
                trace_ref: str | None = None
                trace_event_count: int | None = None
                runtime_source = outcome.runtime_source or str(
                    outcome.observed_state.get("runtime_source", "unknown")
                )
                trace = outcome.execution_trace or outcome.observed_state.get(
                    "execution_trace", []
                )
                if self.require_trace:
                    if self.trace_output is None:
                        raise TraceOutputError("TRACE_OUTPUT_REQUIRED")
                    trace_ref, trace_event_count = self.trace_output.append(
                        run_id=spec.run_id,
                        task_id=spec.task["task_id"],
                        config=spec.config["config_id"],
                        repeat=spec.repeat_index,
                        runtime_source=runtime_source,
                        model_identity=self.model,
                        protocol_hash=self.snapshot.protocol_hash,
                        events=trace,
                    )
                record = self._record(
                    spec,
                    fixture,
                    fault,
                    outcome,
                    validation,
                    started,
                    finished,
                    runtime_source=runtime_source,
                    execution_trace_ref=trace_ref,
                    trace_event_count=trace_event_count,
                )
            except Exception as exc:
                execution_error = exc
            try:
                await self.fixtures.reset(fixture)
            except Exception as exc:
                execution_error = execution_error or exc
            if execution_error is not None:
                raise execution_error
            assert record is not None
            validate_raw_result(record, self.snapshot.root / "schemas" / "result.schema.json")
            self.output.write_raw(record)
        except Exception as exc:
            finished = _utc_now()
            invalid = self._invalid_record(spec, fixture, fault, started, finished, exc)
            validate_raw_result(invalid, self.snapshot.root / "schemas" / "result.schema.json")
            self.output.write_invalid(invalid)

    def _record(
        self,
        spec: RunSpec,
        fixture: FixtureHandle,
        fault: FaultPlan,
        outcome: ExecutionOutcome,
        validation: ValidationOutcome,
        started: datetime,
        finished: datetime,
        *,
        runtime_source: str | None = None,
        execution_trace_ref: str | None = None,
        trace_event_count: int | None = None,
    ) -> dict[str, Any]:
        return {
            "benchmark_version": PROTOCOL_NAME,
            "benchmark_run_id": spec.run_id,
            "task_id": spec.task["task_id"],
            "family": spec.task["family"],
            "repeat_index": spec.repeat_index,
            "configuration": spec.config["config_id"],
            "repo_sha": self.repo_sha,
            "fixture_hash": fixture.fixture_hash,
            "manifest_hash": self.snapshot.manifest_hash,
            "protocol_hash": self.snapshot.protocol_hash,
            "model": self.model,
            "provider": self.provider,
            "runtime_environment": _runtime_environment(
                self.snapshot,
                model=self.model,
                provider=self.provider,
                runtime_source=runtime_source,
                execution_trace_ref=execution_trace_ref,
                trace_event_count=trace_event_count,
            ),
            "validator_id": self.snapshot.protocol["shared_validator_id"],
            "fault_id": fault.fault_id,
            "fault_type": fault.fault_type,
            "claimed_complete": bool(outcome.claimed_complete),
            "verified_completion": bool(validation.verified_completion),
            "false_completion": bool(outcome.claimed_complete and not validation.verified_completion),
            "failure_type": validation.failure_type,
            "recovery_required": bool(outcome.recovery_required),
            "recovery_attempted": bool(outcome.recovery_attempted),
            "recovery_success": bool(outcome.recovery_success),
            "repair_scope": outcome.repair_scope,
            "repair_attempts": int(outcome.repair_attempts),
            "replan_count": int(outcome.replan_count),
            "lost_work_units": outcome.lost_work_units,
            "duplicate_side_effect_count": int(outcome.duplicate_side_effect_count),
            "tool_calls": int(outcome.tool_calls),
            "model_calls": int(outcome.model_calls),
            "attempt_count": int(outcome.attempt_count),
            "tokens_input": outcome.tokens_input,
            "tokens_output": outcome.tokens_output,
            "total_tokens": outcome.total_tokens,
            "model_cost": outcome.model_cost,
            "tool_cost": outcome.tool_cost,
            "wall_time_seconds": outcome.wall_time_seconds,
            "human_intervention": bool(outcome.human_intervention),
            "validity": validation.validity,
            "invalid_reason": None,
            "started_at": _timestamp(started),
            "finished_at": _timestamp(finished),
        }

    def _invalid_record(
        self,
        spec: RunSpec,
        fixture: FixtureHandle | None,
        fault: FaultPlan,
        started: datetime,
        finished: datetime,
        exc: BaseException,
    ) -> dict[str, Any]:
        return {
            "benchmark_version": PROTOCOL_NAME,
            "benchmark_run_id": spec.run_id,
            "task_id": spec.task["task_id"],
            "family": spec.task["family"],
            "repeat_index": spec.repeat_index,
            "configuration": spec.config["config_id"],
            "repo_sha": self.repo_sha,
            "fixture_hash": fixture.fixture_hash if fixture else "UNAVAILABLE",
            "manifest_hash": self.snapshot.manifest_hash,
            "protocol_hash": self.snapshot.protocol_hash,
            "model": self.model,
            "provider": self.provider,
            "runtime_environment": _runtime_environment(self.snapshot, model=self.model, provider=self.provider),
            "validator_id": self.snapshot.protocol["shared_validator_id"],
            "fault_id": fault.fault_id,
            "fault_type": fault.fault_type,
            "claimed_complete": False,
            "verified_completion": False,
            "false_completion": False,
            "failure_type": None,
            "recovery_required": False,
            "recovery_attempted": False,
            "recovery_success": False,
            "repair_scope": None,
            "repair_attempts": 0,
            "replan_count": 0,
            "lost_work_units": NOT_MEASURED,
            "duplicate_side_effect_count": 0,
            "tool_calls": 0,
            "model_calls": 0,
            "attempt_count": 0,
            "tokens_input": NOT_MEASURED,
            "tokens_output": NOT_MEASURED,
            "total_tokens": NOT_MEASURED,
            "model_cost": NOT_MEASURED,
            "tool_cost": NOT_MEASURED,
            "wall_time_seconds": NOT_MEASURED,
            "human_intervention": False,
            "validity": "INVALID_RUN",
            "invalid_reason": json.dumps(_invalid_outcome(exc), sort_keys=True),
            "started_at": _timestamp(started),
            "finished_at": _timestamp(finished),
        }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the frozen phase4-v1 benchmark")
    parser.add_argument("--protocol", default=PROTOCOL_NAME, choices=[PROTOCOL_NAME])
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--headline", action="store_true")
    selection.add_argument("--ablation", action="store_true")
    selection.add_argument("--task")
    parser.add_argument("--config")
    parser.add_argument("--repeat", type=int)
    parser.add_argument("--root", type=Path, default=DEFAULT_PROTOCOL_ROOT)
    parser.add_argument("--output", type=Path, default=Path("artifacts/phase4/phase4-v1"))
    parser.add_argument("--executor", help="module:factory execution adapter")
    parser.add_argument("--model", default=os.environ.get("ODYS_BENCHMARK_MODEL", "FROZEN_BY_P4.2"))
    parser.add_argument("--provider", default=os.environ.get("ODYS_BENCHMARK_PROVIDER", "FROZEN_BY_P4.2"))
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    snapshot = ProtocolSnapshot.load(args.root)
    runs = select_runs(
        snapshot,
        headline=args.headline,
        ablation=args.ablation,
        task_id=args.task,
        config_name=args.config,
        repeat_index=args.repeat,
    )
    runner = Phase4Runner(
        snapshot,
        output_dir=args.output,
        executor=load_executor(args.executor),
        model=args.model,
        provider=args.provider,
        repo_root=args.repo_root,
    )
    counts = asyncio.run(runner.run(runs))
    print(json.dumps({"protocol": args.protocol, "planned": len(runs), **counts}, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
