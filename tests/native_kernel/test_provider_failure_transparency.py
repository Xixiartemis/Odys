import asyncio

from lhas.agent.models import AgentBudget, AgentRequest, AgentRole, AgentStatus
from lhas.domain.enums import EventType, FailureClass, FailureType
from lhas.domain.models import Attempt, Run
from lhas.executors.protocol import ExecutionResult
from lhas.failure import RuleFailureClassifier
from lhas.native.completion import CompletionAuthority
from lhas.native.kernel import NativeAgentKernel
from lhas.native.models import RuntimeTarget
from lhas.native.parser import ModelResponseParser
from lhas.native.persistence import ExecutionSnapshotRepository
from lhas.native.provider import OpenAIChatProviderAdapter
from lhas.native.runtime import ProviderFailureClassifier
from lhas.native.tools import NativeToolDispatcher
from lhas.persistence.event_store import EventStore
from lhas.persistence.repositories import AttemptRepository, RunRepository
from lhas.recovery import DefaultRecoveryPolicy
from lhas.tools.registry import ToolRegistry
from tests.helpers import PassingCommandValidator
from lhas.validation import ValidationCheck, ValidationResult


class _ChatCompletion:
    def model_dump(self, *, mode):
        assert mode == "python"
        return {
            "id": "chatcmpl-test",
            "choices": [{"message": {"content": "done", "tool_calls": []}}],
            "usage": {"total_tokens": 1},
        }


class _Completions:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class _Client:
    def __init__(self, response):
        self.base_url = "https://provider.example/v1"
        self.chat = type("Chat", (), {"completions": _Completions(response)})()


def _adapter(response):
    return OpenAIChatProviderAdapter(
        model="mimo-v2.5",
        api_key="test-secret",
        client=_Client(response),
        provider_id="mimo",
    )


def test_production_adapter_normalizes_mock_chat_completion_for_parser():
    adapter = _adapter(_ChatCompletion())
    context = type(
        "Context",
        (),
        {"messages": [{"role": "user", "content": "finish"}]},
    )()

    normalized = asyncio.run(
        adapter.generate(context=context, tools=[], timeout_seconds=1)
    )
    parsed = ModelResponseParser().parse(normalized)

    assert isinstance(normalized, dict)
    assert set(normalized) == {"id", "choices", "usage"}
    assert parsed.content == "done"
    assert parsed.completion_claim is True


def _kernel_case(db, make_task, response):
    task = make_task(acceptance_criteria=["accepted"])
    run = RunRepository(db).create(Run(task_id=task.id, status="RUNNING"))
    attempt = AttemptRepository(db).create(
        Attempt(run_id=run.id, attempt_number=1, status="RUNNING")
    )
    registry = ToolRegistry()
    provider = _adapter(response)
    dispatcher = NativeToolDispatcher(
        db=db,
        registry=registry,
        allowed_capabilities=set(),
        allowed_side_effect_capabilities=set(),
    )
    kernel = NativeAgentKernel(
        db=db,
        provider=provider,
        dispatcher=dispatcher,
        completion_authority=CompletionAuthority(db=db, validator=PassingCommandValidator()),
    )
    request = AgentRequest(
        agent_id="provider-transparency",
        role=AgentRole.WORKER,
        objective=task.objective,
        context={"acceptance_criteria": task.acceptance_criteria},
        budget=AgentBudget(max_turns=1, max_tool_calls=1),
        metadata={"task_id": task.id, "run_id": run.id, "attempt_id": attempt.id},
    )
    return task, run, attempt, provider, kernel, request


def _events(db, attempt):
    return EventStore(db).list_for_attempt(attempt.id)


