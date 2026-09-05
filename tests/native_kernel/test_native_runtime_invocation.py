"""P2.3 Native Runtime Invocation Integration Tests.

These tests prove that NativeToolDispatcher routes ALL tool invocations
through the ToolContract boundary (CapabilityRegistry → ToolContract →
ToolRegistry → Tool) and that the 12 required invariants hold.
"""

from __future__ import annotations

import asyncio
import inspect
import sys

import pytest

from lhas.agent.models import AgentBudget, AgentRequest, AgentRole
from lhas.capability_registry import (
    CapabilityRegistry,
    CapabilityRuntimeContext,
    default_capabilities,
)
from lhas.domain.models import Attempt, Run
from lhas.native.models import ExecutionSnapshot, ProviderToolCall, SideEffectClass
from lhas.native.persistence import ToolInvocationRepository
from lhas.native.tools import NativeToolDispatcher
from lhas.planning.models import CapabilitySpec
from lhas.tools.contract import ToolContract, ToolErrorCode, _check_semantic_argv
from lhas.tools.protocol import ToolRequest, ToolResult, ToolResultStatus
from lhas.tools.registry import ToolRegistry
from lhas.workspace import (
    CommandPolicy,
    LocalReadOnlyWorkspace,
    register_workspace_tools,
)
from lhas.workspace.command_policy import CommandRule
from lhas.workspace.tools import SafeCliTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _TrackingTool:
    """A tool that records every execute() call for assertion."""

    def __init__(self, name: str, *, handler=None, side_effect: bool = False,
                 requires_human_approval: bool = False, input_schema=None):
        self._name = name
        self._handler = handler
        self._side_effect = side_effect
        self._requires_human_approval = requires_human_approval
        self._input_schema = input_schema
        self.calls: list[ToolRequest] = []

    @property
    def capability(self):
        return CapabilitySpec(
            name=self._name,
            description=f"Tracking tool {self._name}",
            input_schema=self._input_schema or {"type": "object", "additionalProperties": True},
            side_effect=self._side_effect,
            requires_human_approval=self._requires_human_approval,
        )

    async def execute(self, request: ToolRequest) -> ToolResult:
        self.calls.append(request)
        if self._handler:
            return self._handler(request)
        return ToolResult(status=ToolResultStatus.SUCCESS, output={"ok": True})


def _snapshot(task_id="task-1", run_id="run-1", attempt_id="attempt-1",
              delegation_dependencies=None):
    return ExecutionSnapshot(
        task_id=task_id,
        run_id=run_id,
        attempt_id=attempt_id,
        goal="test goal",
        delegation_dependencies=delegation_dependencies or {},
    )


def _request(allowed_capabilities=None, max_delegations=5):
    return AgentRequest(
        agent_id="test-agent",
        role=AgentRole.WORKER,
        objective="test objective",
        allowed_capabilities=allowed_capabilities or set(),
        budget=AgentBudget(max_turns=10, max_tool_calls=20, max_delegations=max_delegations),
        metadata={"task_id": "task-1", "run_id": "run-1", "attempt_id": "attempt-1"},
    )


def _dispatcher(db, registry, allowed=None, side_effect_allowed=None,
                capability_registry=None, tool_contract=None):
    caps = allowed or set(registry.list_capabilities())
    se_caps = side_effect_allowed or set()
    return NativeToolDispatcher(
        db=db,
        registry=registry,
        allowed_capabilities=caps,
        allowed_side_effect_capabilities=se_caps,
        capability_registry=capability_registry,
        tool_contract=tool_contract,
    )


def _call(name, arguments=None, call_id="call-1"):
    return ProviderToolCall(id=call_id, name=name, arguments=arguments or {})


# ---------------------------------------------------------------------------
# Test 1: workspace.read → ToolContract traversed → real backend success
# ---------------------------------------------------------------------------

def test_workspace_read_routes_through_contract_and_succeeds(db, tmp_path):
    """Test 1: workspace.read → ToolContract traversed → real backend success."""
    (tmp_path / "hello.txt").write_text("hello world\n", encoding="utf-8")
    tools = ToolRegistry()
    register_workspace_tools(tools, LocalReadOnlyWorkspace(tmp_path), CommandPolicy())

    dispatcher = _dispatcher(db, tools)
    snapshot = _snapshot()
    request = _request(allowed_capabilities={"workspace.read"})
    call = _call("workspace.read", {"path": "hello.txt"})

    observation = asyncio.run(dispatcher.dispatch(call, request, snapshot))
    assert observation["status"] == "SUCCESS"
    assert observation["capability"] == "workspace.read"
    assert "hello world" in observation["bounded_output"]["content"]

    # Verify the invocation was recorded
    invocations = ToolInvocationRepository(db).list_for_attempt("attempt-1")
    assert len(invocations) == 1
    assert invocations[0].capability == "workspace.read"


