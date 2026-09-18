"""Benchmark tool registry factory.

Creates a ToolRegistry pre-loaded with concrete implementations for
all 7 benchmark protocol capabilities plus the observer tool.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lhas.tools.registry import ToolRegistry

from .filesystem import (
    DiffTool,
    EditLinesTool,
    ListFilesTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
)
from .observer import InspectStateTool
from .testing import RunTestTool


def create_benchmark_tool_registry(
    workspace_root: Path,
    *,
    effect_policy: Any | None = None,
) -> ToolRegistry:
    """Create and register all benchmark tools.

    Returns a ToolRegistry with 8 tools:
      workspace.list, workspace.read, workspace.search,
      workspace.edit, workspace.edit_lines, workspace.diff,
      cli.exec, observer.inspect_state

    Each tool is a proper Tool instance (has ``capability`` property
    returning CapabilitySpec and an async ``execute`` method).
    """
    registry = ToolRegistry()

    registry.register(ListFilesTool(workspace_root))
    registry.register(ReadFileTool(workspace_root))
    registry.register(SearchFilesTool(workspace_root))
    registry.register(WriteFileTool(workspace_root))
    registry.register(EditLinesTool(workspace_root))
    registry.register(DiffTool(workspace_root))
    registry.register(RunTestTool(workspace_root))
    registry.register(InspectStateTool(workspace_root))

    if effect_policy is not None:
        from evals.reliability.effect_policy import apply_phase_effect_policy

        apply_phase_effect_policy(registry, effect_policy)

    return registry
