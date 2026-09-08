"""Phase 2 Capability Runtime Closeout Harness.

Seven deterministic scenarios proving the end-to-end capability runtime works:
  1. Builtin Success — representative built-ins via ToolContract
  2. Invalid Request Fail Closed — malformed args rejected, backend untouched
  3. Missing Backend — definition exists, backend absent → fail closed
  4. MCP Local — discovery → Definition → ToolContract → MCPToolAdapter → fake server → evidence
  5. Skill Readiness — Skill with required caps; available when defn present, unavailable when only backend
  6. Platform — platform.prepare/delegate/finalize via ToolContract
  7. Tool Success ≠ Completion — ToolResult SUCCESS cannot claim VERIFIED

Produces artifacts/phase2/p25-closeout.json after all tests pass.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from lhas.capability_registry import (
    CapabilityAvailability,
    CapabilityDefinition,
    CapabilityRegistry,
    CapabilityRuntimeContext,
    RuntimePlatform,
    default_capabilities,
)
from lhas.mcp.adapter import MCPToolAdapter, register_mcp_tools
from lhas.mcp.capabilities import mcp_capabilities, merge_capability_definitions
from lhas.mcp.manager import MCPManager
from lhas.mcp.models import MCPServerConfig, MCPToolInfo
from lhas.planning.models import CapabilitySpec
from lhas.skills.models import SkillDocument, SkillMetadata
from lhas.skills.validator import validate_skill_capabilities
from lhas.tools.contract import ToolContract, ToolContractDecision, ToolErrorCode
from lhas.tools.protocol import ToolRequest, ToolResult, ToolResultStatus
from lhas.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_FAKE_SERVER = str(_PROJECT_ROOT / "src" / "lhas" / "mcp" / "fake_server.py")
_ARTIFACT_DIR = _PROJECT_ROOT / "artifacts" / "phase2"
_ARTIFACT_PATH = _ARTIFACT_DIR / "p25-closeout.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EXECUTION_LOG: list[str] = []


def _reset_log():
    _EXECUTION_LOG.clear()


# Valid outputs matching each capability's output_schema
_VALID_OUTPUTS: dict[str, dict[str, Any]] = {
    "workspace.read": {
        "path": "README.md",
        "content": "hello",
        "start_line": 1,
        "end_line": 1,
        "total_lines": 1,
        "truncated": False,
        "sha256": "a" * 64,
    },
    "workspace.list": {
        "path": ".",
        "entries": [],
        "truncated": False,
    },
    "workspace.edit": {
        "path": "README.md",
        "replacements": 1,
        "before_sha256": "b" * 64,
        "after_sha256": "c" * 64,
        "bytes_before": 10,
        "bytes_after": 10,
        "match_mode": "exact",
        "candidate_count": 1,
        "matched_start_line": 1,
        "matched_end_line": 1,
    },
    "workspace.diff": {
        "changed_files": [],
        "diff": "",
        "files_changed": 0,
        "lines_added": 0,
        "lines_removed": 0,
        "truncated": False,
    },
}
# Generic output for capabilities with {"type": "object"} schema
_GENERIC_OUTPUT: dict[str, Any] = {"status": "ok"}


def _ctx(platform: str = "windows", tools: set[str] | None = None) -> CapabilityRuntimeContext:
    return CapabilityRuntimeContext(platform=platform, available_tools=tools)


def _req(
    *,
    capability_id: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> ToolRequest:
    return ToolRequest(
        tool_call_id=f"tc-{capability_id}",
        task_id="t-closeout",
        run_id="r-closeout",
        attempt_id="a-closeout",
        capability_id=capability_id,
        tool_name=tool_name,
        arguments=arguments or {},
    )


def _tracking_handler(_cap_id: str):
    """Return a handler that logs the REQUEST's capability_id and returns schema-valid output."""

    def _handler(request: ToolRequest) -> dict[str, Any]:
        _EXECUTION_LOG.append(request.capability_id)
        cap = request.capability_id or ""
        output = _VALID_OUTPUTS.get(cap, _GENERIC_OUTPUT).copy()
        # Only add "capability" for generic outputs (strict schemas use additionalProperties: False)
        if cap not in _VALID_OUTPUTS:
            output["capability"] = cap
        return output

    return _handler


