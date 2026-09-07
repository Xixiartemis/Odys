from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AcceptanceContract(BaseModel):
    """Declarative expected evidence/outcome for a Skill.

    This is purely descriptive.  It MUST NOT: execute Validators, mark Task
    complete, call CompletionAuthority, or change Task lifecycle.  It CAN
    describe expected evidence/outcome that a downstream authority may verify.
    """

    model_config = ConfigDict(extra="forbid")

    description: str = ""
    expected_evidence: list[str] = Field(default_factory=list)
    outcome_criteria: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SkillMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    required_capabilities: list[str] = Field(default_factory=list)
    optional_capabilities: list[str] = Field(default_factory=list)
    acceptance_contract: AcceptanceContract | None = None
    workflow_template: str | None = None


class SkillDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata: SkillMetadata
    content: str
    reference_path: str | None = None


class CapabilityReportEntry(BaseModel):
    """One row in a Skill capability validation report."""

    model_config = ConfigDict(extra="forbid")

    capability_id: str
    required: bool
    available: bool
    known: bool  # True if the capability exists in the CapabilityRegistry at all
    reason: str = ""


class SkillCapabilityReport(BaseModel):
    """Result of validating a Skill's declared capabilities against a
    CapabilityRegistry.

    This report is read-only.  It does NOT mutate the CapabilityRegistry,
    does NOT execute tools, and does NOT change task lifecycle.
    """

    model_config = ConfigDict(extra="forbid")

    skill_name: str
    entries: list[CapabilityReportEntry] = Field(default_factory=list)

    @property
    def all_required_available(self) -> bool:
        return all(
            entry.available
            for entry in self.entries
            if entry.required
        )

    @property
    def missing_required(self) -> list[str]:
        return [
            entry.capability_id
            for entry in self.entries
            if entry.required and not entry.available
        ]

    @property
    def unknown_required(self) -> list[str]:
        return [
            entry.capability_id
            for entry in self.entries
            if entry.required and not entry.known
        ]

    @property
    def unknown_optional(self) -> list[str]:
        return [
            entry.capability_id
            for entry in self.entries
            if not entry.required and not entry.known
        ]
