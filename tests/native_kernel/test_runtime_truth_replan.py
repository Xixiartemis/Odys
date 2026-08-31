import asyncio

import pytest

from lhas.agent.models import AgentBudget, AgentRequest, AgentRole, AgentStatus
from lhas.domain.models import Attempt, Run
from lhas.native import (
    CompletionAuthority,
    ExecutionSnapshotRepository,
    NativeAgentKernel,
    NativeToolDispatcher,
    ProviderFailureClassifier,
    ProviderFailureCategory,
    ProviderHealthRepository,
    ProviderHealthState,
    ProviderResponse,
    RuntimeTarget,
    RuntimeTargetController,
    RuntimeTargetError,
    RuntimeTargetResolver,
    ScriptedProviderAdapter,
    ProviderToolCall,
)
from lhas.persistence.repositories import AttemptRepository, RunRepository
from lhas.tools.registry import ToolRegistry
from lhas.tools.protocol import ToolResult, ToolResultStatus
from lhas.validation import AlwaysPassValidator


def _target(provider, route):
    return RuntimeTarget(provider_id=provider, model_id="mimo-v2.5-pro", endpoint_identity=f"{provider}-host", credential_route_id=route, route_type="chat")


def test_same_model_ambiguity_and_explicit_provider_fail_closed():
    a, b = _target("xiaomi-mimo", "a"), _target("opencode-go", "b")
    with pytest.raises(RuntimeTargetError, match="multiple") as exc:
        RuntimeTargetResolver.resolve("mimo-v2.5-pro", [a, b])
    assert exc.value.code == "AMBIGUOUS_RUNTIME_TARGET"
    assert RuntimeTargetResolver.resolve("mimo-v2.5-pro", [a, b], provider_id="xiaomi-mimo") == a


def test_target_switch_is_atomic_and_isolated(db):
    a, b, z = _target("a", "a"), _target("b", "b"), _target("z", "z")
    controller = RuntimeTargetController(db)
    controller.bind("run-a", a, run_id="run-a", session_id="session-a")
    controller.bind("run-b", b, run_id="run-b", session_id="session-b")
    switched = controller.request_switch("run-a", z, expected_current=a, runtime_id="run-a")
    assert switched.state.value == "COMMITTED"
    assert controller.current("run-a")["effective_target"] == z
    assert controller.current("run-b")["effective_target"] == b
    with pytest.raises(RuntimeTargetError, match="stale"):
        controller.request_switch("run-a", b, expected_current=z, runtime_id="stale-run")
    failed = controller.request_switch("run-a", b, expected_current=z, confirm=False)
    assert failed.state.value == "FAILED"
    assert controller.current("run-a")["effective_target"] == z


def test_quota_classification_and_route_health_are_bounded(db):
    class MonthlyQuota(Exception):
        status_code = 429
        def __str__(self):
            return "Monthly usage limit reached; reset in 11 days"

    assert ProviderFailureClassifier.classify(MonthlyQuota()) is ProviderFailureCategory.QUOTA_EXHAUSTED
    target = _target("a", "a")
    health = ProviderHealthRepository(db)
    health.record(target, ProviderHealthState.QUOTA_BLOCKED, category=ProviderFailureCategory.QUOTA_EXHAUSTED, reason="monthly quota")
    assert health.get(target)["state"] == "QUOTA_BLOCKED"


@pytest.mark.parametrize(("message", "expected"), [
    ("Rate limit reached", ProviderFailureCategory.TRANSIENT_RATE_LIMIT),
    ("Too many requests; Retry-After: 2", ProviderFailureCategory.TRANSIENT_RATE_LIMIT),
    ("Monthly usage limit reached; reset in 11 days", ProviderFailureCategory.QUOTA_EXHAUSTED),
    ("ambiguous provider response", ProviderFailureCategory.UNKNOWN_PROVIDER_FAILURE),
])
def test_429_taxonomy_is_conservative(message, expected):
    class ProviderError(Exception):
        status_code = 429
    assert ProviderFailureClassifier.classify(ProviderError(message)) is expected


