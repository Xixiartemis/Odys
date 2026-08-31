"""Harness-neutral reliability benchmark contracts."""

from __future__ import annotations

from enum import Enum
from typing import Any, Awaitable, Callable, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BenchmarkFamily(str, Enum):
    EXECUTION_STATE_RECOVERY = "execution_state_recovery"
    COMPLETION_INTEGRITY = "completion_integrity"
    DELEGATION_LIFECYCLE = "delegation_lifecycle"
    RUNTIME_TARGET_TRUTH = "runtime_target_truth"
    PROVIDER_QUOTA_EXHAUSTION = "provider_quota_exhaustion"
    COMPLEX_WORKFLOW_REPLAN = "complex_workflow_replan"


class FairnessContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    provider: str
    task_objective: str
    fixture_sha256: str = Field(min_length=64, max_length=64)
    initial_repository_sha256: str = Field(min_length=64, max_length=64)
    tool_capabilities: tuple[str, ...]
    timeout_seconds: float = Field(gt=0)
    turn_budget: int = Field(ge=1)
    api_call_budget: int = Field(ge=1)
    acceptance_validator: str
    failure_injection_point: str
    configured_runtime_target: dict[str, Any] | None = None
    effective_provider_constraints: dict[str, Any] = Field(default_factory=dict)
    repository_digest: str | None = None

    @model_validator(mode="after")
    def require_unique_capabilities(self):
        if len(self.tool_capabilities) != len(set(self.tool_capabilities)):
            raise ValueError("tool capabilities must be unique")
        return self


class BenchmarkScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    family: BenchmarkFamily
    fairness: FairnessContract
    expected_invariants: dict[str, Any] = Field(default_factory=dict)
    metric_names: tuple[str, ...]


class BenchmarkResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    runner: str
    completed: bool
    metrics: dict[str, int | float | bool | str | None]
    invariant_results: dict[str, bool]
    safe_evidence: dict[str, Any] = Field(default_factory=dict)
    # Optional first-class workflow metrics. Deterministic runners may expose
    # them in ``metrics`` without inventing live token/cost observations.
    task_success: bool | None = None
    initial_plan_nodes: int | None = None
    final_plan_nodes: int | None = None
    replan_signals: int | None = None
    replans_attempted: int | None = None
    replans_accepted: int | None = None
    stale_plan_executions: int | None = None
    dead_end_turns: int | None = None
    redundant_tool_calls: int | None = None
    tool_failures: int | None = None
    turns_to_replan: int | None = None
    turns_after_replan: int | None = None
    validator_rejections: int | None = None
    human_intervention: bool | None = None
    final_acceptance: bool | None = None


class BenchmarkRunner(Protocol):
    name: str

    async def run(self, scenario: BenchmarkScenario) -> BenchmarkResult: ...


RunnerFunction = Callable[[BenchmarkScenario], BenchmarkResult | Awaitable[BenchmarkResult]]


class ContractBenchmarkRunner:
    """Small adapter so Hermes and Odys consume the exact same scenario."""

    def __init__(self, name: str, function: RunnerFunction):
        self.name = name
        self.function = function

    async def run(self, scenario: BenchmarkScenario) -> BenchmarkResult:
        value = self.function(scenario)
        if hasattr(value, "__await__"):
            value = await value
        if value.scenario_id != scenario.scenario_id or value.runner != self.name:
            raise ValueError("benchmark runner returned mismatched identity")
        missing = set(scenario.metric_names) - set(value.metrics)
        if missing:
            raise ValueError(f"benchmark result missing metrics: {sorted(missing)}")
        return value
