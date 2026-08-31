"""One agent loop contract shared by Root, Planner, Worker and children."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from lhas.agent.models import AgentRequest, AgentResult, AgentStatus
from lhas.domain.enums import ExecutionStatus
from lhas.executors.protocol import AgentExecutor, ExecutionRequest


@runtime_checkable
class AgentKernel(Protocol):
    async def run(self, request: AgentRequest) -> AgentResult: ...

    async def cancel(self, agent_id: str) -> None: ...

    async def status(self, agent_id: str) -> dict[str, Any]: ...


AgentHandler = Callable[[AgentRequest], AgentResult | Awaitable[AgentResult]]


class ScriptedAgentKernel:
    """Deterministic kernel used by offline acceptance and unit tests."""

    def __init__(self, handler: AgentHandler):
        self._handler = handler
        self._states: dict[str, AgentStatus] = {}

    async def run(self, request: AgentRequest) -> AgentResult:
        self._states[request.agent_id] = AgentStatus.RUNNING
        try:
            result = self._handler(request)
            if inspect.isawaitable(result):
                result = await result
            self._states[request.agent_id] = result.status
            return result
        except Exception:
            self._states[request.agent_id] = AgentStatus.FAILED
            raise

    async def cancel(self, agent_id: str) -> None:
        self._states[agent_id] = AgentStatus.CANCELLED

    async def status(self, agent_id: str) -> dict[str, Any]:
        return {"agent_id": agent_id, "status": self._states.get(agent_id, AgentStatus.PENDING).value}


class WorkerAgentKernelAdapter:
    """Expose an existing InnerAgent/AgentExecutor through AgentKernel."""

    def __init__(self, executor: AgentExecutor):
        self._executor = executor

    async def run(self, request: AgentRequest) -> AgentResult:
        metadata = request.metadata
        execution = await self._executor.execute(
            ExecutionRequest(
                task_id=str(metadata["task_id"]),
                run_id=str(metadata["run_id"]),
                attempt_id=str(metadata["attempt_id"]),
                attempt_number=int(metadata.get("attempt_number", 1)),
                task=dict(metadata.get("task", {"objective": request.objective})),
                context=request.context,
                metadata={
                    "agent_id": request.agent_id,
                    "role": request.role.value,
                    "allowed_capabilities": sorted(request.allowed_capabilities),
                },
            )
        )
        status = AgentStatus.COMPLETED if execution.status is ExecutionStatus.SUCCESS else AgentStatus.FAILED
        raw = execution.raw or {}
        return AgentResult(
            status=status,
            final_output=execution.output or "",
            completion_claim=execution.status is ExecutionStatus.SUCCESS,
            turn_count=int(raw.get("turn_count", 0)),
            tool_call_count=int(raw.get("tool_call_count", 0)),
            usage=execution.usage,
            artifacts=execution.artifacts,
            safe_trace=list(raw.get("safe_trace", []))[-100:],
            error_type=execution.error_type,
        )

    async def cancel(self, agent_id: str) -> None:
        await self._executor.cancel(agent_id)

    async def status(self, agent_id: str) -> dict[str, Any]:
        return await self._executor.status(agent_id)
