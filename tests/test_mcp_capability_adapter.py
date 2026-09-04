"""Tests for MCP capability adapter integration.

Uses the existing fake MCP server (src/lhas/mcp/fake_server.py) for all
offline testing.  No network calls.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

from lhas.capability_registry import (
    CapabilityAvailability,
    CapabilityDefinition,
    CapabilityRegistry,
    CapabilityRuntimeContext,
    RuntimePlatform,
    default_capabilities,
)
from lhas.mcp.adapter import MCPToolAdapter, register_mcp_tools
from lhas.mcp.capabilities import (
    mcp_capabilities,
    mcp_tool_to_capability,
    merge_capability_definitions,
)
from lhas.mcp.manager import MCPManager
from lhas.mcp.models import MCPServerConfig, MCPToolInfo
from lhas.tools.contract import ToolContract, ToolErrorCode
from lhas.tools.protocol import ToolRequest, ToolResultStatus
from lhas.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_SERVER = os.path.join(
    os.path.dirname(__file__), "..", "src", "lhas", "mcp", "fake_server.py"
).replace("\\", "/")


def _fake_config() -> MCPServerConfig:
    return MCPServerConfig(
        name="odys-fake",
        command=[sys.executable, _FAKE_SERVER],
    )


def _context(platform="windows", tools=None):
    return CapabilityRuntimeContext(platform=platform, available_tools=tools)


def _sample_request(
    *,
    capability_id: str,
    tool_name: str,
    arguments: dict | None = None,
) -> ToolRequest:
    return ToolRequest(
        tool_call_id="tc-1",
        task_id="t-1",
        run_id="r-1",
        attempt_id="a-1",
        capability_id=capability_id,
        tool_name=tool_name,
        arguments=arguments or {},
    )


# ---------------------------------------------------------------------------
# MCP discovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_discovery_returns_tools():
    """MCPManager.connect discovers at least one tool from fake server."""
    manager = MCPManager()
    config = _fake_config()
    tools = await manager.connect(config)
    try:
        assert len(tools) >= 1
        echo = next(t for t in tools if t.name.endswith(".echo"))
        assert echo.description
        assert echo.server_name == "odys-fake"
        assert "text" in echo.input_schema.get("properties", {})
    finally:
        await manager.close_all()


# ---------------------------------------------------------------------------
# MCPToolInfo → CapabilityDefinition
# ---------------------------------------------------------------------------


def test_mcp_tool_to_capability_basic():
    """Converter produces a valid CapabilityDefinition from MCPToolInfo."""
    info = MCPToolInfo(
        name="mcp.odys-fake.echo",
        description="Return bounded offline evidence",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        server_name="odys-fake",
    )
    cap = mcp_tool_to_capability(info)

    assert isinstance(cap, CapabilityDefinition)
    assert cap.id == "mcp.odys-fake.echo"
    assert cap.name == "mcp.odys-fake.echo"
    assert cap.description == "Return bounded offline evidence"
    assert cap.category == "mcp.odys-fake"
    assert cap.version == "v1"
    assert cap.preferred_tool == "mcp.odys-fake.echo"
    assert cap.fallback_tools == ()
    assert cap.source == "mcp:odys-fake"
    assert cap.evidence_type == "MCP_TOOL_RESULT"
    assert cap.workspace_scope == "EXTERNAL"
    assert cap.availability == "AVAILABLE"
    assert cap.risk_level == "MEDIUM"  # default from MCPToolInfo
    assert not cap.retryable
    assert RuntimePlatform.WINDOWS in cap.platforms
    assert RuntimePlatform.LINUX in cap.platforms
    assert RuntimePlatform.MACOS in cap.platforms


def test_input_schema_preserved():
    """MCP server input schema is passed through verbatim."""
    schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}, "count": {"type": "integer"}},
        "required": ["text"],
    }
    info = MCPToolInfo(
        name="mcp.s.tool",
        input_schema=schema,
        server_name="s",
    )
    cap = mcp_tool_to_capability(info)
    assert cap.input_schema == schema


def test_output_schema_is_generic():
    """MCP tools declare generic output (protocol doesn't expose output schema)."""
    info = MCPToolInfo(name="mcp.s.tool", server_name="s")
    cap = mcp_tool_to_capability(info)
    assert cap.output_schema == {"type": "object"}


def test_empty_name_rejected():
    with pytest.raises(ValueError, match="must not be empty"):
        mcp_tool_to_capability(MCPToolInfo(name="", server_name="s"))


# ---------------------------------------------------------------------------
# Batch conversion
# ---------------------------------------------------------------------------


def test_mcp_capabilities_batch():
    tools = [
        MCPToolInfo(name="mcp.s.a", server_name="s"),
        MCPToolInfo(name="mcp.s.b", server_name="s"),
    ]
    caps = mcp_capabilities(tools)
    assert len(caps) == 2
    assert caps[0].id == "mcp.s.a"
    assert caps[1].id == "mcp.s.b"


# ---------------------------------------------------------------------------
# Merge with core definitions
# ---------------------------------------------------------------------------


def test_merge_no_collision():
    core = list(default_capabilities())
    mcp = mcp_capabilities([MCPToolInfo(name="mcp.s.tool", server_name="s")])
    merged = merge_capability_definitions(core, mcp)
    assert len(merged) == len(core) + 1


def test_merge_collision_rejected():
    core = list(default_capabilities())
    # Deliberately collide with a core id
    collision = CapabilityDefinition(
        id="workspace.read",
        name="workspace.read",
        description="collision",
        category="mcp.s",
        version="v1",
        input_schema={},
        output_schema={},
        platforms=(RuntimePlatform.WINDOWS,),
        permissions=(),
        risk_level="LOW",
        workspace_scope="EXTERNAL",
        timeout_seconds=10,
        retryable=False,
        preferred_tool="mcp.s.tool",
        source="mcp:s",
        evidence_type="MCP_TOOL_RESULT",
    )
    with pytest.raises(ValueError, match="collides with core"):
        merge_capability_definitions(core, [collision])


# ---------------------------------------------------------------------------
# Concrete ToolRegistry binding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_adapter_registers_in_tool_registry():
    """MCPToolAdapter registered via register_mcp_tools is resolvable."""
    manager = MCPManager()
    config = _fake_config()
    tools = await manager.connect(config)
    try:
        registry = ToolRegistry()
        names = register_mcp_tools(registry, manager, tools)
        assert len(names) == len(tools)
        for name in names:
            assert name in registry.list_capabilities()
            tool = registry.resolve(name)
            assert isinstance(tool, MCPToolAdapter)
    finally:
        await manager.close_all()


# ---------------------------------------------------------------------------
# CapabilityRegistry composed with MCP definitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_composed_registry_has_mcp_capabilities():
    manager = MCPManager()
    tools = await manager.connect(_fake_config())
    try:
        mcp_defs = mcp_capabilities(tools)
        core_defs = list(default_capabilities())
        all_defs = merge_capability_definitions(core_defs, mcp_defs)

        cap_registry = CapabilityRegistry(definitions=all_defs)
        all_ids = {d.id for d in cap_registry.list_all()}

        # Core capabilities present
        assert "workspace.read" in all_ids
        # MCP capabilities present
        assert "mcp.odys-fake.echo" in all_ids
        # No second registry created
        assert len(cap_registry.list_all()) == len(core_defs) + len(mcp_defs)
    finally:
        await manager.close_all()


# ---------------------------------------------------------------------------
# ToolContract invocation succeeds end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_contract_invocation_succeeds():
    """Full path: MCP discovery → CapabilityDefinition → CapabilityRegistry
    + ToolRegistry → ToolContract.invoke succeeds."""
    manager = MCPManager()
    tools = await manager.connect(_fake_config())
    try:
        mcp_defs = mcp_capabilities(tools)
        core_defs = list(default_capabilities())
        all_defs = merge_capability_definitions(core_defs, mcp_defs)

        tool_registry = ToolRegistry()
        register_mcp_tools(tool_registry, manager, tools)

        cap_registry = CapabilityRegistry(
            tool_registry=tool_registry, definitions=all_defs
        )
        contract = ToolContract(cap_registry, tool_registry)

        request = _sample_request(
            capability_id="mcp.odys-fake.echo",
            tool_name="mcp.odys-fake.echo",
            arguments={"text": "hello from contract"},
        )
        result = await contract.invoke(request, _context("windows"))

        assert result.status is ToolResultStatus.SUCCESS
        assert result.output is not None
        # MCP returns {"content": [...], "isError": False}
        assert result.output.get("isError") is False
        content_items = result.output.get("content", [])
        assert any(item.get("text") == "hello from contract" for item in content_items)
    finally:
        await manager.close_all()


# ---------------------------------------------------------------------------
# Unavailable MCP backend fails closed
# ---------------------------------------------------------------------------


def test_missing_mcp_tool_is_unavailable():
    """An MCP capability whose tool is not in ToolRegistry is UNAVAILABLE."""
    info = MCPToolInfo(name="mcp.ghost.missing", server_name="ghost")
    cap = mcp_tool_to_capability(info)

    # Registry with no tools at all
    tool_registry = ToolRegistry()
    cap_registry = CapabilityRegistry(
        tool_registry=tool_registry, definitions=list(default_capabilities()) + [cap]
    )
    records = {r.id: r for r in cap_registry.discover(_context("windows"))}
    assert records["mcp.ghost.missing"].availability is CapabilityAvailability.UNAVAILABLE
    assert records["mcp.ghost.missing"].reason.code == "MISSING_TOOL_BINDING"


@pytest.mark.asyncio
async def test_contract_rejects_unavailable_mcp_backend():
    """ToolContract.invoke returns FAILURE for unavailable MCP backend."""
    info = MCPToolInfo(name="mcp.ghost.missing", server_name="ghost")
    cap = mcp_tool_to_capability(info)

    tool_registry = ToolRegistry()
    cap_registry = CapabilityRegistry(
        tool_registry=tool_registry, definitions=list(default_capabilities()) + [cap]
    )
    contract = ToolContract(cap_registry, tool_registry)

    request = _sample_request(
        capability_id="mcp.ghost.missing",
        tool_name="mcp.ghost.missing",
    )
    result = await contract.invoke(request, _context("windows"))
    assert result.status is ToolResultStatus.FAILURE
    assert result.error_type == ToolErrorCode.CAPABILITY_UNAVAILABLE.value


# ---------------------------------------------------------------------------
# Malformed arguments rejected before MCP call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_arguments_rejected_before_mcp_call():
    """Contract validates input schema; invalid args fail before MCP call."""
    manager = MCPManager()
    tools = await manager.connect(_fake_config())
    try:
        schema = tools[0].input_schema  # requires "text"
        mcp_defs = mcp_capabilities(tools)
        core_defs = list(default_capabilities())

        tool_registry = ToolRegistry()
        register_mcp_tools(tool_registry, manager, tools)

        cap_registry = CapabilityRegistry(
            tool_registry=tool_registry,
            definitions=core_defs + mcp_defs,
        )
        contract = ToolContract(cap_registry, tool_registry)

        # Missing required "text" field
        request = _sample_request(
            capability_id="mcp.odys-fake.echo",
            tool_name="mcp.odys-fake.echo",
            arguments={"wrong_field": 123},
        )
        result = await contract.invoke(request, _context("windows"))
        assert result.status is ToolResultStatus.FAILURE
        assert result.error_type == ToolErrorCode.INVALID_ARGUMENT.value
    finally:
        await manager.close_all()


# ---------------------------------------------------------------------------
# MCP call failure remains machine-readable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_failure_returns_machine_readable_error():
    """MCPToolAdapter.execute wraps exceptions in a structured ToolResult."""
    manager = MCPManager()
    tools = await manager.connect(_fake_config())
    try:
        # Create an adapter for a tool that doesn't exist on the server
        ghost_info = MCPToolInfo(
            name="mcp.odys-fake.nonexistent",
            description="will fail",
            server_name="odys-fake",
        )
        adapter = MCPToolAdapter(manager, ghost_info)
        request = _sample_request(
            capability_id="mcp.odys-fake.nonexistent",
            tool_name="mcp.odys-fake.nonexistent",
            arguments={},
        )
        result = await adapter.execute(request)
        assert result.status is ToolResultStatus.FAILURE
        assert result.error_type is not None
        assert result.error_message is not None
        assert result.metadata.get("origin") == "mcp"
        assert result.metadata.get("server_name") == "odys-fake"
    finally:
        await manager.close_all()


# ---------------------------------------------------------------------------
# Evidence provenance correct
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evidence_provenance_correct():
    """ToolContract produces evidence with correct capability_id, tool_name,
    source, and server metadata for MCP tools."""
    manager = MCPManager()
    tools = await manager.connect(_fake_config())
    try:
        mcp_defs = mcp_capabilities(tools)
        core_defs = list(default_capabilities())

        tool_registry = ToolRegistry()
        register_mcp_tools(tool_registry, manager, tools)

        cap_registry = CapabilityRegistry(
            tool_registry=tool_registry, definitions=core_defs + mcp_defs
        )
        contract = ToolContract(cap_registry, tool_registry)

        request = _sample_request(
            capability_id="mcp.odys-fake.echo",
            tool_name="mcp.odys-fake.echo",
            arguments={"text": "evidence test"},
        )
        result = await contract.invoke(request, _context("windows"))
        assert result.status is ToolResultStatus.SUCCESS
        assert result.evidence is not None
        assert result.evidence.capability_id == "mcp.odys-fake.echo"
        assert result.evidence.tool_name == "mcp.odys-fake.echo"
        assert result.evidence.source == "odys-tool-contract-v1"
        # Evidence identity matches invocation (no forge)
        assert result.evidence.capability_id == request.capability_id
        assert result.evidence.tool_name == request.tool_name
    finally:
        await manager.close_all()


# ---------------------------------------------------------------------------
# No duplicate capability registry
# ---------------------------------------------------------------------------


def test_no_duplicate_capability_ids_in_composed_registry():
    """Merged core + MCP definitions must not produce duplicate IDs."""
    core = list(default_capabilities())
    mcp = mcp_capabilities([
        MCPToolInfo(name="mcp.s.a", server_name="s"),
        MCPToolInfo(name="mcp.s.b", server_name="s"),
    ])
    merged = merge_capability_definitions(core, mcp)
    # CapabilityRegistry itself rejects duplicates
    registry = CapabilityRegistry(definitions=merged)
    ids = [d.id for d in registry.list_all()]
    assert len(ids) == len(set(ids))


def test_mcp_ids_do_not_collide_with_core():
    """MCP tool names use mcp.<server>.<tool> and cannot collide with core."""
    core_ids = {d.id for d in default_capabilities()}
    # Simulate what a real MCP server might produce
    info = MCPToolInfo(name="mcp.some-server.read", server_name="some-server")
    cap = mcp_tool_to_capability(info)
    assert cap.id not in core_ids


# ---------------------------------------------------------------------------
# Invariant: no double-prefix (MCP_CAPABILITY_DOUBLE_PREFIX=NO)
# ---------------------------------------------------------------------------


def test_mcp_capability_id_has_no_double_prefix():
    """MCPManager.discover() produces 'mcp.<server>.<tool>' — the adapter
    must use that as-is, never prepend another 'mcp.' prefix."""
    info = MCPToolInfo(name="mcp.odys-fake.echo", server_name="odys-fake")
    cap = mcp_tool_to_capability(info)
    # Must NOT be 'mcp.mcp.odys-fake.echo'
    assert cap.id == "mcp.odys-fake.echo"
    assert cap.preferred_tool == "mcp.odys-fake.echo"
    assert not cap.id.startswith("mcp.mcp.")
    assert not cap.preferred_tool.startswith("mcp.mcp.")


# ---------------------------------------------------------------------------
# Invariant: CapabilitySpec alone cannot expose MCP semantic capability
# (test 9 from integration spec)
# ---------------------------------------------------------------------------


def test_mcp_capability_spec_only_not_model_visible():
    """MCPToolAdapter registered in ToolRegistry without an explicit
    CapabilityDefinition must NOT be model-visible (per P2.3 frozen
    authority model: CapabilitySpec is backend descriptor only)."""
    # Register MCPToolAdapter without creating CapabilityDefinition
    manager = MCPManager()
    info = MCPToolInfo(name="mcp.ghost.tool", server_name="ghost")
    adapter = MCPToolAdapter(manager, info)

    tool_registry = ToolRegistry()
    tool_registry.register(adapter)

    # The adapter's CapabilitySpec is present as a backend descriptor
    assert adapter.capability.name == "mcp.ghost.tool"
    assert adapter.capability.origin == "mcp"

    # But without a CapabilityDefinition in CapabilityRegistry,
    # the tool should be UNAVAILABLE (not model-visible)
    cap_registry = CapabilityRegistry(
        tool_registry=tool_registry,
        definitions=list(default_capabilities()),  # no MCP defs
    )
    from lhas.capability_registry import CapabilityAvailability
    context = CapabilityRuntimeContext(platform="windows")
    records = {r.id: r for r in cap_registry.discover(context)}
    # The MCP tool is NOT in the capability catalog at all
    assert "mcp.ghost.tool" not in records


# ---------------------------------------------------------------------------
# Invariant: Tool success ≠ Task completion (test 8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_tool_success_is_not_task_completion():
    """A successful MCP tool invocation via ToolContract produces tool
    execution evidence, NOT completion evidence.  The evidence type is
    'TOOL_EXECUTION' and summary says 'tool execution succeeded' — it
    does not claim task completion."""
    manager = MCPManager()
    tools = await manager.connect(_fake_config())
    try:
        mcp_defs = mcp_capabilities(tools)
        core_defs = list(default_capabilities())

        tool_registry = ToolRegistry()
        register_mcp_tools(tool_registry, manager, tools)

        cap_registry = CapabilityRegistry(
            tool_registry=tool_registry, definitions=core_defs + mcp_defs
        )
        contract = ToolContract(cap_registry, tool_registry)

        request = _sample_request(
            capability_id="mcp.odys-fake.echo",
            tool_name="mcp.odys-fake.echo",
            arguments={"text": "success ≠ completion"},
        )
        result = await contract.invoke(request, _context("windows"))
        assert result.status is ToolResultStatus.SUCCESS

        # Evidence exists and is tool-execution evidence, NOT completion
        assert result.evidence is not None
        assert result.evidence.evidence_type == "TOOL_EXECUTION"
        assert "succeeded" in result.evidence.summary
        # No completion claim in evidence
        assert "complete" not in result.evidence.summary.lower() or \
               "tool execution" in result.evidence.summary
    finally:
        await manager.close_all()
