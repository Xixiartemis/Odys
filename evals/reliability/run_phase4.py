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
IMMUTABLE_COLLISION_ERROR = "IMMUTABLE_RESULT_COLLISION"


class RunnerConfigurationError(RuntimeError):
    """Raised when an execution adapter is not configured safely."""


class RunSelectionError(ValueError):
    """Raised for a run selection that is outside the frozen protocol."""


class ResumeIntegrityError(RuntimeError):
    """Raised when an existing result is not compatible with this protocol."""


class RuntimeInfrastructureError(RuntimeError):
    """Raised when execution cannot produce trustworthy benchmark evidence."""


ROOT_API_BUDGET_FAILURE = "ROOT_API_BUDGET_EXHAUSTED"
WALL_TIME_BUDGET_FAILURE = "WALL_TIME_BUDGET_EXHAUSTED"
ATTEMPT_LOCAL_BUDGET_FAILURES = frozenset(
    {
        "TURN_BUDGET_EXHAUSTED",
        "TOOL_CALL_BUDGET_EXHAUSTED",
        "ATTEMPT_BUDGET_EXHAUSTED",
        WALL_TIME_BUDGET_FAILURE,
    }
)


class RunBudgetExhausted(RuntimeError):
    """Raised before a provider call would exceed the run-scoped budget."""

    def __init__(
        self,
        message: str = "RUN_API_BUDGET_EXHAUSTED",
        *,
        budget_type: str = ROOT_API_BUDGET_FAILURE,
    ) -> None:
        self.budget_type = budget_type
        super().__init__(message)


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
    # These identifiers are optional execution-layer evidence.  When a
    # recovery adapter creates a new durable attempt it can provide the
    # authoritative IDs; otherwise the runner records its deterministic
    # boundary IDs in runtime_environment.recovery.
    original_failure_attempt_id: str | None = None
    repair_attempt_id: str | None = None
    # Provider-call accounting is execution evidence, not a frozen metric
    # definition.  The official adapter fills these from every real provider
    # request, including calls made by a recovery attempt.
    provider_calls: int = 0
    provider_call_records: list[dict[str, Any]] = field(default_factory=list)
    # Explicit attempt accounting prevents a raw ``attempt_count`` from
    # hiding provider calls made by nested recovery machinery.
    root_attempt_count: int = 0
    nested_attempt_count: int = 0
    provider_attempt_count: int = 0
    budget_exhausted: bool = False
    # Typed budget provenance separates an attempt-local kernel boundary from
    # exhaustion of the root provider/API ledger.  ``budget_exhausted`` stays
    # a root-ledger terminal flag for backwards compatibility.
    budget_failure_type: str | None = None
    # Preserve the claim at both validator boundaries.  ``claimed_complete``
    # remains the final claim for backwards compatibility.
    initial_claimed_complete: bool | None = None
    final_claimed_complete: bool | None = None
    # External-observation evidence around repair.  These digests are based
    # on the same declared observation view supplied to the validator.
    expected_effect_ids: list[str] = field(default_factory=list)
    pre_repair_state_digest: str | None = None
    post_repair_state_digest: str | None = None
    validator_observed_state_digest: str | None = None
    state_changed_after_repair: bool | None = None
    validator_observed_repaired_state: bool | None = None
    recovery_action: str | None = None
    # Runtime/provider integrity failures are not product observations.  The
    # official runner uses this marker to route the case to invalid.jsonl
    # instead of letting an implementation exception become VALIDATED_FAIL.
    infrastructure_failure: bool = False
    infrastructure_error: str | None = None
    recovery_trace_authoritative: bool = False

    @classmethod
    def from_value(cls, value: "ExecutionOutcome | Mapping[str, Any]") -> "ExecutionOutcome":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("execution adapter must return ExecutionOutcome or mapping")
        allowed = {field_name for field_name in cls.__dataclass_fields__}
        return cls(**{key: value[key] for key in allowed if key in value})


