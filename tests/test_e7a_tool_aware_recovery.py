import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from lhas.inner_agent.models import InnerAgentRequest
from lhas.inner_agent.observability import project_tool_metrics
from lhas.inner_agent.openai_agents_backend import OpenAIAgentsBackend
from lhas.inner_agent.tool_adapter import ToolAwareObserver, _args_signature, allowed_tools, safe_tool_summary
from lhas.inner_agent.trace import InnerAgentTrace
from lhas.tools.protocol import ToolRequest, ToolResult, ToolResultStatus
from lhas.tools.registry import ToolRegistry
from lhas.workspace import CommandPolicy, CommandRule, StagedWorkspace, register_staged_workspace_tools
from lhas.workspace.errors import WorkspaceEditError
from lhas.workspace.tools import SafeCliTool, WorkspaceEditLinesTool, WorkspaceEditTool


def _source(tmp_path: Path, content: bytes = b"alpha\nbeta\ngamma\n") -> Path:
    source=tmp_path / "source"; source.mkdir()
    (source / "sample.txt").write_bytes(content)
    return source


def _request(capability: str, arguments: dict) -> ToolRequest:
    return ToolRequest(tool_call_id="call",task_id="task",run_id="run",attempt_id="attempt",capability=capability,arguments=arguments)


def _invoke(tool, args):
    return asyncio.run(tool.execute(_request(tool.capability.name,args)))


def test_e7a_exact_target_success_is_auditable(tmp_path):
    workspace=StagedWorkspace.create(_source(tmp_path),tmp_path / "stage")
    result=asyncio.run(workspace.edit_file("sample.txt","beta","BETA"))
    assert result["match_mode"] == "EXACT" and result["candidate_count"] == 1
    assert result["matched_start_line"] == result["matched_end_line"] == 2


def test_e7a_unique_trailing_space_normalization(tmp_path):
    workspace=StagedWorkspace.create(_source(tmp_path,b"alpha\nbeta   \ngamma\n"),tmp_path / "stage")
    result=asyncio.run(workspace.edit_file("sample.txt","beta\n","BETA\n"))
    assert result["match_mode"] == "NORMALIZED_UNIQUE"
    assert (workspace.root / "sample.txt").read_bytes() == b"alpha\nBETA\ngamma\n"


def test_e7a_unique_newline_normalization_preserves_crlf(tmp_path):
    workspace=StagedWorkspace.create(_source(tmp_path,b"alpha\r\nbeta\r\ngamma\r\n"),tmp_path / "stage")
    result=asyncio.run(workspace.edit_file("sample.txt","beta\n","BETA\n"))
    assert result["match_mode"] == "NORMALIZED_UNIQUE"
    assert (workspace.root / "sample.txt").read_bytes() == b"alpha\r\nBETA\r\ngamma\r\n"


def test_e7a_missing_target_has_zero_candidates(tmp_path):
    workspace=StagedWorkspace.create(_source(tmp_path),tmp_path / "stage")
    with pytest.raises(WorkspaceEditError) as caught:
        asyncio.run(workspace.edit_file("sample.txt","missing","value"))
    assert caught.value.code == "EDIT_TARGET_NOT_FOUND"
    assert caught.value.diagnostics == {"candidate_count":0,"normalization_attempted":True}


def test_e7a_exact_ambiguity_never_mutates(tmp_path):
    workspace=StagedWorkspace.create(_source(tmp_path,b"same same\n"),tmp_path / "stage")
    before=(workspace.root / "sample.txt").read_bytes()
    with pytest.raises(WorkspaceEditError) as caught:
        asyncio.run(workspace.edit_file("sample.txt","same","new"))
    assert caught.value.code == "EDIT_TARGET_AMBIGUOUS" and caught.value.diagnostics["candidate_count"] == 2
    assert (workspace.root / "sample.txt").read_bytes() == before


def test_e7a_normalized_ambiguity_never_mutates(tmp_path):
    workspace=StagedWorkspace.create(_source(tmp_path,b"same  \nsame \n"),tmp_path / "stage")
    before=(workspace.root / "sample.txt").read_bytes()
    with pytest.raises(WorkspaceEditError) as caught:
        asyncio.run(workspace.edit_file("sample.txt","same\t\n","new\n"))
    assert caught.value.code == "EDIT_TARGET_AMBIGUOUS" and caught.value.diagnostics["match_mode"] == "NORMALIZED"
    assert (workspace.root / "sample.txt").read_bytes() == before


def test_e7a_normalization_is_not_partial_line_fuzzy_matching(tmp_path):
    workspace=StagedWorkspace.create(_source(tmp_path,b"prefix beta suffix\n"),tmp_path / "stage")
    with pytest.raises(WorkspaceEditError,match="EDIT_TARGET_NOT_FOUND"):
        asyncio.run(workspace.edit_file("sample.txt","beta  ","BETA"))
    assert (workspace.root / "sample.txt").read_bytes() == b"prefix beta suffix\n"