# ---------------------------------------------------------------------------
# Test 2: test.run → semantic capability → cli.exec backend
# ---------------------------------------------------------------------------

def test_test_run_semantic_capability_routes_to_cli_exec(db, tmp_path):
    """Test 2: test.run → semantic capability → cli.exec backend."""
    tools = ToolRegistry()
    cli_tool = SafeCliTool(
        LocalReadOnlyWorkspace(tmp_path),
        CommandPolicy(rules=[CommandRule(argv_prefix=[sys.executable])]),
    )
    tools.register(cli_tool)

    dispatcher = _dispatcher(db, tools, allowed={"test.run"})
    snapshot = _snapshot()
    request = _request(allowed_capabilities={"test.run"})
    call = _call("test.run", {"argv": [sys.executable, "-c", "print('ok')"]})

    observation = asyncio.run(dispatcher.dispatch(call, request, snapshot))
    assert observation["status"] == "SUCCESS"
    assert observation["capability"] == "test.run"


# ---------------------------------------------------------------------------
# Test 3: invalid arguments → Tool execute count = 0
# ---------------------------------------------------------------------------

def test_invalid_arguments_prevent_tool_execution(db):
    """Test 3: invalid arguments → Tool execute count = 0."""
    tool = _TrackingTool("safe.tool", input_schema={
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    })
    tools = ToolRegistry()
    tools.register(tool)

    cap_reg = CapabilityRegistry(tools, definitions=[
        _cap_def("safe.tool", "safe.tool", input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        }),
    ])
    contract = ToolContract(cap_reg, tools)
    dispatcher = _dispatcher(db, tools, allowed={"safe.tool"},
                             capability_registry=cap_reg, tool_contract=contract)

    snapshot = _snapshot()
    request = _request(allowed_capabilities={"safe.tool"})
    # Missing required "value" field
    call = _call("safe.tool", {"wrong": "field"})

    observation = asyncio.run(dispatcher.dispatch(call, request, snapshot))
    assert observation["status"] == "FAILURE"
    assert len(tool.calls) == 0, "Tool must not be called when arguments are invalid"


# ---------------------------------------------------------------------------
# Test 4: unknown capability → fail closed
# ---------------------------------------------------------------------------

def test_unknown_capability_fails_closed(db):
    """Test 4: unknown capability → fail closed."""
    tools = ToolRegistry()
    dispatcher = _dispatcher(db, tools, allowed=set())

    snapshot = _snapshot()
    request = _request(allowed_capabilities=set())
    call = _call("does.not.exist", {"x": 1})

    observation = asyncio.run(dispatcher.dispatch(call, request, snapshot))
    assert observation["status"] == "FAILURE"
    assert observation["error_type"] == "UNKNOWN_CAPABILITY"


# ---------------------------------------------------------------------------
# Test 5: backend missing → fail closed
# ---------------------------------------------------------------------------

def test_backend_missing_fails_closed(db):
    """Test 5: backend missing → fail closed."""
    # Register a capability definition but no backend tool
    cap_def = _cap_def("orphan.cap", preferred_tool="missing.tool")
    cap_reg = CapabilityRegistry(definitions=[cap_def])
    tools = ToolRegistry()  # empty — missing.tool not registered

    contract = ToolContract(cap_reg, tools)
    dispatcher = _dispatcher(db, tools, allowed={"orphan.cap"},
                             capability_registry=cap_reg, tool_contract=contract)

    snapshot = _snapshot()
    request = _request(allowed_capabilities={"orphan.cap"})
    call = _call("orphan.cap", {})

    observation = asyncio.run(dispatcher.dispatch(call, request, snapshot))
    assert observation["status"] == "FAILURE"
    # Should fail with CAPABILITY_UNAVAILABLE or TOOL_NOT_FOUND
    assert observation["error_type"] in ("CAPABILITY_UNAVAILABLE", "TOOL_NOT_FOUND")


