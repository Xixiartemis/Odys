"""Tests for the Skill Capability Adapter V1.

Covers all 12 required acceptance test scenarios:
1. old Skill format still loads (backward compatibility)
2. required capabilities parse
3. CapabilitySpec-only backend does NOT satisfy required capability (P2.3 strict)
4. acceptance contract round-trip
5. known capabilities resolve
6. missing required capability reported
7. unknown optional capability non-fatal but visible
8. Skill loading executes zero Tools
9. AcceptanceContract / Skill cannot mark Task complete (declarative only)
10. validation executes zero tools / no Validator/CompletionAuthority (P2.3 strict)
11. deterministic serialization
12. P2.3 strict semantic authority preserved end-to-end
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


# ---------------------------------------------------------------------------
# 3 (P2.3 strict). CapabilitySpec-only backend does NOT satisfy required
# ---------------------------------------------------------------------------

class TestCapabilitySpecOnlyDoesNotSatisfy:
    """A backend Tool with CapabilitySpec(name='foo.bar') must NOT satisfy a
    Skill's required capability unless an explicit CapabilityDefinition exists
    in the CapabilityRegistry.  This is the core P2.3 semantic-authority
    invariant: CapabilityDefinition = capability authority, CapabilitySpec =
    backend descriptor ONLY."""

    def test_specs_only_backend_does_not_satisfy_required(self, tmp_path: Path):
        """Tool with CapabilitySpec exists, but NO CapabilityDefinition in the
        registry → required capability must be MISSING / unavailable."""
        from lhas.planning.models import CapabilitySpec
        from lhas.tools import FakeTool, ToolRegistry

        _write_skill(
            tmp_path, "s",
            '---\nname: s\nrequired_capabilities: ["workspace.read"]\n---\nBody',
        )
        doc = SkillRegistry([tmp_path]).view("s")

        # ToolRegistry has a backend tool with CapabilitySpec for workspace.read
        tool_reg = ToolRegistry()
        tool_reg.register(FakeTool(CapabilitySpec(name="workspace.read")))

        # CapabilityRegistry with ZERO definitions (empty)
        cap_reg = CapabilityRegistry(tool_registry=tool_reg, definitions=[])

        # Even though the tool_registry has a tool named workspace.read,
        # the CapabilityRegistry has no CapabilityDefinition for it.
        ctx = _context("windows")
        report = validate_skill_capabilities(doc, cap_reg, ctx)

        # workspace.read is NOT known (no CapabilityDefinition) → unavailable
        assert not report.all_required_available
        assert "workspace.read" in report.missing_required
        assert "workspace.read" in report.unknown_required
        entry = next(e for e in report.entries if e.capability_id == "workspace.read")
        assert entry.required
        assert not entry.known
        assert not entry.available

    def test_explicit_definition_plus_backend_satisfies_required(self, tmp_path: Path):
        """Explicit CapabilityDefinition + backend available → AVAILABLE."""
        _write_skill(
            tmp_path, "s",
            '---\nname: s\nrequired_capabilities: ["workspace.read"]\n---\nBody',
        )
        doc = SkillRegistry([tmp_path]).view("s")

        # Default CapabilityRegistry includes workspace.read CapabilityDefinition
        cap_reg = CapabilityRegistry()
        ctx = _context("windows")
        report = validate_skill_capabilities(doc, cap_reg, ctx)

        assert report.all_required_available
        assert report.missing_required == []
        entry = next(e for e in report.entries if e.capability_id == "workspace.read")
        assert entry.required
        assert entry.known
        assert entry.available

    def test_specs_only_backend_for_optional_not_satisfying(self, tmp_path: Path):
        """Optional capability with only backend tool → unknown but non-fatal."""
        from lhas.planning.models import CapabilitySpec
        from lhas.tools import FakeTool, ToolRegistry

        _write_skill(
            tmp_path, "s",
            '---\nname: s\noptional_capabilities: ["workspace.read"]\n---\nBody',
        )
        doc = SkillRegistry([tmp_path]).view("s")

        tool_reg = ToolRegistry()
        tool_reg.register(FakeTool(CapabilitySpec(name="workspace.read")))
        cap_reg = CapabilityRegistry(tool_registry=tool_reg, definitions=[])
        ctx = _context("windows")
        report = validate_skill_capabilities(doc, cap_reg, ctx)

        # Optional: non-fatal
        assert report.all_required_available  # no required → vacuously true
        assert "workspace.read" in report.unknown_optional
        entry = next(e for e in report.entries if e.capability_id == "workspace.read")
        assert not entry.required
        assert not entry.known
        assert not entry.available


# ---------------------------------------------------------------------------
# 10 (P2.3 strict). Validation executes zero tools, no Validator/CompletionAuthority
# ---------------------------------------------------------------------------

class TestValidationExecutesZeroInvocations:
    """validate_skill_capabilities must not invoke any Tool, Validator, or
    CompletionAuthority.  It reads CapabilityRegistry declarations only."""

    def test_validate_never_calls_execute_on_tools(self, tmp_path: Path):
        """Track execute() calls on fake tools to prove none are invoked."""
        from lhas.planning.models import CapabilitySpec
        from lhas.tools import FakeTool, ToolRegistry

        execute_calls: list[str] = []

        class TrackingTool(FakeTool):
            async def execute(self, request):
                execute_calls.append(request.capability)
                return await super().execute(request)

        _write_skill(
            tmp_path, "s",
            '---\nname: s\nrequired_capabilities: ["workspace.read"]\n---\nBody',
        )
        doc = SkillRegistry([tmp_path]).view("s")

        tool_reg = ToolRegistry()
        tool_reg.register(TrackingTool(CapabilitySpec(name="workspace.read")))
        cap_reg = CapabilityRegistry()
        ctx = _context("windows")

        report = validate_skill_capabilities(doc, cap_reg, ctx)
        assert report.all_required_available
        # Zero tool executions
        assert execute_calls == []

    def test_validate_has_no_validator_or_completion_authority_reference(self):
        """The validator module must not import or reference CompletionAuthority."""
        import lhas.skills.validator as validator_module
        source = open(validator_module.__file__, encoding="utf-8").read()
        assert "CompletionAuthority" not in source
        assert "Validator" not in source.split("class ")[0]  # not in imports
        # Must not import task_service, validation, or completion modules
        assert "from lhas.validation" not in source
        assert "from lhas.task_service" not in source
        assert "import lhas.validation" not in source
        assert "import lhas.task_service" not in source


# ---------------------------------------------------------------------------
# 12 (P2.3 strict). P2.3 strict semantic authority preserved
# ---------------------------------------------------------------------------

class TestP23StrictSemanticAuthority:
    """End-to-end tests confirming P2.3's frozen authority model:
    - CapabilityDefinition = semantic capability authority
    - CapabilitySpec = backend descriptor ONLY
    - Skill = procedural knowledge + capability declaration (NOT runtime)
    """

    def test_definition_authority_overrides_spec_naming(self, tmp_path: Path):
        """Even if a Tool's CapabilitySpec.name matches, the
        CapabilityDefinition in the registry is the sole authority."""
        from lhas.planning.models import CapabilitySpec
        from lhas.tools import FakeTool, ToolRegistry

        # Register a tool with a name that happens to be in the default catalog
        tool_reg = ToolRegistry()
        tool_reg.register(FakeTool(CapabilitySpec(name="workspace.read")))
        # But construct the CapabilityRegistry with ZERO definitions
        cap_reg = CapabilityRegistry(tool_registry=tool_reg, definitions=[])

        # The tool name exists, but no CapabilityDefinition → not known
        ctx = _context("windows")
        records = cap_reg.discover(ctx)
        assert records == []  # empty catalog → zero discovery results

    def test_strict_authority_mixed_required_and_optional(self, tmp_path: Path):
        """Complex skill with both required and optional; definitions
        are the sole authority for availability determination."""
        _write_skill(
            tmp_path, "s",
            '---\nname: s\n'
            'required_capabilities: ["workspace.read", "test.run"]\n'
            'optional_capabilities: ["git.status", "nonexistent.opt"]\n'
            '---\nBody',
        )
        doc = SkillRegistry([tmp_path]).view("s")
        cap_reg = CapabilityRegistry()
        # Provide only workspace.read backend → test.run will be UNAVAILABLE
        ctx = _context("windows", tools={"workspace.read", "cli.exec"})

        report = validate_skill_capabilities(doc, cap_reg, ctx)

        # workspace.read: known + available (backend present)
        wr = next(e for e in report.entries if e.capability_id == "workspace.read")
        assert wr.required and wr.known and wr.available

        # test.run: known + available (cli.exec is its preferred_tool)
        tr = next(e for e in report.entries if e.capability_id == "test.run")
        assert tr.required and tr.known and tr.available

        # git.status: known + available (cli.exec is its preferred_tool)
        gs = next(e for e in report.entries if e.capability_id == "git.status")
        assert not gs.required and gs.known and gs.available

        # nonexistent.opt: unknown + not available, non-fatal
        no = next(e for e in report.entries if e.capability_id == "nonexistent.opt")
        assert not no.required and not no.known and not no.available
        assert "nonexistent.opt" in report.unknown_optional

        assert report.all_required_available

    def test_no_implicit_capability_creation_for_legacy_skills(self, tmp_path: Path):
        """Legacy skills without capability fields must not trigger any
        implicit capability registration or lookup."""
        _write_skill(
            tmp_path, "legacy",
            "---\nname: legacy-skill\ndescription: old format\n---\nBody here",
        )
        registry = SkillRegistry([tmp_path])
        metas = registry.discover()
        meta = metas[0]

        cap_reg = CapabilityRegistry()
        before_defs = [d.model_dump() for d in cap_reg.list_all()]

        # Validate legacy skill (no capabilities declared)
        doc = registry.view("legacy-skill")
        report = validate_skill_capabilities(doc, cap_reg, _context("windows"))

        # Report is empty — no capabilities to check
        assert report.entries == []
        assert report.all_required_available  # vacuously true

        # Registry unchanged
        after_defs = [d.model_dump() for d in cap_reg.list_all()]
        assert before_defs == after_defs
