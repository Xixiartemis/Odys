import asyncio

import pytest
from typer.testing import CliRunner

from lhas.agent.models import AgentBudget, AgentRequest, AgentRole
from lhas.cli import app
from lhas.cli_runtime import (
    CliConfigurationError,
    CliNativeProviderFactory,
    ProductRuntime,
    ProviderSettings,
    ResumeControlIntent,
)
from lhas.domain.models import Attempt, Project, Run
from lhas.native.completion import CompletionAuthority
from lhas.native.kernel import NativeAgentKernel
from lhas.native.models import ProviderResponse, ProviderToolCall, ReplanSignal, RuntimeTarget
from lhas.native.persistence import ExecutionSnapshotRepository, ToolInvocationRepository
from lhas.native.provider import OpenAIChatProviderAdapter, ScriptedProviderAdapter
from lhas.native.runtime import RuntimeTargetController
from lhas.native.tools import NativeToolDispatcher
from lhas.persistence.planning_repositories import PlanRepository
from lhas.persistence.repositories import AttemptRepository, ProjectRepository, RunRepository, TaskRepository
from lhas.planning.models import Goal, Plan, PlanStatus, PlanStep
from lhas.planning.replan import MacroReplanService
from lhas.tools.registry import ToolRegistry
from lhas.validation import AlwaysPassValidator


def _target(provider: str, endpoint: str, route: str) -> RuntimeTarget:
    return RuntimeTarget(
        provider_id=provider,
        model_id=f"{provider}-model",
        endpoint_identity=endpoint,
        credential_route_id=route,
        route_type="chat_completions",
    )


class _Completions:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return {"choices": [{"message": {"content": "unused", "tool_calls": []}}]}


class _Client:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.chat = type("Chat", (), {"completions": _Completions()})()


def test_openai_runtime_target_is_derived_from_actual_client_transport():
    client = _Client("https://actual.example:9443/api/v1/?api_key=secret")
    provider = OpenAIChatProviderAdapter(
        model="model",
        api_key="secret",
        base_url="https://configured.example/v1",
        endpoint_identity="caller-claimed.example",
        client=client,
    )
    target = provider.runtime_target
    assert target.endpoint_identity == "https://actual.example:9443/api/v1"
    assert target.endpoint_host == "actual.example"
    assert len(target.endpoint_fingerprint) == 64
    assert "secret" not in str(target.safe_projection())
    assert "caller-claimed" not in str(target.safe_projection())


def test_route_target_mismatch_fails_before_candidate_install(monkeypatch, db):
    old = _target("old", "old-host", "old-route")
    requested = _target("new", "https://requested.example/v1", "route-b")
    monkeypatch.setenv("ODYS_AGENT_API_KEY_ROUTE_B", "secret")
    monkeypatch.setenv("ODYS_AGENT_ENDPOINT_ROUTE_B", "https://actual.example/v1")
    factory = CliNativeProviderFactory(ProviderSettings("old", "old-model"), old)
    controller = RuntimeTargetController(db)
    controller.bind("run", old, run_id="run")
    provider = ScriptedProviderAdapter([], runtime_target=old)
    kernel = NativeAgentKernel(
        db=db,
        provider=provider,
        dispatcher=NativeToolDispatcher(
            db=db,
            registry=ToolRegistry(),
            allowed_capabilities=set(),
            allowed_side_effect_capabilities=set(),
        ),
        completion_authority=CompletionAuthority(db=db, validator=AlwaysPassValidator()),
        runtime_target_controller=controller,
        provider_factory=factory,
    )
    switch = kernel.switch_runtime_target(requested, expected_current=old, runtime_id="run")
    assert switch.state.value == "FAILED"
    assert "RUNTIME_TARGET_TRANSPORT_MISMATCH" in switch.failure_reason
    assert kernel.provider is provider
    assert controller.current("run")["effective_target"] == old
    assert provider.calls == []


