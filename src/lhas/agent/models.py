"""Provider-neutral contracts for every Odys agent role."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AgentRole(str, Enum):
    ROOT = "ROOT"
    PLANNER = "PLANNER"
    WORKER = "WORKER"
    RESEARCHER = "RESEARCHER"
    REVIEWER = "REVIEWER"


class AgentBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_turns: int = Field(default=20, ge=1, le=200)
    max_tool_calls: int = Field(default=100, ge=0, le=1000)
    max_delegations: int = Field(default=8, ge=0, le=100)
    max_context_chars: int = Field(default=40_000, ge=1_000, le=500_000)


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=128)
    role: AgentRole
    objective: str = Field(min_length=1, max_length=20_000)
    context: dict[str, Any] = Field(default_factory=dict)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    allowed_capabilities: set[str] = Field(default_factory=set)
    toolsets: set[str] = Field(default_factory=set)
    skill_refs: list[str] = Field(default_factory=list)
    memory_scope: list[str] = Field(default_factory=list)
    knowledge_scope: list[str] = Field(default_factory=list)
    budget: AgentBudget = Field(default_factory=AgentBudget)
    parent_agent_id: str | None = None
    parent_run_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("messages")
    @classmethod
    def bound_messages(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(value) > 100:
            raise ValueError("messages must be bounded to 100 entries")
        return value


class AgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: AgentStatus
    final_output: str = Field(default="", max_length=40_000)
    completion_claim: bool = False
    turn_count: int = Field(default=0, ge=0)
    tool_call_count: int = Field(default=0, ge=0)
    usage: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    safe_trace: list[dict[str, Any]] = Field(default_factory=list)
    child_run_refs: list[str] = Field(default_factory=list)
    error_type: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=512)

    @field_validator("safe_trace")
    @classmethod
    def bound_trace(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return value[-100:]
