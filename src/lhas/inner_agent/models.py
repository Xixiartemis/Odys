from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

class InnerAgentStatus(str, Enum):
    SUCCESS="SUCCESS"
    FAILURE="FAILURE"

class InnerAgentRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    task_id: str
    run_id: str
    attempt_id: str
    objective: str
    constraints: list[str]=Field(default_factory=list)
    acceptance_criteria: list[str]=Field(default_factory=list)
    context: dict[str,Any]=Field(default_factory=dict)
    allowed_capabilities: list[str]=Field(default_factory=list)
    max_turns: int=Field(default=12,ge=1)
    metadata: dict[str,Any]=Field(default_factory=dict)

class InnerAgentResult(BaseModel):
    model_config=ConfigDict(extra="forbid")
    status: InnerAgentStatus
    final_output: str|None=None
    completion_claim: bool=False
    turn_count: int=0
    tool_call_count: int=0
    usage: dict[str,Any]=Field(default_factory=dict)
    artifacts: dict[str,Any]=Field(default_factory=dict)
    error_type: str|None=None
    error_message: str|None=None
    trace: list[dict[str,Any]]=Field(default_factory=list)
    provider_metadata: dict[str,Any]=Field(default_factory=dict)
