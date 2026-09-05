import asyncio
from lhas.domain.models import Project
from lhas.persistence.repositories import ProjectRepository
from lhas.planning.models import Goal, CapabilitySpec, PlanStatus
from lhas.planning.planner import DeterministicPlanner
from lhas.planning.service import PlanExecutionService
from lhas.tools.fakes import FakeTool
from lhas.tools.registry import ToolRegistry
from lhas.persistence.planning_repositories import GoalRepository, PlanRepository
from lhas import HARNESS_VERSION
from lhas.persistence.event_store import EventStore
from lhas.domain.enums import EventType
from lhas.persistence.phaseb_repos import FailureReportRepository, RecoveryActionRepository, ValidationResultRepository

def test_planner_and_tool_execution(db):
    project=Project(name="planning-domain")
    ProjectRepository(db).create(project)
    specs=[CapabilitySpec(name=n,description=n) for n in ("repo.search","repo.read","code.inspect","code.patch","verify.run")]
    reg=ToolRegistry()
    for s in specs: reg.register(FakeTool(s))
    goal=Goal(project_id=project.id,objective="propose issue",allowed_capabilities=[s.name for s in specs],metadata={"plan_steps":[s.name for s in specs]})
    plan=asyncio.run(PlanExecutionService(db,DeterministicPlanner(),reg).execute_goal(goal,experiment_id="exp-test"))
    assert plan.status == PlanStatus.COMPLETED
    assert [s.capability for s in plan.steps] == [s.name for s in specs]
    assert all(s.task_id for s in plan.steps)

def test_unknown_capability_rejected():
    goal=Goal(project_id="p",objective="x",allowed_capabilities=["missing"],metadata={"plan_steps":["missing"]})
    try: asyncio.run(DeterministicPlanner().create_plan(goal=goal,capabilities=[],context={}))
    except ValueError as exc: assert "unknown capability" in str(exc)
    else: raise AssertionError("unknown capability must fail explicitly")

def test_human_approval_blocks_execution(db):
    project=Project(name="approval-domain"); ProjectRepository(db).create(project)
    spec=CapabilitySpec(name="code.patch",description="patch",side_effect=True,requires_human_approval=True)
    reg=ToolRegistry(); reg.register(FakeTool(spec))
    goal=Goal(project_id=project.id,objective="patch",allowed_capabilities=[spec.name],metadata={"plan_steps":[spec.name]})
    plan=asyncio.run(PlanExecutionService(db,DeterministicPlanner(),reg).execute_goal(goal))
    assert plan.status == PlanStatus.WAITING_FOR_HUMAN_APPROVAL
    resumed=asyncio.run(PlanExecutionService(db,DeterministicPlanner(),reg).resume_after_approval(plan.id,goal,plan.steps[0].id))
    assert resumed.status == PlanStatus.COMPLETED
    assert GoalRepository(db).get(goal.id).objective == goal.objective
    assert PlanRepository(db).get(resumed.id).status == PlanStatus.COMPLETED

def test_inter_step_dataflow_and_harness_version(db):
    project=Project(name="dataflow-domain"); ProjectRepository(db).create(project)
    seen=[]
    def first(req): return {"token":"step-one"}
    def second(req):
        seen.append(req.context); return {"received": req.context}
    a=CapabilitySpec(name="a",description="a"); b=CapabilitySpec(name="b",description="b")
    reg=ToolRegistry(); reg.register(FakeTool(a,first)); reg.register(FakeTool(b,second))
    goal=Goal(project_id=project.id,objective="flow",allowed_capabilities=["a","b"],metadata={"plan_steps":["a","b"]})
    plan=asyncio.run(PlanExecutionService(db,DeterministicPlanner(),reg).execute_goal(goal))
    assert plan.status == PlanStatus.COMPLETED, (seen, EventStore(db).list_all())
    assert seen and "steps" in seen[0], seen
    assert seen[0]["steps"][plan.steps[0].id]["output"] == {"token":"step-one"}
    from lhas.persistence.repositories import RunRepository
    rr=RunRepository(db)
    runs=[rr.list_for_task(s.task_id)[0] for s in plan.steps]
    assert all(r.harness_version == HARNESS_VERSION for r in runs)

