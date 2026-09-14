"""Offline adversarial evidence for the P0-A execution-control contract."""

from __future__ import annotations

import asyncio
import sys

import pytest

from lhas.agent.kernel import ScriptedAgentKernel
from lhas.agent.models import AgentRequest, AgentRole, AgentResult, AgentStatus
from lhas.capability_registry import CapabilityRegistry
from lhas.domain.enums import EventType
from lhas.execution_control import (
    ExecutionControlError,
    ExecutionControlToken,
    ExecutionLayerTimeout,
    await_with_control,
)
from lhas.mcp.manager import MCPManager, _Connection
from lhas.mcp.models import MCPServerConfig, MCPToolInfo
from lhas.native.models import ModelContext, ProviderToolCall, ExecutionSnapshot
from lhas.native.provider import OpenAIChatProviderAdapter
from lhas.native.tools import NativeToolDispatcher
from lhas.persistence.database import Database
from lhas.persistence.event_store import EventStore
from lhas.planning.models import CapabilitySpec
from lhas.tools.registry import ToolRegistry
from lhas.workspace import CommandPolicy, CommandRule, LocalReadOnlyWorkspace
from lhas.workspace.safe_cli import SafeCli
from tests.helpers import make_test_capability_definition
from lhas.tools.contract import ToolContract


async def _blocked(started: asyncio.Event | None = None) -> None:
    if started is not None:
        started.set()
    await asyncio.Event().wait()


def _context() -> ModelContext:
    return ModelContext(messages=[], sections={}, chars_used=0, budget_chars=1)


class _BlockedCompletions:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def create(self, **_kwargs):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _FakeClient:
    def __init__(self, completions: _BlockedCompletions) -> None:
        self.chat = type("Chat", (), {"completions": completions})()
        self.base_url = "https://provider.invalid/v1"


@pytest.mark.asyncio
async def test_provider_is_stopped_by_root_cancel_and_late_response_is_not_consumed():
    completions = _BlockedCompletions()
    provider = OpenAIChatProviderAdapter(
        model="offline-test",
        api_key="test-only",
        base_url="https://provider.invalid/v1",
        client=_FakeClient(completions),
    )
    control = ExecutionControlToken("run-provider", attempt_id="a1", timeout_seconds=10)
    provider.bind_execution_control(control)
    task = asyncio.create_task(
        provider.generate(context=_context(), tools=[], timeout_seconds=10)
    )
    await completions.started.wait()
    control.cancel("USER_CANCEL", source="test")
    with pytest.raises(ExecutionControlError) as raised:
        await task
    assert raised.value.reason == "USER_CANCEL"
    assert completions.cancelled is True
    assert task.done()


@pytest.mark.asyncio
async def test_provider_root_deadline_precedes_long_provider_ceiling():
    completions = _BlockedCompletions()
    provider = OpenAIChatProviderAdapter(
        model="offline-test",
        api_key="test-only",
        base_url="https://provider.invalid/v1",
        client=_FakeClient(completions),
    )
    control = ExecutionControlToken("run-provider-deadline", attempt_id="a1", timeout_seconds=0.03)
    provider.bind_execution_control(control)
    with pytest.raises(ExecutionControlError) as raised:
        await provider.generate(context=_context(), tools=[], timeout_seconds=30)
    assert raised.value.failure_type == "ROOT_DEADLINE_EXCEEDED"
    assert completions.cancelled is True


class _BlockedTool:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    @property
    def capability(self):
        return CapabilitySpec(name="test.blocked", description="offline blocked tool")

    async def execute(self, _request):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _blocked_dispatcher(db: Database):
    tool = _BlockedTool()
    registry = ToolRegistry()
    registry.register(tool)
    definition = make_test_capability_definition("test.blocked")
    capability_registry = CapabilityRegistry(registry, definitions=[definition])
    contract = ToolContract(capability_registry, registry)
    dispatcher = NativeToolDispatcher(
        db=db,
        registry=registry,
        allowed_capabilities={"test.blocked"},
        allowed_side_effect_capabilities=set(),
        capability_registry=capability_registry,
        tool_contract=contract,
    )
    return dispatcher, tool


def _agent_request() -> AgentRequest:
    return AgentRequest(
        agent_id="agent-p0a",
        role=AgentRole.WORKER,
        objective="offline control test",
        metadata={"task_id": "task-p0a", "run_id": "run-p0a", "attempt_id": "attempt-p0a"},
    )


