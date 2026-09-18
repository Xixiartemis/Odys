"""Agent-loop protocol, deterministic fixtures, and compatibility adapters.

The production Odys-owned implementation is exported from
``lhas.native.NativeAgentKernel``. ``WorkerAgentKernelAdapter`` preserves the
external-runtime compatibility path.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from lhas.agent.models import AgentRequest, AgentResult, AgentStatus
from lhas.domain.enums import ExecutionStatus
from lhas.executors.protocol import AgentExecutor, ExecutionRequest
from lhas.execution_control import ExecutionControlError, ExecutionControlToken, await_with_control


@runtime_checkable
class AgentKernel(Protocol):
    async def run(self, request: AgentRequest, *, execution_control: ExecutionControlToken | None = None) -> AgentResult: ...

    async def cancel(self, agent_id: str) -> None: ...

    async def status(self, agent_id: str) -> dict[str, Any]: ...


AgentHandler = Callable[[AgentRequest], AgentResult | Awaitable[AgentResult]]


class ScriptedAgentKernel:
    """Deterministic kernel used by offline acceptance and unit tests."""

    def __init__(self, handler: AgentHandler):
        self._handler = handler
        self._states: dict[str, AgentStatus] = {}
        self._controls: dict[str, ExecutionControlToken] = {}

    async def run(self, request: AgentRequest, *, execution_control: ExecutionControlToken | None = None) -> AgentResult:
        self._states[request.agent_id] = AgentStatus.RUNNING
        control = execution_control or request.execution_control
        if control is not None:
            self._controls[request.agent_id] = control
        try:
            if control is not None:
                control.check()
            result = self._handler(request)
            if inspect.isawaitable(result):
                result = await await_with_control(result, control=control, source="child")
            if control is not None:
                control.check()
            self._states[request.agent_id] = result.status
            return result
        except ExecutionControlError:
            self._states[request.agent_id] = AgentStatus.CANCELLED
            raise
        except Exception:
            self._states[request.agent_id] = AgentStatus.FAILED
            raise

    async def cancel(self, agent_id: str) -> None:
        control = self._controls.get(agent_id)
        if control is not None:
            control.cancel("USER_CANCEL", source="scripted-kernel.cancel")
        self._states[agent_id] = AgentStatus.CANCELLED

    async def status(self, agent_id: str) -> dict[str, Any]:
        return {"agent_id": agent_id, "status": self._states.get(agent_id, AgentStatus.PENDING).value}


class WorkerAgentKernelAdapter:
    """Expose an existing InnerAgent/AgentExecutor through AgentKernel."""

    def __init__(self, executor: AgentExecutor):
        self._executor = executor

    async def run(self, request: AgentRequest, *, execution_control: ExecutionControlToken | None = None) -> AgentResult:
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
                execution_control=execution_control or request.execution_control,
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