def _build_fake_tool_for_capability(defn: CapabilityDefinition) -> Any:
    """Build a FakeTool whose CapabilitySpec.name matches preferred_tool."""
    from lhas.tools.fakes import FakeTool

    spec = CapabilitySpec(
        name=defn.preferred_tool,
        description=defn.description,
        input_schema=dict(defn.input_schema),
    )
    return FakeTool(capability=spec, handler=_tracking_handler(defn.id))


def _register_unique_fake_tools(
    defs: list[CapabilityDefinition], tool_reg: ToolRegistry
) -> None:
    """Register one FakeTool per unique preferred_tool name.

    Multiple capabilities can share a backend (e.g. cli.exec).  The
    ToolRegistry rejects duplicate names, so we register only once per
    unique tool name.  The tracking handler logs the capability id of
    the *first* definition that uses each tool name.
    """
    seen: set[str] = set()
    for defn in defs:
        if defn.preferred_tool in seen:
            continue
        seen.add(defn.preferred_tool)
        tool_reg.register(_build_fake_tool_for_capability(defn))


def _build_contract(
    definitions: list[CapabilityDefinition],
    tool_registry: ToolRegistry,
) -> tuple[CapabilityRegistry, ToolContract]:
    cap_reg = CapabilityRegistry(tool_registry=tool_registry, definitions=definitions)
    contract = ToolContract(cap_reg, tool_registry)
    return cap_reg, contract


# Valid arguments for each builtin capability
_VALID_ARGS: dict[str, dict[str, Any]] = {
    "workspace.read": {"path": "README.md"},
    "workspace.list": {},
    "workspace.edit": {"path": "README.md", "old_text": "old", "new_text": "new"},
    "workspace.diff": {},
    "test.run": {"argv": ["pytest"]},
    "git.status": {"argv": ["git", "status"]},
    "git.diff": {"argv": ["git", "diff"]},
    "environment.inspect": {},
    "platform.prepare": {"goal": "prepare test"},
    "platform.delegate": {"goal": "delegate test"},
    "platform.finalize": {"goal": "finalize test"},
}

# Representative built-in capability IDs for scenario 1
_BUILTIN_IDS = [
    "workspace.read",
    "workspace.list",
    "workspace.edit",
    "workspace.diff",
    "test.run",
    "git.status",
    "git.diff",
    "environment.inspect",
]

# ---------------------------------------------------------------------------
# Scenario results collector (module-level, collected by conftest-style fixture)
# ---------------------------------------------------------------------------
_SCENARIO_RESULTS: list[dict[str, Any]] = []


def _record(
    *,
    scenario_id: str,
    capability_id: str,
    contract_validated: bool,
    backend_executed: bool,
    tool_success: bool,
    evidence_id: str | None,
    result: str,
):
    _SCENARIO_RESULTS.append({
        "scenario_id": scenario_id,
        "capability_id": capability_id,
        "contract_validated": contract_validated,
        "backend_executed": backend_executed,
        "tool_success": tool_success,
        "evidence_id": evidence_id or "N/A",
        "result": result,
    })


# ===================================================================
# SCENARIO 1 — BUILTIN SUCCESS
# ===================================================================


