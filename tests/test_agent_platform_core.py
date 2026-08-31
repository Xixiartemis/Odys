import asyncio

import pytest
from pydantic import ValidationError

from lhas.agent import (
    AgentProfileRegistry,
    AgentRequest,
    AgentResult,
    AgentRole,
    AgentStatus,
    ContextAssembler,
    ContextPriority,
    ContextSource,
    ProviderProfile,
    ProviderRegistry,
    ScriptedAgentKernel,
    Toolset,
    ToolsetRegistry,
    WorkerAgentKernelAdapter,
)
from lhas.agent.planner import ScriptedPlatformPlanner
from lhas.agent.root import RootAgentService, RootRoute
from lhas.domain.enums import EventType, ExecutionStatus, FailureLevel
from lhas.executors.protocol import ExecutionResult
from lhas.planning.models import CapabilitySpec, Goal
from lhas.tools.fakes import FakeTool
from lhas.tools.registry import ToolRegistry


def test_agent_request_contract_has_platform_fields():
    request=AgentRequest(agent_id="worker-1",role=AgentRole.WORKER,objective="do work",context={"safe":True},messages=[],allowed_capabilities={"workspace.read"},toolsets={"workspace"},skill_refs=["coding/bug-fix"],memory_scope=["memory"],knowledge_scope=["project"],parent_agent_id="root-1",parent_run_id="run-1",metadata={"lineage":"safe"})
    assert request.role is AgentRole.WORKER
    assert request.parent_run_id=="run-1"
    assert request.budget.max_turns==20


def test_agent_request_bounds_messages():
    with pytest.raises(ValidationError,match="bounded"):
        AgentRequest(agent_id="x",role=AgentRole.ROOT,objective="x",messages=[{"role":"user"}]*101)


def test_agent_result_contract_excludes_hidden_reasoning():
    result=AgentResult(status=AgentStatus.COMPLETED,final_output="done",completion_claim=True,safe_trace=[{"capability":"x"}])
    assert "reasoning" not in result.model_dump()
    with pytest.raises(ValidationError):
        AgentResult(status=AgentStatus.COMPLETED,hidden_chain_of_thought="secret")


def test_scripted_agent_kernel_run_cancel_status():
    kernel=ScriptedAgentKernel(lambda request: AgentResult(status=AgentStatus.COMPLETED,final_output=request.objective,completion_claim=True))
    request=AgentRequest(agent_id="a",role=AgentRole.ROOT,objective="hello")
    result=asyncio.run(kernel.run(request))
    assert result.final_output=="hello"
    assert asyncio.run(kernel.status("a"))["status"]=="COMPLETED"
    asyncio.run(kernel.cancel("a"))
    assert asyncio.run(kernel.status("a"))["status"]=="CANCELLED"


def test_worker_adapter_preserves_existing_executor_path():
    class Executor:
        name="existing-inner-agent"
        async def execute(self,request): return ExecutionResult(status=ExecutionStatus.SUCCESS,output="adapted",raw={"turn_count":2,"tool_call_count":3,"safe_trace":[{"status":"SUCCESS"}]})
        async def resume(self,request): return await self.execute(request)
        async def cancel(self,run_id): return None
        async def status(self,run_id): return {"run_id":run_id}
    request=AgentRequest(agent_id="worker",role=AgentRole.WORKER,objective="work",metadata={"task_id":"t","run_id":"r","attempt_id":"a"})
    result=asyncio.run(WorkerAgentKernelAdapter(Executor()).run(request))
    assert result.status is AgentStatus.COMPLETED and result.turn_count==2 and result.tool_call_count==3


def test_agent_profiles_cover_all_roles():
    registry=AgentProfileRegistry()
    assert {profile.role for profile in registry.list()}==set(AgentRole)
    assert registry.for_role(AgentRole.ROOT).memory_permissions=={"read","write"}
    assert registry.for_role(AgentRole.RESEARCHER).memory_permissions=={"read"}


def test_role_permissions_allow_bounded_worker_child():
    registry=AgentProfileRegistry()
    registry.validate_child(registry.for_role(AgentRole.WORKER),registry.for_role(AgentRole.RESEARCHER),spawn_depth=1)


