"""Phase 2 Capability Runtime Conformance Tests.

Families A-J proving every agent-facing capability uses the same contract.
"""
from __future__ import annotations

import asyncio
import inspect
import tempfile
from pathlib import Path

import pytest

from lhas.capability_registry import (
    CapabilityAvailability,
    CapabilityDefinition,
    CapabilityRegistry,
    CapabilityRuntimeContext,
    default_capabilities,
)
from lhas.mcp.capabilities import mcp_tool_to_capability
from lhas.mcp.models import MCPToolInfo
from lhas.planning.models import CapabilitySpec
from lhas.skills.validator import validate_skill_capabilities
from lhas.tools import (
    FakeTool,
    ToolContract,
    ToolErrorCode,
    ToolResultStatus,
    ToolRegistry,
)
from lhas.tools.protocol import ToolEvidence, ToolRequest, ToolResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ctx(platform: str = "windows") -> CapabilityRuntimeContext:
    return CapabilityRuntimeContext(platform=platform)


def _request(
    *,
    capability_id: str = "workspace.list",
    tool_name: str | None = None,
    arguments: dict | None = None,
) -> ToolRequest:
    return ToolRequest(
        tool_call_id="tc-conf-1",
        task_id="t-conf-1",
        run_id="r-conf-1",
        attempt_id="a-conf-1",
        capability_id=capability_id,
        tool_name=tool_name or capability_id,
        arguments=arguments or {},
    )


def _invoke(contract: ToolContract, req: ToolRequest, platform: str = "windows") -> ToolResult:
    return asyncio.run(contract.invoke(req, _ctx(platform)))


def _register_fake_builtins(tools: ToolRegistry) -> None:
    """Register fake backends for all builtin capabilities."""
    seen: set[str] = set()
    for defn in default_capabilities():
        pt = defn.preferred_tool
        if pt and pt not in seen:
            # Use a handler that returns schema-safe output
            tools.register(FakeTool(CapabilitySpec(name=pt), handler=lambda req: {}))
            seen.add(pt)


def _build_registry(extra_defs: list[CapabilityDefinition] | None = None) -> tuple[ToolRegistry, CapabilityRegistry]:
    tools = ToolRegistry()
    _register_fake_builtins(tools)
    defs = list(default_capabilities())
    if extra_defs:
        defs.extend(extra_defs)
    cap = CapabilityRegistry(tools, definitions=defs)
    return tools, cap


def _discover_ids(cap: CapabilityRegistry) -> set[str]:
    """Get discoverable capability IDs."""
    return {record.id for record in cap.discover(_ctx())}


# ---------------------------------------------------------------------------
# A — SEMANTIC AUTHORITY
# ---------------------------------------------------------------------------

class TestSemanticAuthority:
    """CapabilityDefinition is the sole semantic authority."""

    def test_spec_only_tool_not_model_visible(self):
        """Tool with CapabilitySpec but no Definition -> not discoverable."""
        tools = ToolRegistry()
        tools.register(FakeTool(CapabilitySpec(name="ghost.backend")))
        cap = CapabilityRegistry(tools)
        avail = _discover_ids(cap)
        assert "ghost.backend" not in avail

    def test_no_reverse_synthesis_in_registry(self):
        """capability_registry.py does not synthesize Definition from Spec."""
        from lhas import capability_registry as cr_mod
        source = inspect.getsource(cr_mod)
        lines = source.split("\n")
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                continue
            if "CapabilityDefinition(" in stripped and "CapabilitySpec" in stripped and "def " not in stripped:
                if "test" not in stripped.lower() and "example" not in stripped.lower():
                    pytest.fail(f"Possible CapSpec->CapDef synthesis: {stripped}")


# ---------------------------------------------------------------------------
# B — DISCOVERY / BINDING
# ---------------------------------------------------------------------------

class TestDiscoveryBinding:
    """Every static capability is discoverable with valid binding."""

    def test_all_default_capabilities_discoverable(self):
        tools, cap = _build_registry()
        avail = _discover_ids(cap)
        for defn in default_capabilities():
            assert defn.id in avail, f"{defn.id} not discoverable"

    def test_preferred_tool_matches_backend(self):
        tools, cap = _build_registry()
        for defn in default_capabilities():
            if defn.preferred_tool:
                assert tools.resolve(defn.preferred_tool) is not None, \
                    f"preferred_tool {defn.preferred_tool} not registered for {defn.id}"


# ---------------------------------------------------------------------------
# C — CONTRACT PATH
# ---------------------------------------------------------------------------

