"""Durable Odys-native subagent delegation."""

from __future__ import annotations

import json
from typing import Callable

from lhas import HARNESS_VERSION
from lhas.agent.kernel import AgentKernel
from lhas.agent.models import AgentRequest, AgentStatus
from lhas.agent.profile import AgentProfileRegistry
from lhas.domain.enums import EventType, ExecutionStatus, RunStatus
from lhas.domain.models import Task
from lhas.executors.protocol import ExecutionRequest, ExecutionResult
from lhas.orchestrator_v2 import RecoveringOrchestrator
from lhas.persistence.event_store import EventStore
from lhas.persistence.phaseb_repos import ValidationResultRepository
from lhas.persistence.platform_repositories import DelegationRepository
from lhas.persistence.repositories import AttemptRepository, RunRepository, TaskRepository
from lhas.platform_models import Delegation, DelegationRequest, DelegationResult, DelegationStatus


class _ChildAgentExecutor:
    name = "DurableChildAgentExecutor"

    def __init__(self, kernel: AgentKernel, request: DelegationRequest, allowed_capabilities: set[str]):
        self.kernel=kernel; self.request=request; self.allowed_capabilities=allowed_capabilities

    async def execute(self, execution: ExecutionRequest) -> ExecutionResult:
        agent_request=AgentRequest(
            agent_id=self.request.child_agent_id,
            role=self.request.role,
            objective=self.request.goal,
            context=dict(self.request.context),
            messages=[{"role":"user","content":self.request.goal}],
            allowed_capabilities=self.allowed_capabilities,
            toolsets=self.request.toolsets,
            skill_refs=self.request.skills,
            memory_scope=["memory"],
            knowledge_scope=["project"],
            budget=self.request.budget,
            parent_agent_id=self.request.parent_agent_id,
            parent_run_id=self.request.parent_run_id,
            metadata={"task_id":execution.task_id,"run_id":execution.run_id,"attempt_id":execution.attempt_id,"attempt_number":execution.attempt_number},
        )
        result=await self.kernel.run(agent_request)
        status=ExecutionStatus.SUCCESS if result.status is AgentStatus.COMPLETED else ExecutionStatus.FAILURE
        return ExecutionResult(status=status,output=result.final_output,error_type=result.error_type,usage=result.usage,artifacts=result.artifacts,raw={"completion_claim":result.completion_claim,"turn_count":result.turn_count,"tool_call_count":result.tool_call_count,"safe_trace":result.safe_trace,"child_run_refs":result.child_run_refs})

    async def resume(self, request): return await self.execute(request)
    async def cancel(self, run_id): await self.kernel.cancel(self.request.child_agent_id)
    async def status(self, run_id): return await self.kernel.status(self.request.child_agent_id)


