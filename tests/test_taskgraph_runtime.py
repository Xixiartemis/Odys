import asyncio
from lhas.domain.models import Project
from lhas.domain.enums import EventType
from lhas.persistence.repositories import ProjectRepository
from lhas.persistence.event_store import EventStore
from lhas.planning.models import Goal, Plan, PlanMode, PlanStep, PlanStatus, PlanStepStatus, CapabilitySpec
from lhas.planning.scheduler import TaskGraphScheduler, build_step_dependency_context
from lhas.planning.service import PlanExecutionService
from lhas.tools.fakes import FakeTool
from lhas.tools.registry import ToolRegistry
from lhas.tools.protocol import ToolResult, ToolResultStatus
from tests.helpers import make_test_capability_definition, make_test_capability_registry

class FixedPlanner:
    def __init__(self, plan): self.plan=plan
    async def create_plan(self, **kwargs): return self.plan

def graph_plan(goal, gated=False):
    a=PlanStep(id="a",title="A",objective="A",capability="a")
    b=PlanStep(id="b",title="B",objective="B",capability="b",depends_on=["a"])
    c=PlanStep(id="c",title="C",objective="C",capability="c",depends_on=["a"])
    d=PlanStep(id="d",title="D",objective="D",capability="d",depends_on=["b","c"])
    return Plan(goal_id=goal.id,mode=PlanMode.SIMPLE_DEPENDENCY,status=PlanStatus.DRAFT,steps=[a,b,c,d])

def test_scheduler_diamond_order_and_blocking():
    p=Plan(goal_id="g",mode=PlanMode.SIMPLE_DEPENDENCY,steps=[PlanStep(id="a",title="a",objective="a",capability="a"),PlanStep(id="b",title="b",objective="b",capability="b",depends_on=["a"])])
    s=TaskGraphScheduler().calculate(p); assert [x.id for x in s.ready_steps]==["a"]
    p.steps[0].status=PlanStepStatus.FAILED; s=TaskGraphScheduler().calculate(p); assert [x.id for x in s.blocked_steps]==["b"]

def test_dependency_graph_independent_branch_continues(db):
    project=Project(name="graph-failure"); ProjectRepository(db).create(project); goal=Goal(project_id=project.id,objective="graph")
    plan=graph_plan(goal)
    def fail(req): return ToolResult(status=ToolResultStatus.FAILURE,error_type="TOOL_ERROR",error_message="bad")
    reg=ToolRegistry()
    for name in "abcd": reg.register(FakeTool(CapabilitySpec(name=name,description=name), fail if name=="b" else (lambda r,n=name:n)))
    defs=[make_test_capability_definition(name, output_schema={}) for name in "abcd"]
    cap_reg, contract = make_test_capability_registry(reg, defs)
    result=asyncio.run(PlanExecutionService(db,FixedPlanner(plan),reg,capability_registry=cap_reg,tool_contract=contract).execute_goal(goal)); states={s.id:s.status for s in result.steps}
    assert result.status==PlanStatus.FAILED and states["a"]==PlanStepStatus.VERIFIED and states["b"]==PlanStepStatus.FAILED and states["d"]==PlanStepStatus.BLOCKED and states["c"]==PlanStepStatus.VERIFIED

def test_dependency_approval_resume_same_plan(db):
    project=Project(name="graph-approval"); ProjectRepository(db).create(project); goal=Goal(project_id=project.id,objective="graph")
    a=PlanStep(id="a",title="a",objective="a",capability="a"); b=PlanStep(id="b",title="b",objective="b",capability="b",depends_on=["a"]); c=PlanStep(id="c",title="c",objective="c",capability="c",depends_on=["a"]); d=PlanStep(id="d",title="d",objective="d",capability="d",depends_on=["b"])
    plan=Plan(goal_id=goal.id,mode=PlanMode.SIMPLE_DEPENDENCY,steps=[a,b,c,d]); reg=ToolRegistry()
    counts={n:0 for n in "abcd"}
    def counted(name):
        def run(r): counts[name]+=1; return name
        return run
    for name,gated in (("a",False),("b",True),("c",False),("d",False)): reg.register(FakeTool(CapabilitySpec(name=name,description=name,side_effect=gated,requires_human_approval=gated),counted(name)))
    defs=[make_test_capability_definition(name, output_schema={}, side_effect=(name=="b"), requires_human_approval=(name=="b")) for name in "abcd"]
    cap_reg, contract = make_test_capability_registry(reg, defs)
    svc=PlanExecutionService(db,FixedPlanner(plan),reg,capability_registry=cap_reg,tool_contract=contract); waiting=asyncio.run(svc.execute_goal(goal)); assert waiting.status==PlanStatus.WAITING_FOR_HUMAN_APPROVAL and waiting.steps[2].status==PlanStepStatus.VERIFIED
    resumed=asyncio.run(svc.resume_after_approval(waiting.id,goal,"b")); assert resumed.id==waiting.id and resumed.status==PlanStatus.COMPLETED and resumed.steps[0].status==PlanStepStatus.VERIFIED and counts=={"a":1,"b":1,"c":1,"d":1}

