"""Domain-neutral Goal, Plan and Capability models."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lhas.domain.models import new_id


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
