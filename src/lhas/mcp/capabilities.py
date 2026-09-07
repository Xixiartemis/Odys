"""MCP → CapabilityDefinition adapter.

Narrow deterministic conversion from MCPToolInfo (discovered from an MCP
server) to CapabilityDefinition (registered in CapabilityRegistry).

This module does NOT create a second CapabilityRegistry.  It produces
composable definitions: ``core_definitions + mcp_definitions → one
CapabilityRegistry``.
"""

from __future__ import annotations

from lhas.capability_registry import (
    CapabilityAvailability,
    CapabilityDefinition,
    RuntimePlatform,
)
from lhas.mcp.models import MCPToolInfo

# Output schema used when the MCP protocol does not expose one.
# MCP tools/call returns arbitrary JSON; the contract validates against
# whatever schema the CapabilityDefinition declares, so a permissive
# ``{"type": "object"}`` is the honest declaration.
_GENERIC_OUTPUT_SCHEMA: dict = {"type": "object"}

_ALL_PLATFORMS: tuple[RuntimePlatform, ...] = (
    RuntimePlatform.WINDOWS,
    RuntimePlatform.LINUX,
    RuntimePlatform.MACOS,
)

DEFAULT_MCP_TIMEOUT_SECONDS: float = 30.0


def mcp_tool_to_capability(info: MCPToolInfo) -> CapabilityDefinition:
    """Convert one MCPToolInfo to a CapabilityDefinition.

    The conversion is deterministic and preserves:
    - stable capability ID (``mcp.<server>.<remote_tool>``)
    - description from the MCP server
    - input_schema from the MCP server (JSON Schema dict)
    - server identity (encoded in the ID and ``source``)
    - risk, side-effect, approval metadata
    - backend tool binding (preferred_tool == info.name so
      MCPToolAdapter, which registers under ``info.name`` in
      ToolRegistry, resolves correctly)
    """
    if not info.name:
        raise ValueError("MCPToolInfo.name must not be empty")

    return CapabilityDefinition(
        id=info.name,
        name=info.name,
        description=info.description or info.name,
        category=f"mcp.{info.server_name}",
        version="v1",
        input_schema=dict(info.input_schema),
        output_schema=dict(_GENERIC_OUTPUT_SCHEMA),
        platforms=_ALL_PLATFORMS,
        permissions=(),
        risk_level=info.risk,
        workspace_scope="EXTERNAL",
        timeout_seconds=DEFAULT_MCP_TIMEOUT_SECONDS,
        retryable=False,
        preferred_tool=info.name,
        fallback_tools=(),
        availability=CapabilityAvailability.AVAILABLE,
        source=f"mcp:{info.server_name}",
        evidence_type="MCP_TOOL_RESULT",
    )


def mcp_capabilities(tools: list[MCPToolInfo]) -> list[CapabilityDefinition]:
    """Batch-convert a list of MCPToolInfo to CapabilityDefinition."""
    return [mcp_tool_to_capability(info) for info in tools]


def merge_capability_definitions(
    core: list[CapabilityDefinition],
    mcp: list[CapabilityDefinition],
) -> list[CapabilityDefinition]:
    """Merge core and MCP definitions for a single CapabilityRegistry.

    Raises ``ValueError`` if any MCP definition ID collides with a core ID.
    """
    core_ids = {d.id for d in core}
    for defn in mcp:
        if defn.id in core_ids:
            raise ValueError(
                f"MCP capability id collides with core: {defn.id}"
            )
    return list(core) + list(mcp)