@pytest.mark.asyncio
async def test_tool_is_stopped_by_explicit_cancel_and_started_invocation_remains_durable():
    db = Database(":memory:")
    db.init_db()
    dispatcher, tool = _blocked_dispatcher(db)
    control = ExecutionControlToken("run-tool", attempt_id="attempt-tool", timeout_seconds=10)
    call = ProviderToolCall(id="call-tool", name="test.blocked", arguments={})
    snapshot = ExecutionSnapshot(task_id="task-p0a", run_id="run-tool", attempt_id="attempt-tool", goal="tool")
    task = asyncio.create_task(dispatcher.dispatch(call, _agent_request(), snapshot, execution_control=control))
    await tool.started.wait()
    control.cancel("USER_CANCEL", source="test")
    with pytest.raises(ExecutionControlError):
        await task
    assert tool.cancelled is True
    invocations = dispatcher.invocations.list_for_attempt("attempt-tool")
    assert len(invocations) == 1
    assert invocations[0].state.value == "STARTED"
    db.close()


@pytest.mark.asyncio
async def test_tool_local_ceiling_wins_without_cancelling_root():
    control = ExecutionControlToken("run-tool-ceiling", timeout_seconds=10)
    with pytest.raises(ExecutionLayerTimeout) as raised:
        await await_with_control(
            _blocked(), control=control, local_ceiling=0.03,
            timeout_failure_type="TOOL_TIMEOUT", source="tool",
        )
    assert raised.value.failure_type == "TOOL_TIMEOUT"
    assert control.terminal is False
    control.cancel("USER_CANCEL")


@pytest.mark.asyncio
async def test_tool_root_deadline_wins_over_tool_ceiling():
    db = Database(":memory:")
    db.init_db()
    dispatcher, tool = _blocked_dispatcher(db)
    control = ExecutionControlToken("run-tool-deadline", attempt_id="attempt-tool-deadline", timeout_seconds=0.03)
    call = ProviderToolCall(id="call-tool-deadline", name="test.blocked", arguments={})
    snapshot = ExecutionSnapshot(task_id="task-p0a", run_id="run-tool-deadline", attempt_id="attempt-tool-deadline", goal="tool")
    task = asyncio.create_task(dispatcher.dispatch(call, _agent_request(), snapshot, execution_control=control))
    await tool.started.wait()
    with pytest.raises(ExecutionControlError) as raised:
        await task
    assert raised.value.failure_type == "ROOT_DEADLINE_EXCEEDED"
    assert tool.cancelled is True
    db.close()


class _BlockedStream:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    def write(self, _payload):
        self.started.set()

    async def drain(self):
        await asyncio.Event().wait()

    async def readline(self):
        await asyncio.Event().wait()


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = _BlockedStream()
        self.stdout = _BlockedStream()
        self.returncode = None
        self.terminated = False

    def terminate(self):
        self.terminated = True


class _ClosedStream:
    def write(self, _payload):
        return None

    async def drain(self):
        return None

    async def readline(self):
        return b""


class _ClosedProcess:
    def __init__(self) -> None:
        self.stdin = _ClosedStream()
        self.stdout = _ClosedStream()
        self.returncode = 0

    def terminate(self):
        return None


@pytest.mark.asyncio
async def test_mcp_request_observes_root_deadline_and_terminates_transport():
    manager = MCPManager()
    process = _FakeProcess()
    manager._connections["slow"] = _Connection(
        config=MCPServerConfig(name="slow", command=[sys.executable], timeout_seconds=30),
        process=process,
        lock=asyncio.Lock(),
    )
    manager._tools["mcp.slow.echo"] = MCPToolInfo(name="mcp.slow.echo", server_name="slow")
    control = ExecutionControlToken("run-mcp", timeout_seconds=10)
    task = asyncio.create_task(manager.call_tool("mcp.slow.echo", {}, execution_control=control))
    await process.stdin.started.wait()
    control.cancel("USER_CANCEL", source="test")
    with pytest.raises(ExecutionControlError):
        await task
    assert process.terminated is True
    manager._connections.clear()


@pytest.mark.asyncio
async def test_mcp_request_root_deadline_wins_over_transport_ceiling():
    manager = MCPManager()
    process = _FakeProcess()
    manager._connections["slow"] = _Connection(
        config=MCPServerConfig(name="slow", command=[sys.executable], timeout_seconds=30),
        process=process,
        lock=asyncio.Lock(),
    )
    manager._tools["mcp.slow.echo"] = MCPToolInfo(name="mcp.slow.echo", server_name="slow")
    control = ExecutionControlToken("run-mcp-deadline", timeout_seconds=0.03)
    task = asyncio.create_task(manager.call_tool("mcp.slow.echo", {}, execution_control=control))
    await process.stdin.started.wait()
    with pytest.raises(ExecutionControlError) as raised:
        await task
    assert raised.value.failure_type == "ROOT_DEADLINE_EXCEEDED"
    assert process.terminated is True
    manager._connections.clear()


