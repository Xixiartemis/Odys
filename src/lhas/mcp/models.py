from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MCPServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    command: list[str] = Field(min_length=1)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)


class MCPToolInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    server_name: str
    origin: str = "mcp"
    side_effect: bool = False
    requires_approval: bool = False
    risk: str = "MEDIUM"