def test_e7a_edit_and_diff_agree(tmp_path):
    workspace=StagedWorkspace.create(_source(tmp_path,b"alpha\nbeta  \n"),tmp_path / "stage")
    asyncio.run(workspace.edit_file("sample.txt","beta\n","BETA\n"))
    diff=asyncio.run(workspace.diff())
    assert diff["changed_files"] == ["sample.txt"] and "-beta" in diff["diff"] and "+BETA" in diff["diff"]


def test_e7a_no_change_is_explicit_and_does_not_write(tmp_path):
    workspace=StagedWorkspace.create(_source(tmp_path),tmp_path / "stage")
    before=(workspace.root / "sample.txt").read_bytes()
    result=_invoke(WorkspaceEditTool(workspace),{"path":"sample.txt","old_text":"beta","new_text":"beta"})
    assert result.error_type == "NO_CHANGE" and result.metadata["failure_category"] == "NO_CHANGE"
    assert (workspace.root / "sample.txt").read_bytes() == before


def test_e7a_invalid_edit_range_is_structured(tmp_path):
    workspace=StagedWorkspace.create(_source(tmp_path),tmp_path / "stage")
    digest=hashlib.sha256((workspace.root / "sample.txt").read_bytes()).hexdigest()
    result=_invoke(WorkspaceEditLinesTool(workspace),{"path":"sample.txt","start_line":2,"end_line":99,"new_lines":["x"],"expected_sha256":digest})
    assert result.error_type == "INVALID_EDIT_RANGE"
    assert result.metadata["action"] == "REREAD_AND_REBUILD_LINE_RANGE" and result.metadata["total_lines"] == 3


def _adapter_tools(tmp_path):
    workspace=StagedWorkspace.create(_source(tmp_path),tmp_path / "stage")
    registry=ToolRegistry(); register_staged_workspace_tools(registry,workspace,CommandPolicy())
    trace=InnerAgentTrace()
    request=InnerAgentRequest(task_id="t",run_id="r",attempt_id="a",objective="repair",allowed_capabilities=["workspace.edit","workspace.read","workspace.search"],allowed_side_effect_capabilities=["workspace.edit"])
    tools,_=allowed_tools(registry,request,trace)
    return {tool.name:tool for tool in tools},trace


def test_e7a_repeated_exact_edit_failure_is_observable(tmp_path):
    tools,trace=_adapter_tools(tmp_path); context=SimpleNamespace(tool_call_id="c",context={})
    raw=json.dumps({"path":"sample.txt","old_text":"missing","new_text":"x"})
    first=asyncio.run(tools["workspace.edit"].on_invoke_tool(context,raw))
    second=asyncio.run(tools["workspace.edit"].on_invoke_tool(context,raw))
    assert "failure_repeat_count" not in first
    assert second["failure_repeat_count"] == 2 and second["strategy_change_required"] is True
    assert all("old_text" not in json.dumps(item) for item in trace.items)


def test_e7a_similar_failure_then_read_records_strategy_change(tmp_path):
    tools,trace=_adapter_tools(tmp_path); context=SimpleNamespace(tool_call_id="c",context={})
    for target in ("missing-one","missing-two"):
        asyncio.run(tools["workspace.edit"].on_invoke_tool(context,json.dumps({"path":"sample.txt","old_text":target,"new_text":"x"})))
    asyncio.run(tools["workspace.read"].on_invoke_tool(context,json.dumps({"path":"sample.txt"})))
    observations=[item for item in trace.items if item.get("event") == "TOOL_OBSERVATION_SUMMARY"]
    assert observations[1]["similar_failure_count"] == 2
    assert observations[2]["strategy_change_observed"] is True
    assert observations[2]["strategy_change_to"] == "workspace.read"


def test_e7a_adapter_does_not_hide_or_auto_retry_failures(tmp_path):
    tools,trace=_adapter_tools(tmp_path); context=SimpleNamespace(tool_call_id="c",context={})
    result=asyncio.run(tools["workspace.edit"].on_invoke_tool(context,json.dumps({"path":"sample.txt","old_text":"missing","new_text":"x"})))
    observations=[item for item in trace.items if item.get("event") == "TOOL_OBSERVATION_SUMMARY"]
    assert result["status"] == "FAILURE" and len(observations) == 1
    assert (Path(tmp_path) / "stage" / "sample.txt").read_bytes() == b"alpha\nbeta\ngamma\n"


