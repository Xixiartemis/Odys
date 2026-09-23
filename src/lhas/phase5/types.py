"""Phase 5 core types — enums, models, protocols.

All types are Pydantic models for validation.  This module must not
import any Phase 4 core module at runtime (import-time type references
to domain.enums are acceptable since they are frozen).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


# ── Enums ────────────────────────────────────────────────────────────

class BenchmarkName(str, Enum):
    TOOLMAZE = "toolmaze"
    TOOLSANDBOX = "toolsandbox"
    TERMINAL_BENCH = "terminal_bench"
    TUA_BENCH = "tua_bench"


class TopologyClass(str, Enum):
    """ToolMaze topology classes C1–C4."""
    C1 = "C1"
    C2 = "C2"
    C3 = "C3"
    C4 = "C4"


class PerturbationMode(str, Enum):
    """Benchmark-native perturbation modes."""
    P0 = "P0"   # no perturbation (NP)
    P1 = "P1"   # explicit-transient
    P2 = "P2"   # explicit-permanent
    P3 = "P3"   # implicit-transient
    P4 = "P4"   # implicit-permanent


class ControlArm(str, Enum):
    """Six-arm ablation study arms."""
    A0_BARE = "A0_BARE"
    A1_RETRY_ONLY = "A1_RETRY_ONLY"
    A2_VALIDATOR_ONLY = "A2_VALIDATOR_ONLY"
    A3_ODYS_FULL = "A3_ODYS_FULL"
    A4_ODYS_MINUS_OBSERVABLE_PROGRESS = "A4_ODYS_MINUS_OBSERVABLE_PROGRESS"
    A5_ODYS_MINUS_RECOVERY_BUDGET_POLICY = "A5_ODYS_MINUS_RECOVERY_BUDGET_POLICY"


class FaultSource(str, Enum):
    """Origin of fault semantics."""
    NONE = "NONE"
    BENCHMARK_NATIVE = "BENCHMARK_NATIVE"
    DERIVED_WRAPPER = "DERIVED_WRAPPER"


class TrialStatus(str, Enum):
    """Classification of a completed trial."""
    VALID = "VALID"
    INVALID_INFRA = "INVALID_INFRA"
    EXCLUDED = "EXCLUDED"


class SignalKind(str, Enum):
    """Shadow observer signal types."""
    PROGRESSING = "PROGRESSING"
    STALLED = "STALLED"
    REGRESSING = "REGRESSING"
    ANOMALY = "ANOMALY"
    NO_OBSERVATION = "NO_OBSERVATION"


# ── Models ───────────────────────────────────────────────────────────

class BenchmarkIdentity(BaseModel):
    """Frozen provenance for a benchmark run."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    benchmark_name: BenchmarkName
    benchmark_revision: str
    repository_url: str
    commit_sha: str
    dataset_digest: str
    evaluator_digest: str


class TaskDescriptor(BaseModel):
    """A single benchmark task — identity + metadata only."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    benchmark: BenchmarkName
    topology: Optional[TopologyClass] = None
    complexity: Optional[str] = None
    perturbation_mode: PerturbationMode = PerturbationMode.P0
    perturbation_victim: Optional[str] = None
    native_metadata: dict[str, Any] = Field(default_factory=dict)


class BudgetConfig(BaseModel):
    """Root execution budget shared across arms."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_turns: int = Field(ge=1)
    max_model_calls: int = Field(ge=1)
    token_budget: Optional[int] = None
    deadline_seconds: Optional[float] = None


class RuntimeTask(BaseModel):
    """Runtime-visible task envelope.  No hidden state."""
    model_config = ConfigDict(extra="forbid")

    task_id: str
    objective: str
    visible_tools: list[dict[str, Any]] = Field(default_factory=list)
    prompt: str = ""
    constraints: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    environment_snapshot: dict[str, Any] = Field(default_factory=dict)
    budget: BudgetConfig = Field(default_factory=lambda: BudgetConfig(max_turns=30, max_model_calls=50))