def _kernel_with_runtime(db, provider, controller, factory):
    dispatcher = NativeToolDispatcher(db=db, registry=ToolRegistry(), allowed_capabilities=set(), allowed_side_effect_capabilities=set())
    return NativeAgentKernel(
        db=db,
        provider=provider,
        dispatcher=dispatcher,
        completion_authority=CompletionAuthority(db=db, validator=AlwaysPassValidator()),
        runtime_target_controller=controller,
        provider_factory=factory,
    )


def test_provider_factory_failure_rolls_back_actual_and_durable_target(db, make_task):
    a, b = _target("provider-a", "route-a"), _target("provider-b", "route-b")
    task = make_task()
    run = RunRepository(db).create(Run(task_id=task.id, status="RUNNING"))
    controller = RuntimeTargetController(db)
    controller.bind(run.id, a, run_id=run.id)
    provider_a = ScriptedProviderAdapter([], runtime_target=a)

    def failing_factory(target):
        if target == b:
            raise RuntimeError("provider B unavailable")
        return ScriptedProviderAdapter([], runtime_target=target)

    kernel = _kernel_with_runtime(db, provider_a, controller, failing_factory)
    failed = kernel.switch_runtime_target(b, expected_current=a, runtime_id=run.id)
    assert failed.state.value == "FAILED"
    assert controller.current(run.id)["effective_target"] == a
    assert kernel.provider is provider_a

    provider_b = ScriptedProviderAdapter([], runtime_target=b)
    kernel.provider_factory = lambda target: provider_b if target == b else provider_a
    committed = kernel.switch_runtime_target(b, expected_current=a, runtime_id=run.id)
    assert committed.state.value == "COMMITTED"
    assert controller.current(run.id)["effective_target"] == b
    assert kernel.provider is provider_b


def test_runtime_divergence_fails_closed_before_provider_call(db, make_task):
    a, b = _target("provider-a", "route-a"), _target("provider-b", "route-b")
    task = make_task(acceptance_criteria=["accepted"])
    run = RunRepository(db).create(Run(task_id=task.id, status="RUNNING"))
    attempt = AttemptRepository(db).create(Attempt(run_id=run.id, attempt_number=1, status="RUNNING"))
    controller = RuntimeTargetController(db)
    controller.bind(run.id, b, run_id=run.id)
    provider_a = ScriptedProviderAdapter([ProviderResponse(content="must not run", completion_claim=True)], runtime_target=a)
    kernel = _kernel_with_runtime(db, provider_a, controller, None)
    request = AgentRequest(
        agent_id="divergence", role=AgentRole.WORKER, objective=task.objective,
        budget=AgentBudget(max_turns=1, max_tool_calls=1),
        metadata={"task_id": task.id, "run_id": run.id, "attempt_id": attempt.id,
                  "configured_target": b.model_dump(mode="json")},
    )
    result = asyncio.run(kernel.run(request))
    snapshot = ExecutionSnapshotRepository(db).get_for_attempt(attempt.id)
    assert result.status is AgentStatus.FAILED
    assert result.error_type == "RUNTIME_TARGET_DIVERGENCE"
    assert provider_a.calls == []
    assert snapshot.actual_provider_target == a
    assert snapshot.current_failure["type"] == "RUNTIME_TARGET_DIVERGENCE"


