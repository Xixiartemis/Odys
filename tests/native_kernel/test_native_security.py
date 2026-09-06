import asyncio
import json

from lhas.agent.models import AgentBudget, AgentRequest, AgentRole
from lhas.native.context import NativeContextAssembler
from lhas.native.models import ExecutionSnapshot, ProviderToolCall
from lhas.native.persistence import ToolInvocationRepository
from lhas.native.tools import NativeToolDispatcher
from lhas.persistence.event_store import EventStore
from lhas.capability_registry import (
    CapabilityDefinition,
    CapabilityRegistry,
    RuntimePlatform,
    default_capabilities,
)
from lhas.planning.models import CapabilitySpec
from lhas.tools.protocol import ToolResult, ToolResultStatus
from lhas.tools.registry import ToolRegistry


class SecretEchoTool:
    capability = CapabilitySpec(name="test.secret", description="security test")

    async def execute(self, request):
        return ToolResult(status=ToolResultStatus.SUCCESS, output={"message": "token=super-secret", "path": "safe.txt"})


def test_native_context_is_deterministic_and_bounded():
    snapshot = ExecutionSnapshot(task_id="task", run_id="run", attempt_id="attempt", goal="goal", recent_tool_outcomes=[{"content": "x" * 20_000}])
    request = AgentRequest(agent_id="a", role=AgentRole.WORKER, objective="goal", context={"selected_memory": ["m" * 20_000], "selected_knowledge": ["k" * 20_000]}, budget=AgentBudget(max_context_chars=2_000))
    assembler = NativeContextAssembler()
    first = assembler.build(request, snapshot)
    second = assembler.build(request, snapshot)
    assert first.model_dump() == second.model_dump()
    assert first.chars_used <= 2_000
    assert first.truncated_sections


def _test_secret_definition() -> CapabilityDefinition:
    """Minimal CapabilityDefinition for the test.secret capability."""
    return CapabilityDefinition(
        id="test.secret",
        name="test.secret",
        description="security test",
        category="test",
        version="v1",
        input_schema={"type": "object", "additionalProperties": True},
        output_schema={"type": "object"},
        platforms=(RuntimePlatform.WINDOWS, RuntimePlatform.LINUX, RuntimePlatform.MACOS),
        permissions=(),
        risk_level="LOW",
        workspace_scope="SOURCE_WORKSPACE",
        timeout_seconds=30.0,
        retryable=True,
        preferred_tool="test.secret",
        fallback_tools=(),
        source="test",
        evidence_type="DETERMINISTIC_TOOL_RESULT",
    )


def test_raw_tool_arguments_and_secrets_are_not_persisted(db):
    registry = ToolRegistry()
    registry.register(SecretEchoTool())
    cap_registry = CapabilityRegistry(
        registry, definitions=(*default_capabilities(), _test_secret_definition()),
    )
    dispatcher = NativeToolDispatcher(
        db=db, registry=registry, allowed_capabilities={"test.secret"},
        allowed_side_effect_capabilities=set(), capability_registry=cap_registry,
    )
    snapshot = ExecutionSnapshot(task_id="task", run_id="run", attempt_id="attempt", goal="goal")
    request = AgentRequest(agent_id="a", role=AgentRole.WORKER, objective="goal", allowed_capabilities={"test.secret"}, metadata={"task_id": "task", "run_id": "run", "attempt_id": "attempt"})
    observation = asyncio.run(dispatcher.dispatch(ProviderToolCall(id="secret-call", name="test.secret", arguments={"token": "super-secret", "query": "private"}), request, snapshot))
    invocation = ToolInvocationRepository(db).list_for_attempt("attempt")[0]
    persisted = json.dumps([event.payload for event in EventStore(db).list_for_attempt("attempt")], sort_keys=True)
    assert len(invocation.args_fingerprint) == 64
    assert "super-secret" not in persisted and "private" not in persisted
    assert "super-secret" not in json.dumps(observation)
    assert "[REDACTED]" in json.dumps(observation)


def test_native_tool_schema_excludes_unauthorized_side_effect(db):
    class Mutator:
        capability = CapabilitySpec(name="test.mutate", side_effect=True)

        async def execute(self, request):
            return ToolResult(status=ToolResultStatus.SUCCESS)

    registry = ToolRegistry()
    registry.register(Mutator())
    dispatcher = NativeToolDispatcher(db=db, registry=registry, allowed_capabilities={"test.mutate"}, allowed_side_effect_capabilities=set())
    assert dispatcher.tool_schemas() == []


def test_safe_native_events_do_not_expose_workspace_root(db, tmp_path):
    class PathFailure:
        capability = CapabilitySpec(name="workspace.read")

        async def execute(self, request):
            return ToolResult(status=ToolResultStatus.FAILURE, error_type="WORKSPACE_PATH_ESCAPE", error_message="path outside workspace", metadata={"failure_category": "WORKSPACE_PATH_ERROR", "workspace_root_exposed": False})

    registry = ToolRegistry()
    registry.register(PathFailure())
    dispatcher = NativeToolDispatcher(db=db, registry=registry, allowed_capabilities={"workspace.read"}, allowed_side_effect_capabilities=set())
    snapshot = ExecutionSnapshot(task_id="task", run_id="run", attempt_id="attempt", goal="goal")
    request = AgentRequest(agent_id="a", role=AgentRole.WORKER, objective="goal", allowed_capabilities={"workspace.read"}, metadata={"task_id": "task", "run_id": "run", "attempt_id": "attempt"})
    asyncio.run(dispatcher.dispatch(ProviderToolCall(id="path", name="workspace.read", arguments={"path": "../escape"}), request, snapshot))
    persisted = json.dumps([event.payload for event in EventStore(db).list_for_attempt("attempt")])
    assert str(tmp_path.resolve()) not in persisted
    assert "WORKSPACE_PATH_ESCAPE" in persisted
