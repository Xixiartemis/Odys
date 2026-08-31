"""Provider metadata registry. Credentials are deliberately out of model."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProviderProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_name: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_mode: str = "chat_completions"
    base_url: str | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)


class ProviderRegistry:
    def __init__(self):
        self._profiles: dict[str, ProviderProfile] = {}

    def register(self, name: str, profile: ProviderProfile) -> None:
        if name in self._profiles:
            raise ValueError(f"provider profile already registered: {name}")
        self._profiles[name] = profile

    def resolve(self, name: str) -> ProviderProfile:
        try:
            return self._profiles[name]
        except KeyError as exc:
            raise KeyError(f"unknown provider profile: {name}") from exc

    def list(self) -> list[tuple[str, ProviderProfile]]:
        return [(name, self._profiles[name]) for name in sorted(self._profiles)]