# ---------------------------------------------------------------------------
# Test 6: output validation failure → Native observer sees FAILURE
# ---------------------------------------------------------------------------

def test_output_validation_failure_observed_as_failure(db):
    """Test 6: output validation failure → Native observer sees FAILURE."""
    # Tool returns invalid output (ok should be boolean, not string)
    tool = _TrackingTool(
        "bad.output",
        handler=lambda req: ToolResult(
            status=ToolResultStatus.SUCCESS,
            output={"ok": "not-a-bool"},
        ),
    )
    tools = ToolRegistry()
    tools.register(tool)

    cap_def = _cap_def(
        "bad.output", "bad.output",
        output_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
    )
    cap_reg = CapabilityRegistry(tools, definitions=[cap_def])
    contract = ToolContract(cap_reg, tools)
    dispatcher = _dispatcher(db, tools, allowed={"bad.output"},
                             capability_registry=cap_reg, tool_contract=contract)

    snapshot = _snapshot()
    request = _request(allowed_capabilities={"bad.output"})
    call = _call("bad.output", {})

    observation = asyncio.run(dispatcher.dispatch(call, request, snapshot))
    assert observation["status"] == "FAILURE"
    assert observation["error_type"] == "OUTPUT_VALIDATION_FAILED"


# ---------------------------------------------------------------------------
# Test 7: COMMAND_NOT_ALLOWED preserved through ToolContract
# ---------------------------------------------------------------------------