def test_tool_failure_enters_recovery_runtime(db):
    project=Project(name="recovery-domain"); ProjectRepository(db).create(project)
    calls=[]
    def scripted(req):
        calls.append(req)
        return "" if len(calls)==1 else {"recovered": True}
    spec=CapabilitySpec(name="recover",description="recover")
    reg=ToolRegistry(); reg.register(FakeTool(spec,scripted))
    goal=Goal(project_id=project.id,objective="recover",allowed_capabilities=["recover"],metadata={"plan_steps":["recover"]})
    plan=asyncio.run(PlanExecutionService(db,DeterministicPlanner(),reg).execute_goal(goal))
    assert plan.status == PlanStatus.COMPLETED and len(calls)==2
    assert len(ValidationResultRepository(db).list_for_attempt(calls[0].attempt_id)) == 1
    assert FailureReportRepository(db).list_for_attempt(calls[0].attempt_id)
    assert RecoveryActionRepository(db).list_for_attempt(calls[0].attempt_id)
    types=[e.event_type for e in EventStore(db).list_all()]
    assert EventType.FAILURE_CLASSIFIED in types and EventType.RECOVERY_STARTED in types

def test_true_approval_resume_same_plan(db):
    project=Project(name="approval-resume"); ProjectRepository(db).create(project)
    counts={n:0 for n in ("one","two","three")}
    def handler(name):
        def run(req): counts[name]+=1; return name
        return run
    reg=ToolRegistry()
    for name,approval in (("one",False),("two",True),("three",False)):
        reg.register(FakeTool(CapabilitySpec(name=name,description=name,side_effect=approval,requires_human_approval=approval),handler(name)))
    goal=Goal(project_id=project.id,objective="approval",allowed_capabilities=["one","two","three"],metadata={"plan_steps":["one","two","three"]})
    svc=PlanExecutionService(db,DeterministicPlanner(),reg)
    waiting=asyncio.run(svc.execute_goal(goal)); assert waiting.status == PlanStatus.WAITING_FOR_HUMAN_APPROVAL and counts == {"one":1,"two":0,"three":0}
    resumed=asyncio.run(svc.resume_after_approval(waiting.id,goal,waiting.steps[1].id))
    assert resumed.id == waiting.id and resumed.status == PlanStatus.COMPLETED and counts == {"one":1,"two":1,"three":1}

def test_empty_goal_metadata_roundtrip(db):
    project=Project(name="empty-metadata"); ProjectRepository(db).create(project)
    goal=Goal(project_id=project.id,objective="empty")
    GoalRepository(db).create(goal)
    assert GoalRepository(db).get(goal.id).metadata == {}

def test_structured_step_context(db):
    project=Project(name="structured-context"); ProjectRepository(db).create(project)
    seen=[]
    def a(req): return {"urls":["a"]}
    def b(req):
        seen.append(req.context); return "ok"
    # FakeTool result preserves structured fields through ToolResult.
    from lhas.tools.protocol import ToolResult, ToolResultStatus
    class Structured(FakeTool):
        async def execute(self, req):
            return ToolResult(status=ToolResultStatus.SUCCESS, output={"urls":["a"]}, artifacts={"snapshot":"x"}, usage={"requests":1})
    sa=CapabilitySpec(name="a",description="a"); sb=CapabilitySpec(name="b",description="b")
    reg=ToolRegistry(); reg.register(Structured(sa)); reg.register(FakeTool(sb,b))
    goal=Goal(project_id=project.id,objective="structured",allowed_capabilities=["a","b"],metadata={"plan_steps":["a","b"]})
    plan=asyncio.run(PlanExecutionService(db,DeterministicPlanner(),reg).execute_goal(goal))
    assert plan.status == PlanStatus.COMPLETED
    previous=seen[0]["steps"][plan.steps[0].id]
    assert previous["output"] == {"urls":["a"]} and previous["artifacts"] == {"snapshot":"x"} and previous["usage"] == {"requests":1}

def test_approval_is_step_scoped_for_duplicate_capability(db):
    project=Project(name="approval-duplicate"); ProjectRepository(db).create(project)
    count=[0]
    def handler(req): count[0]+=1; return "ok"
    spec=CapabilitySpec(name="mutate",description="mutate",side_effect=True,requires_human_approval=True)
    reg=ToolRegistry(); reg.register(FakeTool(spec,handler))
    goal=Goal(project_id=project.id,objective="duplicate",allowed_capabilities=["mutate"],metadata={"plan_steps":["mutate","mutate"]})
    svc=PlanExecutionService(db,DeterministicPlanner(),reg)
    waiting=asyncio.run(svc.execute_goal(goal)); assert waiting.status == PlanStatus.WAITING_FOR_HUMAN_APPROVAL and count[0] == 0
    resumed=asyncio.run(svc.resume_after_approval(waiting.id,goal,waiting.steps[0].id))
    assert resumed.status == PlanStatus.WAITING_FOR_HUMAN_APPROVAL and count[0] == 1
    assert asyncio.run(svc.resume_after_approval(resumed.id,goal,resumed.steps[1].id)).status == PlanStatus.COMPLETED and count[0] == 2
