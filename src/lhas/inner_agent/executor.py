import json
from lhas.domain.enums import ExecutionStatus
from lhas.executors.protocol import AgentExecutor, ExecutionRequest, ExecutionResult
from .models import InnerAgentRequest, InnerAgentStatus
from .protocol import InnerAgentBackend
from lhas.domain.enums import EventType
from lhas.persistence.event_store import EventStore

class InnerAgentExecutor:
    name="InnerAgentExecutor"
    def __init__(self, backend: InnerAgentBackend, allowed_capabilities=None, allowed_side_effect_capabilities=None, db=None): self.backend=backend; self.allowed_capabilities=allowed_capabilities; self.allowed_side_effect_capabilities=allowed_side_effect_capabilities; self.db=db
    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        task=request.task
        allowed = self.allowed_capabilities if self.allowed_capabilities is not None else task.get("allowed_capabilities", [])
        allowed_side_effects = self.allowed_side_effect_capabilities if self.allowed_side_effect_capabilities is not None else task.get("allowed_side_effect_capabilities", [])
        inner=InnerAgentRequest(task_id=request.task_id,run_id=request.run_id,attempt_id=request.attempt_id,objective=str(task.get("objective", "")),constraints=task.get("constraints",[]),acceptance_criteria=task.get("acceptance_criteria",[]),context=request.context,allowed_capabilities=allowed,allowed_side_effect_capabilities=allowed_side_effects,max_turns=int(request.metadata.get("max_turns",12)),metadata=request.metadata)
        if self.db: EventStore(self.db).append(EventType.INNER_AGENT_STARTED,task_id=request.task_id,run_id=request.run_id,attempt_id=request.attempt_id,payload={"backend":self.backend.name})
        try: result=await self.backend.run(inner)
        except Exception as exc:
            if self.db: EventStore(self.db).append(EventType.INNER_AGENT_FAILED,task_id=request.task_id,run_id=request.run_id,attempt_id=request.attempt_id,payload={"error_type":type(exc).__name__})
            raise
        if self.db:
            mapping = {"LLM_TURN_STARTED": EventType.INNER_AGENT_LLM_TURN_STARTED, "LLM_TURN_COMPLETED": EventType.INNER_AGENT_LLM_TURN_COMPLETED, "TOOL_STARTED": EventType.INNER_AGENT_TOOL_STARTED, "TOOL_COMPLETED": EventType.INNER_AGENT_TOOL_COMPLETED, "TOOL_OBSERVATION_SUMMARY": EventType.INNER_AGENT_TOOL_OBSERVATION, "WORKSPACE_EDIT_STARTED": EventType.WORKSPACE_EDIT_STARTED, "WORKSPACE_EDIT_COMPLETED": EventType.WORKSPACE_EDIT_COMPLETED, "WORKSPACE_EDIT_FAILED": EventType.WORKSPACE_EDIT_FAILED, "WORKSPACE_RESTORED": EventType.WORKSPACE_RESTORED}
            for item in result.trace:
                if item.get("event") in mapping:
                    EventStore(self.db).append(mapping[item["event"]], task_id=request.task_id, run_id=request.run_id, attempt_id=request.attempt_id, payload={k:v for k,v in item.items() if k not in {"event", "diff"}})
            event_type=EventType.INNER_AGENT_COMPLETED if result.status==InnerAgentStatus.SUCCESS else EventType.INNER_AGENT_FAILED
            EventStore(self.db).append(event_type,task_id=request.task_id,run_id=request.run_id,attempt_id=request.attempt_id,payload={"status":result.status.value,"turn_count":result.turn_count,"tool_call_count":result.tool_call_count,"error_type":result.error_type})
        return ExecutionResult(status=ExecutionStatus.SUCCESS if result.status==InnerAgentStatus.SUCCESS else ExecutionStatus.FAILURE,output=result.final_output,error_type=result.error_type,error_message=result.error_message,usage=result.usage,artifacts=result.artifacts,raw=result.model_dump(mode="json"))
    async def resume(self,request): return await self.execute(request)
    async def cancel(self,run_id): return None
    async def status(self,run_id): return {"run_id":run_id,"backend":self.backend.name}