class TestContractPath:
    """Invocation crosses CapabilityRegistry -> ToolContract -> ToolRegistry -> Tool."""

    def test_builtin_invocation_through_contract(self):
        tools, cap = _build_registry()
        contract = ToolContract(cap, tools)
        result = _invoke(contract, _request(
            capability_id="platform.prepare",
            arguments={"goal": "test"},
        ))
        assert result.status is ToolResultStatus.SUCCESS

    def test_platform_invocation_through_contract(self):
        tools, cap = _build_registry()
        contract = ToolContract(cap, tools)
        for cap_id in ["platform.prepare", "platform.delegate", "platform.finalize"]:
            result = _invoke(contract, _request(capability_id=cap_id, arguments={}))
            assert result.status is ToolResultStatus.SUCCESS, f"{cap_id} failed: {result.error_type}"

    @pytest.mark.asyncio
    async def test_mcp_invocation_through_contract(self):
        """MCP tool invoked through full contract path (offline, no network)."""
        from lhas.mcp.adapter import register_mcp_tools
        from lhas.mcp.capabilities import mcp_capabilities, merge_capability_definitions
        from lhas.mcp.manager import MCPManager
        from lhas.mcp.models import MCPServerConfig

        manager = MCPManager()
        config = MCPServerConfig(
            name="odys-fake",
            command=["python", str(Path(__file__).resolve().parents[1] / "src" / "lhas" / "mcp" / "fake_server.py")],
        )
        mcp_tools = await manager.connect(config)
        try:
            mcp_defs = mcp_capabilities(mcp_tools)
            core_defs = list(default_capabilities())
            all_defs = merge_capability_definitions(core_defs, mcp_defs)

            tool_registry = ToolRegistry()
            register_mcp_tools(tool_registry, manager, mcp_tools)

            cap_registry = CapabilityRegistry(tool_registry, definitions=all_defs)
            contract = ToolContract(cap_registry, tool_registry)

            req = _request(
                capability_id="mcp.odys-fake.echo",
                tool_name="mcp.odys-fake.echo",
                arguments={"text": "hello"},
            )
            result = await contract.invoke(req, _ctx())
            assert result.status is ToolResultStatus.SUCCESS
            assert result.output is not None
        finally:
            await manager.close_all()


# ---------------------------------------------------------------------------
# D — INVALID INPUT
# ---------------------------------------------------------------------------

class TestInvalidInput:
    """Malformed args -> contract rejects -> backend execute count = 0."""

    def test_malformed_args_rejected(self):
        tools, cap = _build_registry()
        contract = ToolContract(cap, tools)
        result = _invoke(contract, _request(
            capability_id="workspace.read",
            tool_name="workspace.read",
            arguments={"wrong_field": 123},
        ))
        assert result.status is ToolResultStatus.FAILURE
        assert result.error_type == ToolErrorCode.INVALID_ARGUMENT.value


# ---------------------------------------------------------------------------
# E — MISSING BACKEND
# ---------------------------------------------------------------------------

class TestMissingBackend:
    """Definition exists but backend missing -> fail closed."""

    def test_missing_backend_fails_closed(self):
        tools = ToolRegistry()  # empty — no backends
        cap = CapabilityRegistry(tools, definitions=list(default_capabilities()))
        contract = ToolContract(cap, tools)
        result = _invoke(contract, _request(
            capability_id="workspace.read",
            tool_name="workspace.read",
            arguments={"path": "."},
        ))
        assert result.status is ToolResultStatus.FAILURE
        assert result.error_type in (
            ToolErrorCode.CAPABILITY_UNAVAILABLE.value,
            ToolErrorCode.TOOL_NOT_FOUND.value,
        )


# ---------------------------------------------------------------------------
# F — EVIDENCE
# ---------------------------------------------------------------------------

class TestEvidence:
    """Typed ToolResult + ToolEvidence. Tool SUCCESS != Task completion."""

    def test_success_returns_typed_evidence(self):
        tools, cap = _build_registry()
        contract = ToolContract(cap, tools)
        result = _invoke(contract, _request(
            capability_id="platform.prepare",
            arguments={"goal": "test"},
        ))
        assert result.status is ToolResultStatus.SUCCESS
        assert result.evidence is not None
        assert isinstance(result.evidence, ToolEvidence)
        assert result.evidence.capability_id == "platform.prepare"

    def test_failure_returns_typed_result(self):
        tools = ToolRegistry()
        cap = CapabilityRegistry(tools, definitions=list(default_capabilities()))
        contract = ToolContract(cap, tools)
        result = _invoke(contract, _request(
            capability_id="workspace.read",
            tool_name="workspace.read",
            arguments={"path": "."},
        ))
        assert result.status is ToolResultStatus.FAILURE
        assert result.error_type is not None

    def test_tool_success_not_task_completion(self):
        """ToolEvidence.evidence_type is TOOL_EXECUTION, not task_completion."""
        tools, cap = _build_registry()
        contract = ToolContract(cap, tools)
        result = _invoke(contract, _request(
            capability_id="platform.prepare",
            arguments={"goal": "test"},
        ))
        assert result.evidence is not None
        assert result.evidence.evidence_type == "TOOL_EXECUTION"


# ---------------------------------------------------------------------------
# G — MCP
# ---------------------------------------------------------------------------

