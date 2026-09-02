"""Domain-neutral Goal, Plan and Capability models."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lhas.domain.models import new_id


def _semantic_value(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    if isinstance(value, dict):
        return {str(key): _semantic_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_semantic_value(item) for item in value]
    return value


def compute_step_semantic_fingerprint(step: "PlanStep", by_id: dict[str, "PlanStep"] | None = None, _seen: set[str] | None = None) -> str:
    by_id = by_id or {}
    seen = set(_seen or set())
    dependency_semantics = []
    if step.id not in seen:
        seen.add(step.id)
        for dependency_id in step.depends_on:
            dependency = by_id.get(dependency_id)
            if dependency is None:
                dependency_semantics.append({"id": dependency_id})
            else:
                dependency_semantics.append({
                    "capability": _semantic_value(dependency.capability),
                    "objective": _semantic_value(dependency.objective),
                    "inputs": _semantic_value(dependency.inputs),
                    "depends_on": [
                        compute_step_semantic_fingerprint(dependency, by_id, seen)
                        if dependency_id not in seen else "cycle"
                    ],
                })
    payload = {
        "capability": _semantic_value(step.capability),
        "objective": _semantic_value(step.objective),
        "inputs": _semantic_value(step.inputs),
        "dependency_semantics": dependency_semantics,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


class PlanMode(str, Enum):
    LINEAR = "LINEAR"
    SIMPLE_DEPENDENCY = "SIMPLE_DEPENDENCY"


class PlanStatus(str, Enum):
    DRAFT = "DRAFT"
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    WAITING_FOR_HUMAN_APPROVAL = "WAITING_FOR_HUMAN_APPROVAL"


class PlanStepStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    WAITING_FOR_HUMAN_APPROVAL = "WAITING_FOR_HUMAN_APPROVAL"
    STALE = "STALE"


class Goal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    project_id: str
    objective: str = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    allowed_capabilities: list[str] = Field(default_factory=list)
    requires_human_approval: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    title: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    expected_output: str = ""
    success_criteria: list[str] = Field(default_factory=list)
    suggested_role: str = "WORKER"
    required_capabilities: list[str] = Field(default_factory=list)
    optional_skill_refs: list[str] = Field(default_factory=list)
    status: PlanStepStatus = PlanStepStatus.PENDING
    task_id: Optional[str] = None
    output: Any = None
    execution_context: dict[str, Any] = Field(default_factory=dict)
    semantic_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)

    @model_validator(mode="after")
    def no_self_dependency(self) -> "PlanStep":
        if self.id in self.depends_on:
            raise ValueError("PlanStep cannot depend on itself")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("PlanStep depends_on must be unique")
        return self


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    goal_id: str
    version: str = "P-0.1"
    mode: PlanMode = PlanMode.LINEAR
    status: PlanStatus = PlanStatus.DRAFT
    steps: list[PlanStep] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    invalidated_step_ids: list[str] = Field(default_factory=list)
    replan_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_dependencies(self) -> "Plan":
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("Plan step ids must be unique")
        known = set(ids)
        for step in self.steps:
            missing = set(step.depends_on) - known
            if missing:
                raise ValueError(f"PlanStep {step.id} depends on unknown step(s): {sorted(missing)}")
        visiting: set[str] = set()
        visited: set[str] = set()
        by_id = {step.id: step for step in self.steps}

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise ValueError("Plan dependencies must be acyclic")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dep in by_id[step_id].depends_on:
                visit(dep)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in ids:
            visit(step_id)
        for step in self.steps:
            step.semantic_fingerprint = compute_step_semantic_fingerprint(step, by_id)
        return self


class CapabilitySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    risk_level: str = "LOW"
    side_effect: bool = False
    requires_human_approval: bool = False
    origin: str = "native"
    server_name: str | None = None