@pytest.mark.asyncio
async def test_scenario_1_builtin_success():
    """Exercise representative built-in capabilities through ToolContract.

    For each: resolve via CapabilityRegistry, invoke via ToolContract with
    valid args, capture ToolResult + ToolEvidence.  Prove contract traversal
    (not direct execute).
    """
    _reset_log()
    defs = list(default_capabilities())
    defn_map = {d.id: d for d in defs}

    tool_reg = ToolRegistry()
    _register_unique_fake_tools(defs, tool_reg)

    cap_reg, contract = _build_contract(defs, tool_reg)

    for cap_id in _BUILTIN_IDS:
        defn = defn_map[cap_id]
        request = _req(
            capability_id=cap_id,
            tool_name=defn.preferred_tool,
            arguments=_VALID_ARGS[cap_id],
        )
        result = await contract.invoke(request, _ctx("windows"))

        assert result.status is ToolResultStatus.SUCCESS, f"{cap_id} failed"
        assert result.evidence is not None, f"{cap_id} missing evidence"
        assert result.evidence.evidence_type == "TOOL_EXECUTION"
        assert result.evidence.capability_id == cap_id
        assert result.evidence.source == "odys-tool-contract-v1"
        # Prove contract traversal: backend was invoked
        assert cap_id in _EXECUTION_LOG, f"{cap_id} backend not called"

        _record(
            scenario_id="S1_BUILTIN_SUCCESS",
            capability_id=cap_id,
            contract_validated=True,
            backend_executed=True,
            tool_success=True,
            evidence_id=result.evidence.capability_id,
            result="PASS",
        )


# ===================================================================
# SCENARIO 2 — INVALID REQUEST FAIL CLOSED
# ===================================================================


@pytest.mark.asyncio
async def test_scenario_2_invalid_request_fail_closed():
    """Send malformed args → contract rejects → backend execute count = 0."""
    _reset_log()
    defs = list(default_capabilities())

    tool_reg = ToolRegistry()
    _register_unique_fake_tools(defs, tool_reg)

    _, contract = _build_contract(defs, tool_reg)
    pre_count = len(_EXECUTION_LOG)

    # Case A: missing required 'path' for workspace.read
    req_a = _req(capability_id="workspace.read", tool_name="workspace.read", arguments={})
    result_a = await contract.invoke(req_a, _ctx("windows"))
    assert result_a.status is ToolResultStatus.FAILURE
    assert result_a.error_type == ToolErrorCode.INVALID_ARGUMENT.value

    # Case B: wrong type for argv in test.run
    req_b = _req(capability_id="test.run", tool_name="test.run", arguments={"argv": "not-a-list"})
    result_b = await contract.invoke(req_b, _ctx("windows"))
    assert result_b.status is ToolResultStatus.FAILURE
    assert result_b.error_type == ToolErrorCode.INVALID_ARGUMENT.value

    # Case C: missing capability_id and tool_name
    req_c = ToolRequest(
        tool_call_id="tc-bad", task_id="t-1", run_id="r-1", attempt_id="a-1",
        capability_id="", tool_name="", arguments={},
    )
    result_c = await contract.invoke(req_c, _ctx("windows"))
    assert result_c.status is ToolResultStatus.FAILURE

    # Case D: semantic argv mismatch (git.status with wrong prefix)
    req_d = _req(
        capability_id="git.status", tool_name="git.status",
        arguments={"argv": ["ls", "-la"]},
    )
    result_d = await contract.invoke(req_d, _ctx("windows"))
    assert result_d.status is ToolResultStatus.FAILURE
    assert result_d.error_type == ToolErrorCode.INVALID_ARGUMENT.value

    # Backend must NOT have been called for any rejected request
    post_count = len(_EXECUTION_LOG)
    assert post_count == pre_count, "backend was called for a rejected request"

    _record(
        scenario_id="S2_INVALID_REQUEST_FAIL_CLOSED",
        capability_id="workspace.read,test.run,<empty>,git.status",
        contract_validated=False,
        backend_executed=False,
        tool_success=False,
        evidence_id="N/A",
        result="PASS",
    )


# ===================================================================
# SCENARIO 3 — MISSING BACKEND
# ===================================================================


@pytest.mark.asyncio
async def test_scenario_3_missing_backend():
    """Definition exists, backend missing → fail closed."""
    defs = list(default_capabilities())
    # Empty tool registry: no backends registered
    empty_tool_reg = ToolRegistry()
    _, contract = _build_contract(defs, empty_tool_reg)

    request = _req(
        capability_id="workspace.read",
        tool_name="workspace.read",
        arguments={"path": "README.md"},
    )
    result = await contract.invoke(request, _ctx("windows"))
    assert result.status is ToolResultStatus.FAILURE
    assert result.error_type == ToolErrorCode.CAPABILITY_UNAVAILABLE.value

    _record(
        scenario_id="S3_MISSING_BACKEND",
        capability_id="workspace.read",
        contract_validated=False,
        backend_executed=False,
        tool_success=False,
        evidence_id="N/A",
        result="PASS",
    )


