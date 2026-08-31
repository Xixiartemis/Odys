"""Named capability bundles expanded into the canonical ToolRegistry."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from lhas.tools.registry import ToolRegistry


class Toolset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    capabilities: set[str] = Field(default_factory=set)
    includes: set[str] = Field(default_factory=set)


class ToolsetRegistry:
    def __init__(self, tool_registry: ToolRegistry, toolsets: list[Toolset] | None = None):
        self.tool_registry = tool_registry
        self._toolsets: dict[str, Toolset] = {}
        for toolset in toolsets or default_toolsets():
            self.register(toolset)

    def register(self, toolset: Toolset) -> None:
        if toolset.name in self._toolsets:
            raise ValueError(f"toolset already registered: {toolset.name}")
        self._toolsets[toolset.name] = toolset

    def list(self) -> list[Toolset]:
        return [self._toolsets[name] for name in sorted(self._toolsets)]

    def extend(self, name: str, capabilities: set[str]) -> None:
        current = self._toolsets[name]
        self._toolsets[name] = current.model_copy(update={"capabilities": current.capabilities | capabilities})

    def resolve(self, names: set[str]) -> set[str]:
        resolved: set[str] = set()
        visiting: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                raise ValueError("toolset includes must be acyclic")
            if name not in self._toolsets:
                raise KeyError(f"unknown toolset: {name}")
            visiting.add(name)
            item = self._toolsets[name]
            resolved.update(item.capabilities)
            for child in item.includes:
                visit(child)
            visiting.remove(name)

        for name in names:
            visit(name)
        available = set(self.tool_registry.list_capabilities())
        return resolved & available


def default_toolsets() -> list[Toolset]:
    return [
        Toolset(name="workspace", capabilities={"workspace.list", "workspace.read", "workspace.search", "workspace.edit", "workspace.edit_lines", "workspace.diff", "workspace.restore"}),
        Toolset(name="terminal", capabilities={"cli.exec"}),
        Toolset(name="skills", capabilities={"skills.list", "skills.view"}),
        Toolset(name="memory", capabilities={"memory.list", "memory.search", "memory.add"}),
        Toolset(name="knowledge", capabilities={"knowledge.search", "knowledge.open"}),
        Toolset(name="mcp", capabilities=set()),
    ]