def test_true_diamond_order_and_context_persistence(db):
    project=Project(name="diamond-context"); ProjectRepository(db).create(project); goal=Goal(project_id=project.id,objective="diamond")
    steps=[PlanStep(id="a",title="a",objective="a",capability="a"),PlanStep(id="b",title="b",objective="b",capability="b",depends_on=["a"]),PlanStep(id="c",title="c",objective="c",capability="c",depends_on=["a"]),PlanStep(id="d",title="d",objective="d",capability="d",depends_on=["b","c"])]
    plan=Plan(goal_id=goal.id,mode=PlanMode.SIMPLE_DEPENDENCY,steps=steps); log=[]; contexts={}
    def tool(name):
        def run(req): log.append(name); contexts[name]=req.context; return name
        return run
    reg=ToolRegistry()
    for name in "abcd": reg.register(FakeTool(CapabilitySpec(name=name,description=name),tool(name)))
    defs=[make_test_capability_definition(name, output_schema={}) for name in "abcd"]
    cap_reg, contract = make_test_capability_registry(reg, defs)
    result=asyncio.run(PlanExecutionService(db,FixedPlanner(plan),reg,capability_registry=cap_reg,tool_contract=contract).execute_goal(goal)); assert [x for x in log]==["a","b","c","d"]
    assert "a" in contexts["b"]["steps"] and "c" not in contexts["b"]["steps"]
    assert "c" in contexts["d"]["steps"] and "b" in contexts["d"]["steps"]
    persisted={s.id:s.execution_context for s in result.steps}; assert "c" not in persisted["b"]["steps"] and "a" in persisted["b"]["steps"]

def test_dependency_context_isolation():
    p=Plan(goal_id="g",mode=PlanMode.SIMPLE_DEPENDENCY,steps=[PlanStep(id="a",title="a",objective="a",capability="a"),PlanStep(id="c",title="c",objective="c",capability="c"),PlanStep(id="b",title="b",objective="b",capability="b",depends_on=["a"])])
    ctx={"runtime":{},"steps":{"a":{"capability":"a","output":1},"c":{"capability":"c","output":2}}}; out=build_step_dependency_context(p,p.steps[2],ctx); assert "a" in out["steps"] and "c" not in out["steps"]

def test_graph_events(db):
    project=Project(name="graph-events"); ProjectRepository(db).create(project); goal=Goal(project_id=project.id,objective="x"); step=PlanStep(id="a",title="a",objective="a",capability="a"); plan=Plan(goal_id=goal.id,mode=PlanMode.SIMPLE_DEPENDENCY,steps=[step]); reg=ToolRegistry(); reg.register(FakeTool(CapabilitySpec(name="a",description="a")))
    asyncio.run(PlanExecutionService(db,FixedPlanner(plan),reg).execute_goal(goal)); types=[e.event_type for e in EventStore(db).list_all()]; assert EventType.PLAN_STEP_READY in types

def test_gated_branch_does_not_pause_independent_descendant(db):
    project=Project(name="approval-descendant"); ProjectRepository(db).create(project); goal=Goal(project_id=project.id,objective="x")
    steps=[PlanStep(id="a",title="a",objective="a",capability="a"),PlanStep(id="b",title="b",objective="b",capability="b",depends_on=["a"]),PlanStep(id="c",title="c",objective="c",capability="c",depends_on=["a"]),PlanStep(id="d",title="d",objective="d",capability="d",depends_on=["b"]),PlanStep(id="e",title="e",objective="e",capability="e",depends_on=["c"])]
    plan=Plan(goal_id=goal.id,mode=PlanMode.SIMPLE_DEPENDENCY,steps=steps); counts={x:0 for x in "abcde"}
    def tool(name):
        def run(req): counts[name]+=1; return name
        return run
    reg=ToolRegistry()
    for name,gated in (("a",False),("b",True),("c",False),("d",False),("e",False)):
        reg.register(FakeTool(CapabilitySpec(name=name,description=name,side_effect=gated,requires_human_approval=gated),tool(name)))
    defs=[make_test_capability_definition(name, output_schema={}, side_effect=(name=="b"), requires_human_approval=(name=="b")) for name in "abcde"]
    cap_reg, contract = make_test_capability_registry(reg, defs)
    svc=PlanExecutionService(db,FixedPlanner(plan),reg,capability_registry=cap_reg,tool_contract=contract); first=asyncio.run(svc.execute_goal(goal)); assert first.status==PlanStatus.WAITING_FOR_HUMAN_APPROVAL and counts=={"a":1,"b":0,"c":1,"d":0,"e":1}
    events=EventStore(db).list_all(); assert sum(e.event_type==EventType.HUMAN_APPROVAL_REQUIRED for e in events)==1
    assert sum(e.event_type==EventType.PLAN_STEP_READY and e.payload.get("step_id")=="b" for e in events)==1
    resumed=asyncio.run(svc.resume_after_approval(first.id,goal,"b")); assert resumed.id==first.id and resumed.status==PlanStatus.COMPLETED and counts=={"a":1,"b":1,"c":1,"d":1,"e":1}
