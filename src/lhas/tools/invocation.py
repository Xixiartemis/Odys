"""Narrow reusable invocation facade for routing through ToolContract.

This module provides ONE helper that wraps ToolContract.invoke so that
agent-facing code paths route through the contract boundary instead of
calling registry.resolve(...).execute(...) directly.
"""

from __future__ import annotations

from lhas.capability_registry import (
    CapabilityDefinition,
    CapabilityRegistry,
    CapabilityRuntimeContext,
    RuntimePlatform,
    default_capabilities,
)
from .contract import ToolContract
from .protocol import ToolRequest, ToolResult


def build_contract_for_registry(registry) -> tuple[CapabilityRegistry, ToolContract]:
    """Build CapabilityRegistry + ToolContract covering all tools in registry.

    Tools already in default_capabilities() keep their canonical definitions.
    Additional tools get permissive runtime definitions for contract boundary
    validation.  INTERNAL USE ONLY -- NOT model-facing.
    """
    existing = {d.id for d in default_capabilities()}
    extra: list[CapabilityDefinition] = []
    for name in getattr(registry, "list_capabilities", lambda: [])():
        if name in existing:
            continue
        tool = registry.resolve(name)
        spec = tool.capability
        extra.append(CapabilityDefinition(
            id=name,
            name=name,
            description=spec.description or f"Internal tool {name}",
            category="internal",
            version="v1",
            input_schema=spec.input_schema or {"type": "object", "additionalProperties": True},
            output_schema={},  # permissive — MCP/internal tools may return any JSON
            platforms=(RuntimePlatform.WINDOWS, RuntimePlatform.LINUX, RuntimePlatform.MACOS),
            permissions=("internal.execute",),
            risk_level="LOW",
            workspace_scope="SOURCE_WORKSPACE",
            timeout_seconds=30.0,
            retryable=True,
            preferred_tool=name,
            source="internal-contract",
            evidence_type="DETERMINISTIC_TOOL_RESULT",
        ))
    cap_reg = CapabilityRegistry(registry, definitions=[*default_capabilities(), *extra])
    contract = ToolContract(cap_reg, registry)
    return cap_reg, contract


async def invoke_via_contract(
    contract: ToolContract,
    request: ToolRequest,
    runtime_context: CapabilityRuntimeContext | None = None,
) -> ToolResult:
    """Route a single tool invocation through the ToolContract boundary."""
    ctx = runtime_context or CapabilityRuntimeContext(platform="windows")
    return await contract.invoke(request, ctx)
