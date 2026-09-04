from __future__ import annotations

import asyncio
import json

import pytest

from lhas.capability_registry import (
    CapabilityDefinition,
    CapabilityRegistry,
    CapabilityRuntimeContext,
    RuntimePlatform,
)
from lhas.planning.models import CapabilitySpec
from lhas.tools import (
    FakeTool,
    ToolContract,
    ToolErrorCode,
    ToolRegistry,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
)
from lhas.workspace import CommandPolicy, LocalReadOnlyWorkspace, StagedWorkspace, WorkspaceLimits, register_staged_workspace_tools, register_workspace_tools
from lhas.workspace.tools import SafeCliTool


def definition(
    *,
    capability_id: str = "safe.read",
    tool_name: str = "safe.tool",
    input_schema: dict | None = None,
    output_schema: dict | None = None,
    timeout_seconds: float = 30,
    retryable: bool = True,
) -> CapabilityDefinition:
    return CapabilityDefinition(
        id=capability_id,
        name=capability_id,
        description="test capability",
        category="test",
        version="v1",
        input_schema=input_schema or {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema=output_schema or {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        platforms=(RuntimePlatform.WINDOWS, RuntimePlatform.LINUX, RuntimePlatform.MACOS),
        permissions=("test.read",),
        risk_level="LOW",
        workspace_scope="SOURCE_WORKSPACE",
        timeout_seconds=timeout_seconds,
        retryable=retryable,
        preferred_tool=tool_name,
        source="test",
        evidence_type="DETERMINISTIC_TOOL_RESULT",
    )


def request(
    *,
    capability_id: str = "safe.read",
    tool_name: str = "safe.tool",
    arguments: dict | None = None,
    timeout_seconds: float | None = None,
) -> ToolRequest:
    return ToolRequest(
        tool_call_id="call-1",
        task_id="task-1",
        run_id="run-1",
        attempt_id="attempt-1",
        capability_id=capability_id,
        tool_name=tool_name,
        arguments={} if arguments is None else arguments,
        context={"workspace": "source"},
        workspace_ref="workspace-1",
        timeout_seconds=timeout_seconds,
        metadata={"source": "deterministic-test"},
    )


def harness(*, cap: CapabilityDefinition | None = None, handler=None, tools=None):
    cap = cap or definition()
    tools = tools or ToolRegistry()
    tool = FakeTool(CapabilitySpec(name=cap.preferred_tool), handler)
    tools.register(tool)
    return ToolContract(CapabilityRegistry(definitions=[cap]), tools), tool


def invoke(contract, req, platform="windows"):
    return asyncio.run(contract.invoke(req, CapabilityRuntimeContext(platform=platform)))


def test_valid_request_executes_existing_tool_once_and_returns_evidence():
    calls = []

    def handler(req):
        calls.append(req)
        return ToolResult(status=ToolResultStatus.SUCCESS, output={"ok": True}, artifacts={"log": "a"})

    contract, _ = harness(handler=handler)
    result = invoke(contract, request(arguments={"value": "x"}))
    assert result.status is ToolResultStatus.SUCCESS
    assert result.evidence.capability_id == "safe.read"
    assert result.evidence.tool_name == "safe.tool"
    assert result.evidence.artifact_refs == ["log"]
    assert len(calls) == 1
    assert calls[0].timeout_seconds == 30


@pytest.mark.parametrize(
    "arguments, expected_path",
    [({}, []), ({"value": "x", "extra": 1}, []), ({"value": 1}, ["value"])],
)
def test_input_schema_rejects_missing_unexpected_and_wrong_type(arguments, expected_path):
    calls = []
    contract, _ = harness(handler=lambda req: calls.append(req) or {"ok": True})
    result = invoke(contract, request(arguments=arguments))
    assert result.status is ToolResultStatus.FAILURE
    assert result.error_type == ToolErrorCode.INVALID_ARGUMENT.value
    assert result.metadata["contract"]["diagnostics"]["path"] == expected_path
    assert calls == []


@pytest.mark.parametrize(
    "arguments",
    [{"argv": []}, {"argv": ["pytest", 1]}, {"argv": "pytest"}],
)
def test_array_shape_and_empty_argv_are_rejected(arguments):
    cap = definition(
        input_schema={
            "type": "object",
            "properties": {"argv": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
            "required": ["argv"],
            "additionalProperties": False,
        }
    )
    contract, _ = harness(cap=cap)
    result = invoke(contract, request(arguments=arguments))
    assert result.error_type == ToolErrorCode.INVALID_ARGUMENT.value


def test_numeric_constraints_are_rejected_before_execution():
    calls = []
    cap = definition(input_schema={
        "type": "object",
        "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 3}},
        "required": ["limit"],
        "additionalProperties": False,
    })
    contract, _ = harness(cap=cap, handler=lambda req: calls.append(req) or {"ok": True})
    result = invoke(contract, request(arguments={"limit": 0}))
    assert result.error_type == ToolErrorCode.INVALID_ARGUMENT.value
    assert calls == []


def test_unknown_capability_fails_closed():
    contract, _ = harness()
    result = invoke(contract, request(capability_id="unknown", tool_name="safe.tool", arguments={"value": "x"}))
    assert result.error_type == ToolErrorCode.CAPABILITY_UNAVAILABLE.value


def test_unavailable_capability_fails_closed():
    cap = definition()
    tools = ToolRegistry()
    contract = ToolContract(CapabilityRegistry(definitions=[cap]), tools)
    result = invoke(contract, request(arguments={"value": "x"}))
    assert result.error_type == ToolErrorCode.CAPABILITY_UNAVAILABLE.value


def test_declaration_only_capability_registry_cannot_authorize_invocation():
    # The declaration fallback remains useful for static discovery, but a
    # contract always requires a concrete ToolRegistry backend.
    contract = ToolContract(CapabilityRegistry(), ToolRegistry())
    result = invoke(contract, request(capability_id="test.run", tool_name="cli.exec", arguments={"argv": ["pytest"]}))
    assert result.error_type == ToolErrorCode.CAPABILITY_UNAVAILABLE.value


def test_missing_concrete_tool_registry_fails_closed():
    contract = ToolContract(CapabilityRegistry(), None)
    result = invoke(contract, request(capability_id="test.run", tool_name="cli.exec", arguments={"argv": ["pytest"]}))
    assert result.error_type == ToolErrorCode.CAPABILITY_UNAVAILABLE.value


def test_missing_tool_binding_is_not_executed():
    cap = definition()
    tools = ToolRegistry()
    contract = ToolContract(CapabilityRegistry(definitions=[cap]), tools)
    result = invoke(contract, request(arguments={"value": "x"}))
    assert result.error_type == "CAPABILITY_UNAVAILABLE"


def test_declared_binding_with_unresolvable_tool_returns_tool_not_found():
    class MissingToolRegistry:
        def list_capabilities(self):
            return ["safe.tool"]

        def resolve(self, name):
            raise KeyError(name)

    contract = ToolContract(CapabilityRegistry(definitions=[definition()]), MissingToolRegistry())
    result = invoke(contract, request(arguments={"value": "x"}))
    assert result.error_type == ToolErrorCode.TOOL_NOT_FOUND.value


@pytest.mark.parametrize("tool_name", ["safe.other", "safe.tool.extra"])
def test_capability_tool_binding_mismatch_is_rejected(tool_name):
    contract, _ = harness()
    result = invoke(contract, request(tool_name=tool_name, arguments={"value": "x"}))
    assert result.error_type == ToolErrorCode.INVALID_ARGUMENT.value
    assert result.metadata["contract"]["diagnostics"]["reason"] == "BINDING_MISMATCH"


def test_adversarial_workspace_read_to_cli_exec_binding_is_rejected():
    cap = definition(capability_id="workspace.read", tool_name="workspace.read")
    tools = ToolRegistry()
    tools.register(FakeTool(CapabilitySpec(name="workspace.read")))
    tools.register(FakeTool(CapabilitySpec(name="cli.exec")))
    contract = ToolContract(CapabilityRegistry(definitions=[cap]), tools)
    result = invoke(contract, request(capability_id="workspace.read", tool_name="cli.exec", arguments={"value": "x"}))
    assert result.error_type == ToolErrorCode.INVALID_ARGUMENT.value


def test_unknown_platform_fails_closed_before_tool_execution():
    calls = []
    contract, _ = harness(handler=lambda req: calls.append(req) or {"ok": True})
    result = invoke(contract, request(arguments={"value": "x"}), platform="unknown")
    assert result.error_type == ToolErrorCode.CAPABILITY_UNAVAILABLE.value
    assert calls == []


def test_timeout_defaults_to_capability_limit():
    contract, _ = harness(cap=definition(timeout_seconds=17), handler=lambda req: {"ok": True})
    result = invoke(contract, request(arguments={"value": "x"}))
    assert result.status is ToolResultStatus.SUCCESS


def test_timeout_is_clamped_to_capability_limit():
    seen = []
    contract, _ = harness(cap=definition(timeout_seconds=17), handler=lambda req: seen.append(req.timeout_seconds) or {"ok": True})
    assert invoke(contract, request(arguments={"value": "x"}, timeout_seconds=99)).status is ToolResultStatus.SUCCESS
    assert seen == [17]


def test_retryable_capability_is_exposed_without_running_a_retry_loop():
    contract, _ = harness(cap=definition(retryable=True))
    decision = contract.prepare(request(arguments={"value": "x"}), CapabilityRuntimeContext(platform="windows"))
    assert decision.retry_allowed is True
    assert decision.retry_reason == "capability is retryable"


def test_non_retryable_capability_is_exposed_without_policy_redesign():
    contract, _ = harness(cap=definition(retryable=False))
    decision = contract.prepare(request(arguments={"value": "x"}), CapabilityRuntimeContext(platform="windows"))
    assert decision.retry_allowed is False
    assert decision.retry_reason == "capability is not retryable"


def test_successful_output_is_validated():
    contract, _ = harness(handler=lambda req: {"ok": True})
    assert invoke(contract, request(arguments={"value": "x"})).status is ToolResultStatus.SUCCESS


def test_invalid_output_becomes_output_validation_failure_without_raw_value_metadata():
    contract, _ = harness(handler=lambda req: {"ok": "not-bool", "secret": "do-not-copy"})
    result = invoke(contract, request(arguments={"value": "x"}))
    assert result.status is ToolResultStatus.FAILURE
    assert result.error_type == ToolErrorCode.OUTPUT_VALIDATION_FAILED.value
    serialized = json.dumps(result.metadata)
    assert "not-bool" not in serialized
    assert "do-not-copy" not in serialized
    assert result.metadata["contract"]["output_diagnostics"]["path"] == ["ok"]


def test_invalid_output_schema_is_an_internal_contract_error():
    cap = definition(output_schema={"type": "not-a-json-schema-type"})
    contract, _ = harness(cap=cap, handler=lambda req: {"ok": True})
    result = invoke(contract, request(arguments={"value": "x"}))
    assert result.error_type == ToolErrorCode.INTERNAL_ERROR.value


def test_tool_failure_status_and_concrete_error_are_preserved():
    original = ToolResult(status=ToolResultStatus.FAILURE, error_type="COMMAND_NOT_ALLOWED", error_message="denied")
    contract, _ = harness(handler=lambda req: original)
    result = invoke(contract, request(arguments={"value": "x"}))
    assert result.status is ToolResultStatus.FAILURE
    assert result.error_type == "COMMAND_NOT_ALLOWED"
    assert result.evidence is not None


def test_tool_waiting_for_approval_status_is_preserved():
    contract, _ = harness(handler=lambda req: ToolResult(status=ToolResultStatus.WAITING_FOR_HUMAN_APPROVAL))
    result = invoke(contract, request(arguments={"value": "x"}))
    assert result.status is ToolResultStatus.WAITING_FOR_HUMAN_APPROVAL


def test_evidence_round_trip_is_typed_and_serializable():
    contract, _ = harness(handler=lambda req: ToolResult(status=ToolResultStatus.SUCCESS, output={"ok": True}))
    result = invoke(contract, request(arguments={"value": "x"}))
    restored = ToolResult.model_validate_json(result.model_dump_json())
    assert restored.evidence == result.evidence


def test_tool_registry_ownership_and_capability_registry_non_execution():
    calls = []
    tools = ToolRegistry()
    tools.register(FakeTool(CapabilitySpec(name="safe.tool"), lambda req: calls.append(req)))
    registry = CapabilityRegistry(tools, definitions=[definition()])
    assert len(registry.list_all()) == 1
    assert tools.list_capabilities() == ["safe.tool"]
    assert calls == []


def test_validation_is_deterministic_on_repeated_requests():
    contract, _ = harness()
    req = request(arguments={"value": 1})
    first = contract.prepare(req, CapabilityRuntimeContext(platform="linux")).model_dump(mode="json")
    second = contract.prepare(req, CapabilityRuntimeContext(platform="linux")).model_dump(mode="json")
    assert first == second


@pytest.mark.parametrize("platform", ["windows", "linux", "macos", RuntimePlatform.WINDOWS, RuntimePlatform.LINUX, RuntimePlatform.MACOS])
def test_known_platforms_are_compatible(platform):
    contract, _ = harness(handler=lambda req: {"ok": True})
    assert invoke(contract, request(arguments={"value": "x"}), platform=platform).status is ToolResultStatus.SUCCESS


def test_existing_safe_cli_command_not_allowed_behavior_is_unchanged(tmp_path):
    cap = definition(
        capability_id="test.run",
        tool_name="cli.exec",
        input_schema={
            "type": "object",
            "properties": {"argv": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
            "required": ["argv"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
    )
    tools = ToolRegistry()
    tools.register(SafeCliTool(LocalReadOnlyWorkspace(tmp_path), CommandPolicy()))
    contract = ToolContract(CapabilityRegistry(definitions=[cap]), tools)
    result = invoke(contract, request(capability_id="test.run", tool_name="cli.exec", arguments={"argv": ["dangerous"]}))
    assert result.error_type == "COMMAND_NOT_ALLOWED"


def test_real_workspace_read_uses_default_registry_schema(tmp_path):
    (tmp_path / "sample.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    tools = ToolRegistry()
    register_workspace_tools(tools, LocalReadOnlyWorkspace(tmp_path), CommandPolicy())
    contract = ToolContract(CapabilityRegistry(), tools)
    result = invoke(contract, ToolRequest(
        tool_call_id="read", task_id="task", run_id="run", attempt_id="attempt",
        capability_id="workspace.read", tool_name="workspace.read", arguments={"path": "sample.txt"},
    ))
    assert result.status is ToolResultStatus.SUCCESS
    assert result.output["path"] == "sample.txt"
    assert result.output["sha256"]


def test_real_workspace_read_truncated_uses_default_registry_schema(tmp_path):
    (tmp_path / "large.txt").write_text("0123456789", encoding="utf-8")
    workspace = LocalReadOnlyWorkspace(tmp_path, WorkspaceLimits(max_read_bytes=1, hard_max_read_bytes=1))
    tools = ToolRegistry()
    register_workspace_tools(tools, workspace, CommandPolicy())
    result = invoke(ToolContract(CapabilityRegistry(), tools), ToolRequest(
        tool_call_id="read", task_id="task", run_id="run", attempt_id="attempt",
        capability_id="workspace.read", tool_name="workspace.read", arguments={"path": "large.txt"},
    ))
    assert result.status is ToolResultStatus.SUCCESS
    assert result.output == {"path": "large.txt", "content": "", "truncated": True, "total_lines": 0}


def test_real_workspace_list_uses_default_registry_schema(tmp_path):
    (tmp_path / "sample.txt").write_text("alpha", encoding="utf-8")
    tools = ToolRegistry()
    register_workspace_tools(tools, LocalReadOnlyWorkspace(tmp_path), CommandPolicy())
    result = invoke(ToolContract(CapabilityRegistry(), tools), ToolRequest(
        tool_call_id="list", task_id="task", run_id="run", attempt_id="attempt",
        capability_id="workspace.list", tool_name="workspace.list", arguments={},
    ))
    assert result.status is ToolResultStatus.SUCCESS
    assert result.output["entries"][0]["path"] == "sample.txt"


def test_real_workspace_edit_uses_default_registry_schema(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "sample.txt").write_text("before\n", encoding="utf-8")
    staged = StagedWorkspace.create(source, tmp_path / "staged")
    tools = ToolRegistry()
    register_staged_workspace_tools(tools, staged, CommandPolicy())
    result = invoke(ToolContract(CapabilityRegistry(), tools), ToolRequest(
        tool_call_id="edit", task_id="task", run_id="run", attempt_id="attempt",
        capability_id="workspace.edit", tool_name="workspace.edit",
        arguments={"path": "sample.txt", "old_text": "before", "new_text": "after"},
    ))
    assert result.status is ToolResultStatus.SUCCESS
    assert result.output["replacements"] == 1
    assert result.output["before_sha256"] != result.output["after_sha256"]


def test_real_workspace_diff_uses_default_registry_schema(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "sample.txt").write_text("before\n", encoding="utf-8")
    staged = StagedWorkspace.create(source, tmp_path / "staged")
    asyncio.run(staged.edit_file("sample.txt", "before", "after"))
    tools = ToolRegistry()
    register_staged_workspace_tools(tools, staged, CommandPolicy())
    result = invoke(ToolContract(CapabilityRegistry(), tools), ToolRequest(
        tool_call_id="diff", task_id="task", run_id="run", attempt_id="attempt",
        capability_id="workspace.diff", tool_name="workspace.diff", arguments={},
    ))
    assert result.status is ToolResultStatus.SUCCESS
    assert result.output["files_changed"] == 1
    assert result.output["changed_files"] == ["sample.txt"]


def test_timeout_capability_clamp_reaches_real_safe_cli(monkeypatch, tmp_path):
    cap = definition(
        capability_id="test.run", tool_name="cli.exec", timeout_seconds=17,
        input_schema={
            "type": "object", "properties": {
                "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "timeout_seconds": {"type": "number"},
            }, "required": ["argv"], "additionalProperties": False,
        }, output_schema={"type": "object"},
    )
    tools = ToolRegistry()
    cli = SafeCliTool(LocalReadOnlyWorkspace(tmp_path), CommandPolicy())
    received = []

    async def capture(argv, cwd=".", timeout_seconds=None):
        received.append(timeout_seconds)
        return {"ok": True}, None

    monkeypatch.setattr(cli.cli, "execute", capture)
    tools.register(cli)
    result = invoke(ToolContract(CapabilityRegistry(definitions=[cap]), tools), request(
        capability_id="test.run", tool_name="cli.exec", arguments={"argv": ["pytest"]}, timeout_seconds=99,
    ))
    assert result.status is ToolResultStatus.SUCCESS
    assert received == [17]


def test_top_level_timeout_overrides_legacy_argument_for_real_safe_cli(monkeypatch, tmp_path):
    cap = definition(
        capability_id="test.run", tool_name="cli.exec", timeout_seconds=30,
        input_schema={
            "type": "object", "properties": {
                "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "timeout_seconds": {"type": "number"},
            }, "required": ["argv"], "additionalProperties": False,
        }, output_schema={"type": "object"},
    )
    tools = ToolRegistry()
    cli = SafeCliTool(LocalReadOnlyWorkspace(tmp_path), CommandPolicy())
    received = []

    async def capture(argv, cwd=".", timeout_seconds=None):
        received.append(timeout_seconds)
        return {"ok": True}, None

    monkeypatch.setattr(cli.cli, "execute", capture)
    tools.register(cli)
    result = invoke(ToolContract(CapabilityRegistry(definitions=[cap]), tools), request(
        capability_id="test.run", tool_name="cli.exec", arguments={"argv": ["pytest"], "timeout_seconds": 120}, timeout_seconds=5,
    ))
    assert result.status is ToolResultStatus.SUCCESS
    assert received == [5]


def test_legacy_argument_timeout_reaches_real_safe_cli(monkeypatch, tmp_path):
    cli = SafeCliTool(LocalReadOnlyWorkspace(tmp_path), CommandPolicy())
    received = []

    async def capture(argv, cwd=".", timeout_seconds=None):
        received.append(timeout_seconds)
        return {"ok": True}, None

    monkeypatch.setattr(cli.cli, "execute", capture)
    result = asyncio.run(cli.execute(ToolRequest(
        tool_call_id="call", task_id="task", run_id="run", attempt_id="attempt",
        capability="cli.exec", arguments={"argv": ["pytest"], "timeout_seconds": 7},
    )))
    assert result.status is ToolResultStatus.SUCCESS
    assert received == [7]


def test_tool_request_rejects_missing_all_capability_identity():
    with pytest.raises(ValueError, match="capability or capability_id is required"):
        ToolRequest(tool_call_id="c", task_id="t", run_id="r", attempt_id="a")


def test_tool_request_supports_legacy_capability_only():
    req = ToolRequest(tool_call_id="c", task_id="t", run_id="r", attempt_id="a", capability="safe.read")
    assert req.capability == req.capability_id == "safe.read"


def test_tool_request_supports_capability_id_only():
    req = ToolRequest(tool_call_id="c", task_id="t", run_id="r", attempt_id="a", capability_id="safe.read")
    assert req.capability == req.capability_id == "safe.read"


def test_tool_request_rejects_capability_alias_mismatch():
    with pytest.raises(ValueError, match="same capability"):
        ToolRequest(tool_call_id="c", task_id="t", run_id="r", attempt_id="a", capability="safe.read", capability_id="safe.write")


def test_mismatched_tool_evidence_fails_closed():
    bad_evidence = {
        "evidence_type": "TOOL_EXECUTION", "source": "fake", "capability_id": "other.capability",
        "tool_name": "other.tool", "summary": "forged", "artifact_refs": [], "metadata": {},
    }
    contract, _ = harness(handler=lambda req: ToolResult(
        status=ToolResultStatus.SUCCESS, output={"ok": True},
        evidence=bad_evidence,
    ))
    result = invoke(contract, request(arguments={"value": "x"}))
    assert result.status is ToolResultStatus.FAILURE
    assert result.error_type == ToolErrorCode.INTERNAL_ERROR.value
    assert result.metadata["contract"]["reason"] == "EVIDENCE_IDENTITY_MISMATCH"
    assert result.evidence.capability_id == "safe.read"
    assert result.evidence.tool_name == "safe.tool"


@pytest.mark.parametrize("code", [item.value for item in ToolErrorCode])
def test_error_taxonomy_codes_are_stable_machine_readable(code):
    assert code == code.upper()