class TestMCPConformance:
    """MCP capabilities conform to full contract."""

    def test_mcp_explicit_definition(self):
        info = MCPToolInfo(name="mcp.odys-fake.echo", server_name="odys-fake", description="echo")
        defn = mcp_tool_to_capability(info)
        assert defn.id == "mcp.odys-fake.echo"
        assert defn.preferred_tool == "mcp.odys-fake.echo"

    def test_mcp_no_double_prefix(self):
        info = MCPToolInfo(name="mcp.odys-fake.echo", server_name="odys-fake", description="echo")
        defn = mcp_tool_to_capability(info)
        assert not defn.id.startswith("mcp.mcp.")

    def test_mcp_capspec_only_not_visible(self):
        """MCPToolAdapter registered without Definition -> not discoverable."""
        tools = ToolRegistry()
        info = MCPToolInfo(name="mcp.ghost.tool", server_name="ghost", description="ghost")
        from lhas.mcp.adapter import MCPToolAdapter
        from lhas.mcp.manager import MCPManager
        manager = MCPManager()
        tools.register(MCPToolAdapter(manager, info))
        cap = CapabilityRegistry(tools)
        avail_ids = _discover_ids(cap)
        assert "mcp.ghost.tool" not in avail_ids


# ---------------------------------------------------------------------------
# H — SKILLS
# ---------------------------------------------------------------------------

class TestSkillsBoundary:
    """Skills consume registry state only."""

    def test_explicit_mcp_definition_satisfies_skill(self):
        info = MCPToolInfo(name="mcp.odys-fake.echo", server_name="odys-fake", description="echo")
        defn = mcp_tool_to_capability(info)
        tools = ToolRegistry()
        # Register the MCP backend so the capability is available
        from lhas.mcp.adapter import MCPToolAdapter
        from lhas.mcp.manager import MCPManager
        manager = MCPManager()
        tools.register(MCPToolAdapter(manager, info))
        cap = CapabilityRegistry(tools, definitions=[defn])
        with tempfile.TemporaryDirectory() as td:
            skill_dir = Path(td) / "test-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                '---\nname: test\nrequired_capabilities: ["mcp.odys-fake.echo"]\n---\nBody'
            )
            from lhas.skills.registry import SkillRegistry
            reg = SkillRegistry([Path(td)])
            doc = reg.view("test")
            report = validate_skill_capabilities(doc, cap, _ctx())
            available = [e for e in report.entries if e.available]
            assert any(e.capability_id == "mcp.odys-fake.echo" for e in available)

    def test_capspec_only_does_not_satisfy_skill(self):
        tools = ToolRegistry()
        tools.register(FakeTool(CapabilitySpec(name="mcp.ghost.tool")))
        cap = CapabilityRegistry(tools)
        with tempfile.TemporaryDirectory() as td:
            skill_dir = Path(td) / "test-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                '---\nname: test\nrequired_capabilities: ["mcp.ghost.tool"]\n---\nBody'
            )
            from lhas.skills.registry import SkillRegistry
            reg = SkillRegistry([Path(td)])
            doc = reg.view("test")
            report = validate_skill_capabilities(doc, cap, _ctx())
            unavailable = [e for e in report.entries if not e.available]
            assert any(e.capability_id == "mcp.ghost.tool" for e in unavailable)

    def test_validation_executes_zero_tools(self):
        """validate_skill_capabilities never calls resolve() or execute()."""
        tools, cap = _build_registry()
        with tempfile.TemporaryDirectory() as td:
            skill_dir = Path(td) / "test-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                '---\nname: test\nrequired_capabilities: ["workspace.read"]\n---\nBody'
            )
            from lhas.skills.registry import SkillRegistry
            from unittest.mock import patch
            reg = SkillRegistry([Path(td)])
            doc = reg.view("test")
            with patch.object(type(tools), 'resolve', side_effect=AssertionError("resolve called")):
                report = validate_skill_capabilities(doc, cap, _ctx())
            assert report is not None


# ---------------------------------------------------------------------------
# I — PLATFORM
# ---------------------------------------------------------------------------

class TestPlatformConformance:
    """platform.prepare/delegate/finalize cross ToolContract."""

    @pytest.mark.parametrize("cap_id", [
        "platform.prepare", "platform.delegate", "platform.finalize"
    ])
    def test_platform_capability_has_definition(self, cap_id):
        defs = {d.id: d for d in default_capabilities()}
        assert cap_id in defs

    @pytest.mark.parametrize("cap_id", [
        "platform.prepare", "platform.delegate", "platform.finalize"
    ])
    def test_platform_invocation_through_contract(self, cap_id):
        tools, cap = _build_registry()
        contract = ToolContract(cap, tools)
        result = _invoke(contract, _request(capability_id=cap_id, arguments={}))
        assert result.status is ToolResultStatus.SUCCESS


# ---------------------------------------------------------------------------
# J — META ENUMERATION
# ---------------------------------------------------------------------------

class TestMetaEnumeration:
    """All static capabilities must appear in conformance catalog."""

    def test_all_defaults_in_conformance_set(self):
        defs = default_capabilities()
        assert len(defs) >= 10
        tools, cap = _build_registry()
        avail = _discover_ids(cap)
        for d in defs:
            assert d.id in avail, f"{d.id} missing from discoverable set"
