"""Deterministic Tool Contract V1 validation and adapter boundary."""

from __future__ import annotations

from enum import Enum
from typing import Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError
from pydantic import BaseModel, ConfigDict, Field

from lhas.capability_registry import (
    CapabilityAvailability,
    CapabilityRegistry,
    CapabilityRuntimeContext,
)

from .protocol import ToolEvidence, ToolRequest, ToolResult, ToolResultStatus


class ToolErrorCode(str, Enum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    POLICY_REJECTED = "POLICY_REJECTED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    TIMEOUT = "TIMEOUT"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    OUTPUT_VALIDATION_FAILED = "OUTPUT_VALIDATION_FAILED"
    CANCELLED = "CANCELLED"
    INTERNAL_ERROR = "INTERNAL_ERROR"

    # Existing concrete tool errors remain representable and are not mapped
    # away by the new contract boundary.
    COMMAND_NOT_ALLOWED = "COMMAND_NOT_ALLOWED"
    WORKSPACE_PATH_ESCAPE = "WORKSPACE_PATH_ESCAPE"
    COMMAND_TIMEOUT = "COMMAND_TIMEOUT"
    SPAWN_ERROR = "SPAWN_ERROR"
    TOOL_ERROR = "TOOL_ERROR"


class ToolContractDecision(BaseModel):
    """Validation result and effective invocation policy, without execution."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    error_type: ToolErrorCode | None = None
    error_message: str | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    effective_timeout_seconds: float | None = None
    retry_allowed: bool = False
    retry_reason: str = "not evaluated"


def _schema_diagnostic(error: ValidationError) -> dict[str, Any]:
    """Return bounded schema location data without copying the invalid value."""

    return {
        "path": [str(part) for part in error.absolute_path],
        "validator": str(error.validator),
        "validator_value": error.validator_value,
        # jsonschema's human message can embed the invalid value (including
        # secrets). Keep the durable diagnostic structural and bounded.
        "message": f"schema violation: {error.validator}",
    }


def _failure(
    code: ToolErrorCode,
    message: str,
    *,
    diagnostics: dict[str, Any] | None = None,
    decision: ToolContractDecision | None = None,
) -> ToolResult:
    metadata: dict[str, Any] = {
        "contract": {
            "error_type": code.value,
            "diagnostics": diagnostics or {},
        }
    }
    if decision is not None:
        metadata["contract"].update({
            "effective_timeout_seconds": decision.effective_timeout_seconds,
            "retry_allowed": decision.retry_allowed,
            "retry_reason": decision.retry_reason,
        })
    return ToolResult(
        status=ToolResultStatus.FAILURE,
        error_type=code.value,
        error_message=message,
        metadata=metadata,
    )


class ToolContract:
    """Validate and adapt one invocation to an existing Tool instance.

    This class is deliberately not a registry, policy engine, retry loop, or
    durable state machine. It performs one pre-execution validation/binding
    pass, calls the already registered Tool once, and validates its output.
    """

    def __init__(self, capability_registry: CapabilityRegistry, tool_registry):
        self.capability_registry = capability_registry
        self.tool_registry = tool_registry

    @staticmethod
    def _context(context: CapabilityRuntimeContext | None) -> CapabilityRuntimeContext:
        if context is None:
            raise ValueError("runtime context is required")
        return context

    def validate_request(
        self,
        request: ToolRequest,
        runtime_context: CapabilityRuntimeContext,
    ) -> ToolContractDecision:
        capability_id = request.capability_id
        if not capability_id or not request.tool_name:
            return ToolContractDecision(
                valid=False,
                error_type=ToolErrorCode.INVALID_ARGUMENT,
                error_message="capability_id and tool_name are required",
                diagnostics={"reason": "MISSING_INVOCATION_IDENTITY"},
                retry_reason="invocation identity is invalid",
            )
        if self.tool_registry is None:
            return ToolContractDecision(
                valid=False,
                error_type=ToolErrorCode.CAPABILITY_UNAVAILABLE,
                error_message="a concrete ToolRegistry is required for invocation",
                diagnostics={"reason": "MISSING_TOOL_REGISTRY"},
                retry_reason="a concrete runtime backend must be supplied",
            )
        try:
            definition = self.capability_registry.get(capability_id)
        except KeyError:
            return ToolContractDecision(
                valid=False,
                error_type=ToolErrorCode.CAPABILITY_UNAVAILABLE,
                error_message="unknown capability",
                diagnostics={"capability_id": capability_id},
                retry_reason="unknown capabilities are not retryable",
            )

        # Use the concrete registry for invocation discovery even when the
        # CapabilityRegistry was constructed for declaration-only diagnostics.
        context = runtime_context.model_copy(
            update={"available_tools": set(self.tool_registry.list_capabilities())}
        )
        record = next(
            item for item in self.capability_registry.discover(context)
            if item.id == capability_id
        )
        if record.availability is not CapabilityAvailability.AVAILABLE:
            reason = record.reason.code if record.reason else "UNAVAILABLE"
            return ToolContractDecision(
                valid=False,
                error_type=ToolErrorCode.CAPABILITY_UNAVAILABLE,
                error_message="capability is unavailable in the current runtime",
                diagnostics={"reason": reason},
                retry_reason="backend or platform availability must change",
            )

        if request.tool_name not in (definition.preferred_tool, *definition.fallback_tools):
            return ToolContractDecision(
                valid=False,
                error_type=ToolErrorCode.INVALID_ARGUMENT,
                error_message="tool_name does not match the capability binding",
                diagnostics={
                    "capability_id": capability_id,
                    "requested_tool": request.tool_name,
                    "allowed_tools": [definition.preferred_tool, *definition.fallback_tools],
                    "reason": "BINDING_MISMATCH",
                },
                retry_reason="binding mismatch is not retryable",
            )

        try:
            actual_tool = self.tool_registry.resolve(request.tool_name)
        except KeyError:
            return ToolContractDecision(
                valid=False,
                error_type=ToolErrorCode.TOOL_NOT_FOUND,
                error_message="bound tool is not registered",
                diagnostics={"tool_name": request.tool_name},
                retry_reason="tool registration must change",
            )
        if actual_tool.capability.name != request.tool_name:
            return ToolContractDecision(
                valid=False,
                error_type=ToolErrorCode.TOOL_NOT_FOUND,
                error_message="registered tool identity does not match its binding",
                diagnostics={"tool_name": request.tool_name},
                retry_reason="tool identity is invalid",
            )

        try:
            validator = Draft202012Validator(definition.input_schema)
            validator.check_schema(definition.input_schema)
            validator.validate(request.arguments)
        except SchemaError:
            return ToolContractDecision(
                valid=False,
                error_type=ToolErrorCode.INTERNAL_ERROR,
                error_message="capability input schema is invalid",
                diagnostics={"reason": "INVALID_INPUT_SCHEMA"},
                retry_reason="schema must be corrected",
            )
        except ValidationError as error:
            return ToolContractDecision(
                valid=False,
                error_type=ToolErrorCode.INVALID_ARGUMENT,
                error_message="tool arguments failed capability input schema",
                diagnostics=_schema_diagnostic(error),
                retry_reason="arguments must be corrected",
            )

        requested_timeout = request.timeout_seconds
        effective_timeout = (
            definition.timeout_seconds
            if requested_timeout is None
            else min(requested_timeout, definition.timeout_seconds)
        )
        return ToolContractDecision(
            valid=True,
            effective_timeout_seconds=effective_timeout,
            retry_allowed=definition.retryable,
            retry_reason=("capability is retryable" if definition.retryable else "capability is not retryable"),
        )

    def prepare(
        self,
        request: ToolRequest,
        runtime_context: CapabilityRuntimeContext,
    ) -> ToolContractDecision:
        return self.validate_request(request, self._context(runtime_context))

    @staticmethod
    def _evidence(request: ToolRequest, result: ToolResult) -> ToolEvidence:
        return ToolEvidence(
            evidence_type="TOOL_EXECUTION",
            source="odys-tool-contract-v1",
            capability_id=str(request.capability_id),
            tool_name=str(request.tool_name),
            summary=("tool execution succeeded" if result.status is ToolResultStatus.SUCCESS else "tool execution failed"),
            artifact_refs=sorted(result.artifacts),
            metadata={"status": result.status.value},
        )

    async def invoke(
        self,
        request: ToolRequest,
        runtime_context: CapabilityRuntimeContext,
    ) -> ToolResult:
        decision = self.prepare(request, runtime_context)
        if not decision.valid:
            return _failure(
                decision.error_type or ToolErrorCode.INTERNAL_ERROR,
                decision.error_message or "tool request rejected",
                diagnostics=decision.diagnostics,
                decision=decision,
            )

        tool = self.tool_registry.resolve(str(request.tool_name))
        effective_request = request.model_copy(
            update={"timeout_seconds": decision.effective_timeout_seconds}
        )
        try:
            result = await tool.execute(effective_request)
        except Exception as exc:  # Do not leak exception text into durable metadata.
            return _failure(
                ToolErrorCode.EXECUTION_FAILED,
                "tool execution failed",
                diagnostics={"exception_type": type(exc).__name__},
                decision=decision,
            )

        if result.status is ToolResultStatus.SUCCESS:
            definition = self.capability_registry.get(str(request.capability_id))
            try:
                validator = Draft202012Validator(definition.output_schema)
                validator.check_schema(definition.output_schema)
                validator.validate(result.output)
            except SchemaError:
                return ToolResult(
                    status=ToolResultStatus.FAILURE,
                    error_type=ToolErrorCode.INTERNAL_ERROR.value,
                    error_message="capability output schema is invalid",
                    artifacts=result.artifacts,
                    usage=result.usage,
                    metadata={**result.metadata, "contract": {"reason": "INVALID_OUTPUT_SCHEMA"}},
                    evidence=self._evidence(request, result),
                )
            except ValidationError as error:
                return ToolResult(
                    status=ToolResultStatus.FAILURE,
                    error_type=ToolErrorCode.OUTPUT_VALIDATION_FAILED.value,
                    error_message="tool output failed capability output schema",
                    artifacts=result.artifacts,
                    usage=result.usage,
                    metadata={
                        **result.metadata,
                        "contract": {"output_diagnostics": _schema_diagnostic(error)},
                    },
                    evidence=self._evidence(request, result),
                )

        if result.evidence is None:
            return result.model_copy(update={"evidence": self._evidence(request, result)})
        return result


# The longer name is useful at public call sites while retaining one class.
ToolInvocationContract = ToolContract


__all__ = [
    "ToolContract",
    "ToolContractDecision",
    "ToolErrorCode",
    "ToolInvocationContract",
]
