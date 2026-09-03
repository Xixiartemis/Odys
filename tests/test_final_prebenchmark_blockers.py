import asyncio
import json

from lhas.agent.models import AgentBudget, AgentRequest, AgentRole, AgentStatus
from lhas.domain.models import Attempt, Project, Run, Task
from lhas.native.completion import CompletionAuthority
from lhas.native.delegation import ChildExecutionState, ChildOutcome, DelegationLifecycleRepository, DeliveryState, DurableDeliveryService
from lhas.native.executor import NativeAgentExecutor
from lhas.native.kernel import NativeAgentKernel
from lhas.native.models import ExecutionSnapshot, ProviderResponse, ProviderToolCall, RuntimeTarget
from lhas.native.persistence import ReplanSignalRepository, ValidationFailureRepository
from lhas.native.provider import ScriptedProviderAdapter
from lhas.native.runtime import RuntimeTargetController
from lhas.native.tools import NativeToolDispatcher
from lhas.persistence.event_store import EventStore
from lhas.persistence.planning_repositories import PlanRepository
from lhas.persistence.platform_repositories import DelegationRepository
from lhas.persistence.repositories import ProjectRepository, RunRepository, AttemptRepository, TaskRepository
from lhas.planning.models import CapabilitySpec, Goal, Plan, PlanMode, PlanStep, PlanStatus, PlanStepStatus
from lhas.planning.scheduler import TaskGraphScheduler
from lhas.planning.service import PlanExecutionService
from lhas.platform_models import Delegation
from lhas.tools.fakes import FakeTool
from lhas.tools.protocol import ToolResult, ToolResultStatus
from lhas.tools.registry import ToolRegistry
from tests.helpers import PassingCommandValidator


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
    failed_b_task = next(task for task in TaskRepository(db).list() if task.objective == "b")
    run = RunRepository(db).list_for_task(failed_b_task.id)[0]
    attempts = AttemptRepository(db).list_for_run(run.id)
    signals = [signal for attempt in attempts for signal in ReplanSignalRepository(db).list_for_attempt(attempt.id)]
    assert {attempt.error_type for attempt in attempts} == {"ASSUMPTION_INVALID"}
    assert [signal.reason for signal in signals] == ["INVALID_ASSUMPTION"]
    assert signals[0].evidence["source"] == "DURABLE_FAILURE_EVIDENCE"


def test_w2_repeated_dead_end_replans_materially_different_path(db):
    class DeadEndPlanner:
        def __init__(self):
            self.calls = []

        async def create_plan(self, *, goal, capabilities, context=None):
            revised = bool((context or {}).get("replan_signals"))
            self.calls.append(revised)
            names = ["alternate", "verify"] if revised else ["probe", "stale-original"]
            steps = []
            for name in names:
                steps.append(PlanStep(
                    title=name,
                    objective=name,
                    capability=name,
                    depends_on=[steps[-1].id] if steps else [],
                ))
            return Plan(goal_id=goal.id, status=PlanStatus.READY, steps=steps)

    log = []
    registry = ToolRegistry()
    for name in ("probe", "stale-original", "alternate", "verify"):
        def run(request, capability=name):
            log.append(capability)
            if capability == "probe":
                return ToolResult(status=ToolResultStatus.FAILURE, error_type="STRATEGY_DEAD_END", error_message="same scoped strategy failed")
            return ToolResult(status=ToolResultStatus.SUCCESS, output={"route": capability})
        registry.register(FakeTool(CapabilitySpec(name=name), run))
    planner = DeadEndPlanner()
    goal = _goal(db, "w2-dead-end")
    goal.allowed_capabilities = ["probe", "stale-original", "alternate", "verify"]
    plan = asyncio.run(PlanExecutionService(db, planner, registry).execute_goal(goal))
    assert log == ["probe", "probe", "alternate", "verify"]
    assert "stale-original" not in log
    assert planner.calls == [False, True]
    failed_task = next(task for task in TaskRepository(db).list() if task.objective == "probe")
    run = RunRepository(db).list_for_task(failed_task.id)[0]
    attempts = AttemptRepository(db).list_for_run(run.id)
    signals = [signal for attempt in attempts for signal in ReplanSignalRepository(db).list_for_attempt(attempt.id)]
    assert len(attempts) == 2
    assert signals[0].reason == "REPEATED_STEP_FAILURE"
    assert signals[0].evidence["failure_signature_count"] == 2
    assert signals[0].evidence["threshold"] == 2
    assert plan.status is PlanStatus.COMPLETED


