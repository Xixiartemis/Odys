import asyncio
import json

from lhas.agent.models import AgentBudget, AgentRequest, AgentRole, AgentStatus
from lhas.domain.models import Attempt, Run
from lhas.native.completion import AcceptedCompletionValidator, CompletionAuthority
from lhas.native.kernel import NativeAgentKernel
from lhas.native.models import ProviderResponse, ProviderToolCall
from lhas.native.persistence import CompletionCandidateRepository, ExecutionSnapshotRepository, ReplanSignalRepository
from lhas.native.provider import ScriptedProviderAdapter
from lhas.native.tools import NativeToolDispatcher
from lhas.persistence.repositories import AttemptRepository, RunRepository
from lhas.planning.models import CapabilitySpec
from lhas.tools.protocol import ToolResult, ToolResultStatus
from lhas.tools.registry import ToolRegistry
from tests.helpers import PassingCommandValidator
from lhas.validation import ValidationCheck, ValidationResult


class EchoTool:
    capability = CapabilitySpec(
        name="test.echo",
        description="echo one value",
        input_schema={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
    )

    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    async def execute(self, request):
        self.calls.append(request.arguments)
        if self.fail:
            return ToolResult(status=ToolResultStatus.FAILURE, error_type="TEST_TOOL_FAILURE", metadata={"failure_category": "TEST_TOOL_FAILURE"})
        return ToolResult(status=ToolResultStatus.SUCCESS, output={"value": request.arguments["value"]})


class SequenceValidator:
    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    async def validate(self, *, task, attempt, result):
        self.calls += 1
        passed = self.values.pop(0)
        return ValidationResult(
            attempt_id=attempt.id,
            passed=passed,
            checks=[ValidationCheck(name="acceptance", passed=passed, detail=None if passed else "not yet")],
            evidence=json.dumps({"command": ["pytest", "-q"], "exit_code": 0 if passed else 1, "timed_out": False}),
        )


def _kernel_case(db, make_task, responses, *, validator=None, tool=None):
    task = make_task(acceptance_criteria=["accepted"])
    run = RunRepository(db).create(Run(task_id=task.id, status="RUNNING"))
    attempt = AttemptRepository(db).create(Attempt(run_id=run.id, attempt_number=1, status="RUNNING"))
    registry = ToolRegistry()
    if tool is not None:
        registry.register(tool)
    provider = ScriptedProviderAdapter(responses)
    dispatcher = NativeToolDispatcher(
        db=db,
        registry=registry,
        allowed_capabilities=set(registry.list_capabilities()),
        allowed_side_effect_capabilities=set(),
    )
    authority = CompletionAuthority(db=db, validator=validator or PassingCommandValidator())
    kernel = NativeAgentKernel(db=db, provider=provider, dispatcher=dispatcher, completion_authority=authority)
    request = AgentRequest(
        agent_id="native-test",
        role=AgentRole.WORKER,
        objective=task.objective,
        context={"acceptance_criteria": task.acceptance_criteria},
        allowed_capabilities=set(registry.list_capabilities()),
        budget=AgentBudget(max_turns=5, max_tool_calls=8),
        metadata={"task_id": task.id, "run_id": run.id, "attempt_id": attempt.id},
    )
    return task, run, attempt, provider, kernel, request


def test_native_model_tool_observation_model_completion(db, make_task):
    tool = EchoTool()
    case = _kernel_case(db, make_task, [
        ProviderResponse(tool_calls=[ProviderToolCall(id="c1", name="test.echo", arguments={"value": "one"})]),
        lambda context: ProviderResponse(content="done", completion_claim=context.sections["execution_state"]["recent_tool_outcomes"][-1]["status"] == "SUCCESS"),
    ], tool=tool)
    result = asyncio.run(case[4].run(case[5]))
    assert result.status is AgentStatus.COMPLETED
    assert result.turn_count == 2 and result.tool_call_count == 1
    assert tool.calls == [{"value": "one"}]
    assert case[3].calls[1]["context"]["sections"]["execution_state"]["recent_tool_outcomes"][-1]["bounded_output"]["value"] == "one"


def test_native_multiple_tool_rounds(db, make_task):
    tool = EchoTool()
    responses = [
        ProviderResponse(tool_calls=[ProviderToolCall(id="c1", name="test.echo", arguments={"value": "one"})]),
        ProviderResponse(tool_calls=[ProviderToolCall(id="c2", name="test.echo", arguments={"value": "two"})]),
        ProviderResponse(content="complete", completion_claim=True),
    ]
    case = _kernel_case(db, make_task, responses, tool=tool)
    result = asyncio.run(case[4].run(case[5]))
    assert result.status is AgentStatus.COMPLETED
    assert result.turn_count == 3 and result.tool_call_count == 2
    assert tool.calls == [{"value": "one"}, {"value": "two"}]


def test_native_tool_failure_is_next_turn_observation(db, make_task):
    tool = EchoTool(fail=True)
    seen = {}

    def recover(context):
        seen.update(context.sections["execution_state"]["recent_tool_outcomes"][-1])
        return ProviderResponse(content="candidate after failure", completion_claim=True)

    case = _kernel_case(db, make_task, [
        ProviderResponse(tool_calls=[ProviderToolCall(id="bad", name="test.echo", arguments={"value": "x"})]),
        recover,
    ], tool=tool)
    result = asyncio.run(case[4].run(case[5]))
    assert result.status is AgentStatus.COMPLETED
    assert seen["status"] == "FAILURE" and seen["error_type"] == "TEST_TOOL_FAILURE"


def test_native_malformed_provider_response_fails_closed(db, make_task):
    case = _kernel_case(db, make_task, [{"choices": []}])
    result = asyncio.run(case[4].run(case[5]))
    assert result.status is AgentStatus.FAILED
    assert result.error_type == "PROVIDER_MALFORMED_RESPONSE"
    assert CompletionCandidateRepository(db).list_for_attempt(case[2].id) == []


def test_native_provider_timeout_is_structured_failure(db, make_task):
    case = _kernel_case(db, make_task, [asyncio.TimeoutError()])
    result = asyncio.run(case[4].run(case[5]))
    assert result.status is AgentStatus.FAILED and result.error_type == "PROVIDER_TIMEOUT"


def test_native_turn_budget_exhaustion_does_not_complete(db, make_task):
    tool = EchoTool()
    case = _kernel_case(db, make_task, [
        ProviderResponse(tool_calls=[ProviderToolCall(id="c1", name="test.echo", arguments={"value": "x"})]),
    ], tool=tool)
    case[5].budget = AgentBudget(max_turns=1, max_tool_calls=8)
    result = asyncio.run(case[4].run(case[5]))
    assert result.status is AgentStatus.FAILED and result.error_type == "BUDGET_EXHAUSTED"
    assert ExecutionSnapshotRepository(db).get_for_attempt(case[2].id).phase.value == "REPLANNING"


def test_native_validator_rejection_reenters_loop(db, make_task):
    validator = SequenceValidator([False, True])
    case = _kernel_case(db, make_task, [
        ProviderResponse(content="premature", completion_claim=True),
        lambda context: ProviderResponse(
            content="repaired",
            completion_claim=bool(context.sections["validation_failures"]),
        ),
    ], validator=validator)
    result = asyncio.run(case[4].run(case[5]))
    candidates = CompletionCandidateRepository(db).list_for_attempt(case[2].id)
    assert result.status is AgentStatus.COMPLETED and validator.calls == 2
    assert [item.status.value for item in candidates] == ["REJECTED", "ACCEPTED"]


def test_native_model_stop_without_claim_cannot_complete(db, make_task):
    case = _kernel_case(db, make_task, [ProviderResponse()])
    result = asyncio.run(case[4].run(case[5]))
    assert result.status is AgentStatus.FAILED
    assert result.error_type == "MODEL_STOPPED_WITHOUT_COMPLETION"
    projected = asyncio.run(AcceptedCompletionValidator(db).validate(task=case[0], attempt=case[2], result=None))
    assert projected.passed is False


def test_repeated_tool_failure_emits_replan_signal(db, make_task):
    tool = EchoTool(fail=True)
    case = _kernel_case(db, make_task, [
        ProviderResponse(tool_calls=[ProviderToolCall(id="f1", name="test.echo", arguments={"value": "same"})]),
        ProviderResponse(tool_calls=[ProviderToolCall(id="f2", name="test.echo", arguments={"value": "same"})]),
        ProviderResponse(content="recover", completion_claim=True),
    ], tool=tool)
    result = asyncio.run(case[4].run(case[5]))
    signals = ReplanSignalRepository(db).list_for_attempt(case[2].id)
    assert result.status is AgentStatus.COMPLETED
    assert "REPEATED_TOOL_FAILURE" in [item.reason for item in signals]


def test_delegation_budget_is_harness_enforced(db, make_task):
    class DelegateTool:
        capability = CapabilitySpec(name="platform.delegate", description="delegate")
        async def execute(self, request):
            raise AssertionError("budget-denied delegation must not execute")

    tool = DelegateTool()
    case = _kernel_case(db, make_task, [
        ProviderResponse(tool_calls=[ProviderToolCall(id="d1", name="platform.delegate", arguments={})]),
        ProviderResponse(content="handled", completion_claim=True),
    ], tool=tool)
    case[5].budget = AgentBudget(max_turns=3, max_tool_calls=3, max_delegations=0)
    result = asyncio.run(case[4].run(case[5]))
    assert result.status is AgentStatus.COMPLETED
    assert result.safe_trace[0]["error_type"] == "DELEGATION_BUDGET_EXHAUSTED"
