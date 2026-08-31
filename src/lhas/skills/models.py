from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SkillMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class SkillDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata: SkillMetadata
    content: str
    reference_path: str | None = None
