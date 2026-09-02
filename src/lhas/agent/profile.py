"""Role profiles and least-privilege delegation policy."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lhas.agent.models import AgentRole


class AgentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    role: AgentRole
    provider: str = "offline"
    model: str = "scripted"
    toolsets: set[str] = Field(default_factory=set)
    skills: list[str] = Field(default_factory=list)
    memory_permissions: set[str] = Field(default_factory=lambda: {"read"})
    knowledge_permissions: set[str] = Field(default_factory=lambda: {"read"})
    delegation_permissions: set[AgentRole] = Field(default_factory=set)
    max_turns: int = Field(default=20, ge=1, le=200)
    max_children: int = Field(default=3, ge=0, le=20)
    max_spawn_depth: int = Field(default=1, ge=0, le=8)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentProfileRegistry:
    def __init__(self, profiles: list[AgentProfile] | None = None):
        self._profiles: dict[str, AgentProfile] = {}
        for profile in profiles or default_profiles():
            self.register(profile)

    def register(self, profile: AgentProfile) -> None:
        if profile.name in self._profiles:
            raise ValueError(f"agent profile already registered: {profile.name}")
        self._profiles[profile.name] = profile

    def get(self, name: str) -> AgentProfile:
        try:
            return self._profiles[name]
        except KeyError as exc:
            raise KeyError(f"unknown agent profile: {name}") from exc

    def for_role(self, role: AgentRole) -> AgentProfile:
        for profile in self.list():
            if profile.role is role:
                return profile
        raise KeyError(f"no profile for role: {role.value}")

    def list(self) -> list[AgentProfile]:
        return [self._profiles[name] for name in sorted(self._profiles)]

    @staticmethod
    def validate_child(parent: AgentProfile, child: AgentProfile, *, spawn_depth: int) -> None:
        if child.role not in parent.delegation_permissions:
            raise PermissionError("CHILD_ROLE_NOT_ALLOWED")
        if spawn_depth > parent.max_spawn_depth:
            raise PermissionError("MAX_SPAWN_DEPTH_EXCEEDED")
        if not child.toolsets.issubset(parent.toolsets):
            raise PermissionError("CHILD_TOOLSETS_EXCEED_PARENT")
        if "write" in child.memory_permissions and "write" not in parent.memory_permissions:
            raise PermissionError("CHILD_MEMORY_EXCEEDS_PARENT")


def default_profiles() -> list[AgentProfile]:
    support = {"workspace", "terminal", "skills", "memory", "knowledge", "mcp"}
    return [
        AgentProfile(
            name="root",
            role=AgentRole.ROOT,
            toolsets=support,
            memory_permissions={"read", "write"},
            delegation_permissions={AgentRole.PLANNER, AgentRole.WORKER, AgentRole.RESEARCHER, AgentRole.REVIEWER},
        ),
        AgentProfile(
            name="planner",
            role=AgentRole.PLANNER,
            toolsets={"skills", "memory", "knowledge"},
            delegation_permissions={AgentRole.WORKER, AgentRole.RESEARCHER, AgentRole.REVIEWER},
        ),
        AgentProfile(
            name="worker",
            role=AgentRole.WORKER,
            toolsets=support,
            delegation_permissions={AgentRole.RESEARCHER, AgentRole.REVIEWER},
        ),
        AgentProfile(
            name="researcher",
            role=AgentRole.RESEARCHER,
            toolsets={"skills", "memory", "knowledge", "mcp"},
            memory_permissions={"read"},
            delegation_permissions=set(),
            max_children=0,
            max_spawn_depth=0,
        ),
        AgentProfile(
            name="reviewer",
            role=AgentRole.REVIEWER,
            toolsets={"workspace", "terminal", "skills", "knowledge"},
            memory_permissions={"read"},
            delegation_permissions=set(),
            max_children=0,
            max_spawn_depth=0,
        ),
    ]
