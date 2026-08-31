import asyncio

from lhas.agent.models import AgentBudget, AgentRequest, AgentRole, AgentStatus
from lhas.domain.models import Attempt, Project, Run
from lhas.native.completion import CompletionAuthority
from lhas.native.kernel import NativeAgentKernel
from lhas.native.models import ExecutionSnapshot, ProviderResponse, ProviderToolCall, RuntimeTarget
from lhas.native.persistence import ReplanSignalRepository
from lhas.native.provider import ScriptedProviderAdapter
from lhas.native.runtime import RuntimeTargetController
from lhas.native.tools import NativeToolDispatcher
from lhas.persistence.event_store import EventStore
from lhas.persistence.planning_repositories import PlanRepository
from lhas.persistence.repositories import ProjectRepository, RunRepository, AttemptRepository
from lhas.planning.models import CapabilitySpec, Goal, Plan, PlanMode, PlanStep, PlanStatus, PlanStepStatus
from lhas.planning.replan import MacroReplanService
from lhas.planning.scheduler import TaskGraphScheduler
from lhas.planning.service import PlanExecutionService
from lhas.tools.fakes import FakeTool
from lhas.tools.protocol import ToolResult, ToolResultStatus
from lhas.tools.registry import ToolRegistry
from lhas.validation import AlwaysPassValidator


class _ReplanPlanner:
    def __init__(self, mode=PlanMode.LINEAR):
        self.mode = mode
        self.calls = []

    async def create_plan(self, *, goal, capabilities, context=None):
        context = context or {}
        revised = bool(context.get("replan_signals"))
        self.calls.append(revised)
        names = ["a", "d", "e"] if revised else ["a", "b", "c"]
        steps = []
        for index, name in enumerate(names):
            steps.append(PlanStep(
                id=f"{name}-proposal-{len(self.calls)}", title=name.upper(), objective=name,
                capability=name, depends_on=[steps[-1].id] if steps and self.mode is PlanMode.LINEAR else [],
                inputs={"replan_reason": "INVALID_ASSUMPTION"} if name == "b" else {},
            ))
        if self.mode is PlanMode.SIMPLE_DEPENDENCY:
            steps[1].depends_on = [steps[0].id]
            steps[2].depends_on = [steps[0].id]
        return Plan(goal_id=goal.id, mode=self.mode, status=PlanStatus.READY, steps=steps)


def _tools(log):
    registry = ToolRegistry()

    def handler(name):
        def run(request):
            log.append(name)
            if name == "b":
                return ToolResult(status=ToolResultStatus.FAILURE, error_type="ASSUMPTION_INVALID", error_message="fixture proves A is false")
            return ToolResult(status=ToolResultStatus.SUCCESS, output={"route": name})
        return run

    for name in "abcde":
        registry.register(FakeTool(CapabilitySpec(name=name, description=name), handler(name)))
    return registry


def _goal(db, name):
    project = ProjectRepository(db).create(Project(name=name, type="test"))
    return Goal(project_id=project.id, objective=name, allowed_capabilities=list("abcde"), success_criteria=["valid"])


def test_w1_invalid_assumption_replans_to_new_route(db):
    log = []
    planner = _ReplanPlanner()
    goal = _goal(db, "w1-invalid-assumption")
    plan = asyncio.run(PlanExecutionService(db, planner, _tools(log)).execute_goal(goal))
    assert planner.calls == [False, True]
    assert log[:1] == ["a"] and log.count("b") == 2
    assert log[-2:] == ["d", "e"]
    assert "c" not in log
    assert plan.status is PlanStatus.COMPLETED
    assert plan.replan_count == 1


def test_w2_repeated_dead_end_replans_materially_different_path(db):
    log = []
    planner = _ReplanPlanner()
    goal = _goal(db, "w2-dead-end")
    plan = asyncio.run(PlanExecutionService(db, planner, _tools(log)).execute_goal(goal))
    assert any(step.capability == "d" for step in plan.steps)
    assert "c" not in log and log[-2:] == ["d", "e"]
    assert planner.calls[-1] is True


def test_w3_validator_rejection_creates_signal_consumed_by_macro_replan(db, make_task):
    from lhas.validation import ValidationCheck, ValidationResult

    class RejectOnce:
        def __init__(self): self.calls = 0
        async def validate(self, *, task, attempt, result):
            self.calls += 1
            passed = self.calls > 1
            return ValidationResult(attempt_id=attempt.id, passed=passed, checks=[ValidationCheck(name="acceptance", passed=passed)], evidence="pass" if passed else "reject")

    task = make_task(acceptance_criteria=["valid"])
    run = RunRepository(db).create(Run(task_id=task.id, status="RUNNING"))
    attempt = AttemptRepository(db).create(Attempt(run_id=run.id, attempt_number=1, status="RUNNING"))
    provider = ScriptedProviderAdapter([ProviderResponse(content="bad", completion_claim=True), ProviderResponse(content="good", completion_claim=True)])
    dispatcher = NativeToolDispatcher(db=db, registry=ToolRegistry(), allowed_capabilities=set(), allowed_side_effect_capabilities=set())
    kernel = NativeAgentKernel(
        db=db, provider=provider, dispatcher=dispatcher, completion_authority=CompletionAuthority(db=db, validator=RejectOnce())
    )
    request = AgentRequest(agent_id="w3", role=AgentRole.WORKER, objective=task.objective, budget=AgentBudget(max_turns=2, max_tool_calls=1), metadata={"task_id": task.id, "run_id": run.id, "attempt_id": attempt.id})
    result = asyncio.run(kernel.run(request))
    signals = ReplanSignalRepository(db).list_for_attempt(attempt.id)
    assert result.status is AgentStatus.COMPLETED
    assert any(signal.reason == "VALIDATOR_REJECTION" for signal in signals)
    goal = Goal(id="w3-goal", project_id=task.project_id, objective="w3 revised workflow", allowed_capabilities=list("abcde"))
    plan = Plan(id="w3-plan", goal_id=goal.id, mode=PlanMode.LINEAR, status=PlanStatus.RUNNING, steps=[
        PlanStep(id="w3-a", title="a", objective="a", capability="a", status=PlanStepStatus.COMPLETED),
        PlanStep(id="w3-b", title="b", objective="b", capability="b"),
    ])
    PlanRepository(db).create(plan)
    planner = _ReplanPlanner()
    registry = _tools([])
    revised = asyncio.run(MacroReplanService(db, planner).consume(goal=goal, plan=plan, signals=[signals[0]], context={"capabilities": registry.specs()}))
    assert revised.accepted is True and revised.plan.version.endswith("-r1")
    finished = asyncio.run(PlanExecutionService(db, planner, registry).execute_goal(goal, resume_plan_id=plan.id))
    assert finished.status is PlanStatus.COMPLETED
    assert {step.capability for step in finished.steps if step.status is PlanStepStatus.COMPLETED} >= {"a", "d", "e"}