class DelegationService:
    def __init__(self, db, profiles: AgentProfileRegistry, kernel_factory: Callable[[DelegationRequest], AgentKernel], capability_resolver: Callable[[set[str]], set[str]]):
        self.db=db; self.profiles=profiles; self.kernel_factory=kernel_factory; self.capability_resolver=capability_resolver
        self.repo=DelegationRepository(db); self.events=EventStore(db)

    async def delegate(self, request: DelegationRequest) -> DelegationResult:
        parent_task=TaskRepository(self.db).get(request.parent_task_id)
        if parent_task is None: raise KeyError(f"parent task not found: {request.parent_task_id}")
        parent_profile=self.profiles.for_role(self._parent_role(request.parent_agent_id))
        child_profile=self.profiles.for_role(request.role)
        self.profiles.validate_child(parent_profile,child_profile,spawn_depth=request.spawn_depth)
        existing=self.repo.list_for_parent_run(request.parent_run_id)
        if len(existing)>=parent_profile.max_children: raise PermissionError("MAX_CHILDREN_EXCEEDED")
        if not request.toolsets.issubset(parent_profile.toolsets): raise PermissionError("CHILD_TOOLSETS_EXCEED_PARENT")
        allowed=self.capability_resolver(request.toolsets)
        child_task=Task(project_id=parent_task.project_id,title=f"Delegated {request.role.value.lower()}",objective=request.goal,constraints=["fresh bounded child context","no parent transcript","no persistent memory write"],acceptance_criteria=["child output is non-empty"],max_attempts=2)
        TaskRepository(self.db).create(child_task)
        safe_context=self._bounded_context(request.context)
        request=request.model_copy(update={"context":safe_context})
        delegation=Delegation(parent_agent_id=request.parent_agent_id,parent_task_id=request.parent_task_id,parent_run_id=request.parent_run_id,child_agent_id=request.child_agent_id,child_task_id=child_task.id,spawn_depth=request.spawn_depth,context=safe_context)
        self.repo.create(delegation)
        self.events.append(EventType.DELEGATION_CREATED,task_id=request.parent_task_id,run_id=request.parent_run_id,payload={"delegation_id":delegation.id,"parent_agent_id":request.parent_agent_id,"child_agent_id":request.child_agent_id,"child_task_id":child_task.id,"spawn_depth":request.spawn_depth,"role":request.role.value})
        delegation.status=DelegationStatus.RUNNING; self.repo.update(delegation)
        self.events.append(EventType.DELEGATION_STARTED,task_id=child_task.id,payload={"delegation_id":delegation.id,"parent_run_id":request.parent_run_id})
        kernel=self.kernel_factory(request)
        executor=lambda: _ChildAgentExecutor(kernel,request,allowed)
        orchestrator=RecoveringOrchestrator(self.db,executor_factory=executor,executor_type="AgentKernel",provider=child_profile.provider,model=child_profile.model,harness_version=HARNESS_VERSION,context_policy_version="CP-3",dataset_version="AGENT-PLATFORM-OFFLINE")
        run=await orchestrator.execute_task(child_task.id)
        delegation.child_run_id=run.id
        self.events.append(EventType.CHILD_RUN_LINKED,task_id=child_task.id,run_id=run.id,payload={"delegation_id":delegation.id,"parent_task_id":request.parent_task_id,"parent_run_id":request.parent_run_id})
        attempts=AttemptRepository(self.db).list_for_run(run.id)
        validation=ValidationResultRepository(self.db).get_for_attempt(attempts[-1].id) if attempts else None
        payload=json.loads(run.result or "{}")
        output=str(payload.get("output", ""))[:8_000]
        delegation.status=DelegationStatus.COMPLETED if run.status is RunStatus.COMPLETED else DelegationStatus.FAILED
        delegation.result={"summary":output,"validation":validation.passed if validation else None,"child_run_id":run.id}
        self.repo.update(delegation)
        event=EventType.DELEGATION_COMPLETED if delegation.status is DelegationStatus.COMPLETED else EventType.DELEGATION_FAILED
        self.events.append(event,task_id=child_task.id,run_id=run.id,payload={"delegation_id":delegation.id,"parent_run_id":request.parent_run_id,"validation":validation.passed if validation else None})
        raw=json.loads(attempts[-1].executor_result or "{}") if attempts else {}
        return DelegationResult(status=delegation.status,summary=output,artifacts=raw.get("artifacts",{}),evidence=validation.evidence if validation else "",changed_files=list(raw.get("artifacts",{}).get("changed_files",[]))[:100],validation=validation.passed if validation else None,child_run_id=run.id)

    @staticmethod
    def _bounded_context(context):
        encoded=json.dumps(context,ensure_ascii=False,default=str)
        if len(encoded)>20_000: return {"summary":encoded[:19_900],"truncated":True}
        return context

    @staticmethod
    def _parent_role(agent_id: str):
        from lhas.agent.models import AgentRole
        lowered=agent_id.casefold()
        if "root" in lowered: return AgentRole.ROOT
        if "planner" in lowered: return AgentRole.PLANNER
        return AgentRole.WORKER
