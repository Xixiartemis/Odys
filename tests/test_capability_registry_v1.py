from __future__ import annotations

import json

import pytest

from lhas.capability_registry import (
    AvailabilityReason,
    CapabilityAvailability,
    CapabilityBindingError,
    CapabilityDefinition,
    CapabilityRegistry,
    CapabilityRuntimeContext,
    RuntimePlatform,
    default_capabilities,
    normalize_platform,
)
from lhas.planning.models import CapabilitySpec
from lhas.tools import FakeTool, ToolRegistry


CAPABILITY_IDS = {
    "workspace.read", "workspace.list", "workspace.edit", "workspace.diff",
    "test.run", "git.status", "git.diff", "environment.inspect",
}


def context(platform="windows", tools=None):
    return CapabilityRuntimeContext(platform=platform, available_tools=tools)


def test_v1_registers_exactly_eight_stable_capabilities():
    registry = CapabilityRegistry()
    assert len(registry.list_all()) == 8
    assert {item.id for item in registry.list_all()} == CAPABILITY_IDS
    assert [item.id for item in registry.list_all()] == sorted(CAPABILITY_IDS)


@pytest.mark.parametrize("definition", default_capabilities())
def test_each_definition_has_required_metadata(definition):
    assert definition.id == definition.name
    assert definition.description and definition.category and definition.version
    assert definition.input_schema and definition.output_schema
    assert definition.platforms == (
        RuntimePlatform.WINDOWS, RuntimePlatform.LINUX, RuntimePlatform.MACOS
    )
    assert definition.permissions and definition.risk_level
    assert definition.workspace_scope == "SOURCE_WORKSPACE"
    assert definition.timeout_seconds > 0
    assert definition.preferred_tool
    assert definition.source and definition.evidence_type


def test_get_returns_definition_and_unknown_is_rejected():
    registry = CapabilityRegistry()
    assert registry.get("workspace.read").preferred_tool == "workspace.read"
    with pytest.raises(KeyError, match="unknown capability"):
        registry.get("workspace.execute")


@pytest.mark.parametrize("platform", ["windows", "linux", "macos"])
def test_known_platforms_discover_all_eight(platform):
    assert {item.id for item in CapabilityRegistry().list_available(context(platform))} == CAPABILITY_IDS


@pytest.mark.parametrize("platform", ["unknown", "solaris", ""])
def test_unknown_platform_fails_closed(platform):
    registry = CapabilityRegistry()
    assert registry.list_available(context(platform)) == []
    assert {record.availability for record in registry.discover(context(platform))} == {CapabilityAvailability.UNKNOWN}
    assert all(record.reason.code == "UNKNOWN_PLATFORM" for record in registry.discover(context(platform)))


def test_platform_labels_are_normalized_without_defaulting_unknown():
    assert normalize_platform("win32") is RuntimePlatform.WINDOWS
    assert normalize_platform("darwin") is RuntimePlatform.MACOS
    assert normalize_platform("linux2") is RuntimePlatform.LINUX
    assert normalize_platform("other") is RuntimePlatform.UNKNOWN


def test_missing_backend_is_unavailable_with_structured_reason():
    registry = CapabilityRegistry()
    records = {record.id: record for record in registry.discover(context(tools={"workspace.read"}))}
    assert records["workspace.read"].availability is CapabilityAvailability.AVAILABLE
    missing = records["test.run"]
    assert missing.availability is CapabilityAvailability.UNAVAILABLE
    assert isinstance(missing.reason, AvailabilityReason)
    assert missing.reason.code == "MISSING_TOOL_BINDING"
    assert missing.reason.details["preferred_tool"] == "cli.exec"


def test_explicit_tool_registry_is_used_for_availability_and_binding():
    tools = ToolRegistry()
    read = FakeTool(CapabilitySpec(name="workspace.read"))
    cli = FakeTool(CapabilitySpec(name="cli.exec"))
    tools.register(read)
    tools.register(cli)
    registry = CapabilityRegistry(tools)
    assert {item.id for item in registry.list_available(context("linux"))} == {"workspace.read", "test.run", "git.status", "git.diff", "environment.inspect"}
    assert registry.resolve_tool("test.run") is cli
    assert registry.resolve_tool("workspace.read") is read


def test_missing_binding_cannot_be_resolved():
    tools = ToolRegistry()
    tools.register(FakeTool(CapabilitySpec(name="workspace.read")))
    with pytest.raises(CapabilityBindingError, match="no existing tool binding"):
        CapabilityRegistry(tools).resolve_tool("test.run")


def test_all_bindings_are_explicit_and_resolve_against_existing_tool_names():
    registry = CapabilityRegistry()
    assert {item.preferred_tool for item in registry.list_all()} == {"workspace.read", "workspace.list", "workspace.edit", "workspace.diff", "cli.exec"}
    assert registry.get("test.run").preferred_tool == "cli.exec"


def test_many_capabilities_may_share_cli_adapter_but_one_definition_cannot_duplicate_it():
    registry = CapabilityRegistry()
    assert sum(item.preferred_tool == "cli.exec" for item in registry.list_all()) == 4
    base = registry.get("test.run")
    duplicate = base.model_copy(update={"fallback_tools": ("cli.exec",)})
    with pytest.raises(ValueError, match="preferred tool also listed"):
        CapabilityRegistry(definitions=[duplicate])


def test_duplicate_capability_ids_are_rejected():
    definition = CapabilityRegistry().get("workspace.read")
    with pytest.raises(ValueError, match="duplicate capability id"):
        CapabilityRegistry(definitions=[definition, definition.model_copy()])


def test_discovery_is_deterministic_and_does_not_discover_executables():
    registry = CapabilityRegistry()
    first = [record.model_dump(mode="json") for record in registry.discover(context("windows"))]
    second = [record.model_dump(mode="json") for record in registry.discover(context("windows"))]
    assert first == second
    assert [item["capability"]["id"] for item in first] == sorted(CAPABILITY_IDS)


@pytest.mark.parametrize("capability_id", sorted(CAPABILITY_IDS))
def test_json_round_trip_preserves_definition(capability_id):
    definition = CapabilityRegistry().get(capability_id)
    restored = CapabilityDefinition.model_validate_json(definition.model_dump_json())
    assert restored == definition


def test_capability_registry_does_not_change_existing_tool_registry():
    tools = ToolRegistry()
    tools.register(FakeTool(CapabilitySpec(name="workspace.read")))
    before = tools.list_capabilities()
    CapabilityRegistry(tools).list_available(context("windows"))
    assert tools.list_capabilities() == before == ["workspace.read"]


def test_metrics_projection_is_deterministic_and_has_required_fields():
    metrics = CapabilityRegistry.new_metrics()
    metrics.record(valid=True, successful=True)
    metrics.record(valid=False, policy_rejected=True)
    assert metrics.model_dump() == {
        "tool_selection_attempts": 2,
        "invalid_selection_attempts": 1,
        "policy_rejection_count": 1,
        "successful_tool_selection": 1,
    }
    assert json.loads(metrics.model_dump_json()) == metrics.model_dump()


def test_command_not_allowed_policy_remains_outside_registry():
    definition = CapabilityRegistry().get("test.run")
    assert definition.preferred_tool == "cli.exec"
    assert "COMMAND_NOT_ALLOWED" not in definition.description


def test_definitions_do_not_claim_runtime_execution_evidence():
    assert all(item.evidence_type == "DETERMINISTIC_TOOL_RESULT" for item in CapabilityRegistry().list_all())