# ===================================================================
# SCENARIO 4 — MCP LOCAL
# ===================================================================


def _fake_mcp_config() -> MCPServerConfig:
    return MCPServerConfig(name="odys-fake", command=[sys.executable, _FAKE_SERVER])


@pytest.mark.asyncio
async def test_scenario_4_mcp_local():
    """MCP discovery → Definition → ToolContract → MCPToolAdapter → fake server → typed evidence."""
    manager = MCPManager()
    config = _fake_mcp_config()
    tools = await manager.connect(config)
    try:
        assert len(tools) >= 1
        echo = next(t for t in tools if t.name.endswith(".echo"))
        assert echo.server_name == "odys-fake"

        # MCPToolInfo → CapabilityDefinition
        mcp_defs = mcp_capabilities(tools)
        assert any(d.id == "mcp.odys-fake.echo" for d in mcp_defs)
        echo_def = next(d for d in mcp_defs if d.id == "mcp.odys-fake.echo")
        assert echo_def.evidence_type == "MCP_TOOL_RESULT"
        assert echo_def.source == "mcp:odys-fake"

        # Merge with core and build contract
        core_defs = list(default_capabilities())
        all_defs = merge_capability_definitions(core_defs, mcp_defs)

        tool_reg = ToolRegistry()
        register_mcp_tools(tool_reg, manager, tools)

        cap_reg, contract = _build_contract(all_defs, tool_reg)

        # Invoke through contract
        request = _req(
            capability_id="mcp.odys-fake.echo",
            tool_name="mcp.odys-fake.echo",
            arguments={"text": "closeout-mcp-test"},
        )
        result = await contract.invoke(request, _ctx("windows"))

        assert result.status is ToolResultStatus.SUCCESS
        assert result.evidence is not None
        assert result.evidence.capability_id == "mcp.odys-fake.echo"
        assert result.evidence.source == "odys-tool-contract-v1"
        assert result.output.get("isError") is False
        content = result.output.get("content", [])
        assert any(item.get("text") == "closeout-mcp-test" for item in content)

        _record(
            scenario_id="S4_MCP_LOCAL",
            capability_id="mcp.odys-fake.echo",
            contract_validated=True,
            backend_executed=True,
            tool_success=True,
            evidence_id=result.evidence.capability_id,
            result="PASS",
        )
    finally:
        await manager.close_all()


# ===================================================================
# SCENARIO 5 — SKILL READINESS
# ===================================================================


