import asyncio

import pytest

from lhas.agent.models import AgentBudget, AgentRequest, AgentRole, AgentStatus
from lhas.domain.models import Attempt, Run
from lhas.native.completion import CompletionAuthority
from lhas.native.kernel import NativeAgentKernel
from lhas.native.models import NativeFaultPoint, ProviderResponse, ProviderToolCall
from lhas.native.persistence import CompletionCandidateRepository, ExecutionSnapshotRepository, ToolInvocationRepository
from lhas.native.provider import ScriptedProviderAdapter
from lhas.native.tools import NativeToolDispatcher
from lhas.persistence.repositories import AttemptRepository, RunRepository
from lhas.planning.models import CapabilitySpec
from lhas.tools.protocol import ToolResult, ToolResultStatus
from lhas.tools.registry import ToolRegistry
from tests.helpers import PassingCommandValidator


class CrashOnce:
    def __init__(self, point):
        self.point = point
        self.hits = 0

    def hit(self, point, **context):
        if point is self.point and self.hits == 0:
            self.hits += 1
            raise RuntimeError(f"crash:{point.value}")


class MutationTool:
    capability = CapabilitySpec(
        name="test.mutate",
        description="deterministic side effect",
        input_schema={"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]},
        side_effect=True,
    )

    def __init__(self):
        self.calls = 0
        self.value = 0

    async def execute(self, request):
        self.calls += 1
        before = str(self.value)
        self.value = int(request.arguments["value"])
        return ToolResult(status=ToolResultStatus.SUCCESS, output={"path": "state", "before_sha256": before.zfill(64), "after_sha256": str(self.value).zfill(64)})


class CountingValidator(PassingCommandValidator):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def validate(self, **kwargs):
        self.calls += 1
        return await super().validate(**kwargs)


def _durable_case(db, make_task):
    task = make_task(acceptance_criteria=["valid"])
    run = RunRepository(db).create(Run(task_id=task.id, status="RUNNING"))
    attempt = AttemptRepository(db).create(Attempt(run_id=run.id, attempt_number=1, status="RUNNING"))
    request = AgentRequest(
        agent_id="recovering-native",
        role=AgentRole.WORKER,
        objective=task.objective,
        allowed_capabilities={"test.mutate"},
        budget=AgentBudget(max_turns=5, max_tool_calls=5),
        metadata={"task_id": task.id, "run_id": run.id, "attempt_id": attempt.id},
    )
    return task, run, attempt, request


def _kernel(db, request, tool, responses, validator, *, fault=None, mutation_probe=None):
    registry = ToolRegistry()
    registry.register(tool)
    dispatcher = NativeToolDispatcher(
        db=db,
        registry=registry,
        allowed_capabilities={"test.mutate"},
        allowed_side_effect_capabilities={"test.mutate"},
        fault_injector=fault,
        mutation_probe=mutation_probe,
    )
    authority = CompletionAuthority(db=db, validator=validator, fault_injector=fault)
    return NativeAgentKernel(
        db=db,
        provider=ScriptedProviderAdapter(responses),
        dispatcher=dispatcher,
        completion_authority=authority,
        fault_injector=fault,
    )


def test_crash_before_mutation_does_not_blindly_reexecute(db, make_task):
    _, _, attempt, request = _durable_case(db, make_task)
    tool = MutationTool()
    crash = CrashOnce(NativeFaultPoint.AFTER_TOOL_REQUESTED)
    first = _kernel(db, request, tool, [ProviderResponse(tool_calls=[ProviderToolCall(id="same", name="test.mutate", arguments={"value": 1})])], PassingCommandValidator(), fault=crash)
    with pytest.raises(RuntimeError, match="AFTER_TOOL_REQUESTED"):
        asyncio.run(first.run(request))
    assert tool.calls == 0

    seen = {}
    second = _kernel(db, request, tool, [lambda context: (seen.update(context.sections["execution_state"]["recent_tool_outcomes"][-1]) or ProviderResponse(content="done", completion_claim=True))], PassingCommandValidator())
    result = asyncio.run(second.run(request))
    assert result.status is AgentStatus.COMPLETED and tool.calls == 0
    assert seen["reconciliation"] == "SAFE_TO_RETRY" and seen["retry_was_automatic"] is False
    assert ToolInvocationRepository(db).get(ToolInvocationRepository(db).list_for_attempt(attempt.id)[0].id).state.value == "RECONCILED"


def test_crash_after_mutation_reconciles_and_never_duplicates(db, make_task):
    _, _, attempt, request = _durable_case(db, make_task)
    tool = MutationTool()
    crash = CrashOnce(NativeFaultPoint.AFTER_TOOL_EXECUTED)
    first = _kernel(db, request, tool, [ProviderResponse(tool_calls=[ProviderToolCall(id="mutation", name="test.mutate", arguments={"value": 7})])], PassingCommandValidator(), fault=crash)
    with pytest.raises(RuntimeError, match="AFTER_TOOL_EXECUTED"):
        asyncio.run(first.run(request))
    assert tool.calls == 1 and tool.value == 7

    async def mutation_present():
        return True

    seen = {}
    second = _kernel(db, request, tool, [lambda context: (seen.update(context.sections["execution_state"]["recent_tool_outcomes"][-1]) or ProviderResponse(content="validated", completion_claim=True))], PassingCommandValidator(), mutation_probe=mutation_present)
    result = asyncio.run(second.run(request))
    assert result.status is AgentStatus.COMPLETED and tool.calls == 1
    assert seen["reconciliation"] == "DO_NOT_RETRY"
    assert ToolInvocationRepository(db).list_for_attempt(attempt.id)[0].reconciliation.value == "DO_NOT_RETRY"


def test_crash_before_completion_validation_resumes_candidate(db, make_task):
    _, _, attempt, request = _durable_case(db, make_task)
    tool = MutationTool()
    validator = CountingValidator()
    crash = CrashOnce(NativeFaultPoint.AFTER_CANDIDATE_PERSISTED)
    first = _kernel(db, request, tool, [ProviderResponse(content="candidate", completion_claim=True)], validator, fault=crash)
    with pytest.raises(RuntimeError, match="AFTER_CANDIDATE_PERSISTED"):
        asyncio.run(first.run(request))
    assert validator.calls == 0
    assert CompletionCandidateRepository(db).latest_for_attempt(attempt.id).status.value == "CANDIDATE_COMPLETION"

    second = _kernel(db, request, tool, [], validator)
    result = asyncio.run(second.run(request))
    assert result.status is AgentStatus.COMPLETED and validator.calls == 1


def test_crash_after_completion_validation_does_not_revalidate(db, make_task):
    _, _, attempt, request = _durable_case(db, make_task)
    tool = MutationTool()
    validator = CountingValidator()
    crash = CrashOnce(NativeFaultPoint.AFTER_CANDIDATE_VALIDATED)
    first = _kernel(db, request, tool, [ProviderResponse(content="candidate", completion_claim=True)], validator, fault=crash)
    with pytest.raises(RuntimeError, match="AFTER_CANDIDATE_VALIDATED"):
        asyncio.run(first.run(request))
    assert validator.calls == 1
    assert CompletionCandidateRepository(db).latest_for_attempt(attempt.id).status.value == "ACCEPTED"

    second = _kernel(db, request, tool, [], validator)
    result = asyncio.run(second.run(request))
    assert result.status is AgentStatus.COMPLETED and validator.calls == 1


def test_recovered_snapshot_matches_durable_invocation_state(db, make_task):
    _, _, attempt, request = _durable_case(db, make_task)
    tool = MutationTool()
    crash = CrashOnce(NativeFaultPoint.AFTER_TOOL_OBSERVED)
    first = _kernel(db, request, tool, [ProviderResponse(tool_calls=[ProviderToolCall(id="observed", name="test.mutate", arguments={"value": 3})])], PassingCommandValidator(), fault=crash)
    with pytest.raises(RuntimeError, match="AFTER_TOOL_OBSERVED"):
        asyncio.run(first.run(request))
    invocation = ToolInvocationRepository(db).list_for_attempt(attempt.id)[0]
    snapshot = ExecutionSnapshotRepository(db).get_for_attempt(attempt.id)
    assert invocation.state.value == "FINISHED" and invocation.observed_mutation is True
    assert snapshot.model_turn_count == 1 and snapshot.tool_call_count == 0

    second = _kernel(db, request, tool, [ProviderResponse(content="done", completion_claim=True)], PassingCommandValidator())
    result = asyncio.run(second.run(request))
    assert result.status is AgentStatus.COMPLETED and tool.calls == 1
    recovered = ExecutionSnapshotRepository(db).get_for_attempt(attempt.id)
    assert recovered.tool_call_count == 1 and recovered.workspace_mutation_version == 1


def test_e7a_repeated_failure_state_is_durable_within_attempt(db, make_task):
    from lhas.inner_agent.tool_adapter import ToolAwareObserver, _args_signature

    observer = ToolAwareObserver()
    failure = ToolResult(status=ToolResultStatus.FAILURE, error_type="EDIT_TARGET_NOT_FOUND", metadata={"failure_category": "EDIT_TARGET_NOT_FOUND"})
    args = {"path": "a.py", "old_text": "x", "new_text": "y"}
    observer.decorate("workspace.edit", args, failure, {"capability": "workspace.edit", "status": "FAILURE"}, _args_signature(args))
    state = observer.snapshot()
    restored = ToolAwareObserver()
    restored.restore(state)
    summary = restored.decorate("workspace.edit", args, failure, {"capability": "workspace.edit", "status": "FAILURE"}, _args_signature(args))
    assert summary["failure_repeat_count"] == 2
    assert summary["similar_failure_count"] == 2


def test_e7a_state_resets_across_attempts(db, make_task):
    from lhas.inner_agent.tool_adapter import ToolAwareObserver, _args_signature

    failure = ToolResult(status=ToolResultStatus.FAILURE, error_type="EDIT_TARGET_NOT_FOUND", metadata={"failure_category": "EDIT_TARGET_NOT_FOUND"})
    args = {"path": "a.py", "old_text": "x", "new_text": "y"}
    first = ToolAwareObserver()
    first.decorate("workspace.edit", args, failure, {"capability": "workspace.edit", "status": "FAILURE"}, _args_signature(args))
    second = ToolAwareObserver()
    summary = second.decorate("workspace.edit", args, failure, {"capability": "workspace.edit", "status": "FAILURE"}, _args_signature(args))
    assert "failure_repeat_count" not in summary and "similar_failure_count" not in summary


def test_fresh_outer_orchestrator_reenters_same_native_attempt(db, project, tmp_path):
    from lhas.command_validation import ExplicitCommandValidator, explicit_command_policy
    from lhas.domain.models import Project, Task
    from lhas.native.completion import AcceptedCompletionValidator
    from lhas.native.executor import NativeAgentExecutor
    from lhas.orchestrator_v2 import RecoveringOrchestrator
    from lhas.persistence.repositories import TaskRepository
    from lhas.tools.registry import ToolRegistry
    from lhas.workspace import RunWorkspaceManager, register_staged_workspace_tools

    source = tmp_path / "source"
    source.mkdir()
    (source / "test_ok.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    task = TaskRepository(db).create(Task(project_id=project.id, title="native resume", objective="validate", acceptance_criteria=["pytest"], max_attempts=2, timeout_seconds=30))
    manager = RunWorkspaceManager(db, tmp_path / "workspaces", source_root=source)
    validator = CountingValidator()

    class ProcessExit(BaseException):
        pass

    class ExitAfterCandidate:
        def __init__(self): self.hit_once = False
        def hit(self, point, **context):
            if point is NativeFaultPoint.AFTER_CANDIDATE_PERSISTED and not self.hit_once:
                self.hit_once = True
                raise ProcessExit("simulated process termination")

    crash = ExitAfterCandidate()

    def build_orchestrator(provider_responses, fault=None):
        def factory(workspace):
            registry = ToolRegistry()
            register_staged_workspace_tools(registry, workspace, explicit_command_policy(["pytest", "-q"]))
            dispatcher = NativeToolDispatcher(db=db, registry=registry, allowed_capabilities=set(), allowed_side_effect_capabilities=set(), fault_injector=fault)
            authority = CompletionAuthority(db=db, validator=validator, fault_injector=fault)
            kernel = NativeAgentKernel(db=db, provider=ScriptedProviderAdapter(provider_responses), dispatcher=dispatcher, completion_authority=authority, fault_injector=fault)
            return NativeAgentExecutor(kernel, allowed_capabilities=set(), allowed_side_effect_capabilities=set(), max_turns=3)
        return RecoveringOrchestrator(db, workspace_executor_factory=factory, workspace_manager=manager, validator=AcceptedCompletionValidator(db), executor_type="NativeAgentExecutor")

    first = build_orchestrator([ProviderResponse(content="candidate", completion_claim=True)], crash)
    with pytest.raises(ProcessExit):
        asyncio.run(first.execute_task(task.id))
    run = RunRepository(db).list_for_task(task.id)[0]
    attempts_before = AttemptRepository(db).list_for_run(run.id)
    assert len(attempts_before) == 1 and attempts_before[0].status.value == "RUNNING"
    assert CompletionCandidateRepository(db).latest_for_attempt(attempts_before[0].id).status.value == "CANDIDATE_COMPLETION"

    fresh = build_orchestrator([])
    resumed = asyncio.run(fresh.resume_run(run.id))
    attempts_after = AttemptRepository(db).list_for_run(run.id)
    assert resumed.status.value == "COMPLETED"
    assert len(attempts_after) == 1 and attempts_after[0].id == attempts_before[0].id
    assert attempts_after[0].status.value == "COMPLETED"
    assert validator.calls == 1