def test_native_valid_empty_and_provider_exception_are_distinct(db, make_task):
    valid = _kernel_case(
        db,
        make_task,
        _ChatCompletion(),
    )
    valid_result = asyncio.run(valid[4].run(valid[5]))
    valid_events = _events(db, valid[2])
    valid_types = [event.event_type for event in valid_events]
    parsed = next(
        event
        for event in valid_events
        if event.event_type is EventType.MODEL_RESPONSE_PARSED
    )
    assert valid_result.status is AgentStatus.COMPLETED
    assert EventType.MODEL_CALL_STARTED in valid_types
    assert EventType.MODEL_RESPONSE_RECEIVED in valid_types
    assert EventType.MODEL_RESPONSE_PARSED in valid_types
    assert EventType.MODEL_RESPONSE_REJECTED not in valid_types
    assert parsed.payload["content_length"] == 4
    assert parsed.payload["tool_call_count"] == 0
    assert parsed.payload["completion_claim"] is True

    empty = _kernel_case(db, make_task, {})
    empty_result = asyncio.run(empty[4].run(empty[5]))
    empty_events = _events(db, empty[2])
    empty_types = [event.event_type for event in empty_events]
    empty_parsed = next(
        event
        for event in empty_events
        if event.event_type is EventType.MODEL_RESPONSE_PARSED
    )
    assert empty_result.status is AgentStatus.FAILED
    assert empty_result.error_type == "MODEL_STOPPED_WITHOUT_COMPLETION"
    assert EventType.MODEL_RESPONSE_RECEIVED in empty_types
    assert EventType.MODEL_RESPONSE_PARSED in empty_types
    assert EventType.MODEL_RESPONSE_REJECTED not in empty_types
    assert empty_parsed.payload == {
        "turn": 1,
        "content_length": 0,
        "tool_call_count": 0,
        "completion_claim": False,
    }

    provider_failure = _kernel_case(
        db,
        make_task,
        RuntimeError("upstream opaque failure"),
    )
    failure_result = asyncio.run(provider_failure[4].run(provider_failure[5]))
    failure_events = _events(db, provider_failure[2])
    failure_types = [event.event_type for event in failure_events]
    rejected = next(
        event
        for event in failure_events
        if event.event_type is EventType.MODEL_RESPONSE_REJECTED
    )
    assert failure_result.status is AgentStatus.FAILED
    assert failure_result.error_type == "UNKNOWN_PROVIDER_FAILURE"
    assert failure_result.error_message == "upstream opaque failure"
    assert EventType.MODEL_CALL_STARTED in failure_types
    assert EventType.MODEL_RESPONSE_RECEIVED not in failure_types
    assert EventType.MODEL_RESPONSE_PARSED not in failure_types
    assert rejected.payload["stage"] == "PROVIDER_GENERATE"
    assert rejected.payload["failure_code"] == "unknown_provider_failure"
    assert rejected.payload["detail"] == "upstream opaque failure"
    assert {valid_result.status, empty_result.status, failure_result.status} == {
        AgentStatus.COMPLETED,
        AgentStatus.FAILED,
    }
    assert empty_result.error_type != failure_result.error_type


def test_malformed_provider_response_is_rejected_as_invalid_response(db, make_task):
    case = _kernel_case(db, make_task, {"choices": []})
    result = asyncio.run(case[4].run(case[5]))
    events = _events(db, case[2])
    types = [event.event_type for event in events]
    rejected = next(
        event
        for event in events
        if event.event_type is EventType.MODEL_RESPONSE_REJECTED
    )

    assert result.status is AgentStatus.FAILED
    assert result.error_type == "PROVIDER_MALFORMED_RESPONSE"
    assert EventType.MODEL_RESPONSE_RECEIVED in types
    assert EventType.MODEL_RESPONSE_PARSED not in types
    assert rejected.payload["failure_code"] == "invalid_response"


def test_non_normalizable_sdk_response_is_invalid_response(db, make_task):
    case = _kernel_case(db, make_task, object())
    result = asyncio.run(case[4].run(case[5]))
    rejected = next(
        event
        for event in _events(db, case[2])
        if event.event_type is EventType.MODEL_RESPONSE_REJECTED
    )

    assert result.status is AgentStatus.FAILED
    assert result.error_type == "MALFORMED_PROVIDER_RESPONSE"
    assert rejected.payload["failure_code"] == "invalid_response"
    assert rejected.payload["stage"] == "PROVIDER_GENERATE"


def test_unknown_provider_failure_preserves_taxonomy_and_recovery_decision():
    assert ProviderFailureClassifier.taxonomy_code("UNKNOWN_PROVIDER_FAILURE") == "unknown_provider_failure"
    attempt = Attempt(
        run_id="run",
        attempt_number=1,
        status="FAILED",
        error_type="UNKNOWN_PROVIDER_FAILURE",
        error_message="upstream opaque failure",
    )
    task = type("TaskLike", (), {"objective": "objective"})()
    result = ExecutionResult(
        status="FAILURE",
        output=None,
        error_type="UNKNOWN_PROVIDER_FAILURE",
        error_message="upstream opaque failure",
    )
    validation = ValidationResult(
        attempt_id=attempt.id,
        passed=False,
        checks=[ValidationCheck(name="acceptance", passed=False, detail="not reached")],
        evidence="no accepted completion",
    )

    report = asyncio.run(
        RuleFailureClassifier().classify(
            task=task,
            attempt=attempt,
            result=result,
            validation=validation,
        )
    )
    action = asyncio.run(
        DefaultRecoveryPolicy().decide(
            task=task,
            attempt=attempt,
            failure_report=report,
            attempt_number=1,
            max_attempts=2,
            history=[],
        )
    )

    assert report.failure_type is FailureType.UNKNOWN_PROVIDER_FAILURE
    assert report.failure_class is FailureClass.EXECUTION
    assert report.failure_type is not FailureType.EMPTY_RESULT
    assert action.action_type.value == "RETRY_WITH_FAILURE_CONTEXT"
    assert action.added_context["failure_type"] == "UNKNOWN_PROVIDER_FAILURE"
