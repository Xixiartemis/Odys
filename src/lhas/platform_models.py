"""Durable platform models that link into Odys Task/Run/Attempt."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lhas.agent.models import AgentBudget, AgentRole
from lhas.domain.models import new_id, utcnow


class ConversationSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=new_id)
    title: str = Field(default="New conversation", max_length=256)
    parent_session_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class SessionMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=new_id)
    session_id: str
    role: str
    content: str = Field(max_length=40_000)
    safe_tool_summary: str | None = Field(default=None, max_length=8_000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class DelegationStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Delegation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=new_id)
    parent_agent_id: str
    parent_task_id: str
    parent_run_id: str
    child_agent_id: str
    child_task_id: str
    child_run_id: str | None = None
    spawn_depth: int = Field(ge=1)
    status: DelegationStatus = DelegationStatus.CREATED
    context: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class DelegationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parent_agent_id: str
    parent_task_id: str
    parent_run_id: str
    child_agent_id: str = Field(default_factory=lambda: f"child-{new_id()}")
    goal: str = Field(min_length=1, max_length=20_000)
    context: dict[str, Any] = Field(default_factory=dict)
    role: AgentRole
    toolsets: set[str] = Field(default_factory=set)
    skills: list[str] = Field(default_factory=list)
    budget: AgentBudget = Field(default_factory=AgentBudget)
    spawn_depth: int = Field(default=1, ge=1)


class DelegationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: DelegationStatus
    summary: str = Field(default="", max_length=8_000)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    evidence: str = Field(default="", max_length=8_000)
    changed_files: list[str] = Field(default_factory=list)
    validation: bool | None = None
    child_run_id: str
