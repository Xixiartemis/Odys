"""Provider-neutral Tool contract."""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lhas.planning.models import CapabilitySpec


class ToolResultStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    WAITING_FOR_HUMAN_APPROVAL = "WAITING_FOR_HUMAN_APPROVAL"


class ToolEvidence(BaseModel):
    """Evidence produced by one Tool invocation, not completion evidence."""

    model_config = ConfigDict(extra="forbid")

    evidence_type: str = Field(min_length=1)
    source: str = Field(min_length=1)
    capability_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    artifact_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_call_id: str
    task_id: str
    run_id: str
    attempt_id: str
    # ``capability`` remains as a compatibility field for existing callers.
    # New contract callers should provide ``capability_id`` and ``tool_name``.
    capability: str | None = None
    capability_id: str | None = None
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    workspace_ref: str | None = None
    timeout_seconds: float | None = Field(default=None, gt=0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def synchronize_capability_alias(self) -> "ToolRequest":
        if self.capability is None and self.capability_id is None:
            raise ValueError("capability or capability_id is required")
        if self.capability and self.capability_id and self.capability != self.capability_id:
            raise ValueError("capability and capability_id must identify the same capability")
        if self.capability_id is None:
            self.capability_id = self.capability
        if self.capability is None:
            self.capability = self.capability_id
        return self


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ToolResultStatus
    output: Any = None
    artifacts: dict[str, Any] = Field(default_factory=dict)
    error_type: str | None = None
    error_message: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    evidence: ToolEvidence | None = None


class Tool(Protocol):
    @property
    def capability(self) -> CapabilitySpec:
        ...

    async def execute(self, request: ToolRequest) -> ToolResult:
        ...
