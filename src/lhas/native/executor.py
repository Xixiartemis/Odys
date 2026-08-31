"""AgentExecutor adapter for the native kernel."""

from __future__ import annotations

from lhas.agent.models import AgentBudget, AgentRequest, AgentRole, AgentStatus
from lhas.domain.enums import ExecutionStatus
from lhas.executors.protocol import ExecutionRequest, ExecutionResult


class NativeAgentExecutor:
    name = "NativeAgentExecutor"

    def __init__(
        self,
        kernel,
        *,
        allowed_capabilities: set[str] | list[str],
        allowed_side_effect_capabilities: set[str] | list[str],
        max_turns: int,
        max_tool_calls: int = 100,
    ):
        self.kernel = kernel
        self.allowed_capabilities = set(allowed_capabilities)
        self.allowed_side_effect_capabilities = set(allowed_side_effect_capabilities)
        self.max_turns = max_turns
        self.max_tool_calls = max_tool_calls

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        agent_request = AgentRequest(
            agent_id=f"native-{request.attempt_id}",
            role=AgentRole.WORKER,
            objective=str(request.task.get("objective", "")),
            context={
                **request.context,
                "acceptance_criteria": list(request.task.get("acceptance_criteria", [])),
            },
            messages=[],
            allowed_capabilities=self.allowed_capabilities,
            budget=AgentBudget(max_turns=self.max_turns, max_tool_calls=self.max_tool_calls),
            metadata={
                **request.metadata,
                "task_id": request.task_id,
                "run_id": request.run_id,
                "attempt_id": request.attempt_id,
                "attempt_number": request.attempt_number,
                "allowed_side_effect_capabilities": sorted(self.allowed_side_effect_capabilities),
            },
        )
        result = await self.kernel.run(agent_request)
        return ExecutionResult(
            status=ExecutionStatus.SUCCESS if result.status is AgentStatus.COMPLETED else ExecutionStatus.FAILURE,
            output=result.final_output or None,
            error_type=result.error_type,
            error_message=None if result.status is AgentStatus.COMPLETED else "native agent attempt did not reach accepted completion",
            usage=result.usage,
            artifacts=result.artifacts,
            raw=result.model_dump(mode="json"),
        )

    async def resume(self, request: ExecutionRequest) -> ExecutionResult:
        return await self.execute(request)

    async def cancel(self, run_id: str) -> None:
        await self.kernel.cancel(f"native-{run_id}")

    async def status(self, run_id: str):
        return await self.kernel.status(f"native-{run_id}")
