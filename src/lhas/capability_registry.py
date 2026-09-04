"""Capability Registry V1 for the Odys capability runtime.

The registry describes what the runtime may offer.  It does not execute a
tool, alter the command policy, or replace :class:`ToolRegistry`, which
continues to own concrete Tool instances and execution.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CapabilityAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class RuntimePlatform(str, Enum):
    WINDOWS = "windows"
    LINUX = "linux"
    MACOS = "macos"
    UNKNOWN = "unknown"


def normalize_platform(value: str | RuntimePlatform | None) -> RuntimePlatform:
    """Normalize platform labels without treating an unknown value as safe."""

    if isinstance(value, RuntimePlatform):
        return value
    normalized = str(value or "").strip().casefold()
    if normalized in {"windows", "win", "win32", "cygwin", "msys"}:
        return RuntimePlatform.WINDOWS
    if normalized in {"linux", "linux2"}:
        return RuntimePlatform.LINUX
    if normalized in {"macos", "mac", "darwin", "osx"}:
        return RuntimePlatform.MACOS
    return RuntimePlatform.UNKNOWN


class CapabilityRuntimeContext(BaseModel):
    """Facts supplied by the current runtime during capability discovery.

    ``available_tools=None`` means that the registry's explicit binding
    catalog is being used.  A concrete runtime should pass the names exposed
    by its existing ``ToolRegistry`` so missing backends become unavailable.
    """

    model_config = ConfigDict(extra="forbid")

    platform: str | RuntimePlatform
    available_tools: set[str] | None = None

    @property
    def normalized_platform(self) -> RuntimePlatform:
        return normalize_platform(self.platform)


class AvailabilityReason(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class CapabilityDefinition(BaseModel):
    """Provider-neutral, serializable declaration of one runtime capability."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    category: str = Field(min_length=1)
    version: str = Field(min_length=1)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    platforms: tuple[RuntimePlatform, ...]
    permissions: tuple[str, ...]
    risk_level: str = Field(min_length=1)
    workspace_scope: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    retryable: bool
    preferred_tool: str = Field(min_length=1)
    fallback_tools: tuple[str, ...] = ()
    availability: CapabilityAvailability = CapabilityAvailability.AVAILABLE
    source: str = Field(min_length=1)
    evidence_type: str = Field(min_length=1)

    @field_validator("platforms")
    @classmethod
    def known_platforms_only(cls, value: tuple[RuntimePlatform, ...]) -> tuple[RuntimePlatform, ...]:
        if not value:
            raise ValueError("capability must declare at least one platform")
        if len(value) != len(set(value)):
            raise ValueError("capability platforms must be unique")
        return value

    @field_validator("fallback_tools")
    @classmethod
    def distinct_tool_bindings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("fallback tool bindings must be unique")
        return value


class CapabilityAvailabilityRecord(BaseModel):
    """Context-specific discovery result, including a structured reason."""

    model_config = ConfigDict(extra="forbid")

    capability: CapabilityDefinition
    availability: CapabilityAvailability
    reason: AvailabilityReason | None = None

    @property
    def id(self) -> str:
        return self.capability.id


class CapabilitySelectionMetrics(BaseModel):
    """Small deterministic projection helper; it is not a persistence authority."""

    model_config = ConfigDict(extra="forbid")

    tool_selection_attempts: int = Field(default=0, ge=0)
    invalid_selection_attempts: int = Field(default=0, ge=0)
    policy_rejection_count: int = Field(default=0, ge=0)
    successful_tool_selection: int = Field(default=0, ge=0)

    def record(
        self,
        *,
        valid: bool,
        policy_rejected: bool = False,
        successful: bool = False,
    ) -> "CapabilitySelectionMetrics":
        self.tool_selection_attempts += 1
        if not valid:
            self.invalid_selection_attempts += 1
        if policy_rejected:
            self.policy_rejection_count += 1
        if successful:
            self.successful_tool_selection += 1
        return self


def _object_schema(properties: Mapping[str, Any], required: Iterable[str] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": dict(properties),
    }
    required = list(required)
    if required:
        schema["required"] = required
    return schema


