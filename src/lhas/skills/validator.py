"""Skill ↔ CapabilityRegistry validation.

Produces a declarative report of available/missing capabilities without
mutating the CapabilityRegistry or executing any tools.
"""

from __future__ import annotations

from lhas.capability_registry import (
    CapabilityAvailability,
    CapabilityRegistry,
    CapabilityRuntimeContext,
)

from .models import (
    CapabilityReportEntry,
    SkillCapabilityReport,
    SkillDocument,
)


def validate_skill_capabilities(
    skill: SkillDocument,
    capability_registry: CapabilityRegistry,
    runtime_context: CapabilityRuntimeContext,
) -> SkillCapabilityReport:
    """Return a read-only report of the Skill's capability requirements.

    Unknown required capabilities are reported explicitly — they are never
    silently dropped.  Unknown optional capabilities are non-fatal but
    visible in the report.
    """

    required_ids = set(skill.metadata.required_capabilities)
    optional_ids = set(skill.metadata.optional_capabilities)

    # Gather which capability ids the registry actually knows about.
    known_ids = {defn.id for defn in capability_registry.list_all()}

    # Discover availability in the given runtime context.
    availability_by_id: dict[str, CapabilityAvailability] = {}
    for record in capability_registry.discover(runtime_context):
        availability_by_id[record.id] = record.availability

    entries: list[CapabilityReportEntry] = []

    for cap_id in sorted(required_ids | optional_ids):
        is_required = cap_id in required_ids
        known = cap_id in known_ids
        if known:
            avail = availability_by_id.get(cap_id) is CapabilityAvailability.AVAILABLE
            reason = "" if avail else f"capability exists but is unavailable in current runtime"
        else:
            avail = False
            reason = "unknown capability — not in CapabilityRegistry"
        entries.append(
            CapabilityReportEntry(
                capability_id=cap_id,
                required=is_required,
                available=avail,
                known=known,
                reason=reason,
            )
        )

    return SkillCapabilityReport(skill_name=skill.metadata.name, entries=entries)
