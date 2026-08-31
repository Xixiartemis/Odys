"""Harness-neutral reliability benchmark contracts."""

from __future__ import annotations

from enum import Enum
from typing import Any, Awaitable, Callable, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BenchmarkFamily(str, Enum):
    EXECUTION_STATE_RECOVERY = "execution_state_recovery"
    COMPLETION_INTEGRITY = "completion_integrity"
    DELEGATION_LIFECYCLE = "delegation_lifecycle"


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
