"""Small deterministic validator doubles used by native completion tests.

Also provides helpers for creating explicit CapabilityDefinitions in tests,
so that test tools are registered through the same semantic authority path
as production tools (FIX-06 migration).
"""

from __future__ import annotations

import json
from typing import Any

from lhas.capability_registry import (
    CapabilityDefinition,
    CapabilityRegistry,
    CapabilityRuntimeContext,
    RuntimePlatform,
    default_capabilities,
)
from lhas.planning.models import CapabilitySpec
from lhas.tools.contract import ToolContract
from lhas.validation import ValidationCheck, ValidationResult


class PassingCommandValidator:
    """Represents a completed command validator with observed exit code zero."""

    def __init__(self, command: list[str] | None = None):
        self.command = command or ["pytest", "-q"]

    async def validate(self, *, task, attempt, result):
        return ValidationResult(
            attempt_id=attempt.id,
            passed=True,
            checks=[ValidationCheck(name="explicit_command_exit_zero", passed=True)],
            evidence=json.dumps({"command": self.command, "exit_code": 0, "timed_out": False}),
        )


# ---------------------------------------------------------------------------
# FIX-06: Test capability definition helpers
# ---------------------------------------------------------------------------
#
# After FIX-05, a Tool with only a CapabilitySpec is NOT semantically
# invocable.  Tests that exercise the semantic capability path (planner,
# NativeToolDispatcher, allowed_tools) must register an explicit
# ``CapabilityDefinition`` alongside the tool's ``CapabilitySpec``.
#
# These helpers create deterministic test definitions WITHOUT weakening the
# fail-closed invariant.  They are test infrastructure only — they never
# become production fallback behaviour.


def make_test_capability_definition(
    capability_id: str,
    *,
    description: str | None = None,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    preferred_tool: str | None = None,
    side_effect: bool = False,
    requires_human_approval: bool = False,
    retryable: bool = True,
) -> CapabilityDefinition:
    """Create an explicit ``CapabilityDefinition`` for a test capability.

    This is the single source of truth for test capability declarations.
    It does NOT inspect the backend ``CapabilitySpec`` — the definition is
    always explicit.
    """
    return CapabilityDefinition(
        id=capability_id,
        name=capability_id,
        description=description or f"test capability {capability_id}",
        category="test",
        version="v1-test",
        input_schema=input_schema or {"type": "object", "additionalProperties": False},
        output_schema=output_schema or {},
        platforms=(RuntimePlatform.WINDOWS, RuntimePlatform.LINUX, RuntimePlatform.MACOS),
        permissions=("test.execute",),
        risk_level="LOW",
        workspace_scope="SOURCE_WORKSPACE",
        timeout_seconds=30.0,
        retryable=retryable,
        preferred_tool=preferred_tool or capability_id,
        fallback_tools=(),
        source="test-fixture",
        evidence_type="DETERMINISTIC_TOOL_RESULT",
    )


def make_test_capability_registry(
    registry,
    definitions: list[CapabilityDefinition] | None = None,
) -> tuple[CapabilityRegistry, ToolContract]:
    """Build a ``CapabilityRegistry`` + ``ToolContract`` that includes the
    explicit core catalog PLUS any supplied test definitions.

    Returns ``(capability_registry, tool_contract)``.
    """
    all_defs = [*default_capabilities(), *(definitions or [])]
    cap_reg = CapabilityRegistry(registry, definitions=all_defs)
    contract = ToolContract(cap_reg, registry)
    return cap_reg, contract


# ---------------------------------------------------------------------------
# P3.1 Verification seam test doubles
# ---------------------------------------------------------------------------

class _VerificationResult:
    def __init__(self, accepted: bool, reason: str = ""):
        self.accepted = accepted
        self.reason = reason


class AcceptingVerifier:
    """Test-only verifier that always accepts (CLAIMED_COMPLETE → VERIFIED)."""
    def verify(self, step, plan, events):
        return _VerificationResult(True, "test_accept")


class RejectingVerifier:
    """Test-only verifier that always rejects (CLAIMED_COMPLETE → CLASSIFIED_FAILURE)."""
    def verify(self, step, plan, events):
        return _VerificationResult(False, "test_reject")
