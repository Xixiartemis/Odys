"""RootAgent conversation surface and deterministic routing boundary."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from lhas.agent.context import ContextAssembler, ContextPriority, ContextSource
from lhas.agent.kernel import AgentKernel
from lhas.agent.models import AgentRequest, AgentRole
from lhas.domain.enums import EventType
from lhas.domain.models import new_id
from lhas.knowledge import ProjectContextDiscovery
from lhas.persistence.event_store import EventStore
from lhas.persistence.platform_repositories import SessionRepository
from lhas.platform_models import ConversationSession, SessionMessage


class RootRoute(str, Enum):
    SIMPLE_INTERACTION="SIMPLE_INTERACTION"
    LONG_RUNNING_GOAL="LONG_RUNNING_GOAL"


class GoalSubmissionResult(BaseModel):
    model_config=ConfigDict(extra="forbid")
    goal_id: str
    plan_id: str
    status: str
    run_refs: list[str]=Field(default_factory=list)
    summary: str=""


class GoalSubmissionService(Protocol):
    async def submit(self, objective: str, context: dict, project_id: str) -> GoalSubmissionResult: ...


class RootAgentResponse(BaseModel):
    model_config=ConfigDict(extra="forbid")
    session_id: str
    route: RootRoute
    output: str
    goal_id: str | None=None
    plan_id: str | None=None
    run_refs: list[str]=Field(default_factory=list)


class RootAgentService:
    def __init__(self,db,kernel:AgentKernel,goal_service:GoalSubmissionService,sessions:SessionRepository,memory,skills,project_root:Path,context_assembler:ContextAssembler|None=None):
        self.db=db; self.kernel=kernel; self.goal_service=goal_service; self.sessions=sessions; self.memory=memory; self.skills=skills; self.project_root=project_root.resolve(); self.assembler=context_assembler or ContextAssembler(); self.events=EventStore(db)

    @staticmethod
    def classify(text: str) -> RootRoute:
        lowered=text.casefold()
        long_markers=("build ","implement ","repair ","refactor ","create platform","long-running","多步骤","实现","修复","构建")
        return RootRoute.LONG_RUNNING_GOAL if len(text)>240 or any(marker in lowered for marker in long_markers) else RootRoute.SIMPLE_INTERACTION

    async def handle(self,text:str,*,project_id:str,session_id:str|None=None) -> RootAgentResponse:
        if session_id is None:
            conversation=ConversationSession(title=" ".join(text.split())[:80] or "New conversation"); self.sessions.create(conversation); session_id=conversation.id
        self.sessions.append(SessionMessage(session_id=session_id,role="user",content=text))
        route=self.classify(text); root_id=f"root-{new_id()}"
        self.events.append(EventType.ROOT_AGENT_STARTED,payload={"agent_id":root_id,"session_id":session_id,"route":route.value})
        memories=self.memory.search(text)[:10]
        self.events.append(EventType.MEMORY_READ,payload={"agent_id":root_id,"session_id":session_id,"count":len(memories)})
        skills=self.skills.list()
        self.events.append(EventType.SKILL_DISCOVERED,payload={"agent_id":root_id,"count":len(skills),"names":[item.name for item in skills[:20]]})
        project_context=ProjectContextDiscovery().discover(self.project_root)
        assembled=self.assembler.assemble([
            ContextSource("objective",text,ContextPriority.REQUIRED,12_000),
            ContextSource("project_context",project_context,ContextPriority.HIGH,12_000),
            ContextSource("memory",[item.model_dump() for item in memories],ContextPriority.NORMAL,6_000),
            ContextSource("skill_index",[item.model_dump() for item in skills],ContextPriority.LOW,5_000),
        ],budget_chars=30_000)
        if route is RootRoute.LONG_RUNNING_GOAL:
            self.events.append(EventType.PLAN_REQUESTED,payload={"agent_id":root_id,"session_id":session_id})
            result=await self.goal_service.submit(text,assembled.sections,project_id)
            output=result.summary or f"Goal {result.status}: {result.goal_id}"
            response=RootAgentResponse(session_id=session_id,route=route,output=output,goal_id=result.goal_id,plan_id=result.plan_id,run_refs=result.run_refs)
        else:
            result=await self.kernel.run(AgentRequest(agent_id=root_id,role=AgentRole.ROOT,objective=text,context=assembled.sections,messages=[{"role":"user","content":text}],toolsets={"skills","memory","knowledge"},memory_scope=["memory","user"],knowledge_scope=["project"]))
            response=RootAgentResponse(session_id=session_id,route=route,output=result.final_output,run_refs=result.child_run_refs)
        self.sessions.append(SessionMessage(session_id=session_id,role="assistant",content=response.output,metadata={"route":route.value,"goal_id":response.goal_id,"plan_id":response.plan_id}))
        self.events.append(EventType.ROOT_AGENT_COMPLETED,payload={"agent_id":root_id,"session_id":session_id,"route":route.value,"goal_id":response.goal_id,"plan_id":response.plan_id})
        return response