def test_w3_validator_rejection_flows_through_plan_execution(db):
    from lhas.validation import ValidationCheck, ValidationResult

    class RejectBOnce:
        def __init__(self):
            self.rejected = False

        async def validate(self, *, task, attempt, result):
            passed = not (task.objective == "b" and not self.rejected)
            if not passed:
                self.rejected = True
            return ValidationResult(
                attempt_id=attempt.id,
                passed=passed,
                checks=[ValidationCheck(name="acceptance", passed=passed)],
                evidence=json.dumps({"command": ["pytest", "-q"], "exit_code": 0 if passed else 1, "timed_out": False}),
            )

    validator = RejectBOnce()
    executed = []

    def agent_factory(step):
        executed.append(step.capability)
        provider = ScriptedProviderAdapter([
            ProviderResponse(content=f"{step.capability}-candidate-1", completion_claim=True),
            ProviderResponse(content=f"{step.capability}-candidate-2", completion_claim=True),
        ])
        kernel = NativeAgentKernel(
            db=db,
            provider=provider,
            dispatcher=NativeToolDispatcher(db=db, registry=ToolRegistry(), allowed_capabilities=set(), allowed_side_effect_capabilities=set()),
            completion_authority=CompletionAuthority(db=db, validator=validator),
        )
        return NativeAgentExecutor(kernel, allowed_capabilities=[], allowed_side_effect_capabilities=[], max_turns=2)

    goal = _goal(db, "w3-validator")
    planner = _ReplanPlanner()
    registry = _tools([])
    finished = asyncio.run(PlanExecutionService(db, planner, registry, agent_executor_factory=agent_factory).execute_goal(goal))
    assert finished.status is PlanStatus.COMPLETED
    assert {step.capability for step in finished.steps if step.status is PlanStepStatus.COMPLETED} >= {"a", "d", "e"}
    assert "c" not in executed and executed == ["a", "b", "d", "e"]
    failed_b_task = next(task for task in TaskRepository(db).list() if task.objective == "b")
    run = RunRepository(db).list_for_task(failed_b_task.id)[0]
    attempt = AttemptRepository(db).list_for_run(run.id)[0]
    assert len(ValidationFailureRepository(db).list_for_attempt(attempt.id)) == 1
    assert [signal.reason for signal in ReplanSignalRepository(db).list_for_attempt(attempt.id)] == ["VALIDATOR_REJECTION"]


def test_w4_durable_child_failure_flows_through_parent_plan_execution(db):
    goal = _goal(db, "w4-child")
    planner = _ReplanPlanner(PlanMode.SIMPLE_DEPENDENCY)
    registry = _tools([])
    delegation_ids = []
    executed = []

    class FailedChildThenNative:
        def __init__(self, native):
            self.native = native

        async def execute(self, request):
            child_task = TaskRepository(db).create(Task(project_id=goal.project_id, title="child", objective="child"))
            child_run = RunRepository(db).create(Run(task_id=child_task.id, status="FAILED"))
            delegation = Delegation(
                parent_agent_id="parent",
                parent_task_id=request.task_id,
                parent_run_id=request.run_id,
                child_agent_id="child",
                child_task_id=child_task.id,
                child_run_id=child_run.id,
                spawn_depth=1,
            )
            DelegationRepository(db).create(delegation)
            DelegationLifecycleRepository(db).create(
                delegation_id=delegation.id,
                parent_attempt_id=request.attempt_id,
                execution_owner="child",
                conversation_owner="parent",
                delivery_owner="parent",
            )
            delivery = DurableDeliveryService(db)
            delivery.record_started(delegation.id)
            delivery.record_outcome(
                delegation.id,
                ChildOutcome(status=ChildExecutionState.FAILED, failure_type="CHILD_FIXTURE_FAILURE", child_run_id=child_run.id),
                validator_result=False,
            )
            delivery.deliver(delegation.id)
            delegation_ids.append(delegation.id)
            return await self.native.execute(request)

        async def resume(self, request):
            return await self.execute(request)

        async def cancel(self, run_id):
            return await self.native.cancel(run_id)

        async def status(self, run_id):
            return await self.native.status(run_id)

    def agent_factory(step):
        executed.append(step.capability)
        provider = ScriptedProviderAdapter([ProviderResponse(content=f"{step.capability}-done", completion_claim=True)])
        kernel = NativeAgentKernel(
            db=db,
            provider=provider,
            dispatcher=NativeToolDispatcher(db=db, registry=ToolRegistry(), allowed_capabilities=set(), allowed_side_effect_capabilities=set()),
            completion_authority=CompletionAuthority(db=db, validator=PassingCommandValidator()),
        )
        native = NativeAgentExecutor(kernel, allowed_capabilities=[], allowed_side_effect_capabilities=[], max_turns=1)
        return FailedChildThenNative(native) if step.capability == "b" else native

    finished = asyncio.run(PlanExecutionService(db, planner, registry, agent_executor_factory=agent_factory).execute_goal(goal))
    assert finished.status is PlanStatus.COMPLETED
    assert "c" not in executed
    assert executed[:2] == ["a", "b"] and executed[-2:] == ["d", "e"]
    assert any(step.capability == "d" for step in finished.steps)
    assert len(delegation_ids) == 1
    lifecycle = DelegationLifecycleRepository(db).get(delegation_ids[0])
    assert lifecycle.delivery_state is DeliveryState.CONSUMED
    failed_b_task = next(task for task in TaskRepository(db).list() if task.objective == "b")
    run = RunRepository(db).list_for_task(failed_b_task.id)[0]
    attempts = AttemptRepository(db).list_for_run(run.id)
    signals = [signal for attempt in attempts for signal in ReplanSignalRepository(db).list_for_attempt(attempt.id)]
    assert [signal.reason for signal in signals] == ["CHILD_FAILURE"]


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