def test_provider_migration_resumes_same_attempt_on_new_provider(db, make_task):
    a, b = _target("provider-a", "route-a"), _target("provider-b", "route-b")
    task = make_task(acceptance_criteria=["accepted"])
    run = RunRepository(db).create(Run(task_id=task.id, status="RUNNING"))
    attempt = AttemptRepository(db).create(Attempt(run_id=run.id, attempt_number=1, status="RUNNING"))
    controller = RuntimeTargetController(db)

    class MonthlyQuota(Exception):
        status_code = 429
        def __str__(self):
            return "Monthly usage limit reached; reset in 11 days"

    class MutationTool:
        from lhas.planning.models import CapabilitySpec
        capability = CapabilitySpec(name="workspace.edit", side_effect=True)
        async def execute(self, request):
            return ToolResult(
                status=ToolResultStatus.SUCCESS,
                output={"before_sha256": "a" * 64, "after_sha256": "b" * 64},
            )

    registry = ToolRegistry()
    registry.register(MutationTool())
    provider_a = ScriptedProviderAdapter([
        ProviderResponse(tool_calls=[ProviderToolCall(id="mutation-1", name="workspace.edit", arguments={})]),
        MonthlyQuota(),
    ], runtime_target=a)
    provider_b = ScriptedProviderAdapter([ProviderResponse(content="finished", completion_claim=True)], runtime_target=b)
    providers = {a.composite_id: provider_a, b.composite_id: provider_b}
    controller.bind(run.id, a, run_id=run.id)
    dispatcher = NativeToolDispatcher(db=db, registry=registry, allowed_capabilities={"workspace.edit"}, allowed_side_effect_capabilities={"workspace.edit"})
    kernel = NativeAgentKernel(
        db=db, provider=provider_a, dispatcher=dispatcher,
        completion_authority=CompletionAuthority(db=db, validator=AlwaysPassValidator()),
        runtime_target_controller=controller,
        provider_factory=lambda target: providers[target.composite_id],
    )
    request = AgentRequest(
        agent_id="migration", role=AgentRole.WORKER, objective=task.objective,
        allowed_capabilities={"workspace.edit"},
        budget=AgentBudget(max_turns=4, max_tool_calls=4),
        metadata={"task_id": task.id, "run_id": run.id, "attempt_id": attempt.id,
                  "configured_target": a.model_dump(mode="json")},
    )
    first = asyncio.run(kernel.run(request))
    assert first.status is AgentStatus.FAILED
    calls_before_commit = len(provider_a.calls)
    snapshot_before = ExecutionSnapshotRepository(db).get_for_attempt(attempt.id)
    switch = kernel.switch_runtime_target(b, expected_current=a, runtime_id=run.id)
    assert switch.state.value == "COMMITTED"
    second = asyncio.run(kernel.run(request))
    snapshot_after = ExecutionSnapshotRepository(db).get_for_attempt(attempt.id)
    assert second.status is AgentStatus.COMPLETED
    assert len(provider_a.calls) == calls_before_commit
    assert len(provider_b.calls) >= 1
    assert snapshot_after.id == snapshot_before.id
    assert snapshot_after.task_id == task.id and snapshot_after.run_id == run.id and snapshot_after.attempt_id == attempt.id
    assert snapshot_after.effective_target == b
    assert snapshot_after.workspace_mutation_version >= 1


def test_native_turn_durably_records_configured_and_effective_targets(db, make_task):
    target = _target("provider-a", "route-a")
    task = make_task(acceptance_criteria=["accepted"])
    run = RunRepository(db).create(Run(task_id=task.id, status="RUNNING"))
    attempt = AttemptRepository(db).create(Attempt(run_id=run.id, attempt_number=1, status="RUNNING"))
    provider = ScriptedProviderAdapter([ProviderResponse(content="done", completion_claim=True)], runtime_target=target)
    dispatcher = NativeToolDispatcher(db=db, registry=ToolRegistry(), allowed_capabilities=set(), allowed_side_effect_capabilities=set())
    kernel = NativeAgentKernel(db=db, provider=provider, dispatcher=dispatcher, completion_authority=CompletionAuthority(db=db, validator=AlwaysPassValidator()))
    request = AgentRequest(agent_id="truth", role=AgentRole.WORKER, objective=task.objective,
        budget=AgentBudget(max_turns=2, max_tool_calls=1), metadata={"task_id": task.id, "run_id": run.id, "attempt_id": attempt.id,
        "configured_target": target.model_dump(mode="json")})
    result = asyncio.run(kernel.run(request))
    assert result.status is AgentStatus.COMPLETED
    snapshot = ExecutionSnapshotRepository(db).get_for_attempt(attempt.id)
    assert snapshot.configured_target == target == snapshot.effective_target
    assert provider.calls
