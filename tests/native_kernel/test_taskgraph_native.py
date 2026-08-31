import asyncio

from lhas.native.completion import CompletionAuthority
from lhas.native.executor import NativeAgentExecutor
from lhas.native.kernel import NativeAgentKernel
from lhas.native.models import ProviderResponse
from lhas.native.provider import ScriptedProviderAdapter
from lhas.native.tools import NativeToolDispatcher
from lhas.planning.models import CapabilitySpec, Goal
from lhas.planning.planner import DeterministicPlanner
from lhas.planning.service import PlanExecutionService
from lhas.tools.protocol import ToolResult, ToolResultStatus
from lhas.tools.registry import ToolRegistry
from lhas.validation import AlwaysPassValidator


class DeclaredCapability:
    capability = CapabilitySpec(name="native.work", description="execute one native taskgraph node")

    async def execute(self, request):
        return ToolResult(status=ToolResultStatus.SUCCESS, output={})


def test_goal_plan_taskgraph_active_node_flows_into_native_kernel(db, project):
    registry = ToolRegistry()
    registry.register(DeclaredCapability())
    seen = []

    def executor_factory(step):
        provider = ScriptedProviderAdapter([
            lambda context: (seen.append(context.sections["taskgraph"]) or ProviderResponse(content="node accepted", completion_claim=True)),
        ])
        dispatcher = NativeToolDispatcher(db=db, registry=registry, allowed_capabilities=set(), allowed_side_effect_capabilities=set())
        kernel = NativeAgentKernel(db=db, provider=provider, dispatcher=dispatcher, completion_authority=CompletionAuthority(db=db, validator=AlwaysPassValidator()))
        return NativeAgentExecutor(kernel, allowed_capabilities=set(), allowed_side_effect_capabilities=set(), max_turns=2)

    goal = Goal(project_id=project.id, objective="complete canonical graph", success_criteria=["accepted"], allowed_capabilities=["native.work"])
    service = PlanExecutionService(db, DeterministicPlanner(), registry, agent_executor_factory=executor_factory)
    plan = asyncio.run(service.execute_goal(goal))
    assert plan.status.value == "COMPLETED"
    assert len(plan.steps) == 1
    assert seen[0]["plan_id"] == plan.id
    assert seen[0]["active_node"] == plan.steps[0].id
    assert seen[0]["completed_nodes"] == [] and seen[0]["pending_nodes"] == []