_PATH = {"type": "string"}
_ARGV = {"type": "array", "items": {"type": "string"}, "minItems": 1}


def _capability(
    capability_id: str,
    description: str,
    category: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
    *,
    permissions: tuple[str, ...],
    risk_level: str = "LOW",
    timeout_seconds: float = 30.0,
    retryable: bool = True,
    preferred_tool: str,
) -> CapabilityDefinition:
    return CapabilityDefinition(
        id=capability_id,
        name=capability_id,
        description=description,
        category=category,
        version="v1",
        input_schema=input_schema,
        output_schema=output_schema,
        platforms=(RuntimePlatform.WINDOWS, RuntimePlatform.LINUX, RuntimePlatform.MACOS),
        permissions=permissions,
        risk_level=risk_level,
        workspace_scope="SOURCE_WORKSPACE",
        timeout_seconds=timeout_seconds,
        retryable=retryable,
        preferred_tool=preferred_tool,
        fallback_tools=(),
        source="odys-runtime",
        evidence_type="DETERMINISTIC_TOOL_RESULT",
    )


def default_capabilities() -> tuple[CapabilityDefinition, ...]:
    """Return the explicit and stable V1 catalog in canonical order."""

    return (
        _capability(
            "workspace.read", "Read a text file in the source workspace", "workspace",
            _object_schema({"path": _PATH, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, ["path"]),
            _object_schema({"content": {"type": "string"}, "sha256": {"type": "string"}}),
            permissions=("workspace.read",), preferred_tool="workspace.read",
        ),
        _capability(
            "workspace.list", "List files in the source workspace", "workspace",
            _object_schema({"path": _PATH, "recursive": {"type": "boolean"}, "max_entries": {"type": "integer"}}),
            {"type": "array"}, permissions=("workspace.read",), preferred_tool="workspace.list",
        ),
        _capability(
            "workspace.edit", "Apply an explicit edit in the staged workspace", "workspace",
            _object_schema({"path": _PATH, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, ["path", "old_text", "new_text"]),
            _object_schema({"path": _PATH, "sha256": {"type": "string"}}),
            permissions=("workspace.write",), risk_level="MEDIUM", retryable=False, preferred_tool="workspace.edit",
        ),
        _capability(
            "workspace.diff", "Show staged workspace changes", "workspace",
            _object_schema({"path": _PATH, "max_diff_bytes": {"type": "integer"}}),
            {"type": "object"}, permissions=("workspace.read",), preferred_tool="workspace.diff",
        ),
        _capability(
            "test.run", "Run an explicitly permitted test command", "verification",
            _object_schema({"argv": _ARGV, "cwd": _PATH, "timeout_seconds": {"type": "number"}}, ["argv"]),
            {"type": "object"}, permissions=("process.execute",), timeout_seconds=120.0, preferred_tool="cli.exec",
        ),
        _capability(
            "git.status", "Inspect source workspace Git status", "source-control",
            _object_schema({"argv": _ARGV, "cwd": _PATH}), {"type": "object"},
            permissions=("process.execute",), preferred_tool="cli.exec",
        ),
        _capability(
            "git.diff", "Inspect source workspace Git diff", "source-control",
            _object_schema({"argv": _ARGV, "cwd": _PATH}), {"type": "object"},
            permissions=("process.execute",), preferred_tool="cli.exec",
        ),
        _capability(
            "environment.inspect", "Inspect bounded runtime environment facts", "environment",
            _object_schema({}), {"type": "object"},
            permissions=("environment.read",), preferred_tool="cli.exec",
        ),
    )


class CapabilityBindingError(LookupError):
    """Raised when a declared capability cannot resolve its existing tool."""


class CapabilityRegistry:
    """Explicit V1 capability catalog and deterministic availability view."""

    def __init__(self, tool_registry=None, definitions: Iterable[CapabilityDefinition] | None = None):
        self._tool_registry = tool_registry
        values = tuple(definitions if definitions is not None else default_capabilities())
        by_id: dict[str, CapabilityDefinition] = {}
        for definition in values:
            if definition.id in by_id:
                raise ValueError(f"duplicate capability id: {definition.id}")
            if definition.preferred_tool in definition.fallback_tools:
                raise ValueError(f"preferred tool also listed as fallback: {definition.id}")
            by_id[definition.id] = definition
        self._definitions = by_id

    def list_all(self) -> list[CapabilityDefinition]:
        return [self._definitions[key] for key in sorted(self._definitions)]

    def get(self, capability_id: str) -> CapabilityDefinition:
        try:
            return self._definitions[capability_id]
        except KeyError as exc:
            raise KeyError(f"unknown capability: {capability_id}") from exc

    def _available_tools(self, context: CapabilityRuntimeContext) -> set[str]:
        if context.available_tools is not None:
            return set(context.available_tools)
        if self._tool_registry is not None:
            return set(self._tool_registry.list_capabilities())
        return {definition.preferred_tool for definition in self._definitions.values()}

    def discover(self, context: CapabilityRuntimeContext) -> list[CapabilityAvailabilityRecord]:
        platform = context.normalized_platform
        tools = self._available_tools(context)
        records: list[CapabilityAvailabilityRecord] = []
        for definition in self.list_all():
            if platform is RuntimePlatform.UNKNOWN:
                records.append(CapabilityAvailabilityRecord(
                    capability=definition,
                    availability=CapabilityAvailability.UNKNOWN,
                    reason=AvailabilityReason(
                        code="UNKNOWN_PLATFORM",
                        message="capability availability is fail-closed for an unknown platform",
                        details={"platform": str(context.platform)},
                    ),
                ))
                continue
            if platform not in definition.platforms:
                records.append(CapabilityAvailabilityRecord(
                    capability=definition,
                    availability=CapabilityAvailability.UNAVAILABLE,
                    reason=AvailabilityReason(
                        code="PLATFORM_UNSUPPORTED",
                        message="capability is not declared for this platform",
                        details={"platform": platform.value},
                    ),
                ))
                continue
            if definition.preferred_tool not in tools and not any(tool in tools for tool in definition.fallback_tools):
                records.append(CapabilityAvailabilityRecord(
                    capability=definition,
                    availability=CapabilityAvailability.UNAVAILABLE,
                    reason=AvailabilityReason(
                        code="MISSING_TOOL_BINDING",
                        message="no declared existing tool backend is available",
                        details={
                            "preferred_tool": definition.preferred_tool,
                            "fallback_tools": list(definition.fallback_tools),
                        },
                    ),
                ))
                continue
            records.append(CapabilityAvailabilityRecord(
                capability=definition,
                availability=CapabilityAvailability.AVAILABLE,
            ))
        return records

    def list_available(self, context: CapabilityRuntimeContext) -> list[CapabilityDefinition]:
        """Return only capabilities safely available in ``context``."""

        return [record.capability for record in self.discover(context) if record.availability is CapabilityAvailability.AVAILABLE]

    def resolve_tool(self, capability_id: str, tool_registry=None):
        definition = self.get(capability_id)
        registry = tool_registry or self._tool_registry
        if registry is None:
            raise CapabilityBindingError(f"tool registry unavailable for capability: {capability_id}")
        for tool_name in (definition.preferred_tool, *definition.fallback_tools):
            try:
                return registry.resolve(tool_name)
            except KeyError:
                continue
        raise CapabilityBindingError(
            f"no existing tool binding available for capability: {capability_id}"
        )

    @staticmethod
    def new_metrics() -> CapabilitySelectionMetrics:
        return CapabilitySelectionMetrics()


# Friendly aliases keep the runtime vocabulary concise without introducing a
# second model or registry implementation.
RuntimeContext = CapabilityRuntimeContext
Capability = CapabilityDefinition


__all__ = [
    "AvailabilityReason",
    "Capability",
    "CapabilityAvailability",
    "CapabilityAvailabilityRecord",
    "CapabilityBindingError",
    "CapabilityDefinition",
    "CapabilityRegistry",
    "CapabilityRuntimeContext",
    "CapabilitySelectionMetrics",
    "RuntimeContext",
    "RuntimePlatform",
    "default_capabilities",
    "normalize_platform",
]