def test_request_time_transport_change_fails_before_model_call(db, make_task):
    client = _Client("https://a.example/v1")
    provider = OpenAIChatProviderAdapter(model="m", api_key="secret", client=client)
    durable = provider.runtime_target
    task = make_task()
    run = RunRepository(db).create(Run(task_id=task.id, status="RUNNING"))
    attempt = AttemptRepository(db).create(Attempt(run_id=run.id, attempt_number=1, status="RUNNING"))
    controller = RuntimeTargetController(db)
    controller.bind(run.id, durable, run_id=run.id)
    client.base_url = "https://b.example/v1"
    kernel = NativeAgentKernel(
        db=db,
        provider=provider,
        dispatcher=NativeToolDispatcher(db=db, registry=ToolRegistry(), allowed_capabilities=set(), allowed_side_effect_capabilities=set()),
        completion_authority=CompletionAuthority(db=db, validator=AlwaysPassValidator()),
        runtime_target_controller=controller,
    )
    request = AgentRequest(
        agent_id="transport-divergence",
        role=AgentRole.WORKER,
        objective=task.objective,
        budget=AgentBudget(max_turns=1, max_tool_calls=1),
        metadata={
            "task_id": task.id,
            "run_id": run.id,
            "attempt_id": attempt.id,
            "configured_target": durable.model_dump(mode="json"),
        },
    )
    result = asyncio.run(kernel.run(request))
    snapshot = ExecutionSnapshotRepository(db).get_for_attempt(attempt.id)
    assert result.error_type == "RUNTIME_TARGET_DIVERGENCE"
    assert client.chat.completions.calls == []
    assert snapshot.actual_transport_endpoint_identity == "https://b.example:443/v1"
    assert snapshot.actual_transport_endpoint_fingerprint == snapshot.actual_provider_target.endpoint_fingerprint


def test_resume_migration_arguments_fail_closed(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "ok.py").write_text("value = 1\n", encoding="utf-8")
    runtime = ProductRuntime(tmp_path / "runtime.db")
    try:
        prepared = runtime.prepare_new(
            goal="verify",
            repo=source,
            verify_argv=["python", "-c", "print('ok')"],
            max_attempts=1,
            max_turns=1,
            provider="offline",
            kernel="external",
        )
        run = asyncio.run(prepared.orchestrator.prepare_task_run(prepared.task.id))
        with pytest.raises(CliConfigurationError, match="PARTIAL_PROVIDER_MIGRATION"):
            runtime.prepare_resume(run.id, migrate_provider="b")
        with pytest.raises(CliConfigurationError, match="REQUIRES_NATIVE"):
            runtime.prepare_resume(
                run.id,
                migrate_provider="b",
                migrate_model="m",
                credential_route="route-b",
            )
    finally:
        runtime.close()


def test_cli_provider_migration_resumes_same_native_execution(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "module.py").write_text("value = 1\n", encoding="utf-8")
    db_path = tmp_path / "runtime.db"
    monkeypatch.setenv("ODYS_AGENT_MODEL", "provider-a-model")
    monkeypatch.setenv("ODYS_AGENT_API_KEY", "provider-a-secret")
    monkeypatch.setenv("ODYS_AGENT_BASE_URL", "https://a.example/v1")
    monkeypatch.setenv("ODYS_AGENT_API_KEY_ROUTE_B", "provider-b-secret")
    monkeypatch.setenv("ODYS_AGENT_ENDPOINT_ROUTE_B", "https://b.example/api/v1")

    class MonthlyQuota(Exception):
        status_code = 429

        def __str__(self):
            return "Monthly usage limit reached; reset in 11 days"

    providers = {}

    def deterministic_provider(_factory, target):
        if target.provider_id == "mimo":
            providers.setdefault(
                "a",
                ScriptedProviderAdapter(
                    [
                        ProviderResponse(tool_calls=[ProviderToolCall(
                            id="edit-once",
                            name="workspace.edit",
                            arguments={"path": "module.py", "old_text": "value = 1", "new_text": "value = 2"},
                        )]),
                        MonthlyQuota(),
                    ],
                    runtime_target=target,
                ),
            )
            return providers["a"]
        providers.setdefault(
            "b",
            ScriptedProviderAdapter(
                [ProviderResponse(content="workspace repair complete", completion_claim=True)],
                runtime_target=target,
            ),
        )
        return providers["b"]

    monkeypatch.setattr(CliNativeProviderFactory, "__call__", deterministic_provider)
    runtime = ProductRuntime(db_path)
    try:
        prepared = runtime.prepare_new(
            goal="change module value and verify it",
            repo=source,
            verify_argv=["python", "-c", "from pathlib import Path; assert 'value = 2' in Path('module.py').read_text()"],
            max_attempts=2,
            max_turns=4,
            provider="mimo",
            kernel="native",
        )
        blocked = asyncio.run(prepared.execute())
        task_id = prepared.task.id
        run_id = blocked.id
        attempts = AttemptRepository(runtime.db).list_for_run(run_id)
        attempt_id = attempts[0].id
        before = ExecutionSnapshotRepository(runtime.db).get_for_attempt(attempt_id)
        old_calls_at_block = len(providers["a"].calls)
        assert blocked.status.value == "BLOCKED_PROVIDER"
        assert TaskRepository(runtime.db).get(task_id).status.value == "BLOCKED_PROVIDER"
        assert len(attempts) == 1
    finally:
        runtime.close()

    result = CliRunner().invoke(app, [
        "resume",
        run_id,
        "--migrate-provider", "provider-b",
        "--migrate-model", "provider-b-model",
        "--credential-route", "route-b",
        "--no-ui",
        "--db", str(db_path),
    ])
    assert result.exit_code == 0, result.output
    assert "RESULT: PASS" in result.output
    assert "RUNTIME TARGET" in result.output
    assert "configured:" in result.output
    assert "effective:" in result.output
    assert "transport:" in result.output
    assert "fingerprint:" in result.output

    runtime = ProductRuntime(db_path)
    try:
        task = TaskRepository(runtime.db).get(task_id)
        run = RunRepository(runtime.db).get(run_id)
        attempts = AttemptRepository(runtime.db).list_for_run(run_id)
        after = ExecutionSnapshotRepository(runtime.db).get_for_attempt(attempt_id)
        invocations = ToolInvocationRepository(runtime.db).list_for_attempt(attempt_id)
        assert task.status.value == "COMPLETED"
        assert run.status.value == "COMPLETED"
        assert len(attempts) == 1 and attempts[0].id == attempt_id
        assert after.id == before.id
        assert len(providers["a"].calls) == old_calls_at_block
        assert len(providers["b"].calls) > 0
        assert sum(item.capability == "workspace.edit" for item in invocations) == 1
        assert after.workspace_mutation_version == 1
    finally:
        runtime.close()
    assert (source / "module.py").read_text(encoding="utf-8") == "value = 1\n"


