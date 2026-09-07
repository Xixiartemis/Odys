"""Narrow reusable invocation facade for routing through ToolContract.

This module provides ONE helper that wraps ToolContract.invoke so that
agent-facing code paths route through the contract boundary instead of
calling registry.resolve(...).execute(...) directly.
"""

from __future__ import annotations

from collections.abc import Iterable

from lhas.capability_registry import (
    CapabilityDefinition,
    CapabilityRegistry,
    CapabilityRuntimeContext,
    default_capabilities,
)
from .contract import ToolContract
from .protocol import ToolRequest, ToolResult


def build_contract_for_registry(
    registry,
    *,
    definitions: Iterable[CapabilityDefinition] | None = None,
) -> tuple[CapabilityRegistry, ToolContract]:
    """Build a contract from explicit semantic definitions only.

    ``ToolRegistry`` is deliberately not inspected for capability metadata
    here.  A backend ``CapabilitySpec`` is an implementation descriptor and
    cannot synthesize a semantic ``CapabilityDefinition``.  Adapter callers
    that add capabilities must pass their explicit definitions via
    ``definitions``; the contract will then verify the declared backend
    binding during invocation.
    """
    declared = [*default_capabilities(), *(definitions or ())]
    cap_reg = CapabilityRegistry(registry, definitions=declared)
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
