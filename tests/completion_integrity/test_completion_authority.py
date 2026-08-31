import asyncio

from lhas.agent.models import AgentBudget, AgentRequest, AgentRole, AgentStatus
from lhas.domain.enums import EventType, ExecutionStatus, TaskStatus
from lhas.executors.protocol import ExecutionResult
from lhas.native.completion import AcceptedCompletionValidator, CompletionAuthority
from lhas.native.executor import NativeAgentExecutor
from lhas.native.kernel import NativeAgentKernel
from lhas.native.models import ProviderResponse, ProviderToolCall
from lhas.native.persistence import CompletionCandidateRepository
from lhas.native.provider import ScriptedProviderAdapter
from lhas.native.tools import NativeToolDispatcher
from lhas.orchestrator_v2 import RecoveringOrchestrator
from lhas.persistence.event_store import EventStore
from lhas.persistence.repositories import AttemptRepository, TaskRepository
from lhas.planning.models import CapabilitySpec
from lhas.tools.protocol import ToolResult, ToolResultStatus
from lhas.tools.registry import ToolRegistry
from lhas.validation import AlwaysPassValidator, NeverPassValidator


class PassingPytestTool:
    capability = CapabilitySpec(name="cli.exec", description="pytest", input_schema={"type": "object"})

    async def execute(self, request):
        return ToolResult(status=ToolResultStatus.SUCCESS, output={"exit_code": 0, "timed_out": False, "duration_ms": 1, "stdout_truncated": False, "stderr_truncated": False})


def _direct(db, make_task, responses, validator, *, tool=None, turns=3):
    from lhas.domain.models import Attempt, Run
    from lhas.persistence.repositories import AttemptRepository, RunRepository

    task = make_task(max_attempts=1, acceptance_criteria=["authoritative criterion"])
    run = RunRepository(db).create(Run(task_id=task.id, status="RUNNING"))
    attempt = AttemptRepository(db).create(Attempt(run_id=run.id, attempt_number=1, status="RUNNING"))
    registry = ToolRegistry()
    if tool:
        registry.register(tool)
    dispatcher = NativeToolDispatcher(db=db, registry=registry, allowed_capabilities=set(registry.list_capabilities()), allowed_side_effect_capabilities=set())
    kernel = NativeAgentKernel(db=db, provider=ScriptedProviderAdapter(responses), dispatcher=dispatcher, completion_authority=CompletionAuthority(db=db, validator=validator))
    request = AgentRequest(agent_id="completion", role=AgentRole.WORKER, objective=task.objective, allowed_capabilities=set(registry.list_capabilities()), budget=AgentBudget(max_turns=turns, max_tool_calls=5), metadata={"task_id": task.id, "run_id": run.id, "attempt_id": attempt.id})
    return task, run, attempt, kernel, request


def test_executor_completion_claim_validator_false_is_rejected(db, make_task):
    case = _direct(db, make_task, [ProviderResponse(content="done", completion_claim=True)], NeverPassValidator(), turns=1)
    result = asyncio.run(case[3].run(case[4]))
    candidate = CompletionCandidateRepository(db).latest_for_attempt(case[2].id)
    assert result.status is AgentStatus.FAILED
    assert candidate.status.value == "REJECTED"


def test_pytest_pass_does_not_bypass_validator_false(db, make_task):
    case = _direct(db, make_task, [
        ProviderResponse(tool_calls=[ProviderToolCall(id="pytest", name="cli.exec", arguments={"argv": ["pytest", "-q"]})]),
        ProviderResponse(content="tests pass", completion_claim=True),
    ], NeverPassValidator(), tool=PassingPytestTool(), turns=2)
    result = asyncio.run(case[3].run(case[4]))
    candidate = CompletionCandidateRepository(db).latest_for_attempt(case[2].id)
    assert result.status is AgentStatus.FAILED
    assert candidate.status.value == "REJECTED"
    assert result.safe_trace[0]["safe_summary"]["pytest_observation"] == "PASS"


def test_child_success_context_cannot_bypass_parent_acceptance(db, make_task):
    case = _direct(db, make_task, [ProviderResponse(content="child succeeded", completion_claim=True)], NeverPassValidator(), turns=1)
    case[4].context = {"delegation_dependencies": {"token": {"outcome": {"status": "COMPLETED"}}}}
    result = asyncio.run(case[3].run(case[4]))
    assert result.status is AgentStatus.FAILED
    assert CompletionCandidateRepository(db).latest_for_attempt(case[2].id).status.value == "REJECTED"


def test_executor_success_without_candidate_cannot_complete_task(db, make_task):
    task = make_task(max_attempts=1)

    class SuccessExecutor:
        name = "claim-only"

        async def execute(self, request):
            return ExecutionResult(status=ExecutionStatus.SUCCESS, output="model final text")

        async def resume(self, request):
            return await self.execute(request)

        async def cancel(self, run_id):
            return None

        async def status(self, run_id):
            return {}

    orchestrator = RecoveringOrchestrator(db, executor_factory=SuccessExecutor, validator=AcceptedCompletionValidator(db))
    run = asyncio.run(orchestrator.execute_task(task.id))
    assert run.status.value != "COMPLETED"
    assert TaskRepository(db).get(task.id).status is not TaskStatus.COMPLETED


def test_false_completion_never_emits_task_completed(db, make_task):
    task = make_task(max_attempts=1)

    def factory():
        registry = ToolRegistry()
        dispatcher = NativeToolDispatcher(db=db, registry=registry, allowed_capabilities=set(), allowed_side_effect_capabilities=set())
        kernel = NativeAgentKernel(db=db, provider=ScriptedProviderAdapter([ProviderResponse(content="false claim", completion_claim=True)]), dispatcher=dispatcher, completion_authority=CompletionAuthority(db=db, validator=NeverPassValidator()))
        return NativeAgentExecutor(kernel, allowed_capabilities=set(), allowed_side_effect_capabilities=set(), max_turns=1)

    orchestrator = RecoveringOrchestrator(db, executor_factory=factory, validator=AcceptedCompletionValidator(db))
    run = asyncio.run(orchestrator.execute_task(task.id))
    assert run.status.value != "COMPLETED"
    assert EventType.TASK_COMPLETED not in [event.event_type for event in EventStore(db).list_for_task(task.id)]


def test_accepted_completion_transitions_exactly_once(db, make_task):
    task = make_task(max_attempts=1)

    def factory():
        registry = ToolRegistry()
        dispatcher = NativeToolDispatcher(db=db, registry=registry, allowed_capabilities=set(), allowed_side_effect_capabilities=set())
        kernel = NativeAgentKernel(db=db, provider=ScriptedProviderAdapter([ProviderResponse(content="valid claim", completion_claim=True)]), dispatcher=dispatcher, completion_authority=CompletionAuthority(db=db, validator=AlwaysPassValidator()))
        return NativeAgentExecutor(kernel, allowed_capabilities=set(), allowed_side_effect_capabilities=set(), max_turns=1)

    orchestrator = RecoveringOrchestrator(db, executor_factory=factory, validator=AcceptedCompletionValidator(db))
    run = asyncio.run(orchestrator.execute_task(task.id))
    events = [event.event_type for event in EventStore(db).list_for_task(task.id)]
    attempts = AttemptRepository(db).list_for_run(run.id)
    assert run.status.value == "COMPLETED" and TaskRepository(db).get(task.id).status is TaskStatus.COMPLETED
    assert events.count(EventType.TASK_COMPLETED) == 1
    assert len(CompletionCandidateRepository(db).list_for_attempt(attempts[0].id)) == 1