class _ConcurrentPlanner:
    def __init__(self, name, ready, release):
        self.name = name
        self.ready = ready
        self.release = release

    async def create_plan(self, *, goal, capabilities, context=None):
        self.ready.set()
        await self.release.wait()
        return Plan(
            goal_id=goal.id,
            status=PlanStatus.READY,
            steps=[PlanStep(title=self.name, objective=self.name, capability=self.name)],
        )


def test_concurrent_replans_use_cas_and_preserve_first_commit(db):
    project = ProjectRepository(db).create(Project(name="cas", type="test"))
    goal = Goal(project_id=project.id, objective="cas")
    plan = Plan(
        goal_id=goal.id,
        status=PlanStatus.RUNNING,
        steps=[PlanStep(title="old", objective="old", capability="old")],
    )
    PlanRepository(db).create(plan)
    signal = ReplanSignal(
        task_id="task",
        run_id="run",
        attempt_id="attempt",
        reason="INVALID_ASSUMPTION",
    )

    async def race():
        ready_a, ready_b = asyncio.Event(), asyncio.Event()
        release_a, release_b = asyncio.Event(), asyncio.Event()
        service_a = MacroReplanService(db, _ConcurrentPlanner("route-a", ready_a, release_a))
        service_b = MacroReplanService(db, _ConcurrentPlanner("route-b", ready_b, release_b))
        task_a = asyncio.create_task(service_a.consume(goal=goal, plan=plan, signals=[signal]))
        task_b = asyncio.create_task(service_b.consume(goal=goal, plan=plan, signals=[signal]))
        await ready_a.wait()
        await ready_b.wait()
        release_a.set()
        result_a = await task_a
        release_b.set()
        result_b = await task_b
        return result_a, result_b

    result_a, result_b = asyncio.run(race())
    authoritative = PlanRepository(db).get(plan.id)
    assert result_a.accepted is True
    assert result_b.accepted is False
    assert result_b.error_type == "REPLAN_VERSION_CONFLICT"
    assert any(step.capability == "route-a" for step in authoritative.steps)
    assert not any(step.capability == "route-b" for step in authoritative.steps)
    assert authoritative.version == result_a.plan.version


def test_async_install_result_fails_before_switch_commit(db):
    old = _target("old", "old-host", "old-route")
    new = _target("new", "new-host", "new-route")
    controller = RuntimeTargetController(db)
    controller.bind("run", old, run_id="run")

    async def install(_candidate):
        return None

    switch = controller.request_switch(
        "run",
        new,
        expected_current=old,
        prepare=lambda: ScriptedProviderAdapter([], runtime_target=new),
        install=install,
    )
    assert switch.state.value == "FAILED"
    assert "ASYNC_PROVIDER_INSTALL_UNSUPPORTED" in switch.failure_reason
    assert controller.current("run")["effective_target"] == old
