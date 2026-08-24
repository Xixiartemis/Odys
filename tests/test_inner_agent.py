import asyncio
from lhas.domain.models import Project
from lhas.persistence.repositories import ProjectRepository, TaskRepository
from lhas.persistence.database import Database
from lhas.orchestrator_v2 import RecoveringOrchestrator
from lhas.task_service import create_task
from lhas.validation import NeverPassValidator
from lhas.inner_agent.models import InnerAgentResult, InnerAgentStatus, InnerAgentRequest
from lhas.inner_agent.scripted import ScriptedInnerAgentBackend
from lhas.inner_agent.executor import InnerAgentExecutor
from lhas.tools.registry import ToolRegistry
from lhas.tools.fakes import FakeTool
from lhas.planning.models import CapabilitySpec
from lhas.inner_agent.tool_adapter import allowed_tools

def test_scripted_inner_executor_one_outer_attempt(db):
    project=Project(name="inner-scripted"); ProjectRepository(db).create(project)
    task=create_task(db,project_id=project.id,title="inner",objective="subgoal",acceptance_criteria=[] ,max_attempts=1)
    backend=ScriptedInnerAgentBackend(InnerAgentResult(status=InnerAgentStatus.SUCCESS,final_output="candidate answer",completion_claim=True))
    run=asyncio.run(RecoveringOrchestrator(db,executor_factory=lambda:InnerAgentExecutor(backend),validator=NeverPassValidator(),harness_version="HV-0.7").execute_task(task.id))
    assert backend.invocations==1 and run.status.value=="ESCALATED"

def test_final_claim_does_not_bypass_validator(db):
    project=Project(name="inner-claim"); ProjectRepository(db).create(project)
    task=create_task(db,project_id=project.id,title="claim",objective="do",acceptance_criteria=[],max_attempts=1)
    backend=ScriptedInnerAgentBackend(InnerAgentResult(status=InnerAgentStatus.SUCCESS,final_output="任务已经完成",completion_claim=True))
    run=asyncio.run(RecoveringOrchestrator(db,executor_factory=lambda:InnerAgentExecutor(backend),validator=NeverPassValidator(),harness_version="HV-0.7").execute_task(task.id))
    assert run.status.value=="ESCALATED"

def test_tool_allowlist_and_side_effect_filter():
    reg=ToolRegistry(); reg.register(FakeTool(CapabilitySpec(name="safe.a",description="a"))); reg.register(FakeTool(CapabilitySpec(name="secret.c",description="c"))); reg.register(FakeTool(CapabilitySpec(name="write.file",description="write",side_effect=True,requires_human_approval=True)))
    request=InnerAgentRequest(task_id="t",run_id="r",attempt_id="a",objective="x",allowed_capabilities=["safe.a","write.file"])
    # Adapter is tested only when optional SDK is installed.
    try: tools,filtered=allowed_tools(reg,request)
    except RuntimeError: return
    assert [t.name for t in tools]==["safe.a"] and set(filtered)=={"write.file"}

def test_scripted_failure_maps_to_execution_failure():
    backend=ScriptedInnerAgentBackend(InnerAgentResult(status=InnerAgentStatus.FAILURE,error_type="AGENT_TURN_LIMIT",error_message="limit"))
    from lhas.executors.protocol import ExecutionRequest
    result=asyncio.run(InnerAgentExecutor(backend).execute(ExecutionRequest(task_id="t",run_id="r",attempt_id="a",attempt_number=1,task={"objective":"x"})))
    assert result.status.value=="FAILURE" and result.error_type=="AGENT_TURN_LIMIT"