def test_w4_child_failure_reaches_parent_replan_signal(db, make_task):
    task = make_task()
    run = RunRepository(db).create(Run(task_id=task.id, status="RUNNING"))
    attempt = AttemptRepository(db).create(Attempt(run_id=run.id, attempt_number=1, status="RUNNING"))
    provider = ScriptedProviderAdapter([], runtime_target=RuntimeTarget(provider_id="p", model_id="m", endpoint_identity="e", credential_route_id="r", route_type="test"))
    kernel = NativeAgentKernel(db=db, provider=provider, dispatcher=NativeToolDispatcher(db=db, registry=ToolRegistry(), allowed_capabilities=set(), allowed_side_effect_capabilities=set()), completion_authority=CompletionAuthority(db=db, validator=AlwaysPassValidator()))
    kernel.delivery = type("FailedChildDelivery", (), {"consume_for_parent_attempt": lambda self, attempt_id, consumed: [{"delivery_token": "child-delivery-1", "outcome": {"status": "FAILED"}}]})()
    snapshot = ExecutionSnapshot(task_id=task.id, run_id=run.id, attempt_id=attempt.id, goal=task.objective)
    kernel._consume_deliveries(snapshot)
    signals = ReplanSignalRepository(db).list_for_attempt(attempt.id)
    assert any(signal.reason == "CHILD_FAILURE" for signal in signals)
    goal = Goal(id="w4-goal", project_id=task.project_id, objective="w4 parent workflow", allowed_capabilities=list("abcde"))
    plan = Plan(id="w4-plan", goal_id=goal.id, mode=PlanMode.SIMPLE_DEPENDENCY, status=PlanStatus.RUNNING, steps=[
        PlanStep(id="w4-a", title="a", objective="a", capability="a", status=PlanStepStatus.COMPLETED),
        PlanStep(id="w4-b", title="b", objective="b", capability="b", depends_on=["w4-a"]),
    ])
    PlanRepository(db).create(plan)
    revised = asyncio.run(MacroReplanService(db, _ReplanPlanner(PlanMode.SIMPLE_DEPENDENCY)).consume(
        goal=goal, plan=plan, signals=[signals[0]], context={"capabilities": _tools([]).specs()}))
    assert revised.accepted is True and any(step.capability == "d" for step in revised.plan.steps)


def test_w5_stale_worker_has_zero_side_effects(db):
    from lhas.executors.protocol import ExecutionRequest, ExecutionResult
    from lhas.domain.enums import ExecutionStatus
    from lhas.planning.service import _TaskGraphAgentExecutor

    goal = _goal(db, "w5-stale")
    plan = Plan(id="w5-plan", goal_id=goal.id, status=PlanStatus.READY, steps=[PlanStep(id="w5-step", title="step", objective="step", capability="step")])
    PlanRepository(db).create(plan)
    calls = []

    class Executor:
        async def execute(self, request):
            calls.append("side-effect")
            return ExecutionResult(status=ExecutionStatus.SUCCESS)
        async def cancel(self, run_id): pass
        async def status(self, run_id): return {}

    worker = _TaskGraphAgentExecutor(Executor(), plan, plan.steps[0], db)
    plan.version = "P-0.1-r1"
    PlanRepository(db).update(plan)
    request = ExecutionRequest(task_id="t", run_id="r", attempt_id="a", attempt_number=1, task={}, context={}, metadata={})
    result = asyncio.run(worker.execute(request))
    assert result.error_type == "STALE_PLAN"
    assert calls == []


def test_native_cli_production_path_wires_controller_and_factory(tmp_path):
    from lhas.cli_runtime import ProductRuntime

    source = tmp_path / "source"
    source.mkdir()
    (source / "module.py").write_text("value = 1\n", encoding="utf-8")
    runtime = ProductRuntime(tmp_path / "runtime.db")
    try:
        prepared = runtime.prepare_new(
            goal="run native wiring smoke",
            repo=source,
            verify_argv=["python", "-c", "print('ok')"],
            max_attempts=1,
            max_turns=1,
            provider="offline",
            kernel="native",
        )
        orchestrator = prepared.orchestrator
        run = asyncio.run(orchestrator.prepare_task_run(prepared.task.id))
        executor = orchestrator._make_executor(run.id)
        kernel = executor.kernel
        asyncio.run(orchestrator.continue_prepared_run(run.id))
        binding = kernel.runtime_target_controller.current(run.id)
        assert kernel.runtime_target_controller is not None
        assert kernel.provider_factory is not None
        assert kernel.provider.runtime_target == binding["effective_target"]
    finally:
        runtime.close()
