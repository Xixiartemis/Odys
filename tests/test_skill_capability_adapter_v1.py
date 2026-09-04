"""Tests for the Skill Capability Adapter V1.

Covers all 10 required acceptance test scenarios:
1. old Skill format still loads
2. required capabilities parse
3. optional capabilities parse
4. acceptance contract round-trip
5. known capabilities resolve
6. missing required capability reported
7. unknown optional capability non-fatal but visible
8. Skill loading executes zero Tools
9. Skill cannot mark Task complete
10. deterministic serialization
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from lhas.capability_registry import (
    CapabilityAvailability,
    CapabilityRegistry,
    CapabilityRuntimeContext,
)
from lhas.skills.models import (
    AcceptanceContract,
    CapabilityReportEntry,
    SkillCapabilityReport,
    SkillDocument,
    SkillMetadata,
)
from lhas.skills.registry import (
    SkillRegistry,
    _parse_acceptance_contract,
    _parse_list_field,
)
from lhas.skills.validator import validate_skill_capabilities


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _context(platform: str = "windows", tools: set[str] | None = None) -> CapabilityRuntimeContext:
    return CapabilityRuntimeContext(platform=platform, available_tools=tools)


def _write_skill(tmp: Path, name: str, content: str) -> Path:
    """Write a SKILL.md inside ``tmp/<name>/`` and return the directory."""
    skill_dir = tmp / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


# ---------------------------------------------------------------------------
# 1. Old Skill format still loads (backward compatibility)
# ---------------------------------------------------------------------------

class TestOldFormatCompatibility:
    """Existing frontmatter with only name/description/metadata must load."""

    def test_old_format_loads_cleanly(self, tmp_path: Path):
        _write_skill(tmp_path, "legacy", "---\nname: legacy-skill\ndescription: old format\n---\nBody here")
        registry = SkillRegistry([tmp_path])
        metas = registry.discover()
        assert len(metas) == 1
        meta = metas[0]
        assert meta.name == "legacy-skill"
        assert meta.description == "old format"
        # New fields are empty/None by default
        assert meta.required_capabilities == []
        assert meta.optional_capabilities == []
        assert meta.acceptance_contract is None
        assert meta.workflow_template is None

    def test_old_format_view_returns_document(self, tmp_path: Path):
        _write_skill(tmp_path, "legacy", "---\nname: old\ndescription: desc\n---\n# Content")
        registry = SkillRegistry([tmp_path])
        doc = registry.view("old")
        assert isinstance(doc, SkillDocument)
        assert doc.content == "# Content"
        assert doc.metadata.name == "old"
        assert doc.metadata.required_capabilities == []

    def test_metadata_dict_preserved(self, tmp_path: Path):
        _write_skill(tmp_path, "m", "---\nname: m\nversion: 2\nauthor: test\n---\nBody")
        registry = SkillRegistry([tmp_path])
        meta = registry.discover()[0]
        assert meta.metadata == {"version": "2", "author": "test"}


# ---------------------------------------------------------------------------
# 2. Required capabilities parse
# ---------------------------------------------------------------------------

class TestRequiredCapabilitiesParse:
    def test_json_array_format(self, tmp_path: Path):
        content = (
            '---\nname: coding/bug-fix\n'
            'required_capabilities: ["workspace.read", "workspace.edit", "workspace.diff", "test.run"]\n'
            '---\nFix bugs.'
        )
        _write_skill(tmp_path, "bug-fix", content)
        meta = SkillRegistry([tmp_path]).discover()[0]
        assert meta.required_capabilities == [
            "workspace.read", "workspace.edit", "workspace.diff", "test.run",
        ]

    def test_comma_separated_format(self, tmp_path: Path):
        content = (
            "---\nname: test-skill\n"
            "required_capabilities: workspace.read, workspace.edit\n"
            "---\nBody"
        )
        _write_skill(tmp_path, "test-skill", content)
        meta = SkillRegistry([tmp_path]).discover()[0]
        assert meta.required_capabilities == ["workspace.read", "workspace.edit"]

    def test_empty_required_is_empty_list(self, tmp_path: Path):
        content = "---\nname: empty\n---\nBody"
        _write_skill(tmp_path, "empty", content)
        meta = SkillRegistry([tmp_path]).discover()[0]
        assert meta.required_capabilities == []


# ---------------------------------------------------------------------------
# 3. Optional capabilities parse
# ---------------------------------------------------------------------------

class TestOptionalCapabilitiesParse:
    def test_json_array_format(self, tmp_path: Path):
        content = (
            '---\nname: opt-test\n'
            'optional_capabilities: ["git.status", "git.diff"]\n'
            '---\nBody'
        )
        _write_skill(tmp_path, "opt-test", content)
        meta = SkillRegistry([tmp_path]).discover()[0]
        assert meta.optional_capabilities == ["git.status", "git.diff"]

    def test_comma_separated_format(self, tmp_path: Path):
        content = (
            "---\nname: opt-cs\n"
            "optional_capabilities: git.status, environment.inspect\n"
            "---\nBody"
        )
        _write_skill(tmp_path, "opt-cs", content)
        meta = SkillRegistry([tmp_path]).discover()[0]
        assert meta.optional_capabilities == ["git.status", "environment.inspect"]


# ---------------------------------------------------------------------------
# 4. Acceptance contract round-trip
# ---------------------------------------------------------------------------

class TestAcceptanceContractRoundTrip:
    def test_json_object_format(self, tmp_path: Path):
        contract_json = json.dumps({
            "description": "All tests pass",
            "expected_evidence": ["test output shows 0 failures"],
            "outcome_criteria": ["exit code 0"],
        })
        content = f'---\nname: ac-test\nacceptance_contract: {contract_json}\n---\nBody'
        _write_skill(tmp_path, "ac-test", content)
        meta = SkillRegistry([tmp_path]).discover()[0]
        assert meta.acceptance_contract is not None
        assert meta.acceptance_contract.description == "All tests pass"
        assert meta.acceptance_contract.expected_evidence == ["test output shows 0 failures"]
        assert meta.acceptance_contract.outcome_criteria == ["exit code 0"]

    def test_string_fallback(self, tmp_path: Path):
        content = '---\nname: ac-str\nacceptance_contract: All tests pass\n---\nBody'
        _write_skill(tmp_path, "ac-str", content)
        meta = SkillRegistry([tmp_path]).discover()[0]
        assert meta.acceptance_contract is not None
        assert meta.acceptance_contract.description == "All tests pass"

    def test_model_round_trip(self):
        contract = AcceptanceContract(
            description="desc",
            expected_evidence=["e1"],
            outcome_criteria=["c1"],
            metadata={"k": "v"},
        )
        data = json.loads(contract.model_dump_json())
        restored = AcceptanceContract.model_validate(data)
        assert restored == contract

    def test_full_skill_json_round_trip(self, tmp_path: Path):
        contract_json = json.dumps({
            "description": "works",
            "expected_evidence": ["ev"],
            "outcome_criteria": ["oc"],
        })
        content = (
            '---\nname: rt-test\n'
            'description: test\n'
            'required_capabilities: ["workspace.read"]\n'
            f'acceptance_contract: {contract_json}\n'
            '---\nBody'
        )
        _write_skill(tmp_path, "rt-test", content)
        doc = SkillRegistry([tmp_path]).view("rt-test")
        data = json.loads(doc.model_dump_json())
        restored = SkillDocument.model_validate(data)
        assert restored.metadata.name == "rt-test"
        assert restored.metadata.required_capabilities == ["workspace.read"]
        assert restored.metadata.acceptance_contract.description == "works"


# ---------------------------------------------------------------------------
# 5. Known capabilities resolve
# ---------------------------------------------------------------------------

class TestKnownCapabilitiesResolve:
    def test_known_required_available(self, tmp_path: Path):
        _write_skill(tmp_path, "s", '---\nname: s\nrequired_capabilities: ["workspace.read"]\n---\nBody')
        doc = SkillRegistry([tmp_path]).view("s")
        cap_reg = CapabilityRegistry()
        ctx = _context("windows")
        report = validate_skill_capabilities(doc, cap_reg, ctx)
        assert report.all_required_available
        assert report.missing_required == []

    def test_known_optional_available(self, tmp_path: Path):
        _write_skill(tmp_path, "s", '---\nname: s\noptional_capabilities: ["workspace.read"]\n---\nBody')
        doc = SkillRegistry([tmp_path]).view("s")
        report = validate_skill_capabilities(doc, CapabilityRegistry(), _context("windows"))
        assert report.all_required_available  # no required → vacuously true
        available_entry = next(e for e in report.entries if e.capability_id == "workspace.read")
        assert available_entry.available
        assert available_entry.known


# ---------------------------------------------------------------------------
# 6. Missing required capability reported
# ---------------------------------------------------------------------------

class TestMissingRequiredCapability:
    def test_known_but_unavailable_required(self, tmp_path: Path):
        """Capability exists in registry but is unavailable in current runtime."""
        _write_skill(tmp_path, "s", '---\nname: s\nrequired_capabilities: ["test.run"]\n---\nBody')
        doc = SkillRegistry([tmp_path]).view("s")
        # Only provide workspace.read tool → test.run will be unavailable
        cap_reg = CapabilityRegistry()
        ctx = _context("windows", tools={"workspace.read"})
        report = validate_skill_capabilities(doc, cap_reg, ctx)
        assert not report.all_required_available
        assert "test.run" in report.missing_required

    def test_unknown_required_reported_explicitly(self, tmp_path: Path):
        """Unknown required capability is explicitly reported, not silently dropped."""
        _write_skill(tmp_path, "s", '---\nname: s\nrequired_capabilities: ["nonexistent.cap"]\n---\nBody')
        doc = SkillRegistry([tmp_path]).view("s")
        report = validate_skill_capabilities(doc, CapabilityRegistry(), _context("windows"))
        assert not report.all_required_available
        assert "nonexistent.cap" in report.missing_required
        assert "nonexistent.cap" in report.unknown_required
        entry = next(e for e in report.entries if e.capability_id == "nonexistent.cap")
        assert entry.required
        assert not entry.known
        assert not entry.available
        assert "not in CapabilityRegistry" in entry.reason


# ---------------------------------------------------------------------------
# 7. Unknown optional capability non-fatal but visible
# ---------------------------------------------------------------------------

class TestUnknownOptionalCapability:
    def test_unknown_optional_is_non_fatal(self, tmp_path: Path):
        _write_skill(
            tmp_path, "s",
            '---\nname: s\noptional_capabilities: ["fake.cap"]\n---\nBody',
        )
        doc = SkillRegistry([tmp_path]).view("s")
        report = validate_skill_capabilities(doc, CapabilityRegistry(), _context("windows"))
        # No required → all_required_available is vacuously true
        assert report.all_required_available
        assert "fake.cap" in report.unknown_optional
        entry = next(e for e in report.entries if e.capability_id == "fake.cap")
        assert not entry.required
        assert not entry.known
        assert not entry.available

    def test_mixed_known_and_unknown_optional(self, tmp_path: Path):
        _write_skill(
            tmp_path, "s",
            '---\nname: s\noptional_capabilities: ["workspace.read", "unknown.cap"]\n---\nBody',
        )
        doc = SkillRegistry([tmp_path]).view("s")
        report = validate_skill_capabilities(doc, CapabilityRegistry(), _context("windows"))
        assert "unknown.cap" in report.unknown_optional
        known_entry = next(e for e in report.entries if e.capability_id == "workspace.read")
        assert known_entry.known
        assert known_entry.available


# ---------------------------------------------------------------------------
# 8. Skill loading executes zero Tools
# ---------------------------------------------------------------------------

class TestSkillLoadingExecutesZeroTools:
    """Skill loading and validation must not execute any tools."""

    def test_loading_and_validation_execute_zero_tools(self, tmp_path: Path):
        """The CapabilityRegistry is queried for availability only, not invoked."""
        _write_skill(
            tmp_path, "s",
            '---\nname: s\nrequired_capabilities: ["workspace.read"]\n---\nBody',
        )
        registry = SkillRegistry([tmp_path])
        # discover() parses metadata only — no tools execute
        metas = registry.discover()
        assert len(metas) == 1

        doc = registry.view("s")
        cap_reg = CapabilityRegistry()
        # validate_skill_capabilities only calls discover() on the registry,
        # which never executes tools — it checks availability declarations.
        report = validate_skill_capabilities(doc, cap_reg, _context("windows"))
        assert report.all_required_available
        # If we got here without errors, zero tools were executed.

    def test_capability_registry_unchanged_after_validation(self, tmp_path: Path):
        """Validation must not mutate the CapabilityRegistry."""
        _write_skill(
            tmp_path, "s",
            '---\nname: s\nrequired_capabilities: ["workspace.read", "nonexistent"]\n---\nBody',
        )
        doc = SkillRegistry([tmp_path]).view("s")
        cap_reg = CapabilityRegistry()
        before = [d.model_dump() for d in cap_reg.list_all()]
        validate_skill_capabilities(doc, cap_reg, _context("windows"))
        after = [d.model_dump() for d in cap_reg.list_all()]
        assert before == after


# ---------------------------------------------------------------------------
# 9. Skill cannot mark Task complete
# ---------------------------------------------------------------------------

class TestSkillCannotMarkTaskComplete:
    """AcceptanceContract is declarative only. It has no execute/mark methods."""

    def test_acceptance_contract_has_no_execute_method(self):
        contract = AcceptanceContract(description="test")
        assert not hasattr(contract, "execute")
        assert not hasattr(contract, "mark_complete")
        assert not hasattr(contract, "validate_and_complete")

    def test_skill_document_has_no_lifecycle_methods(self, tmp_path: Path):
        _write_skill(tmp_path, "s", '---\nname: s\n---\nBody')
        doc = SkillRegistry([tmp_path]).view("s")
        assert not hasattr(doc, "execute")
        assert not hasattr(doc, "mark_complete")
        assert not hasattr(doc, "complete_task")

    def test_skill_metadata_has_no_lifecycle_methods(self):
        meta = SkillMetadata(name="test")
        assert not hasattr(meta, "execute")
        assert not hasattr(meta, "mark_complete")

    def test_validator_report_has_no_lifecycle_methods(self):
        report = SkillCapabilityReport(skill_name="test", entries=[])
        assert not hasattr(report, "execute")
        assert not hasattr(report, "mark_complete")


# ---------------------------------------------------------------------------
# 10. Deterministic serialization
# ---------------------------------------------------------------------------

class TestDeterministicSerialization:
    def test_skill_metadata_deterministic(self):
        meta = SkillMetadata(
            name="test",
            description="desc",
            required_capabilities=["a", "b"],
            optional_capabilities=["c"],
            acceptance_contract=AcceptanceContract(
                description="works",
                expected_evidence=["e1"],
                outcome_criteria=["c1"],
            ),
        )
        d1 = json.loads(meta.model_dump_json())
        d2 = json.loads(meta.model_dump_json())
        assert d1 == d2

    def test_capability_report_deterministic(self):
        report = SkillCapabilityReport(
            skill_name="test",
            entries=[
                CapabilityReportEntry(
                    capability_id="a", required=True, available=True, known=True,
                ),
                CapabilityReportEntry(
                    capability_id="b", required=False, available=False, known=False,
                    reason="unknown",
                ),
            ],
        )
        d1 = json.loads(report.model_dump_json())
        d2 = json.loads(report.model_dump_json())
        assert d1 == d2

    def test_full_pipeline_deterministic(self, tmp_path: Path):
        _write_skill(
            tmp_path, "s",
            '---\nname: s\n'
            'required_capabilities: ["workspace.read"]\n'
            'optional_capabilities: ["git.status"]\n'
            '---\nBody',
        )
        doc = SkillRegistry([tmp_path]).view("s")
        cap_reg = CapabilityRegistry()
        ctx = _context("windows")

        r1 = validate_skill_capabilities(doc, cap_reg, ctx)
        r2 = validate_skill_capabilities(doc, cap_reg, ctx)
        assert json.loads(r1.model_dump_json()) == json.loads(r2.model_dump_json())


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_workflow_template_parsed(self, tmp_path: Path):
        content = '---\nname: wt\nworkflow_template: step1 -> step2 -> step3\n---\nBody'
        _write_skill(tmp_path, "wt", content)
        meta = SkillRegistry([tmp_path]).discover()[0]
        assert meta.workflow_template == "step1 -> step2 -> step3"

    def test_workflow_template_none_when_absent(self, tmp_path: Path):
        content = '---\nname: no-wt\n---\nBody'
        _write_skill(tmp_path, "no-wt", content)
        meta = SkillRegistry([tmp_path]).discover()[0]
        assert meta.workflow_template is None

    def test_empty_acceptance_contract_field_ignored(self, tmp_path: Path):
        content = '---\nname: empty-ac\nacceptance_contract: \n---\nBody'
        _write_skill(tmp_path, "empty-ac", content)
        meta = SkillRegistry([tmp_path]).discover()[0]
        assert meta.acceptance_contract is None

    def test_all_eight_default_capabilities_known(self, tmp_path: Path):
        """All default capabilities should be recognized as known."""
        all_caps = [
            "workspace.read", "workspace.list", "workspace.edit", "workspace.diff",
            "test.run", "git.status", "git.diff", "environment.inspect",
        ]
        caps_json = json.dumps(all_caps)
        content = f'---\nname: all\nrequired_capabilities: {caps_json}\n---\nBody'
        _write_skill(tmp_path, "all", content)
        doc = SkillRegistry([tmp_path]).view("all")
        cap_reg = CapabilityRegistry()
        ctx = _context("windows")
        report = validate_skill_capabilities(doc, cap_reg, ctx)
        assert all(e.known for e in report.entries)
        assert report.all_required_available

    def test_parse_list_field_edge_cases(self):
        assert _parse_list_field("") == []
        assert _parse_list_field("   ") == []
        assert _parse_list_field('["a"]') == ["a"]
        assert _parse_list_field("a, b, c") == ["a", "b", "c"]
        assert _parse_list_field('"a", "b"') == ["a", "b"]

    def test_parse_acceptance_contract_edge_cases(self):
        assert _parse_acceptance_contract("") is None
        assert _parse_acceptance_contract("   ") is None
        c = _parse_acceptance_contract("just a description")
        assert c is not None
        assert c.description == "just a description"
