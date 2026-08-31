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
)
from lhas.persistence.repositories import AttemptRepository, RunRepository
from lhas.tools.registry import ToolRegistry
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

