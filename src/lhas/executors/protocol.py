"""AgentExecutor Protocol (docs/04_EXECUTOR_PROTOCOL.md).

The executor is "the one who does the work", never the one who decides whether
the task is complete. LHAS core depends only on this protocol, never on a
concrete provider (no Codex, no third-party LLM) in Phase A.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from lhas.domain.enums import ExecutionStatus


class ExecutionRequest(BaseModel):
    """Everything an executor needs for one attempt."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    run_id: str
    attempt_id: str
    attempt_number: int
    # Task snapshot (objective / constraints / acceptance criteria)
    task: dict[str, Any] = Field(default_factory=dict)
    # Executor-visible context (built by the orchestrator / ContextBuilder)
    context: dict[str, Any] = Field(default_factory=dict)
    # Run / Attempt metadata (executor_type, provider, model, harness_version, ...)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # In-process only: the parent root owns this authority and it is never
    # serialized into executor input or benchmark evidence.
    execution_control: Any = Field(default=None, exclude=True)


class ExecutionResult(BaseModel):
    """Executor output for one attempt."""

    model_config = ConfigDict(extra="forbid")

    status: ExecutionStatus
    output: Optional[str] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    usage: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int = 0
    raw: Optional[dict[str, Any]] = None


@runtime_checkable
class AgentExecutor(Protocol):
    """V0 executor interface (async, per docs/04).

    Implementations MUST be stateless w.r.t. attempts: all per-attempt state
    arrives via ExecutionRequest (or the orchestrator creates a fresh executor
    instance per attempt through an executor factory).
    """

    name: str

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Run one attempt; return a result. Timeout is the orchestrator's job."""
        ...

    async def resume(self, request: ExecutionRequest) -> ExecutionResult:
        """Resume a previously interrupted attempt (Phase A: re-execute)."""
        ...

    async def cancel(self, run_id: str) -> None:
        """Best-effort cancellation (Phase A: no-op)."""
        ...

    async def status(self, run_id: str) -> dict[str, Any]:
        """Current executor-side status for a run."""
        ...