def test_e7a_repeat_observation_count_is_bounded():
    observer=ToolAwareObserver(); result=ToolResult(status=ToolResultStatus.FAILURE,error_type="EDIT_TARGET_NOT_FOUND",metadata={"failure_category":"EDIT_TARGET_NOT_FOUND"})
    summary={}
    args={"path":"x","old_text":"missing","new_text":"y"}; signature=_args_signature(args)
    for _ in range(105):
        summary=observer.decorate("workspace.edit",args,result,safe_tool_summary("workspace.edit",args,result),signature)
    assert summary["failure_repeat_count"] == 100 and summary["similar_failure_count"] == 100
    assert summary["repeat_count_truncated"] is True


def test_e7a_command_not_allowed_feedback_stays_narrow(tmp_path):
    workspace=StagedWorkspace.create(_source(tmp_path),tmp_path / "stage")
    policy=CommandPolicy([CommandRule(["pytest"],True)])
    result=_invoke(SafeCliTool(workspace,policy),{"argv":["git","status"]})
    assert result.error_type == "COMMAND_NOT_ALLOWED"
    assert result.metadata["failure_category"] == "COMMAND_POLICY_ERROR"
    assert result.metadata["allowed_command_prefixes"] == [["pytest"]]
    assert policy.allows(["git","status"]) is False


def test_e7a_workspace_path_escape_feedback_hides_root(tmp_path):
    workspace=StagedWorkspace.create(_source(tmp_path),tmp_path / "stage")
    result=_invoke(SafeCliTool(workspace,CommandPolicy([CommandRule(["pytest"],True)])),{"argv":["pytest"],"cwd":"../outside"})
    assert result.error_type == "WORKSPACE_PATH_ESCAPE"
    assert result.metadata["failure_category"] == "WORKSPACE_PATH_ERROR"
    assert result.metadata["path_policy"] == "WORKSPACE_RELATIVE_ONLY"
    assert result.metadata["workspace_root_exposed"] is False
    assert str(workspace.root) not in json.dumps(result.model_dump())


def test_e7a_real_pytest_fail_then_edit_diff_pass_is_observed(tmp_path):
    source=tmp_path / "source"; source.mkdir()
    (source / "test_value.py").write_text("def test_value():\n    assert 1 == 2\n",encoding="utf-8")
    workspace=StagedWorkspace.create(source,tmp_path / "stage")
    argv=[sys.executable,"-m","pytest","-q"]
    cli=SafeCliTool(workspace,CommandPolicy([CommandRule([sys.executable,"-m","pytest"],True)]))
    observer=ToolAwareObserver(); observations=[]
    def observe(name,args,result):
        summary=observer.decorate(name,args,result,safe_tool_summary(name,args,result),_args_signature(args)); observations.append(summary); return summary
    before=_invoke(cli,{"argv":argv}); observe("cli.exec",{"argv":argv},before)
    edit_args={"path":"test_value.py","old_text":"    assert 1 == 2\n","new_text":"    assert True\n"}
    edit=_invoke(WorkspaceEditTool(workspace),edit_args); observe("workspace.edit",edit_args,edit)
    diff=asyncio.run(workspace.diff()); observe("workspace.diff",{},ToolResult(status=ToolResultStatus.SUCCESS,output=diff))
    after=_invoke(cli,{"argv":argv}); final=observe("cli.exec",{"argv":argv},after)
    assert observations[0]["pytest_observation"] == "FAIL"
    assert final["pytest_observation"] == "PASS" and final["inspection_before_verification"] is True
    assert final["post_edit_verification"] is True


def test_e7a_safe_metrics_cover_required_tool_dimensions():
    metrics=project_tool_metrics([
        {"capability":"workspace.edit","status":"FAILURE","failure_category":"EDIT_TARGET_NOT_FOUND","similar_failure_count":2,"tool_call_index":1},
        {"capability":"workspace.read","status":"SUCCESS","after_edit_failure":True,"strategy_change_observed":True,"tool_call_index":2},
        {"capability":"workspace.edit","status":"SUCCESS","tool_call_index":3},
        {"capability":"cli.exec","status":"SUCCESS","verification_kind":"PYTEST","pytest_observation":"PASS","tool_call_index":4},
    ])
    assert metrics["workspace_edit_calls"] == 2 and metrics["workspace_edit_failures"] == 1
    assert metrics["repeated_edit_failures"] == metrics["strategy_changes_after_repeated_failure"] == 1
    assert metrics["pytest_executions"] == metrics["pytest_pass_observations"] == 1
    assert metrics["total_tool_calls"] == 4 and metrics["total_tool_failures"] == 1


def test_e7a_pytest_pass_remains_a_candidate_for_outer_validation():
    instructions=OpenAIAgentsBackend._instructions
    rendered=instructions(SimpleNamespace(),SimpleNamespace(objective="x",constraints=[],acceptance_criteria=[],context={}))
    assert "never replaces the outer validator" in rendered
    assert "Final output is only a candidate claim" in rendered