def _validator_observation_view(
    outcome: ExecutionOutcome,
    task: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the declared external state that the shared validator reads.

    Runtime internals, traces, and provider metadata are intentionally not
    part of this view.  If a fixture supplies a concrete observation mapping,
    it is preferred; otherwise the outcome's declared keys are used.
    Missing expected keys are represented as ``None`` so a before/after
    comparison cannot mistake absence for unchanged evidence.
    """
    observed = outcome.observed_state if isinstance(outcome.observed_state, Mapping) else {}
    # Fixture observations are authoritative external state.  They are
    # merged into the same view the validator receives; only declared
    # expected-effect values are additionally accepted from the runtime
    # (for semantics such as a canonical repair scope).  This excludes
    # traces, provider metadata, counters, and attempt bookkeeping from the
    # state digest without reducing the validator's truth rules.
    source: dict[str, Any] = {}
    fixture_observed = observed.get("fixture_observations")
    if isinstance(fixture_observed, Mapping):
        source.update(
            {
                str(key): value
                for key, value in fixture_observed.items()
            }
        )
    expected = task.get("expected_observable_effects", {})
    if isinstance(expected, Mapping):
        for key in expected:
            if key in observed:
                source[str(key)] = observed[key]
            else:
                source.setdefault(str(key), None)
    if not isinstance(fixture_observed, Mapping) and not isinstance(expected, Mapping):
        source.update({str(key): value for key, value in observed.items()})
    return source


def _observation_digest(outcome: ExecutionOutcome, task: Mapping[str, Any]) -> str:
    """Hash only the validator-visible, externally observable state."""
    return _document_hash(_validator_observation_view(outcome, task))


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
    # ``SUCCESS`` means the validator ran successfully.  It does not mean
    # that the task was accepted.  Keeping these separate prevents the old
    # ambiguous VALIDATION_RESULT.result=pass representation.
    validator_execution_status: str = "SUCCESS"
    acceptance_status: str = ""

    def __post_init__(self) -> None:
        if not self.acceptance_status:
            object.__setattr__(
                self,
                "acceptance_status",
                "ACCEPTED" if self.verified_completion else "REJECTED",
            )


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
        observed = _validator_observation_view(outcome, task)
        verified = all(
            key in observed and _observed_matches(value, observed[key])
            for key, value in expected.items()
        )
        return ValidationOutcome(
            verified_completion=verified,
            validity="VALIDATED_PASS" if verified else "VALIDATED_FAIL",
            failure_type=outcome.failure_type,
            validator_execution_status="SUCCESS",
            acceptance_status="ACCEPTED" if verified else "REJECTED",
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
    benchmark_version: str | None = None,
    benchmark_config_hash: str | None = None,
    runtime_source: str | None = None,
    execution_trace_ref: str | None = None,
    trace_event_count: int | None = None,
    validation: Mapping[str, Any] | None = None,
    recovery: Mapping[str, Any] | None = None,
    execution_accounting: Mapping[str, Any] | None = None,
    state_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    identity = snapshot.fairness_identity(model=model, provider=provider)
    if benchmark_version is not None:
        identity["benchmark_version"] = benchmark_version
    if benchmark_config_hash is not None:
        identity["benchmark_config_hash"] = benchmark_config_hash
    environment: dict[str, Any] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "runner": "phase4-v1",
        "base_protocol_version": snapshot.protocol["benchmark_version"],
        "identity": identity,
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
    # The frozen raw-result schema intentionally leaves this object
    # extensible.  P410's semantic additions live here so the frozen Phase 4
    # inputs (and therefore protocol_hash) remain unchanged.
    if validation is not None:
        environment["validation"] = dict(validation)
    if recovery is not None:
        environment["recovery"] = dict(recovery)
    if execution_accounting is not None:
        environment["execution_accounting"] = dict(execution_accounting)
    if state_evidence is not None:
        environment["state_evidence"] = dict(state_evidence)
    return environment


def _invalid_outcome(exc: BaseException) -> dict[str, Any]:
    return {
        "reason": f"{type(exc).__name__}: {exc}",
        "exception_type": type(exc).__name__,
    }


def _validate_runner_result(
    result: dict[str, Any],
    schema_path: Path,
    *,
    benchmark_version: str,
) -> None:
    """Validate a result while keeping the frozen base schema unchanged.

    The committed schema describes the frozen ``phase4-v1`` protocol.  A
    provider/model profile is an execution-bundle identity layered over that
    protocol, so its version is allowed only when the result also records the
    frozen base version in ``runtime_environment.base_protocol_version``.
    The schema file itself is never rewritten or re-hashed.
    """
    if benchmark_version == PROTOCOL_NAME:
        validate_raw_result(result, schema_path)
        return
    environment = result.get("runtime_environment")
    if not isinstance(environment, Mapping) or environment.get("base_protocol_version") != PROTOCOL_NAME:
        raise ProtocolError("PROFILE_BASE_PROTOCOL_VERSION_MISSING")
    import jsonschema

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["properties"]["benchmark_version"] = {"const": benchmark_version}
    jsonschema.validate(result, schema)


class ResultWriter:
    """Append-only raw/invalid writer with derived aggregation input."""

    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.raw_path = self.output_dir / "raw.jsonl"
        self.invalid_path = self.output_dir / "invalid.jsonl"
        self.aggregation_path = self.output_dir / "aggregation-input.jsonl"
        # The artifact contract requires this sidecar even when no run is
        # invalid.  touch() is non-destructive for an existing append-only
        # file and makes empty bundles auditable.
        self.invalid_path.touch(exist_ok=True)
        self._repair_trailing_partial(self.raw_path)
        self._repair_trailing_partial(self.invalid_path)
        self._raw = self._load(self.raw_path)
        self._invalid = self._load(self.invalid_path)
        self._ids = {
            record["benchmark_run_id"]
            for record in (*self._raw, *self._invalid)
        }
        # A collision record intentionally shares the run ID of the
        # canonical raw result.  Keep raw authoritative when rebuilding the
        # resume index after reopening the output bundle.
        self._records = {
            record["benchmark_run_id"]: record for record in self._invalid
        }
        self._records.update(
            {record["benchmark_run_id"]: record for record in self._raw}
        )

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
        # An immutable collision is an infrastructure event about a run that
        # already has an immutable result.  It must be append-only evidence in
        # invalid.jsonl, but it must not replace the canonical raw record or
        # make the same run ID appear twice in the resume index.
        if record.get("error_type") == IMMUTABLE_COLLISION_ERROR:
            self._append(self.invalid_path, record)
            self._invalid.append(record)
            if run_id not in self._ids:
                self._ids.add(run_id)
                self._records[run_id] = record
            return
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


def _normalize_trace_timestamp(value: Any) -> str:
    """Normalize persisted trace timestamps to canonical UTC ``Z`` form."""
    if not isinstance(value, str):
        raise TraceOutputError("TRACE_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TraceOutputError("TRACE_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None:
        raise TraceOutputError("TRACE_TIMESTAMP_NOT_UTC")
    return _timestamp(parsed)


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
            normalized_event = dict(event)
            normalized_event["timestamp"] = _normalize_trace_timestamp(
                normalized_event["timestamp"]
            )
            normalized.append(normalized_event)
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
        benchmark_version: str = PROTOCOL_NAME,
        benchmark_config_hash: str | None = None,
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
            "benchmark_version": benchmark_version,
            "execution_trace": normalized,
            "trace_event_count": len(normalized),
        }
        if benchmark_config_hash is not None:
            record["benchmark_config_hash"] = benchmark_config_hash
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        self._line_count += 1
        reference = f"{self.path.name}#L{self._line_count}"
        self._records[run_id] = (reference, len(normalized))
        return reference, len(normalized)


def _trace_event(
    event_type: str,
    *,
    task_id: str,
    step_id: str = "root",
    attempt_id: str,
    status: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Create a trace-contract event at a runner-owned boundary."""
    event_metadata = dict(metadata or {})
    event_metadata.update(extra)
    return {
        "timestamp": _timestamp(_utc_now()),
        "event_type": event_type,
        "task_id": task_id,
        "step_id": step_id,
        "attempt_id": attempt_id,
        "status": status or event_type.casefold(),
        "metadata": event_metadata,
    }


def _validation_metadata(
    validation: ValidationOutcome,
    *,
    claimed_complete: bool,
    phase: str,
) -> dict[str, Any]:
    """Return unambiguous validator/external-acceptance trace metadata."""
    return {
        "phase": phase,
        "validator_execution_status": validation.validator_execution_status,
        "acceptance_status": validation.acceptance_status,
        "agent_claimed_complete": bool(claimed_complete),
        "failure_type": validation.failure_type,
    }


def _normalize_validation_trace(
    trace: Any,
    *,
    task_id: str,
    attempt_id: str,
    validation: ValidationOutcome,
    claimed_complete: bool,
    phase: str,
) -> list[dict[str, Any]]:
    """Normalize executor placeholders without changing observed events.

    Executors may emit a lifecycle trace before the shared validator runs.
    Any existing ``VALIDATION_RESULT`` placeholder is rewritten at this
    runner boundary; ``result=pass`` is deliberately removed because it was
    ambiguous between validator execution and task acceptance.
    """
    normalized: list[dict[str, Any]] = []
    found = False
    for raw_event in trace if isinstance(trace, list) else []:
        if not isinstance(raw_event, Mapping):
            continue
        event = dict(raw_event)
        metadata = dict(event.get("metadata") or {})
        if event.get("event_type") == "VALIDATION_RESULT":
            metadata.pop("result", None)
            metadata.update(
                _validation_metadata(
                    validation,
                    claimed_complete=claimed_complete,
                    phase=phase,
                )
            )
            event["metadata"] = metadata
            event["status"] = validation.acceptance_status.casefold()
            found = True
        normalized.append(event)
    if not found:
        normalized.append(
            _trace_event(
                "VALIDATION_RESULT",
                task_id=task_id,
                attempt_id=attempt_id,
                status=validation.acceptance_status.casefold(),
                metadata=_validation_metadata(
                    validation,
                    claimed_complete=claimed_complete,
                    phase=phase,
                ),
            )
        )
    if (
        validation.acceptance_status == "ACCEPTED"
        and not any(
            isinstance(event, Mapping)
            and event.get("event_type") == "VERIFICATION_PASSED"
            for event in normalized
        )
    ):
        normalized.append(
            _trace_event(
                "VERIFICATION_PASSED",
                task_id=task_id,
                attempt_id=attempt_id,
                status="passed",
                metadata=_validation_metadata(
                    validation,
                    claimed_complete=claimed_complete,
                    phase=phase,
                ),
            )
        )
    return normalized


def _recovery_enabled(config: Mapping[str, Any]) -> bool:
    features = config.get("features")
    if not isinstance(features, Mapping):
        return False
    return bool(
        features.get("durable_workflow_recovery")
        or features.get("selective_repair")
        or features.get("failure_provenance")
    )


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
        benchmark_version: str | None = None,
        benchmark_config_hash: str | None = None,
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
        # A profile version identifies the experiment bundle; the frozen
        # protocol version remains available as base_protocol_version in the
        # runtime environment and is never changed here.
        self.benchmark_version = benchmark_version or snapshot.protocol["benchmark_version"]
        self.benchmark_config_hash = benchmark_config_hash
        self.repo_root = Path(repo_root or Path.cwd()).resolve()
        self.repo_sha = _repo_sha(self.repo_root)
        self.require_trace = require_trace
        self.trace_output = TraceWriter(trace_path) if trace_path is not None else None
        configure_budget = getattr(self.executor, "configure_frozen_budget", None)
        if callable(configure_budget):
            configure_budget(self.snapshot.protocol.get("budgets", {}))

    def _experiment_identity(self) -> dict[str, str]:
        identity = self.snapshot.fairness_identity(
            model=self.model,
            provider=self.provider,
        )
        identity["benchmark_version"] = self.benchmark_version
        if self.benchmark_config_hash is not None:
            identity["benchmark_config_hash"] = self.benchmark_config_hash
        return identity

    async def run(self, runs: Iterable[RunSpec]) -> dict[str, int]:
        for spec in runs:
            if self.output.has_run(spec.run_id):
                self.output.assert_resume_compatible(
                    spec.run_id,
                    self._experiment_identity(),
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
        diagnostic_trace: list[dict[str, Any]] = []
        diagnostic_runtime_source: str | None = None
        diagnostic_validation: ValidationOutcome | None = None
        diagnostic_trace_status: str | None = None
        try:
            fixture = self.fixtures.prepare(spec.task)
            record: dict[str, Any] | None = None
            execution_error: BaseException | None = None
            request: ExecutionRequest | None = None
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
                if outcome.infrastructure_failure:
                    raise RuntimeInfrastructureError(
                        outcome.infrastructure_error
                        or outcome.failure_type
                        or "RUNTIME_INFRASTRUCTURE_FAILURE"
                    )
                if outcome.wall_time_seconds == NOT_MEASURED:
                    outcome.wall_time_seconds = round(time.perf_counter() - started_clock, 6)
                if outcome.initial_claimed_complete is None:
                    outcome.initial_claimed_complete = bool(outcome.claimed_complete)
                outcome.expected_effect_ids = [
                    str(key)
                    for key in (spec.task.get("expected_observable_effects", {}) or {})
                ]
                # This is the first (initial) validator observation.  A
                # rejection is data, not an executor exception.
                initial_validation = self.validator.validate(spec.task, fixture, outcome)
                validation = initial_validation
                finished = _utc_now()
                trace_ref: str | None = None
                trace_event_count: int | None = None
                runtime_source = outcome.runtime_source or str(
                    outcome.observed_state.get("runtime_source", "unknown")
                )
                trace = outcome.execution_trace or outcome.observed_state.get(
                    "execution_trace", []
                )
                initial_attempt_id = str(
                    outcome.original_failure_attempt_id
                    or self._first_attempt_id(trace)
                    or f"{spec.run_id}::attempt-{spec.repeat_index}"
                )
                trace = _normalize_validation_trace(
                    trace,
                    task_id=spec.task["task_id"],
                    attempt_id=initial_attempt_id,
                    validation=initial_validation,
                    claimed_complete=outcome.claimed_complete,
                    phase="initial",
                )
                diagnostic_runtime_source = runtime_source
                diagnostic_validation = initial_validation
                # This is the observed rejection boundary at which recovery
                # is requested.  Recording it before invoking recovery keeps
                # an infrastructure-failed run diagnostically useful without
                # inventing any post-failure lifecycle events.
                if (
                    initial_validation.acceptance_status == "REJECTED"
                    and _recovery_enabled(spec.config)
                ):
                    trace.append(
                        _trace_event(
                            "FAILURE_DETECTED",
                            task_id=spec.task["task_id"],
                            attempt_id=initial_attempt_id,
                            status="detected",
                            failure_type=initial_validation.failure_type
                            or "VALIDATOR_REJECTION",
                        )
                    )
                diagnostic_trace = list(trace)

                # Recovery is an explicit executor capability.  The runner
                # owns the validator boundary and orchestration events; the
                # adapter owns the actual repair/reverification mechanics.
                false_completion_detected = bool(
                    outcome.claimed_complete
                    and initial_validation.acceptance_status == "REJECTED"
                )
                recovery = await self._recover_after_rejection(
                    spec,
                    fixture,
                    request,
                    outcome,
                    initial_validation,
                    trace,
                    initial_attempt_id=initial_attempt_id,
                )
                if recovery is not None:
                    outcome, validation, trace = recovery
                    runtime_source = outcome.runtime_source or runtime_source
                else:
                    outcome.final_claimed_complete = bool(outcome.claimed_complete)
                    outcome.validator_observed_state_digest = _observation_digest(
                        outcome, spec.task
                    )
                finished = _utc_now()
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
                        benchmark_version=self.benchmark_version,
                        benchmark_config_hash=self.benchmark_config_hash,
                        events=trace,
                    )
                record = self._record(
                    spec,
                    fixture,
                    fault,
                    outcome,
                    validation,
                    initial_validation=initial_validation,
                    false_completion_detected=false_completion_detected,
                    initial_attempt_id=initial_attempt_id,
                    started=started,
                    finished=finished,
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
            finally:
                cleanup = getattr(self.executor, "cleanup", None)
                if callable(cleanup) and request is not None:
                    try:
                        cleanup(request)
                    except Exception as exc:
                        execution_error = execution_error or exc
            if execution_error is not None:
                raise execution_error
            assert record is not None
            _validate_runner_result(
                record,
                self.snapshot.root / "schemas" / "result.schema.json",
                benchmark_version=self.benchmark_version,
            )
            self.output.write_raw(record)
        except Exception as exc:
            if "immutable result collision" in str(exc).lower():
                # Do not pass this through _invalid_record: that is a normal
                # phase4 result and cannot coexist with the already-written
                # immutable record under the same run ID.  Collision evidence
                # intentionally uses the small infra-event schema instead.
                self.output.write_invalid(
                    {
                        "benchmark_run_id": spec.run_id,
                        "run_id": spec.run_id,
                        "status": "INFRA_FAILURE",
                        "error_type": IMMUTABLE_COLLISION_ERROR,
                    }
                )
                return
            diagnostic_trace_ref: str | None = None
            diagnostic_trace_event_count: int | None = None
            if self.require_trace and self.trace_output is not None and diagnostic_trace:
                try:
                    diagnostic_trace_ref, diagnostic_trace_event_count = self.trace_output.append(
                        run_id=spec.run_id,
                        task_id=spec.task["task_id"],
                        config=spec.config["config_id"],
                        repeat=spec.repeat_index,
                        runtime_source=diagnostic_runtime_source or "unknown",
                        model_identity=self.model,
                        protocol_hash=self.snapshot.protocol_hash,
                        benchmark_version=self.benchmark_version,
                        benchmark_config_hash=self.benchmark_config_hash,
                        events=diagnostic_trace,
                    )
                    diagnostic_trace_status = "PARTIAL_DIAGNOSTIC_TRACE"
                except Exception as trace_exc:
                    diagnostic_trace_status = (
                        f"TRACE_PERSISTENCE_FAILED:{type(trace_exc).__name__}"
                    )
            elif self.require_trace:
                diagnostic_trace_status = "UNAVAILABLE_BEFORE_TRACE_INITIALIZATION"
            finished = _utc_now()
            invalid = self._invalid_record(
                spec,
                fixture,
                fault,
                started,
                finished,
                exc,
                benchmark_version=self.benchmark_version,
                benchmark_config_hash=self.benchmark_config_hash,
                runtime_source=diagnostic_runtime_source,
                execution_trace_ref=diagnostic_trace_ref,
                trace_event_count=diagnostic_trace_event_count,
                validation=diagnostic_validation,
                diagnostic_trace_status=diagnostic_trace_status,
            )
            _validate_runner_result(
                invalid,
                self.snapshot.root / "schemas" / "result.schema.json",
                benchmark_version=self.benchmark_version,
            )
            self.output.write_invalid(invalid)

    @staticmethod
    def _first_attempt_id(trace: Any) -> str | None:
        if not isinstance(trace, list):
            return None
        for event in trace:
            if isinstance(event, Mapping) and event.get("attempt_id"):
                return str(event["attempt_id"])
        return None

    async def _recover_after_rejection(
        self,
        spec: RunSpec,
        fixture: FixtureHandle,
        request: ExecutionRequest,
        outcome: ExecutionOutcome,
        initial_validation: ValidationOutcome,
        trace: list[dict[str, Any]],
        *,
        initial_attempt_id: str,
    ) -> tuple[ExecutionOutcome, ValidationOutcome, list[dict[str, Any]]] | None:
        """Run an adapter-owned repair/reverification after rejection.

        The optional hook is deliberately narrow.  It is not a second
        validator and it cannot alter frozen task truth; it only returns the
        observations from the existing runtime recovery path.  If no hook is
        provided, the original rejection remains a valid benchmark result.
        """
        if initial_validation.acceptance_status != "REJECTED":
            return None
        if not _recovery_enabled(spec.config):
            return None
        failure_type = str(
            outcome.budget_failure_type or outcome.failure_type or ""
        ).upper()
        # A root-ledger exhaustion is terminal.  The generic value is kept as
        # a compatibility boundary for older adapters; new official runtime
        # outcomes use ROOT_API_BUDGET_FAILURE explicitly.
        if (
            outcome.budget_exhausted
            or failure_type in {ROOT_API_BUDGET_FAILURE, "BUDGET_EXHAUSTED"}
        ):
            outcome.budget_failure_type = ROOT_API_BUDGET_FAILURE
            outcome.failure_type = ROOT_API_BUDGET_FAILURE
            outcome.budget_exhausted = True
            outcome.recovery_required = False
            outcome.recovery_attempted = False
            return None
        recover = getattr(self.executor, "recover_after_validation", None)
        if not callable(recover):
            return None

        # Attempt-local exhaustion is recoverable only when the executor can
        # prove that the same root ledger still has capacity and one bounded
        # repair attempt remains.  No new ledger is created here.
        if failure_type in ATTEMPT_LOCAL_BUDGET_FAILURES:
            can_attempt = getattr(self.executor, "can_attempt_recovery", None)
            if callable(can_attempt):
                allowed = can_attempt(spec.run_id)
                if inspect.isawaitable(allowed):
                    allowed = await allowed
                if not allowed:
                    snapshot = getattr(self.executor, "run_budget_snapshot", None)
                    budget = snapshot(spec.run_id) if callable(snapshot) else None
                    if inspect.isawaitable(budget):
                        budget = await budget
                    if isinstance(budget, Mapping) and (
                        bool(budget.get("exhausted"))
                        or (
                            isinstance(budget.get("max_provider_calls"), int)
                            and int(budget.get("provider_calls", 0))
                            >= int(budget["max_provider_calls"])
                        )
                    ):
                        outcome.budget_failure_type = ROOT_API_BUDGET_FAILURE
                        outcome.failure_type = ROOT_API_BUDGET_FAILURE
                        outcome.budget_exhausted = True
                    outcome.recovery_required = False
                    outcome.recovery_attempted = False
                    return None
            outcome.recovery_required = True

        outcome.initial_claimed_complete = bool(
            outcome.initial_claimed_complete
            if outcome.initial_claimed_complete is not None
            else outcome.claimed_complete
        )
        outcome.expected_effect_ids = [
            str(key)
            for key in (spec.task.get("expected_observable_effects", {}) or {})
        ]
        outcome.pre_repair_state_digest = _observation_digest(outcome, spec.task)

        recovery_result = recover(request, outcome, initial_validation)
        if inspect.isawaitable(recovery_result):
            recovery_result = await recovery_result
        if recovery_result is None:
            return None
        repaired = ExecutionOutcome.from_value(recovery_result)
        if not repaired.provider_call_records and outcome.provider_call_records:
            repaired.provider_call_records = [
                dict(item) for item in outcome.provider_call_records
            ]
            repaired.provider_calls = outcome.provider_calls
        if repaired.provider_call_records:
            # P45's provider slice already contains initial + recovery calls.
            repaired.model_calls = max(
                int(repaired.model_calls), int(outcome.model_calls)
            )
        else:
            repaired.model_calls = int(outcome.model_calls) + int(repaired.model_calls)
        repaired.tool_calls = int(outcome.tool_calls) + int(repaired.tool_calls)

        repair_scope = str(repaired.repair_scope or "")
        is_macro_replan = repair_scope.upper() == "MACRO_REPLAN"
        repair_attempt_value = (
            repaired.repair_attempt_id
            or repaired.observed_state.get("repair_attempt_id")
        )
        repair_attempt_id = (
            str(repair_attempt_value)
            if repair_attempt_value
            else (None if is_macro_replan else f"{spec.run_id}::repair-attempt-1")
        )
        repaired.original_failure_attempt_id = str(
            repaired.original_failure_attempt_id
            or repaired.observed_state.get("original_failure_attempt_id", "")
            or initial_attempt_id
        )
        repaired.repair_attempt_id = repair_attempt_id
        repaired.recovery_required = True
        repaired.recovery_attempted = True
        repaired.repair_attempts = (
            0 if is_macro_replan else max(1, int(repaired.repair_attempts))
        )
        repaired.attempt_count = (
            int(outcome.attempt_count) + int(repaired.attempt_count)
            if is_macro_replan is False
            else int(outcome.attempt_count)
        )
        repaired.runtime_source = repaired.runtime_source or outcome.runtime_source
        repaired.initial_claimed_complete = outcome.initial_claimed_complete
        repaired.budget_failure_type = (
            repaired.budget_failure_type or outcome.budget_failure_type
        )
        repaired.expected_effect_ids = list(outcome.expected_effect_ids)
        repaired.pre_repair_state_digest = outcome.pre_repair_state_digest
        repaired.root_attempt_count = max(
            int(repaired.root_attempt_count), int(outcome.root_attempt_count), 1
        )
        if not is_macro_replan:
            repaired.nested_attempt_count = max(
                int(repaired.nested_attempt_count),
                int(outcome.nested_attempt_count),
                1,
            )
        repaired.provider_attempt_count = max(
            int(repaired.provider_attempt_count),
            int(outcome.provider_attempt_count),
        )

        repair_trace = repaired.execution_trace or repaired.observed_state.get(
            "execution_trace", []
        )
        merged_trace = list(trace)
        if not any(
            isinstance(event, Mapping)
            and event.get("event_type") == "FAILURE_DETECTED"
            for event in merged_trace
        ):
            merged_trace.append(
                _trace_event(
                    "FAILURE_DETECTED",
                    task_id=spec.task["task_id"],
                    attempt_id=initial_attempt_id,
                    status="detected",
                    failure_type=initial_validation.failure_type
                    or "VALIDATOR_REJECTION",
                )
            )
        repair_events = [
            dict(event)
            for event in repair_trace
            if isinstance(event, Mapping)
        ] if isinstance(repair_trace, list) else []
        repair_event_types = {event.get("event_type") for event in repair_events}
        if repaired.recovery_trace_authoritative:
            required_recovery_events = {"StepFailureProvenance"}
            if not is_macro_replan:
                required_recovery_events.update({"REPAIR_STARTED", "REPAIR_COMPLETED"})
            if not required_recovery_events.issubset(repair_event_types):
                raise RuntimeInfrastructureError("RECOVERY_TRACE_NOT_AUTHORITATIVE")
        if "StepFailureProvenance" not in repair_event_types:
            merged_trace.append(
                _trace_event(
                    "StepFailureProvenance",
                    task_id=spec.task["task_id"],
                    attempt_id=initial_attempt_id,
                    status="recorded",
                    original_failure_attempt_id=initial_attempt_id,
                    repair_attempt_id=repair_attempt_id,
                    failure_type=initial_validation.failure_type or "VALIDATOR_REJECTION",
                )
            )
        repair_event_attempt_id = repair_attempt_id or initial_attempt_id
        if not is_macro_replan and "REPAIR_STARTED" not in repair_event_types:
            merged_trace.append(
                _trace_event(
                    "REPAIR_STARTED",
                    task_id=spec.task["task_id"],
                    attempt_id=repair_event_attempt_id,
                    status="started",
                    original_failure_attempt_id=initial_attempt_id,
                    repair_attempt_id=repair_attempt_id,
                    repair_scope=repaired.repair_scope,
                )
            )
        merged_trace.extend(repair_events)
        if not is_macro_replan and "REPAIR_COMPLETED" not in repair_event_types:
            merged_trace.append(
                _trace_event(
                    "REPAIR_COMPLETED",
                    task_id=spec.task["task_id"],
                    attempt_id=repair_event_attempt_id,
                    status="completed",
                    original_failure_attempt_id=initial_attempt_id,
                    repair_attempt_id=repair_attempt_id,
                )
            )

        final_validation = self.validator.validate(spec.task, fixture, repaired)
        # Keep the initial rejection in the trace and append a separate,
        # explicitly labelled revalidation result.
        merged_trace.append(
            _trace_event(
                "VALIDATION_RESULT",
                task_id=spec.task["task_id"],
                attempt_id=repair_event_attempt_id,
                status=final_validation.acceptance_status.casefold(),
                metadata=_validation_metadata(
                    final_validation,
                    claimed_complete=repaired.claimed_complete,
                    phase="revalidation",
                ),
            )
        )
        repaired.final_claimed_complete = bool(repaired.claimed_complete)
        repaired.post_repair_state_digest = _observation_digest(repaired, spec.task)
        repaired.validator_observed_state_digest = (
            repaired.post_repair_state_digest
            if final_validation.validator_execution_status == "SUCCESS"
            else None
        )
        repaired.state_changed_after_repair = (
            repaired.pre_repair_state_digest
            != repaired.post_repair_state_digest
        )
        repaired.validator_observed_repaired_state = bool(
            repaired.validator_observed_state_digest
            and final_validation.validator_execution_status == "SUCCESS"
        )
        validation_event = merged_trace[-1]
        validation_event["metadata"].update(
            {
                "initial_agent_claimed_complete": bool(
                    repaired.initial_claimed_complete
                ),
                "final_agent_claimed_complete": bool(
                    repaired.final_claimed_complete
                ),
                "pre_repair_state_digest": repaired.pre_repair_state_digest,
                "post_repair_state_digest": repaired.post_repair_state_digest,
                "validator_observed_state_digest": repaired.validator_observed_state_digest,
                "state_changed_after_repair": repaired.state_changed_after_repair,
                "validator_observed_repaired_state": repaired.validator_observed_repaired_state,
            }
        )
        if final_validation.acceptance_status == "ACCEPTED":
            merged_trace.append(
                _trace_event(
                    "STEP_VERIFIED",
                    task_id=spec.task["task_id"],
                    attempt_id=repair_event_attempt_id,
                    status="verified",
                    original_failure_attempt_id=initial_attempt_id,
                    repair_attempt_id=repair_attempt_id,
                )
            )
            merged_trace.append(
                _trace_event(
                    "VERIFICATION_PASSED",
                    task_id=spec.task["task_id"],
                    attempt_id=repair_event_attempt_id,
                    status="passed",
                    original_failure_attempt_id=initial_attempt_id,
                    repair_attempt_id=repair_attempt_id,
                )
            )
            repaired.recovery_success = True
        else:
            merged_trace.append(
                _trace_event(
                    "VERIFICATION_FAILED",
                    task_id=spec.task["task_id"],
                    attempt_id=repair_event_attempt_id,
                    status="failed",
                    original_failure_attempt_id=initial_attempt_id,
                    repair_attempt_id=repair_attempt_id,
                )
            )
            repaired.recovery_success = False
        repaired.execution_trace = merged_trace
        repaired.observed_state["execution_trace"] = merged_trace
        repaired.observed_state["original_failure_attempt_id"] = initial_attempt_id
        if repair_attempt_id is not None:
            repaired.observed_state["repair_attempt_id"] = repair_attempt_id
        return repaired, final_validation, merged_trace

    def _record(
        self,
        spec: RunSpec,
        fixture: FixtureHandle,
        fault: FaultPlan,
        outcome: ExecutionOutcome,
        validation: ValidationOutcome,
        *,
        initial_validation: ValidationOutcome | None = None,
        false_completion_detected: bool | None = None,
        initial_attempt_id: str | None = None,
        started: datetime,
        finished: datetime,
        runtime_source: str | None = None,
        execution_trace_ref: str | None = None,
        trace_event_count: int | None = None,
        benchmark_version: str | None = None,
        benchmark_config_hash: str | None = None,
    ) -> dict[str, Any]:
        initial_validation = initial_validation or validation
        if false_completion_detected is None:
            false_completion_detected = bool(
                outcome.claimed_complete
                and initial_validation.acceptance_status == "REJECTED"
            )
        initial_attempt_id = initial_attempt_id or outcome.original_failure_attempt_id
        recovery_candidate = bool(outcome.recovery_required)
        recovery_required_after_validation = bool(
            initial_validation.acceptance_status == "REJECTED"
            and (recovery_candidate or outcome.recovery_attempted)
        )
        recovery_identity = {
            # The runtime may detect a potentially recoverable execution
            # failure before the shared external validator runs.  Keep that
            # signal, but do not call it a required recovery when validation
            # already accepted the observable state (for example ESR-04).
            "recovery_candidate": recovery_candidate,
            "recovery_required_after_validation": recovery_required_after_validation,
            "recovery_required": recovery_required_after_validation,
            "recovery_attempted": bool(outcome.recovery_attempted),
            "recovery_success": bool(outcome.recovery_success),
            "repair_scope": outcome.repair_scope,
            "repair_attempts": int(outcome.repair_attempts),
            "original_failure_attempt_id": outcome.original_failure_attempt_id or initial_attempt_id,
            "repair_attempt_id": outcome.repair_attempt_id,
        }
        validation_identity = {
            # acceptance_status is the initial validator decision.  This
            # preserves a detected false-completion rejection even when a
            # subsequent recovery is accepted.
            "validator_execution_status": initial_validation.validator_execution_status,
            "acceptance_status": initial_validation.acceptance_status,
            "final_validator_execution_status": validation.validator_execution_status,
            "final_acceptance_status": validation.acceptance_status,
            "false_completion_detected": bool(false_completion_detected),
            "initial_agent_claimed_complete": bool(
                outcome.initial_claimed_complete
                if outcome.initial_claimed_complete is not None
                else outcome.claimed_complete
            ),
            "final_agent_claimed_complete": bool(
                outcome.final_claimed_complete
                if outcome.final_claimed_complete is not None
                else outcome.claimed_complete
            ),
            # Backwards-compatible alias for consumers of the P410 schema.
            "agent_claimed_complete": bool(outcome.claimed_complete),
        }
        accounting = {
            "provider_calls": int(outcome.provider_calls),
            "model_calls": int(outcome.model_calls),
            "root_attempt_count": int(outcome.root_attempt_count),
            "nested_attempt_count": int(outcome.nested_attempt_count),
            "provider_attempt_count": int(outcome.provider_attempt_count),
            "budget_exhausted": bool(outcome.budget_exhausted),
            "budget_failure_type": outcome.budget_failure_type,
            "provider_call_records": [
                dict(item) for item in outcome.provider_call_records
            ],
            "tokens_input": outcome.tokens_input,
            "tokens_output": outcome.tokens_output,
            "total_tokens": outcome.total_tokens,
        }
        state_evidence = {
            "expected_effect_ids": list(outcome.expected_effect_ids),
            "pre_repair_state_digest": outcome.pre_repair_state_digest,
            "post_repair_state_digest": outcome.post_repair_state_digest,
            "validator_observed_state_digest": outcome.validator_observed_state_digest,
            "state_changed_after_repair": outcome.state_changed_after_repair,
            "validator_observed_repaired_state": outcome.validator_observed_repaired_state,
        }
        recovery_identity["recovery_action"] = outcome.recovery_action
        return {
            "benchmark_version": benchmark_version or self.benchmark_version,
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
                benchmark_version=benchmark_version or self.benchmark_version,
                benchmark_config_hash=benchmark_config_hash
                if benchmark_config_hash is not None
                else self.benchmark_config_hash,
                runtime_source=runtime_source,
                execution_trace_ref=execution_trace_ref,
                trace_event_count=trace_event_count,
                validation=validation_identity,
                recovery=recovery_identity,
                execution_accounting=accounting,
                state_evidence=state_evidence,
            ),
            "validator_id": self.snapshot.protocol["shared_validator_id"],
            "fault_id": fault.fault_id,
            "fault_type": fault.fault_type,
            "claimed_complete": bool(outcome.claimed_complete),
            "verified_completion": bool(validation.verified_completion),
            # Preserve the initial claim/rejection signal even if a later
            # recovery reaches verified completion.  The new explicit
            # false_completion_detected metric is derived from the same
            # boundary evidence.
            "false_completion": bool(false_completion_detected),
            "failure_type": validation.failure_type,
            "recovery_required": recovery_required_after_validation,
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
        *,
        benchmark_version: str | None = None,
        benchmark_config_hash: str | None = None,
        runtime_source: str | None = None,
        execution_trace_ref: str | None = None,
        trace_event_count: int | None = None,
        validation: ValidationOutcome | None = None,
        diagnostic_trace_status: str | None = None,
    ) -> dict[str, Any]:
        validation_identity: dict[str, Any] = {
            "validator_execution_status": "NOT_EXECUTED",
            "acceptance_status": "NOT_EVALUATED",
            "final_validator_execution_status": "NOT_EXECUTED",
            "final_acceptance_status": "NOT_EVALUATED",
            "false_completion_detected": False,
            "agent_claimed_complete": False,
        }
        if validation is not None:
            validation_identity.update(
                {
                    "validator_execution_status": validation.validator_execution_status,
                    "acceptance_status": validation.acceptance_status,
                    "false_completion_detected": False,
                }
            )
        recovery_identity = {
            "recovery_required": False,
            "recovery_attempted": False,
            "recovery_success": False,
            "repair_scope": None,
            "repair_attempts": 0,
            "original_failure_attempt_id": None,
            "repair_attempt_id": None,
        }
        runtime_environment = _runtime_environment(
            self.snapshot,
            model=self.model,
            provider=self.provider,
            benchmark_version=benchmark_version or self.benchmark_version,
            benchmark_config_hash=benchmark_config_hash
            if benchmark_config_hash is not None
            else self.benchmark_config_hash,
            runtime_source=runtime_source,
            execution_trace_ref=execution_trace_ref,
            trace_event_count=trace_event_count,
            validation=validation_identity,
            recovery=recovery_identity,
        )
        if diagnostic_trace_status is not None:
            runtime_environment["diagnostic_trace_status"] = diagnostic_trace_status
        return {
            "benchmark_version": benchmark_version or self.benchmark_version,
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
            "runtime_environment": runtime_environment,
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