class GenerationConfig(BaseModel):
    """Model generation parameters — frozen per trial."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: str
    provider: str
    temperature: float = 0.0
    top_p: float = 1.0
    max_output_tokens: Optional[int] = None
    seed: Optional[int] = None


class TrialManifest(BaseModel):
    """Immutable identity of a single trial."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "phase5-falsification-01"
    experiment_id: str
    trial_id: str
    benchmark_name: BenchmarkName
    benchmark_revision: str
    dataset_digest: str
    task_id: str
    native_condition: str
    complexity: Optional[str] = None
    perturbation_mode: PerturbationMode
    arm: ControlArm
    model_id: str
    provider: str
    generation_config: GenerationConfig
    root_budget: BudgetConfig
    token_budget: Optional[int] = None
    deadline: Optional[float] = None
    fault_source: FaultSource = FaultSource.BENCHMARK_NATIVE


class NativeResult(BaseModel):
    """Benchmark-native scoring result — never modified by Odys."""
    model_config = ConfigDict(extra="forbid")

    tsr: Optional[float] = None       # Task Success Rate
    prr: Optional[float] = None       # Perturbation Recovery Rate
    rc: Optional[float] = None        # Recovery Cost
    raw_score: Optional[float] = None
    native_metrics: dict[str, Any] = Field(default_factory=dict)
    judge_output: Optional[dict[str, Any]] = None


class DerivedMetrics(BaseModel):
    """Phase5-derived metrics — always labeled as derived, never native."""
    model_config = ConfigDict(extra="forbid")

    recovery_metrics: dict[str, Any] = Field(default_factory=dict)
    progress_labels: dict[str, Any] = Field(default_factory=dict)
    milestone_alignment: Optional[float] = None
    no_advancement_detection: Optional[float] = None
    detection_latency: Optional[float] = None
    missed_stall_episodes: Optional[int] = None
    premature_intervention_candidates: Optional[int] = None
    validity: TrialStatus = TrialStatus.VALID


class ShadowRecord(BaseModel):
    """One line in progress_shadow.jsonl."""
    model_config = ConfigDict(extra="forbid")

    trial_id: str
    step: int
    observable_features: dict[str, Any] = Field(default_factory=dict)
    signal: SignalKind
    signal_reason: str
    confidence: Optional[float] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AuditReport(BaseModel):
    """Firewall / pairing / accounting audit."""
    model_config = ConfigDict(extra="forbid")

    firewall: dict[str, str] = Field(default_factory=dict)
    pairing: dict[str, Any] = Field(default_factory=dict)
    accounting: dict[str, Any] = Field(default_factory=dict)
    exclusions: list[dict[str, Any]] = Field(default_factory=list)


# ── Protocols ────────────────────────────────────────────────────────

@runtime_checkable
class BenchmarkAdapter(Protocol):
    """Common adapter interface for all benchmarks."""

    @property
    def benchmark_identity(self) -> BenchmarkIdentity: ...

    def enumerate_tasks(self) -> Sequence[TaskDescriptor]: ...

    def build_runtime_task(self, descriptor: TaskDescriptor) -> RuntimeTask: ...

    async def reset_environment(self, task: RuntimeTask) -> None: ...

    def native_condition(self, descriptor: TaskDescriptor) -> str: ...

    def collect_public_observation(self, task_id: str, step: int) -> dict[str, Any]: ...

    def finalize_runtime_artifact(self, task_id: str) -> dict[str, Any]: ...

    def offline_native_evaluate(
        self,
        task_id: str,
        runtime_artifact: dict[str, Any],
    ) -> NativeResult: ...


@runtime_checkable
class ProgressObserver(Protocol):
    """Shadow observer — read-only, no execution influence."""

    def observe(
        self,
        *,
        task_id: str,
        step: int,
        action_identity: str,
        tool_result: dict[str, Any],
        environment_observation: Optional[dict[str, Any]] = None,
    ) -> ShadowRecord: ...


@runtime_checkable
class ControlPolicy(Protocol):
    """Arm-specific policy that wraps a benchmark run."""

    @property
    def arm(self) -> ControlArm: ...

    async def execute_trial(
        self,
        *,
        task: RuntimeTask,
        adapter: BenchmarkAdapter,
        generation_config: GenerationConfig,
    ) -> dict[str, Any]: ...
