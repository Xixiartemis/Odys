import asyncio

from lhas.domain.models import Project
from lhas.executors.protocol import ExecutionRequest
from lhas.domain.enums import ExecutionStatus
from lhas.native.models import ReplanSignal
from lhas.persistence.planning_repositories import PlanRepository
from lhas.persistence.repositories import ProjectRepository
from lhas.planning.models import CapabilitySpec, Goal, Plan, PlanMode, PlanStep, PlanStepStatus
from lhas.planning.replan import MacroReplanService
from lhas.planning.scheduler import TaskGraphScheduler
from lhas.planning.service import _TaskGraphAgentExecutor


class RevisedPlanner:
    async def create_plan(self, *, goal, capabilities, context=None):
        return Plan(
            id="proposal",
            goal_id=goal.id,
            mode=PlanMode.SIMPLE_DEPENDENCY,
            status="READY",
            steps=[
                PlanStep(id="early-proposal", title="prepare", objective="prepare", capability="prepare"),
                PlanStep(id="alternate", title="alternate", objective="use alternate path", capability="alternate", depends_on=["early-proposal"]),
            ],
        )


class UnchangedPlanner:
    async def create_plan(self, *, goal, capabilities, context=None):
        current = context["current_plan"]
        return Plan.model_validate(current)


def test_macro_replan_preserves_completed_work_and_changes_version(db):
    project = ProjectRepository(db).create(Project(name="replan", type="test"))
    goal = Goal(id="goal-replan", project_id=project.id, objective="finish workflow", success_criteria=["valid"], allowed_capabilities=["prepare", "assume", "alternate"])
    plan = Plan(id="plan-replan", goal_id=goal.id, mode=PlanMode.SIMPLE_DEPENDENCY, status="RUNNING", steps=[
        PlanStep(id="early", title="prepare", objective="prepare", capability="prepare", status=PlanStepStatus.COMPLETED, output={"done": True}),
        PlanStep(id="assumption", title="assumption", objective="assumption", capability="assume", depends_on=["early"]),
        PlanStep(id="independent", title="independent", objective="independent", capability="prepare", status=PlanStepStatus.VERIFIED, output={"done": True}),
    ])
    PlanRepository(db).create(plan)
    signal = ReplanSignal(task_id="task", run_id="run", attempt_id="attempt", reason="WRONG_ASSUMPTION", failed_node_id="assumption", evidence={"observed": "not A"})
    result = asyncio.run(MacroReplanService(db, RevisedPlanner()).consume(goal=goal, plan=plan, signals=[signal], context={"capabilities": [CapabilitySpec(name="prepare"), CapabilitySpec(name="alternate"), CapabilitySpec(name="assume")]}))
    assert result.accepted is True
    assert plan.version == "P-0.1-r1"
    assert plan.replan_count == 1
    assert next(step for step in plan.steps if step.id == "early").status is PlanStepStatus.VERIFIED
    assert next(step for step in plan.steps if step.id == "assumption").status is PlanStepStatus.STALE
    assert next(step for step in plan.steps if step.id == "independent").status is PlanStepStatus.VERIFIED
    assert "independent" not in result.invalidated_step_ids
    assert next(step for step in plan.steps if step.id == "alternate").status is PlanStepStatus.PENDING
    ready_ids = {step.id for step in TaskGraphScheduler().calculate(plan).ready_steps}
    assert ready_ids == {"alternate"}


def test_stale_plan_worker_fails_closed(db):
    project = ProjectRepository(db).create(Project(name="stale", type="test"))
    goal = Goal(project_id=project.id, objective="stale")
    plan = Plan(id="plan-stale", goal_id=goal.id, status="READY", steps=[PlanStep(id="s", title="s", objective="s", capability="s")])
    PlanRepository(db).create(plan)
    step = plan.steps[0]
    plan.version = "P-0.1-r1"
    PlanRepository(db).update(plan)

    class Executor:
        async def execute(self, request):
            raise AssertionError("stale worker must not execute")
        async def cancel(self, run_id): pass
        async def status(self, run_id): return {}

    wrapper = _TaskGraphAgentExecutor(Executor(), Plan(id="plan-stale", goal_id=goal.id, status="READY", steps=[step]), step, db)
    request = ExecutionRequest(task_id="t", run_id="r", attempt_id="a", attempt_number=1, task={}, context={}, metadata={})
    result = asyncio.run(wrapper.execute(request))
    assert result.status is ExecutionStatus.FAILURE
    assert result.error_type == "STALE_PLAN"


def test_macro_replan_rejects_unchanged_strategy_with_typed_result(db):
    project = ProjectRepository(db).create(Project(name="no-change", type="test"))
    goal = Goal(project_id=project.id, objective="finish", allowed_capabilities=["assume"])
    plan = Plan(
        id="plan-no-change",
        goal_id=goal.id,
        mode=PlanMode.SIMPLE_DEPENDENCY,
        status="RUNNING",
        steps=[PlanStep(id="failed", title="assume", objective="assume", capability="assume")],
    )
    PlanRepository(db).create(plan)
    signal = ReplanSignal(
        task_id="task",
        run_id="run",
        attempt_id="attempt",
        reason="REPAIR_NO_PROGRESS",
        failed_node_id="failed",
    )

    result = asyncio.run(
        MacroReplanService(db, UnchangedPlanner()).consume(
            goal=goal,
            plan=plan,
            signals=[signal],
            context={"capabilities": [CapabilitySpec(name="assume")]},
        )
    )

    assert result.accepted is False
    assert result.error_type == "REPLAN_NO_CHANGE"
    assert result.old_strategy_fingerprint == result.new_strategy_fingerprint