@pytest.mark.asyncio
async def test_mcp_server_death_is_explicit_failure():
    manager = MCPManager()
    manager._connections["dead"] = _Connection(
        config=MCPServerConfig(name="dead", command=[sys.executable]),
        process=_ClosedProcess(),
        lock=asyncio.Lock(),
    )
    manager._tools["mcp.dead.echo"] = MCPToolInfo(name="mcp.dead.echo", server_name="dead")
    with pytest.raises(RuntimeError, match="MCP_SERVER_CLOSED"):
        await manager.call_tool("mcp.dead.echo", {})
    manager._connections.clear()


@pytest.mark.asyncio
async def test_safe_cli_cancel_kills_blocked_process_before_delayed_mutation(tmp_path):
    workspace = LocalReadOnlyWorkspace(tmp_path)
    cli = SafeCli(
        workspace,
        CommandPolicy([CommandRule([sys.executable], allow_extra_args=True)]),
        default_timeout=10,
        max_timeout=10,
    )
    control = ExecutionControlToken("run-process", timeout_seconds=10)
    task = asyncio.create_task(
        cli.execute(
            [sys.executable, "-c", "import time; time.sleep(2); open('late.txt','w').write('late')"],
            execution_control=control,
        )
    )
    await asyncio.sleep(0.05)
    control.cancel("USER_CANCEL", source="test")
    with pytest.raises(ExecutionControlError):
        await task
    await asyncio.sleep(0.1)
    assert not (tmp_path / "late.txt").exists()


@pytest.mark.asyncio
async def test_parent_cancel_propagates_to_child_and_recovery_without_post_cancel_mutation():
    parent = ExecutionControlToken("root", timeout_seconds=10)
    child = parent.derive(run_id="child-run", attempt_id="child-attempt")
    mutated = []

    async def recovery_work():
        await asyncio.sleep(1)
        mutated.append("after-cancel")

    task = asyncio.create_task(
        await_with_control(recovery_work(), control=child, source="recovery")
    )
    await asyncio.sleep(0.02)
    parent.cancel("PARENT_CANCELLED", source="test")
    with pytest.raises(ExecutionControlError) as raised:
        await task
    assert raised.value.reason == "PARENT_CANCELLED"
    assert mutated == []
    assert child.terminal is True


@pytest.mark.asyncio
async def test_child_late_result_is_not_consumed_after_parent_cancel():
    parent = ExecutionControlToken("root-child", timeout_seconds=10)
    child = parent.derive(run_id="child", attempt_id="attempt-child")
    result = []

    async def child_work():
        await asyncio.sleep(0.2)
        result.append("late")
        return "late-result"

    task = asyncio.create_task(await_with_control(child_work(), control=child, source="child"))
    await asyncio.sleep(0.02)
    parent.cancel("PARENT_CANCELLED", source="test")
    with pytest.raises(ExecutionControlError):
        await task
    assert result == []


@pytest.mark.asyncio
async def test_scripted_child_kernel_receives_parent_control():
    started = asyncio.Event()

    async def handler(_request):
        await _blocked(started)
        return AgentResult(status=AgentStatus.COMPLETED, final_output="late")

    parent = ExecutionControlToken("root-kernel", timeout_seconds=10)
    child = parent.derive(run_id="child-kernel", attempt_id="attempt-kernel")
    kernel = ScriptedAgentKernel(handler)
    request = _agent_request().model_copy(update={"agent_id": "child-kernel"})
    task = asyncio.create_task(kernel.run(request, execution_control=child))
    await started.wait()
    parent.cancel("PARENT_CANCELLED", source="test")
    with pytest.raises(ExecutionControlError):
        await task
    assert (await kernel.status("child-kernel"))["status"] == AgentStatus.CANCELLED.value


def test_cancel_is_idempotent_and_terminal_evidence_survives_db_reopen(tmp_path):
    token = ExecutionControlToken("run-reopen", attempt_id="attempt-reopen", timeout_seconds=30)
    assert token.cancel("USER_CANCEL", source="test") is True
    assert token.cancel("USER_CANCEL", source="test-again") is False
    path = tmp_path / "control.db"
    db = Database(path)
    db.init_db()
    EventStore(db).append(
        EventType.EXECUTION_CANCELLED,
        task_id="task-reopen",
        run_id="run-reopen",
        attempt_id="attempt-reopen",
        payload=token.evidence(),
    )
    db.close()
    reopened = Database(path)
    reopened.init_db()
    events = EventStore(reopened).list_for_run("run-reopen")
    assert len(events) == 1
    assert events[0].event_type is EventType.EXECUTION_CANCELLED
    assert events[0].payload["reason"] == "USER_CANCEL"
    reopened.close()


def test_control_never_extends_root_deadline():
    control = ExecutionControlToken("run-deadline", timeout_seconds=0.01)
    with pytest.raises(ExecutionControlError) as raised:
        asyncio.run(await_with_control(_blocked(), control=control, local_ceiling=30, source="provider"))
    assert raised.value.failure_type == "ROOT_DEADLINE_EXCEEDED"