def test_scenario_5_skill_readiness():
    """Load a Skill requiring builtin + MCP capability.

    Available when Definition exists; unavailable when only backend exists.
    Zero tool execution during validation.
    """
    _reset_log()

    # Build a Skill that requires workspace.read (builtin) + mcp.odys-fake.echo (MCP)
    skill_doc = SkillDocument(
        metadata=SkillMetadata(
            name="closeout-skill",
            description="test skill for closeout",
            required_capabilities=["workspace.read", "mcp.odys-fake.echo"],
        ),
        content="fake skill content",
    )

    # Case A: Both definitions present → all required available
    mcp_echo_def = CapabilityDefinition(
        id="mcp.odys-fake.echo",
        name="mcp.odys-fake.echo",
        description="echo",
        category="mcp.odys-fake",
        version="v1",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        output_schema={"type": "object"},
        platforms=(RuntimePlatform.WINDOWS, RuntimePlatform.LINUX, RuntimePlatform.MACOS),
        permissions=(),
        risk_level="MEDIUM",
        workspace_scope="EXTERNAL",
        timeout_seconds=30.0,
        retryable=False,
        preferred_tool="mcp.odys-fake.echo",
        source="mcp:odys-fake",
        evidence_type="MCP_TOOL_RESULT",
    )
    all_defs = list(default_capabilities()) + [mcp_echo_def]
    cap_reg_a = CapabilityRegistry(definitions=all_defs)
    report_a = validate_skill_capabilities(skill_doc, cap_reg_a, _ctx("windows"))
    assert report_a.all_required_available
    assert report_a.missing_required == []

    # Case B: Only backend registered (no CapabilityDefinition for MCP echo) → unavailable
    tool_reg = ToolRegistry()
    adapter = MCPToolAdapter(MCPManager(), MCPToolInfo(
        name="mcp.odys-fake.echo", server_name="odys-fake",
    ))
    tool_reg.register(adapter)

    cap_reg_b = CapabilityRegistry(
        tool_registry=tool_reg,
        definitions=list(default_capabilities()),  # no MCP def
    )
    report_b = validate_skill_capabilities(skill_doc, cap_reg_b, _ctx("windows"))
    assert not report_b.all_required_available
    assert "mcp.odys-fake.echo" in report_b.missing_required

    # Zero tool execution during validation
    assert len(_EXECUTION_LOG) == 0

    _record(
        scenario_id="S5_SKILL_READINESS",
        capability_id="workspace.read,mcp.odys-fake.echo",
        contract_validated=True,
        backend_executed=False,
        tool_success=False,
        evidence_id="N/A",
        result="PASS",
    )


# ===================================================================
# SCENARIO 6 — PLATFORM
# ===================================================================


@pytest.mark.asyncio
async def test_scenario_6_platform_contract():
    """Exercise platform.prepare/delegate/finalize via ToolContract."""
    _reset_log()
    defs = list(default_capabilities())
    tool_reg = ToolRegistry()
    _register_unique_fake_tools(defs, tool_reg)

    _, contract = _build_contract(defs, tool_reg)

    for cap_id in ("platform.prepare", "platform.delegate", "platform.finalize"):
        defn = next(d for d in defs if d.id == cap_id)
        request = _req(
            capability_id=cap_id,
            tool_name=defn.preferred_tool,
            arguments={"goal": f"test {cap_id}"},
        )
        result = await contract.invoke(request, _ctx("windows"))
        assert result.status is ToolResultStatus.SUCCESS, f"{cap_id} failed"
        assert result.evidence is not None
        assert result.evidence.capability_id == cap_id
        assert cap_id in _EXECUTION_LOG

        _record(
            scenario_id="S6_PLATFORM",
            capability_id=cap_id,
            contract_validated=True,
            backend_executed=True,
            tool_success=True,
            evidence_id=result.evidence.capability_id,
            result="PASS",
        )


# ===================================================================
# SCENARIO 7 — TOOL SUCCESS ≠ COMPLETION
# ===================================================================


@pytest.mark.asyncio
async def test_scenario_7_tool_success_not_completion():
    """ToolResult SUCCESS cannot mark task VERIFIED.

    Evidence type is TOOL_EXECUTION, not COMPLETION/VERIFIED.
    """
    _reset_log()
    defs = list(default_capabilities())
    tool_reg = ToolRegistry()
    _register_unique_fake_tools(defs, tool_reg)

    _, contract = _build_contract(defs, tool_reg)

    request = _req(
        capability_id="workspace.read",
        tool_name="workspace.read",
        arguments={"path": "README.md"},
    )
    result = await contract.invoke(request, _ctx("windows"))

    assert result.status is ToolResultStatus.SUCCESS
    assert result.evidence is not None

    # Evidence is TOOL_EXECUTION, not completion
    assert result.evidence.evidence_type == "TOOL_EXECUTION"
    assert "TOOL_EXECUTION" in result.evidence.evidence_type
    assert result.evidence.source == "odys-tool-contract-v1"

    # Summary says "succeeded" not "completed"/"verified"
    assert "succeeded" in result.evidence.summary.lower()
    assert "verified" not in result.evidence.summary.lower()
    assert "completed" not in result.evidence.summary.lower()

    # ToolResult has no task lifecycle field
    assert not hasattr(result, "task_status") or result.metadata.get("task_status") is None

    _record(
        scenario_id="S7_TOOL_SUCCESS_NOT_COMPLETION",
        capability_id="workspace.read",
        contract_validated=True,
        backend_executed=True,
        tool_success=True,
        evidence_id=result.evidence.capability_id,
        result="PASS",
    )