def test_role_permissions_reject_spawn_depth():
    registry=AgentProfileRegistry()
    with pytest.raises(PermissionError,match="MAX_SPAWN_DEPTH"):
        registry.validate_child(registry.for_role(AgentRole.WORKER),registry.for_role(AgentRole.RESEARCHER),spawn_depth=2)


def test_role_permissions_reject_capability_escalation():
    registry=AgentProfileRegistry(); parent=registry.for_role(AgentRole.PLANNER); child=registry.for_role(AgentRole.REVIEWER)
    with pytest.raises(PermissionError,match="TOOLSETS"):
        registry.validate_child(parent,child,spawn_depth=1)


@pytest.mark.parametrize("text",["hello","what is the current status?"])
def test_root_simple_routing(text):
    assert RootAgentService.classify(text) is RootRoute.SIMPLE_INTERACTION


@pytest.mark.parametrize("text",["Implement a durable subsystem","修复这个多步骤平台问题"])
def test_root_long_goal_routing(text):
    assert RootAgentService.classify(text) is RootRoute.LONG_RUNNING_GOAL


def test_scripted_planner_emits_schema_valid_three_step_graph():
    planner=ScriptedPlatformPlanner(); goal=Goal(project_id="p",objective="build platform")
    specs=[CapabilitySpec(name=name) for name in planner.CAPABILITIES]
    plan=asyncio.run(planner.create_plan(goal=goal,capabilities=specs))
    assert len(plan.steps)==3
    assert plan.steps[1].depends_on==[plan.steps[0].id]
    assert plan.steps[2].suggested_role=="REVIEWER"
    assert plan.steps[1].required_capabilities==["platform.delegate"]
    assert plan.steps[1].optional_skill_refs


def test_planner_rejects_missing_runtime_capability():
    with pytest.raises(ValueError,match="missing"):
        asyncio.run(ScriptedPlatformPlanner().create_plan(goal=Goal(project_id="p",objective="x"),capabilities=[]))


def test_context_assembler_prioritizes_and_bounds():
    result=ContextAssembler().assemble([ContextSource("low","z"*500,ContextPriority.LOW,500),ContextSource("required",{"x":"y"},ContextPriority.REQUIRED,100)],budget_chars=120)
    assert "required" in result.sections
    assert result.chars_used<=120
    assert "low" in result.truncated_sections


def test_context_assembler_rejects_duplicate_sections():
    with pytest.raises(ValueError,match="duplicate"):
        ContextAssembler().assemble([ContextSource("x",1),ContextSource("x",2)],budget_chars=100)


def test_provider_registry_contains_no_credential_field():
    profile=ProviderProfile(provider_name="mimo",model="model",api_mode="responses",base_url="https://example.invalid")
    registry=ProviderRegistry(); registry.register("default",profile)
    assert registry.resolve("default").model=="model"
    with pytest.raises(ValidationError):
        ProviderProfile(provider_name="x",model="x",api_key="secret")


def test_toolset_registry_expands_only_registered_capabilities():
    tools=ToolRegistry(); tools.register(FakeTool(CapabilitySpec(name="workspace.read")))
    registry=ToolsetRegistry(tools)
    assert registry.resolve({"workspace"})=={"workspace.read"}
    assert {item.name for item in registry.list()}>={"workspace","terminal","skills","memory","knowledge","mcp"}


def test_toolset_registry_detects_cycles():
    registry=ToolsetRegistry(ToolRegistry(),[Toolset(name="a",includes={"b"}),Toolset(name="b",includes={"a"})])
    with pytest.raises(ValueError,match="acyclic"):
        registry.resolve({"a"})


def test_failure_hierarchy_contract_is_complete():
    assert [level.value for level in FailureLevel]==["TOOL","ATTEMPT","TASK","PLAN","GOAL"]


def test_failure_router_persists_safe_routing_boundary():
    from lhas.agent.failure import FailureRouter,FailureSignal
    from lhas.persistence.database import Database
    from lhas.persistence.event_store import EventStore
    db=Database(":memory:"); db.init_db(); route=FailureRouter(db).route(FailureSignal(level=FailureLevel.PLAN,source="planner",error_type="SCHEMA_INVALID",summary="bounded"))
    assert route=="DYNAMIC_REPLAN_BOUNDARY"
    event=EventStore(db).list_all()[-1]
    assert event.event_type is EventType.FAILURE_ROUTED and "summary" not in event.payload
    db.close()