def test_command_not_allowed_preserved_through_contract(db, tmp_path):
    """Test 7: COMMAND_NOT_ALLOWED preserved through semantic capability path."""
    tools = ToolRegistry()
    tools.register(SafeCliTool(LocalReadOnlyWorkspace(tmp_path), CommandPolicy()))

    # test.run is a semantic capability that routes to cli.exec backend.
    # CommandPolicy with no rules rejects everything → COMMAND_NOT_ALLOWED.
    cap_def = _cap_def("test.run", "cli.exec", input_schema={
        "type": "object",
        "properties": {
            "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        },
        "required": ["argv"],
        "additionalProperties": False,
    })
    # Replace existing test.run definition with one that has explicit input_schema
    defs = [d for d in default_capabilities() if d.id != "test.run"]
    cap_reg = CapabilityRegistry(tools, definitions=[*defs, cap_def])
    contract = ToolContract(cap_reg, tools)
    dispatcher = _dispatcher(db, tools, allowed={"test.run"},
                             capability_registry=cap_reg, tool_contract=contract)
    snapshot = _snapshot()
    request = _request(allowed_capabilities={"test.run"})
    call = _call("test.run", {"argv": ["dangerous-command"]})

    observation = asyncio.run(dispatcher.dispatch(call, request, snapshot))
    assert observation["status"] == "FAILURE"
    assert observation["error_type"] == "COMMAND_NOT_ALLOWED"


# ---------------------------------------------------------------------------
# Test 8: git.status cannot execute git.diff semantics
# ---------------------------------------------------------------------------

def test_git_status_rejects_git_diff_argv(db):
    """Test 8: git.status cannot execute git.diff semantics."""
    assert _check_semantic_argv("git.status", {"argv": ["git", "diff"]}) is not None


def test_git_status_accepts_correct_argv(db):
    """git.status accepts correct argv prefix."""
    assert _check_semantic_argv("git.status", {"argv": ["git", "status"]}) is None


# ---------------------------------------------------------------------------
# Test 9: git.diff cannot execute arbitrary pytest semantics
# ---------------------------------------------------------------------------

def test_git_diff_rejects_pytest_argv(db):
    """Test 9: git.diff cannot execute arbitrary pytest semantics."""
    assert _check_semantic_argv("git.diff", {"argv": ["pytest"]}) is not None


def test_git_diff_accepts_correct_argv(db):
    """git.diff accepts correct argv prefix."""
    assert _check_semantic_argv("git.diff", {"argv": ["git", "diff"]}) is None


# ---------------------------------------------------------------------------
# Test 10: platform.delegate retains delegation budget, side effect
#          classification, durable invocation semantics
# ---------------------------------------------------------------------------

def test_platform_delegate_retains_delegation_budget_and_side_effect(db):
    """Test 10: platform.delegate retains delegation budget, side effect,
    and durable invocation semantics."""
    from lhas.native.models import NativeFaultPoint

    delegate_calls = []

    class DelegateTool:
        capability = CapabilitySpec(name="platform.delegate", description="delegate")

        async def execute(self, request):
            delegate_calls.append(request)
            return ToolResult(status=ToolResultStatus.SUCCESS, output={"delegated": True})

    tools = ToolRegistry()
    tools.register(DelegateTool())
    dispatcher = _dispatcher(db, tools, allowed={"platform.delegate"})

    # Budget = 0 → should be denied before execution
    snapshot = _snapshot(delegation_dependencies={})
    request = _request(allowed_capabilities={"platform.delegate"}, max_delegations=0)
    call = _call("platform.delegate", {})

    observation = asyncio.run(dispatcher.dispatch(call, request, snapshot))
    assert observation["status"] == "FAILURE"
    assert observation["error_type"] == "DELEGATION_BUDGET_EXHAUSTED"
    assert len(delegate_calls) == 0, "Delegate tool must not be called when budget exhausted"

    # Verify the invocation has DELEGATION side effect class
    invocations = ToolInvocationRepository(db).list_for_attempt("attempt-1")
    assert len(invocations) == 1
    assert invocations[0].side_effect_class == SideEffectClass.DELEGATION


def test_platform_delegate_success_with_budget(db):
    """platform.delegate succeeds when budget allows."""
    delegate_calls = []

    class DelegateTool:
        capability = CapabilitySpec(name="platform.delegate", description="delegate")

        async def execute(self, request):
            delegate_calls.append(request)
            return ToolResult(status=ToolResultStatus.SUCCESS, output={"delegated": True})

    tools = ToolRegistry()
    tools.register(DelegateTool())
    dispatcher = _dispatcher(db, tools, allowed={"platform.delegate"})

    snapshot = _snapshot(delegation_dependencies={})
    request = _request(allowed_capabilities={"platform.delegate"}, max_delegations=5)
    call = _call("platform.delegate", {})

    observation = asyncio.run(dispatcher.dispatch(call, request, snapshot))
    assert observation["status"] == "SUCCESS"
    assert len(delegate_calls) == 1

    # Verify durable invocation semantics
    invocations = ToolInvocationRepository(db).list_for_attempt("attempt-1")
    assert len(invocations) == 1
    assert invocations[0].side_effect_class == SideEffectClass.DELEGATION
    assert invocations[0].result_status == "SUCCESS"
    assert invocations[0].observed_mutation is True  # delegate success = mutation


# ---------------------------------------------------------------------------
# Test 11: Tool SUCCESS does NOT update CompletionAuthority by itself
# ---------------------------------------------------------------------------

def test_tool_success_does_not_update_completion_authority(db):
    """Test 11: Tool SUCCESS does NOT update CompletionAuthority by itself."""
    tool = _TrackingTool("test.echo")
    tools = ToolRegistry()
    tools.register(tool)
    cap_def = _cap_def("test.echo", "test.echo")
    cap_reg = CapabilityRegistry(tools, definitions=[*default_capabilities(), cap_def])
    contract = ToolContract(cap_reg, tools)
    dispatcher = _dispatcher(db, tools, allowed={"test.echo"},
                             capability_registry=cap_reg, tool_contract=contract)

    snapshot = _snapshot()
    request = _request(allowed_capabilities={"test.echo"})
    call = _call("test.echo", {"value": "hello"})

    observation = asyncio.run(dispatcher.dispatch(call, request, snapshot))
    assert observation["status"] == "SUCCESS"

    # Verify no completion candidates were created
    from lhas.native.persistence import CompletionCandidateRepository
    candidates = CompletionCandidateRepository(db).list_for_attempt("attempt-1")
    assert candidates == [], "Tool SUCCESS must not create completion candidates"


# ---------------------------------------------------------------------------
# Test 12: No direct Tool.execute bypass remains in NativeToolDispatcher
# ---------------------------------------------------------------------------

def test_no_direct_tool_execute_bypass_in_dispatcher():
    """Test 12: No direct migrated Tool.execute bypass remains in
    NativeToolDispatcher."""
    source = inspect.getsource(NativeToolDispatcher.dispatch)
    # The only place tool.execute should appear is inside tool_contract.invoke,
    # not as a direct call in dispatch().
    lines = [
        line for line in source.splitlines()
        if "tool.execute" in line or "await tool.execute" in line
        or ".execute(" in line and "tool_contract" not in line
    ]
    # Filter out comments and the tool_contract.invoke line
    direct_bypasses = [
        line.strip() for line in lines
        if not line.strip().startswith("#")
        and "tool_contract" not in line
        and "contract_request" not in line
        and "self.tool_contract" not in line
    ]
    assert direct_bypasses == [], (
        f"Direct tool.execute bypass found in dispatch(): {direct_bypasses}"
    )


def test_all_execution_routes_through_tool_contract(db):
    """Verify the dispatch method calls tool_contract.invoke, not tool.execute."""
    tool = _TrackingTool("test.echo")
    tools = ToolRegistry()
    tools.register(tool)
    cap_def = _cap_def("test.echo", "test.echo")
    cap_reg = CapabilityRegistry(tools, definitions=[*default_capabilities(), cap_def])
    contract = ToolContract(cap_reg, tools)
    dispatcher = _dispatcher(db, tools, allowed={"test.echo"},
                             capability_registry=cap_reg, tool_contract=contract)

    snapshot = _snapshot()
    request = _request(allowed_capabilities={"test.echo"})
    call = _call("test.echo", {"value": "hello"})

    observation = asyncio.run(dispatcher.dispatch(call, request, snapshot))
    assert observation["status"] == "SUCCESS"
    assert len(tool.calls) == 1
    # Verify the ToolRequest has capability_id and tool_name (contract fields)
    assert tool.calls[0].capability_id == "test.echo"
    assert tool.calls[0].tool_name == "test.echo"


# ---------------------------------------------------------------------------
# Semantic argv guard integration through full dispatch
# ---------------------------------------------------------------------------

def test_git_status_semantic_guard_rejects_diff_argv_through_contract(db, tmp_path):
    """git.status semantic guard works end-to-end through the contract."""
    tools = ToolRegistry()
    tools.register(SafeCliTool(LocalReadOnlyWorkspace(tmp_path),
                                CommandPolicy(rules=[CommandRule(argv_prefix=["git"])])))

    cap_def = _cap_def("git.status", "cli.exec", input_schema={
        "type": "object",
        "properties": {
            "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        },
        "required": ["argv"],
        "additionalProperties": False,
    })
    cap_reg = CapabilityRegistry(tools, definitions=[cap_def])
    contract = ToolContract(cap_reg, tools)
    dispatcher = _dispatcher(db, tools, allowed={"git.status"},
                             capability_registry=cap_reg, tool_contract=contract)

    snapshot = _snapshot()
    request = _request(allowed_capabilities={"git.status"})
    call = _call("git.status", {"argv": ["git", "diff"]})

    observation = asyncio.run(dispatcher.dispatch(call, request, snapshot))
    assert observation["status"] == "FAILURE"
    assert observation["error_type"] == "INVALID_ARGUMENT"


def test_git_diff_semantic_guard_rejects_pytest_argv_through_contract(db, tmp_path):
    """git.diff semantic guard works end-to-end through the contract."""
    tools = ToolRegistry()
    tools.register(SafeCliTool(LocalReadOnlyWorkspace(tmp_path),
                                CommandPolicy(rules=[CommandRule(argv_prefix=["git"])])))

    cap_def = _cap_def("git.diff", "cli.exec", input_schema={
        "type": "object",
        "properties": {
            "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        },
        "required": ["argv"],
        "additionalProperties": False,
    })
    cap_reg = CapabilityRegistry(tools, definitions=[cap_def])
    contract = ToolContract(cap_reg, tools)
    dispatcher = _dispatcher(db, tools, allowed={"git.diff"},
                             capability_registry=cap_reg, tool_contract=contract)

    snapshot = _snapshot()
    request = _request(allowed_capabilities={"git.diff"})
    call = _call("git.diff", {"argv": ["pytest"]})

    observation = asyncio.run(dispatcher.dispatch(call, request, snapshot))
    assert observation["status"] == "FAILURE"
    assert observation["error_type"] == "INVALID_ARGUMENT"


# ---------------------------------------------------------------------------
# Platform capabilities in default catalog
# ---------------------------------------------------------------------------

def test_platform_capabilities_in_default_catalog():
    """Platform capabilities are registered in the default catalog."""
    registry = CapabilityRegistry()
    ids = {d.id for d in registry.list_all()}
    assert "platform.prepare" in ids
    assert "platform.delegate" in ids
    assert "platform.finalize" in ids


def test_platform_prepare_preferred_tool_is_platform_prepare():
    """platform.prepare maps to platform.prepare tool."""
    registry = CapabilityRegistry()
    definition = registry.get("platform.prepare")
    assert definition.preferred_tool == "platform.prepare"


# ---------------------------------------------------------------------------
# ToolContract boundary evidence
# ---------------------------------------------------------------------------

def test_contract_evidence_present_on_success(db):
    """ToolContract adds evidence to successful results."""
    tool = _TrackingTool("test.echo")
    tools = ToolRegistry()
    tools.register(tool)
    cap_def = _cap_def("test.echo", "test.echo")
    cap_reg = CapabilityRegistry(tools, definitions=[*default_capabilities(), cap_def])
    contract = ToolContract(cap_reg, tools)
    dispatcher = _dispatcher(db, tools, allowed={"test.echo"},
                             capability_registry=cap_reg, tool_contract=contract)

    snapshot = _snapshot()
    request = _request(allowed_capabilities={"test.echo"})
    call = _call("test.echo", {"value": "hello"})

    # Capture the raw result via the tool
    from lhas.capability_registry import CapabilityRuntimeContext
    from lhas.tools.contract import ToolContract as TC

    cap_reg = dispatcher.capability_registry
    tc = dispatcher.tool_contract

    tool_request = ToolRequest(
        tool_call_id="evidence-test",
        task_id="t", run_id="r", attempt_id="a",
        capability_id="test.echo",
        tool_name="test.echo",
        arguments={"value": "hello"},
    )
    result = asyncio.run(tc.invoke(tool_request, CapabilityRuntimeContext(platform="windows")))
    assert result.status is ToolResultStatus.SUCCESS
    assert result.evidence is not None
    assert result.evidence.capability_id == "test.echo"
    assert result.evidence.source == "odys-tool-contract-v1"


# ---------------------------------------------------------------------------
# Duplicate invocation reconciliation still works
# ---------------------------------------------------------------------------

def test_duplicate_invocation_reconciliation_preserved(db):
    """Duplicate invocations return cached result without re-executing."""
    tool = _TrackingTool("test.echo")
    tools = ToolRegistry()
    tools.register(tool)
    cap_def = _cap_def("test.echo", "test.echo")
    cap_reg = CapabilityRegistry(tools, definitions=[*default_capabilities(), cap_def])
    contract = ToolContract(cap_reg, tools)
    dispatcher = _dispatcher(db, tools, allowed={"test.echo"},
                             capability_registry=cap_reg, tool_contract=contract)

    snapshot = _snapshot()
    request = _request(allowed_capabilities={"test.echo"})
    call = _call("test.echo", {"value": "hello"})

    obs1 = asyncio.run(dispatcher.dispatch(call, request, snapshot))
    assert obs1["status"] == "SUCCESS"
    assert len(tool.calls) == 1

    # Same call ID → should be reconciled without re-execution
    obs2 = asyncio.run(dispatcher.dispatch(call, request, snapshot))
    assert obs2.get("duplicate_logical_invocation") is True
    assert len(tool.calls) == 1, "Duplicate must not re-execute tool"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cap_def(capability_id, preferred_tool=None, input_schema=None, output_schema=None):
    from lhas.capability_registry import CapabilityDefinition, RuntimePlatform
    return CapabilityDefinition(
        id=capability_id,
        name=capability_id,
        description=f"Test capability {capability_id}",
        category="test",
        version="v1",
        input_schema=input_schema or {"type": "object", "additionalProperties": True},
        output_schema=output_schema or {"type": "object"},
        platforms=(RuntimePlatform.WINDOWS, RuntimePlatform.LINUX, RuntimePlatform.MACOS),
        permissions=("test.execute",),
        risk_level="LOW",
        workspace_scope="SOURCE_WORKSPACE",
        timeout_seconds=30.0,
        retryable=True,
        preferred_tool=preferred_tool or capability_id,
        source="test",
        evidence_type="DETERMINISTIC_TOOL_RESULT",
    )


# ---------------------------------------------------------------------------
# Test 13a: CapabilitySpec-only Tool is NOT model-facing
# ---------------------------------------------------------------------------

def test_capability_spec_only_tool_not_model_facing(db):
    """Test 13a: A Tool with only CapabilitySpec (no CapabilityDefinition)
    is NOT model-visible but CAN be invoked for routing.
    CAPABILITY_SPEC_CAN_CREATE_MODEL_SCHEMA=NO."""
    tool = _TrackingTool("spec.only.tool")
    tools = ToolRegistry()
    tools.register(tool)

    # Build dispatcher without providing CapabilityDefinition for spec.only.tool
    dispatcher = _dispatcher(db, tools, allowed={"spec.only.tool"})

    # The tool exists in ToolRegistry (concrete backend)
    assert tools.resolve("spec.only.tool") is tool

    # It must NOT appear in tool_schemas (model-facing)
    schemas = dispatcher.tool_schemas()
    schema_names = {s["function"]["name"] for s in schemas}
    assert "spec.only.tool" not in schema_names, (
        "CapabilitySpec-only tool must not appear in model-facing tool_schemas"
    )

    # It CAN be invoked for routing (fallback creates runtime definition)
    snapshot = _snapshot()
    request = _request(allowed_capabilities={"spec.only.tool"})
    call = _call("spec.only.tool", {})
    observation = asyncio.run(dispatcher.dispatch(call, request, snapshot))
    assert observation["status"] == "SUCCESS"


# ---------------------------------------------------------------------------
# Test 13b: Explicit CapabilityDefinition is required for model visibility
# ---------------------------------------------------------------------------

def test_explicit_definition_required_for_model_visibility(db):
    """Test 13b: Only explicit CapabilityDefinition makes a tool model-visible."""
    tool = _TrackingTool("explicit.tool")
    tools = ToolRegistry()
    tools.register(tool)

    cap_def = _cap_def("explicit.tool", "explicit.tool")
    cap_reg = CapabilityRegistry(tools, definitions=[cap_def])
    contract = ToolContract(cap_reg, tools)
    dispatcher = _dispatcher(db, tools, allowed={"explicit.tool"},
                             capability_registry=cap_reg, tool_contract=contract)

    schemas = dispatcher.tool_schemas()
    schema_names = {s["function"]["name"] for s in schemas}
    assert "explicit.tool" in schema_names, (
        "Explicitly declared tool must appear in model-facing tool_schemas"
    )


# ---------------------------------------------------------------------------
# Test 14: platform.delegate child path uses unified contract invocation
# ---------------------------------------------------------------------------

def test_platform_delegate_child_path_uses_contract_invocation(db, tmp_path):
    """Test 14: platform.delegate child path routes through ToolContract."""
    from lhas.tools.invocation import build_contract_for_registry, invoke_via_contract

    tools = ToolRegistry()
    tool = _TrackingTool("child.cap")
    tools.register(tool)
    cap_def = _cap_def("child.cap", "child.cap")
    cap_reg = CapabilityRegistry(tools, definitions=[cap_def])
    contract = ToolContract(cap_reg, tools)

    # Simulate the child_handler pattern: invoke through contract
    tr = ToolRequest(
        tool_call_id="test-child",
        task_id="t", run_id="r", attempt_id="a",
        capability_id="child.cap",
        tool_name="child.cap",
        arguments={"value": "hello"},
    )
    result = asyncio.run(invoke_via_contract(contract, tr))
    assert result.status is ToolResultStatus.SUCCESS
    assert len(tool.calls) == 1
    assert tool.calls[0].capability_id == "child.cap"


# ---------------------------------------------------------------------------
# Test 15: CLI agent-facing path cannot bypass ToolContract
# ---------------------------------------------------------------------------

def test_cli_agent_path_uses_contract(tmp_path):
    """Test 15: OfflineDemoBackend routes through ToolContract, not direct execute."""
    source = ""
    try:
        import lhas.cli_runtime as mod
        source = inspect.getsource(mod.OfflineDemoBackend)
    except (ImportError, OSError):
        pytest.skip("cannot inspect cli_runtime source")

    # Verify no direct .execute(ToolRequest pattern remains
    direct_calls = [
        line.strip() for line in source.splitlines()
        if ".execute(ToolRequest" in line
        and "invoke_via_contract" not in line
    ]
    assert direct_calls == [], (
        f"Direct tool.execute bypass found in OfflineDemoBackend: {direct_calls}"
    )


# ---------------------------------------------------------------------------
# Test 16: inner-agent agent-facing path cannot bypass ToolContract
# ---------------------------------------------------------------------------

def test_inner_agent_path_uses_contract():
    """Test 16: inner_agent.tool_adapter routes through ToolContract."""
    source = ""
    try:
        import lhas.inner_agent.tool_adapter as mod
        source = inspect.getsource(mod)
    except (ImportError, OSError):
        pytest.skip("cannot inspect tool_adapter source")

    direct_calls = [
        line.strip() for line in source.splitlines()
        if ".execute(ToolRequest" in line
        and "invoke_via_contract" not in line
    ]
    assert direct_calls == [], (
        f"Direct tool.execute bypass found in tool_adapter: {direct_calls}"
    )


# ---------------------------------------------------------------------------
# Test 17: invalid delegated child args execute backend 0 times
# ---------------------------------------------------------------------------

def test_invalid_delegated_child_args_execute_zero_times(db):
    """Test 17: Invalid args routed through contract hit backend 0 times."""
    tool = _TrackingTool("safe.child", input_schema={
        "type": "object",
        "properties": {"required_field": {"type": "string"}},
        "required": ["required_field"],
        "additionalProperties": False,
    })
    tools = ToolRegistry()
    tools.register(tool)
    cap_def = _cap_def("safe.child", "safe.child", input_schema={
        "type": "object",
        "properties": {"required_field": {"type": "string"}},
        "required": ["required_field"],
        "additionalProperties": False,
    })
    cap_reg = CapabilityRegistry(tools, definitions=[cap_def])
    contract = ToolContract(cap_reg, tools)

    from lhas.tools.invocation import invoke_via_contract
    tr = ToolRequest(
        tool_call_id="invalid-child",
        task_id="t", run_id="r", attempt_id="a",
        capability_id="safe.child", tool_name="safe.child",
        arguments={"wrong": "field"},
    )
    result = asyncio.run(invoke_via_contract(contract, tr))
    assert result.status is ToolResultStatus.FAILURE
    assert len(tool.calls) == 0, "Tool must not be called when args are invalid"


# ---------------------------------------------------------------------------
# Test 18: output validation failure propagates through delegated path
# ---------------------------------------------------------------------------

def test_output_validation_failure_through_delegated_path(db):
    """Test 18: Output validation failure propagates through contract."""
    tool = _TrackingTool(
        "bad.output.child",
        handler=lambda req: ToolResult(
            status=ToolResultStatus.SUCCESS,
            output={"ok": "not-a-bool"},
        ),
    )
    tools = ToolRegistry()
    tools.register(tool)
    cap_def = _cap_def(
        "bad.output.child", "bad.output.child",
        output_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
    )
    cap_reg = CapabilityRegistry(tools, definitions=[cap_def])
    contract = ToolContract(cap_reg, tools)

    from lhas.tools.invocation import invoke_via_contract
    tr = ToolRequest(
        tool_call_id="bad-output-child",
        task_id="t", run_id="r", attempt_id="a",
        capability_id="bad.output.child", tool_name="bad.output.child",
        arguments={},
    )
    result = asyncio.run(invoke_via_contract(contract, tr))
    assert result.status is ToolResultStatus.FAILURE
    assert result.error_type == ToolErrorCode.OUTPUT_VALIDATION_FAILED.value


# ---------------------------------------------------------------------------
# Test 19: unexplained direct Tool.execute agent bypass count = 0
# ---------------------------------------------------------------------------

_AGENT_FACING_FILES = [
    "src/lhas/agent/platform.py",
    "src/lhas/cli_runtime.py",
    "src/lhas/inner_agent/tool_adapter.py",
    "src/lhas/native/tools.py",
]


def test_unexplained_direct_tool_execute_agent_bypass_count_zero():
    """Test 19: No unexplained direct .execute(ToolRequest) calls remain
    in agent-facing code paths."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    violations = []
    for rel_path in _AGENT_FACING_FILES:
        abs_path = os.path.join(root, rel_path)
        if not os.path.exists(abs_path):
            continue
        with open(abs_path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                stripped = line.strip()
                if ".execute(ToolRequest" in stripped and "invoke_via_contract" not in stripped:
                    # Allow tool_contract.invoke() calls (the contract boundary itself)
                    if "tool_contract" in stripped or "contract" in stripped.lower():
                        continue
                    violations.append(f"{rel_path}:{lineno}: {stripped[:120]}")
    assert violations == [], (
        f"Unexplained direct Tool.execute agent bypasses found: {violations}"
    )