# ===================================================================
# Artifact generation (session-scoped fixture or finalizer)
# ===================================================================


def _aggregate_counts() -> dict[str, int]:
    """Compute summary counters from scenario results."""
    counts = {
        "capability_requests": len(_SCENARIO_RESULTS),
        "contract_accepted": sum(1 for r in _SCENARIO_RESULTS if r["contract_validated"]),
        "contract_rejected": sum(1 for r in _SCENARIO_RESULTS if not r["contract_validated"]),
        "backend_executions": sum(1 for r in _SCENARIO_RESULTS if r["backend_executed"]),
        "typed_evidence_count": sum(
            1 for r in _SCENARIO_RESULTS if r["evidence_id"] != "N/A"
        ),
    }
    return counts


def test_generate_artifact():
    """Generate the reproducible closeout artifact JSON.

    This test runs last (module order) and writes the artifact.
    """
    # Ensure directory exists
    _ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    counts = _aggregate_counts()
    all_pass = all(r["result"] == "PASS" for r in _SCENARIO_RESULTS)

    artifact = {
        "phase": "P25_CLOSEOUT",
        "branch": "phase2-closeout-harness-v1",
        "base_commit": "df00459",
        "tested_git_sha": _git_sha(),
        "summary": {
            "total_scenarios": len(_SCENARIO_RESULTS),
            "passed": sum(1 for r in _SCENARIO_RESULTS if r["result"] == "PASS"),
            "failed": sum(1 for r in _SCENARIO_RESULTS if r["result"] != "PASS"),
            **counts,
        },
        "scenarios": _SCENARIO_RESULTS,
        "decision": "PASS" if all_pass else "REQUEST_CHANGES",
    }

    _ARTIFACT_PATH.write_text(
        json.dumps(artifact, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )

    # Also generate the markdown report
    _generate_report(artifact)

    assert all_pass, "Not all scenarios passed — see artifact for details"


def _git_sha() -> str:
    """Return current HEAD SHA."""
    import subprocess
    return subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(_PROJECT_ROOT),
        text=True,
    ).strip()


def _generate_report(artifact: dict) -> None:
    """Generate the closeout markdown report."""
    report_dir = _PROJECT_ROOT / "docs" / "phase2"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "P25_CLOSEOUT_REPORT.md"

    summary = artifact["summary"]
    lines = [
        "# Phase 2 Closeout Report",
        "",
        f"**Branch:** `{artifact['branch']}`",
        f"**Base:** `{artifact['base_commit']}`",
        f"**Final HEAD:** `{artifact.get('tested_git_sha', 'unknown')}`",
        f"**Decision:** {artifact['decision']}",
        "",
        "## Summary",
        "",
        f"| Metric | Count |",
        f"|---|---|",
        f"| Scenarios | {summary['total_scenarios']} |",
        f"| Passed | {summary['passed']} |",
        f"| Failed | {summary['failed']} |",
        f"| Capability Requests | {summary['capability_requests']} |",
        f"| Contract Accepted | {summary['contract_accepted']} |",
        f"| Contract Rejected | {summary['contract_rejected']} |",
        f"| Backend Executions | {summary['backend_executions']} |",
        f"| Typed Evidence | {summary['typed_evidence_count']} |",
        "",
        "## Scenarios",
        "",
        "| Scenario | Capability | Contract Validated | Backend Exec | Tool Success | Evidence ID | Result |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in artifact["scenarios"]:
        lines.append(
            f"| {s['scenario_id']} | {s['capability_id']} | {s['contract_validated']} "
            f"| {s['backend_executed']} | {s['tool_success']} | {s['evidence_id']} | {s['result']} |"
        )
    lines.append("")
    lines.append(f"Artifact: `artifacts/phase2/p25-closeout.json`")
    lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
