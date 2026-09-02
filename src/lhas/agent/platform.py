"""Offline Agent Platform assembly over the authoritative Odys control plane."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from lhas.agent.context import ContextAssembler
from lhas.agent.delegation import DelegationService
from lhas.agent.kernel import ScriptedAgentKernel
from lhas.agent.models import AgentRequest, AgentResult, AgentRole, AgentStatus
from lhas.agent.planner import ScriptedPlatformPlanner
from lhas.agent.profile import AgentProfileRegistry
from lhas.agent.root import GoalSubmissionResult, RootAgentService
from lhas.agent.toolsets import ToolsetRegistry
from lhas.domain.enums import EventType
from lhas.domain.models import Project, new_id
from lhas.knowledge import LocalKnowledgeProvider
from lhas.mcp import MCPManager, MCPServerConfig, register_mcp_tools
from lhas.memory import BuiltinMemoryProvider
from lhas.persistence.event_store import EventStore
from lhas.persistence.platform_repositories import SessionRepository
from lhas.persistence.repositories import ProjectRepository, RunRepository
from lhas.planning.models import CapabilitySpec, Goal
from lhas.planning.service import PlanExecutionService
from lhas.platform_models import DelegationRequest
from lhas.skills import SkillRegistry
from lhas.tools.protocol import ToolRequest, ToolResult, ToolResultStatus
from lhas.tools.registry import ToolRegistry


class _SkillsTool:
    def __init__(self, skills: SkillRegistry, db, action: str): self.skills=skills; self.db=db; self.action=action
    @property
    def capability(self): return CapabilitySpec(name=f"skills.{self.action}",description=f"{self.action} progressively disclosed skills")
    async def execute(self, request):
        if self.action=="list": output=[item.model_dump(exclude={"source"}) for item in self.skills.list()]
        else:
            name=str(request.arguments.get("name","")); reference=request.arguments.get("reference_path"); doc=self.skills.view(name,reference); output=doc.model_dump()
            EventStore(self.db).append(EventType.SKILL_LOADED,task_id=request.task_id,run_id=request.run_id,attempt_id=request.attempt_id,payload={"name":name,"reference_path":reference})
        return ToolResult(status=ToolResultStatus.SUCCESS,output=output)


class _KnowledgeTool:
    def __init__(self, knowledge: LocalKnowledgeProvider, db, action: str): self.knowledge=knowledge; self.db=db; self.action=action
    @property
    def capability(self): return CapabilitySpec(name=f"knowledge.{self.action}",description=f"{self.action} bounded project knowledge")
    async def execute(self, request):
        if self.action=="search":
            query=str(request.arguments.get("query","")); output=[item.model_dump() for item in self.knowledge.search(query)]
            EventStore(self.db).append(EventType.KNOWLEDGE_SEARCHED,task_id=request.task_id,run_id=request.run_id,attempt_id=request.attempt_id,payload={"result_count":len(output)})
        else: output={"content":self.knowledge.open(str(request.arguments.get("ref","")))}
        return ToolResult(status=ToolResultStatus.SUCCESS,output=output)


class _MemoryTool:
    def __init__(self,memory:BuiltinMemoryProvider,action:str): self.memory=memory; self.action=action
    @property
    def capability(self): return CapabilitySpec(name=f"memory.{self.action}",description=f"{self.action} bounded persistent memory",side_effect=self.action=="add",requires_human_approval=self.action=="add")
    async def execute(self,request):
        if self.action=="list": output=[item.model_dump() for item in self.memory.list(request.arguments.get("scope"))]
        elif self.action=="search": output=[item.model_dump() for item in self.memory.search(str(request.arguments.get("query","")),request.arguments.get("scope"))]
        else:
            role=AgentRole(str(request.metadata.get("role","WORKER"))); item=self.memory.add(str(request.arguments.get("content","")),scope=str(request.arguments.get("scope","memory")),role=role,approved=bool(request.metadata.get("approved",False))); output=item.model_dump()
        return ToolResult(status=ToolResultStatus.SUCCESS,output=output)


class _KernelTool:
    def __init__(self,name:str,kernel:ScriptedAgentKernel,role:AgentRole): self.name=name; self.kernel=kernel; self.role=role
    @property
    def capability(self): return CapabilitySpec(name=self.name,description=f"AgentKernel {self.role.value.lower()} step")
    async def execute(self,request:ToolRequest):
        result=await self.kernel.run(AgentRequest(agent_id=f"{self.role.value.lower()}-{request.task_id}",role=self.role,objective=str(request.arguments.get("goal",self.name)),context=request.context,messages=[],allowed_capabilities={self.name},toolsets=set(),metadata={"task_id":request.task_id,"run_id":request.run_id,"attempt_id":request.attempt_id}))
        status=ToolResultStatus.SUCCESS if result.status is AgentStatus.COMPLETED else ToolResultStatus.FAILURE
        return ToolResult(status=status,output={"summary":result.final_output,"completion_claim":result.completion_claim},artifacts=result.artifacts,error_type=result.error_type,usage=result.usage)


class _DelegationTool:
    def __init__(self,service:DelegationService): self.service=service
    @property
    def capability(self): return CapabilitySpec(name="platform.delegate",description="Create a durable child Task, Run and Attempt")
    async def execute(self,request:ToolRequest):
        result=await self.service.delegate(DelegationRequest(parent_agent_id=f"worker-{request.task_id}",parent_task_id=request.task_id,parent_run_id=request.run_id,goal="Collect bounded platform evidence through skill, knowledge, and MCP channels",context={"dependency_steps":request.context.get("steps",{})},role=AgentRole.RESEARCHER,toolsets={"skills","memory","knowledge","mcp"},skills=["coding/code-review"],spawn_depth=1))
        return ToolResult(status=ToolResultStatus.SUCCESS if result.status.value=="COMPLETED" else ToolResultStatus.FAILURE,output=result.model_dump(mode="json"),artifacts=result.artifacts)


class PlatformGoalService:
    def __init__(self,db,planner,registry): self.db=db; self.planner=planner; self.registry=registry
    async def submit(self,objective:str,context:dict,project_id:str)->GoalSubmissionResult:
        goal=Goal(project_id=project_id,objective=objective,success_criteria=["all planned tasks pass validator"],allowed_capabilities=list(ScriptedPlatformPlanner.CAPABILITIES),metadata={"platform":"agent-foundation"})
        plan=await PlanExecutionService(self.db,self.planner,self.registry).execute_goal(goal,context=context)
        refs=[]
        for step in plan.steps:
            if step.task_id:
                refs.extend(run.id for run in RunRepository(self.db).list_for_task(step.task_id))
        if plan.status.value=="COMPLETED": EventStore(self.db).append(EventType.GOAL_COMPLETED,payload={"goal_id":goal.id,"plan_id":plan.id,"run_refs":refs})
        return GoalSubmissionResult(goal_id=goal.id,plan_id=plan.id,status=plan.status.value,run_refs=refs,summary=f"Goal {plan.status.value}: {len(plan.steps)} plan steps, {len(refs)} worker runs")


class OfflineAgentPlatform:
    def __init__(self,db,project_root:Path,memory_root:Path): self.db=db; self.project_root=project_root.resolve(); self.memory_root=memory_root.resolve(); self.mcp=MCPManager(); self.root:RootAgentService|None=None; self.registry=ToolRegistry()

    @classmethod
    async def create(cls,db,project_root:Path,memory_root:Path|None=None):
        db.init_db(); self=cls(db,project_root,memory_root or (project_root/".odys"/"memory"))
        project_repo=ProjectRepository(db); project=project_repo.get_by_name(f"agent-platform:{self.project_root}")
        if project is None: project=project_repo.create(Project(name=f"agent-platform:{self.project_root}",type="agent-platform",root_path=str(self.project_root)))
        bundled_root=Path(__file__).resolve().parents[3]/".odys"/"skills"
        skills=SkillRegistry([self.project_root/".odys"/"skills",bundled_root,Path.home()/".odys"/"skills"])
        memory=BuiltinMemoryProvider(self.memory_root)
        knowledge=LocalKnowledgeProvider(self.project_root)
        for action in ("list","view"): self.registry.register(_SkillsTool(skills,db,action))
        for action in ("search","open"): self.registry.register(_KnowledgeTool(knowledge,db,action))
        for action in ("list","search","add"): self.registry.register(_MemoryTool(memory,action))
        infos=await self.mcp.connect(MCPServerConfig(name="offline",command=[sys.executable,"-m","lhas.mcp.fake_server"]))
        EventStore(db).append(EventType.MCP_SERVER_CONNECTED,payload={"server_name":"offline","transport":"stdio"})
        mcp_caps=set(register_mcp_tools(self.registry,self.mcp,infos))
        for info in infos: EventStore(db).append(EventType.MCP_TOOL_DISCOVERED,payload={"server_name":info.server_name,"capability":info.name,"origin":"mcp"})
        toolsets=ToolsetRegistry(self.registry); toolsets.extend("mcp",mcp_caps)

        async def child_handler(request:AgentRequest):
            traces=[]
            async def call(capability,args):
                result=await self.registry.resolve(capability).execute(ToolRequest(tool_call_id=new_id(),task_id=str(request.metadata["task_id"]),run_id=str(request.metadata["run_id"]),attempt_id=str(request.metadata["attempt_id"]),capability=capability,arguments=args,context=request.context,metadata={"role":request.role.value}))
                traces.append({"capability":capability,"status":result.status.value,"error_type":result.error_type})
                return result
            skill=await call("skills.view",{"name":"coding/code-review"})
            hits=await call("knowledge.search",{"query":"Odys agent runtime architecture"})
            echo=await call("mcp.offline.echo",{"text":"offline MCP evidence"})
            return AgentResult(status=AgentStatus.COMPLETED,final_output=f"Child evidence: skill={skill.status.value}; knowledge_hits={len(hits.output)}; mcp={echo.status.value}",completion_claim=True,turn_count=1,tool_call_count=3,safe_trace=traces,artifacts={"evidence_channels":["skill","knowledge","mcp"]})

        def kernel_factory(_request): return ScriptedAgentKernel(child_handler)
        profiles=AgentProfileRegistry()
        delegation=DelegationService(db,profiles,kernel_factory,toolsets.resolve)
        worker_kernel=ScriptedAgentKernel(lambda request: AgentResult(status=AgentStatus.COMPLETED,final_output=f"{request.role.value} completed bounded step",completion_claim=True,turn_count=1,tool_call_count=0))
        self.registry.register(_KernelTool("platform.prepare",worker_kernel,AgentRole.WORKER))
        self.registry.register(_DelegationTool(delegation))
        self.registry.register(_KernelTool("platform.finalize",worker_kernel,AgentRole.REVIEWER))
        simple_kernel=ScriptedAgentKernel(lambda request: AgentResult(status=AgentStatus.COMPLETED,final_output=f"Odys offline: {request.objective[:500]}",completion_claim=True,turn_count=1))
        goal_service=PlatformGoalService(db,ScriptedPlatformPlanner(),self.registry)
        self.root=RootAgentService(db,simple_kernel,goal_service,SessionRepository(db),memory,skills,self.project_root,ContextAssembler())
        self.project=project; self.skills=skills; self.memory=memory; self.knowledge=knowledge; self.toolsets=toolsets; self.delegation=delegation
        return self

    async def close(self): await self.mcp.close_all()
