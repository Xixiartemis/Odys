import asyncio, hashlib, json
from pathlib import Path
from types import SimpleNamespace

from lhas.inner_agent import InnerAgentRequest, OpenAIAgentsBackend, AgentsSdkModelConfig
from lhas.inner_agent.tool_adapter import allowed_tools
from lhas.planning.models import CapabilitySpec
from lhas.tools.registry import ToolRegistry
from lhas.tools.protocol import ToolRequest
from lhas.workspace import (CommandPolicy, CommandRule, LocalReadOnlyWorkspace, StagedWorkspace,
                            StagingLimitExceeded, WorkspaceLimits, register_staged_workspace_tools)
from lhas.workspace.tools import WorkspaceEditTool

def source_repo(tmp_path):
    src=tmp_path / "source"; (src / "src").mkdir(parents=True); (src / "tests").mkdir()
    (src / "src" / "calculator.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (src / "tests" / "test_calculator.py").write_text("assert add(1, 2) == 3\n", encoding="utf-8")
    return src
def req(cap,args): return ToolRequest(tool_call_id="c",task_id="t",run_id="r",attempt_id="a",capability=cap,arguments=args)

def test_staged_edit_does_not_mutate_source(tmp_path):
    source=source_repo(tmp_path); original=(source / "src/calculator.py").read_bytes(); stage=StagedWorkspace.create(source, tmp_path / "stage")
    result=asyncio.run(stage.edit_file("src/calculator.py", "return a - b", "return a + b"))
    assert result["replacements"] == 1 and (source / "src/calculator.py").read_bytes() == original
    assert "return a + b" in (stage.root / "src/calculator.py").read_text()
    assert hashlib.sha256(original).hexdigest() == hashlib.sha256((source / "src/calculator.py").read_bytes()).hexdigest()

def test_edit_sha_guard_ambiguous_and_not_found(tmp_path):
    stage=StagedWorkspace.create(source_repo(tmp_path), tmp_path / "stage")
    data=asyncio.run(stage.read_file("src/calculator.py"));
    try: asyncio.run(stage.edit_file("src/calculator.py", "return a - b", "return a + b", expected_sha256="bad")); assert False
    except ValueError as exc: assert str(exc) == "STALE_FILE_VERSION"
    (stage.root / "src/calculator.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    try: asyncio.run(stage.edit_file("src/calculator.py", "x = 1", "x = 2")); assert False
    except ValueError as exc: assert str(exc) == "EDIT_TARGET_AMBIGUOUS"
    try: asyncio.run(stage.edit_file("src/calculator.py", "nope", "x")); assert False
    except ValueError as exc: assert str(exc) == "EDIT_TARGET_NOT_FOUND"

def test_diff_and_restore(tmp_path):
    stage=StagedWorkspace.create(source_repo(tmp_path), tmp_path / "stage")
    asyncio.run(stage.edit_file("src/calculator.py", "return a - b", "return a + b"))
    diff=asyncio.run(stage.diff()); assert diff["files_changed"] == 1 and "-    return a - b" in diff["diff"] and "+    return a + b" in diff["diff"]
    asyncio.run(stage.restore_file("src/calculator.py")); assert asyncio.run(stage.diff())["files_changed"] == 0

def test_staging_copy_skips_symlink_and_limits(tmp_path):
    source=source_repo(tmp_path); outside=tmp_path / "outside.txt"; outside.write_text("secret", encoding="utf-8")
    try: (source / "leak").symlink_to(outside)
    except (OSError, NotImplementedError): pass
    stage=StagedWorkspace.create(source, tmp_path / "stage"); assert not (stage.root / "leak").exists()
    try: StagedWorkspace.create(source, tmp_path / "tiny", WorkspaceLimits(max_files=1)) ; assert False
    except StagingLimitExceeded: pass

def test_edit_path_and_binary_protection(tmp_path):
    source=source_repo(tmp_path); (source / "blob").write_bytes(b"x\0y"); stage=StagedWorkspace.create(source, tmp_path / "stage")
    from lhas.workspace.errors import BinaryFileError, WorkspacePathEscape
    for path in ("../outside", str(tmp_path / "outside"), "C:\\outside"):
        try: asyncio.run(stage.edit_file(path, "x", "y")); assert False
        except WorkspacePathEscape: pass
    try: asyncio.run(stage.edit_file("blob", "x", "y")); assert False
    except BinaryFileError: pass

def test_side_effect_exposure_requires_explicit_grant(tmp_path):
    stage=StagedWorkspace.create(source_repo(tmp_path), tmp_path / "stage"); reg=ToolRegistry(); register_staged_workspace_tools(reg, stage, CommandPolicy())
    base=InnerAgentRequest(task_id="t",run_id="r",attempt_id="a",objective="x",allowed_capabilities=["workspace.read","workspace.edit"])
    tools,_=allowed_tools(reg,base); assert [x.name for x in tools] == ["workspace.read"]
    granted=base.model_copy(update={"allowed_side_effect_capabilities":["workspace.edit"]}); tools,_=allowed_tools(reg,granted); assert {x.name for x in tools} == {"workspace.read","workspace.edit"}
    reg.register(type("ApprovalTool", (), {"capability": CapabilitySpec(name="danger",description="",side_effect=True,requires_human_approval=True)})())
    approval=granted.model_copy(update={"allowed_capabilities":["danger"],"allowed_side_effect_capabilities":["danger"]}); tools,_=allowed_tools(reg,approval); assert tools == []

def test_full_fail_edit_pass_diff_inner_run(tmp_path):
    source=source_repo(tmp_path); stage=StagedWorkspace.create(source, tmp_path / "stage"); reg=ToolRegistry(); register_staged_workspace_tools(reg, stage, CommandPolicy([CommandRule(["git","diff"], True)]))
    calls=[]
    async def fake_cli(argv, cwd=".", timeout_seconds=None):
        calls.append(argv)
        return ({"exit_code": 1 if len(calls) == 1 else 0, "stdout": "", "stderr": "", "timed_out": False, "duration_ms": 1, "stdout_truncated": False, "stderr_truncated": False}, None)
    reg.resolve("cli.exec").cli.execute = fake_cli
    class Runner:
        async def run(self, agent, input, **kwargs):
            ctx=SimpleNamespace(tool_call_id="call")
            async def call(name,args):
                tool=next(x for x in agent.tools if x.name == name); await kwargs["hooks"].on_tool_start(ctx,agent,tool); out=await tool.on_invoke_tool(ctx,json.dumps(args)); await kwargs["hooks"].on_tool_end(ctx,agent,tool,out); return out
            await call("workspace.read", {"path":"src/calculator.py"})
            before=await call("cli.exec", {"argv":["git","diff","--bad"]}); assert before["status"] == "SUCCESS"
            await call("workspace.read", {"path":"src/calculator.py"})
            edit=await call("workspace.edit", {"path":"src/calculator.py","old_text":"return a - b","new_text":"return a + b"}); assert edit["status"] == "SUCCESS"
            after=await call("cli.exec", {"argv":["git","diff","--quiet"]}); assert after["status"] == "SUCCESS" and after["output"]["exit_code"] == 0
            diff=await call("workspace.diff", {}); assert diff["output"]["files_changed"] == 1
            return SimpleNamespace(final_output="fixed", context_wrapper=SimpleNamespace(usage={}))
    allowed=["workspace.read","cli.exec","workspace.edit","workspace.diff"]
    request=InnerAgentRequest(task_id="t",run_id="r",attempt_id="a",objective="fix",allowed_capabilities=allowed,allowed_side_effect_capabilities=["workspace.edit"])
    result=asyncio.run(OpenAIAgentsBackend(reg, AgentsSdkModelConfig(model="m",api_key="k"),runner=Runner()).run(request))
    assert result.status.value == "SUCCESS" and result.tool_call_count == 6
    assert result.artifacts["workspace_changes"]
    assert "return a - b" in (source / "src/calculator.py").read_text() and "return a + b" in (stage.root / "src/calculator.py").read_text()

def test_edit_tool_output_is_bounded_audit(tmp_path):
    stage=StagedWorkspace.create(source_repo(tmp_path), tmp_path / "stage"); result=asyncio.run(WorkspaceEditTool(stage).execute(req("workspace.edit", {"path":"src/calculator.py","old_text":"return a - b","new_text":"return a + b"})))
    assert result.status.value == "SUCCESS" and "after_sha256" in result.output and "content" not in result.output
