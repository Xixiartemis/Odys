import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from lhas.inner_agent import AgentsSdkModelConfig, InnerAgentRequest, InnerAgentStatus, OpenAIAgentsBackend
from lhas.inner_agent.tool_adapter import allowed_tools
from lhas.inner_agent.trace import InnerAgentTrace
from lhas.planning.models import CapabilitySpec
from lhas.tools.fakes import FakeTool
from lhas.tools.protocol import ToolRequest, ToolResult, ToolResultStatus
from lhas.tools.registry import ToolRegistry
from lhas.workspace import CommandPolicy, StagedWorkspace, WorkspaceLimits


_SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "e5_live_model_smoke.py"
_SPEC = importlib.util.spec_from_file_location("e5_live_model_smoke", _SCRIPT_PATH)
_SMOKE = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_SMOKE)


def test_failure_path_preserves_latest_workspace_artifacts():
    from agents import MaxTurnsExceeded

    class Runner:
        async def run(self, agent, prompt, **kwargs):
            trace = kwargs["hooks"].trace
            trace.add("WORKSPACE_CHANGE", tool_name="workspace.edit", summary={"path": "src/calculator.py"})
            trace.add("WORKSPACE_PATCH", patch={"changed_files": ["old.py"], "diff": "old"})
            trace.add("WORKSPACE_PATCH", patch={"changed_files": ["src/calculator.py"], "diff": "latest"})
            raise MaxTurnsExceeded("limit")

    result = asyncio.run(
        OpenAIAgentsBackend(
            ToolRegistry(), AgentsSdkModelConfig(model="m", api_key="k"), runner=Runner()
        ).run(InnerAgentRequest(task_id="t", run_id="r", attempt_id="a", objective="x"))
    )
    assert result.status is InnerAgentStatus.FAILURE
    assert result.artifacts["workspace_changes"]
    assert result.artifacts["workspace_patch"]["diff"] == "latest"


def test_strict_validator_is_independent_and_rejects_invalid_cases():
    valid = dict(test_before_passed=False, test_after_passed=True, source_repo_unchanged=True, validator_final_patch_files=["src/calculator.py"])
    assert _SMOKE._strict_validator(**valid) is True
    for override in (
        {"test_before_passed": True},
        {"test_after_passed": False},
        {"source_repo_unchanged": False},
        {"validator_final_patch_files": []},
        {"validator_final_patch_files": ["other.py"]},
    ):
        candidate = {**valid, **override}
        assert _SMOKE._strict_validator(**candidate) is False


def test_functional_validation_can_pass_when_agent_hits_turn_limit():
    assert _SMOKE._strict_validator(test_before_passed=False, test_after_passed=True, source_repo_unchanged=True, validator_final_patch_files=["src/calculator.py"])
    assert _SMOKE._termination_status("FAILURE", "AGENT_TURN_LIMIT", False) == "TURN_LIMIT"


def test_failed_tool_observation_has_safe_error_type_only():
    registry = ToolRegistry()
    registry.register(
        FakeTool(
            CapabilitySpec(name="safe.tool", description="safe"),
            lambda request: ToolResult(status=ToolResultStatus.FAILURE, error_type="COMMAND_NOT_ALLOWED", error_message="denied"),
        )
    )
    request = InnerAgentRequest(task_id="t", run_id="r", attempt_id="a", objective="x", allowed_capabilities=["safe.tool"])
    trace = InnerAgentTrace()
    tools, _ = allowed_tools(registry, request, trace=trace)
    observed = asyncio.run(tools[0].on_invoke_tool(SimpleNamespace(tool_call_id="c", context={}), "{}"))
    summary = next(item for item in trace.items if item["event"] == "TOOL_OBSERVATION_SUMMARY")
    assert observed["error_type"] == "COMMAND_NOT_ALLOWED"
    assert summary == {"event": "TOOL_OBSERVATION_SUMMARY", "capability": "safe.tool", "status": "FAILURE", "error_type": "COMMAND_NOT_ALLOWED"}
    assert not any(key in summary for key in ("argv", "stdout", "stderr", "content", "reasoning"))


def test_tool_failure_aggregation_is_by_type_and_capability():
    failures, by_type, by_capability = _SMOKE._tool_failure_aggregation(
        [
            {"capability": "cli.exec", "status": "FAILURE", "error_type": "COMMAND_NOT_ALLOWED"},
            {"capability": "cli.exec", "status": "FAILURE", "error_type": "COMMAND_NOT_ALLOWED"},
            {"capability": "workspace.read", "status": "FAILURE", "error_type": "FILE_NOT_FOUND"},
        ]
    )
    assert len(failures) == 3
    assert by_type == {"COMMAND_NOT_ALLOWED": 2, "FILE_NOT_FOUND": 1}
    assert by_capability == {"cli.exec": 2, "workspace.read": 1}


def test_live_workspace_excludes_pytest_cache_and_cli_policy_stays_narrow(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    cache = source / ".pytest_cache" / "v"
    cache.mkdir(parents=True)
    (cache / "nodeids").write_text("noise", encoding="utf-8")
    stage = StagedWorkspace.create(source, tmp_path / "stage", WorkspaceLimits())
    assert not (stage.root / ".pytest_cache").exists()
    policy = _SMOKE._live_command_policy()
    assert policy.allows(["pytest", "-q"])
    assert not policy.allows(["python", "-c", "print(1)"])
    assert _SMOKE.MAX_TURNS == 20


def test_live_task_exposes_cli_policy_without_broadening_authority():
    task = _SMOKE._live_task()
    assert any("restricted to pytest" in constraint for constraint in task["constraints"])
    assert "cli.exec" in task["allowed_capabilities"]
    assert "git.push" not in task["allowed_capabilities"]
