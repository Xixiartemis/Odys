import asyncio, hashlib, json
from pathlib import Path
from types import SimpleNamespace

from lhas.inner_agent import InnerAgentRequest, OpenAIAgentsBackend, AgentsSdkModelConfig
from lhas.inner_agent.executor import InnerAgentExecutor
from lhas.inner_agent.tool_adapter import allowed_tools
from lhas.planning.models import CapabilitySpec
from lhas.tools.registry import ToolRegistry
from lhas.tools.protocol import ToolRequest
from lhas.workspace import (CommandPolicy, CommandRule, LocalReadOnlyWorkspace, StagedWorkspace,
                            StagingLimitExceeded, StagingRootConflict, WorkspaceLimits, register_staged_workspace_tools)
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

def test_executor_wires_side_effect_grant(tmp_path):
    stage=StagedWorkspace.create(source_repo(tmp_path), tmp_path / "stage"); reg=ToolRegistry(); register_staged_workspace_tools(reg, stage, CommandPolicy())
    seen=[]
    class Backend:
        name="capture"
        async def run(self, request):
            seen.append(request); return __import__("lhas.inner_agent.models", fromlist=["InnerAgentResult"]).InnerAgentResult(status="SUCCESS")
    from lhas.executors.protocol import ExecutionRequest
    task={"objective":"x","allowed_capabilities":["workspace.read","workspace.edit"],"allowed_side_effect_capabilities":["workspace.edit"]}
    asyncio.run(InnerAgentExecutor(Backend(), allowed_side_effect_capabilities=None).execute(ExecutionRequest(task_id="t",run_id="r",attempt_id="a",attempt_number=1,task=task)))
    assert seen[0].allowed_side_effect_capabilities == ["workspace.edit"]
    seen.clear(); asyncio.run(InnerAgentExecutor(Backend(), allowed_side_effect_capabilities=[]).execute(ExecutionRequest(task_id="t",run_id="r",attempt_id="a",attempt_number=1,task=task)))
    assert seen[0].allowed_side_effect_capabilities == []

def test_staging_root_conflict_before_copy(tmp_path):
    source=source_repo(tmp_path); before=hashlib.sha256((source / "src/calculator.py").read_bytes()).hexdigest()
    for target in (source, source / ".odys-stage"):
        try: StagedWorkspace.create(source, target); assert False
        except StagingRootConflict as exc: assert str(exc) == "STAGING_ROOT_CONFLICT"
    assert hashlib.sha256((source / "src/calculator.py").read_bytes()).hexdigest() == before

def test_failed_staging_limit_cleans_auto_directory(tmp_path, monkeypatch):
    source=source_repo(tmp_path); from lhas.workspace import staged
    created=[]; real=staged.tempfile.mkdtemp
    def make(*args, **kwargs):
        result=real(*args, **kwargs); created.append(Path(result)); return result
    monkeypatch.setattr(staged.tempfile, "mkdtemp", make)
    try: StagedWorkspace.create(source, None, WorkspaceLimits(max_files=0)); assert False
    except StagingLimitExceeded: pass
    assert all(not p.exists() for p in created)

def test_executor_openai_backend_carries_candidate_patch_and_events(db, tmp_path):
    source=source_repo(tmp_path); stage=StagedWorkspace.create(source, tmp_path / "stage"); reg=ToolRegistry(); register_staged_workspace_tools(reg, stage, CommandPolicy())
    class Runner:
        async def run(self, agent, input, **kwargs):
            ctx=SimpleNamespace(tool_call_id="c")
            async def call(name,args):
                tool=next(x for x in agent.tools if x.name == name); await kwargs["hooks"].on_tool_start(ctx,agent,tool); out=await tool.on_invoke_tool(ctx,json.dumps(args)); await kwargs["hooks"].on_tool_end(ctx,agent,tool,out); return out
            await call("workspace.read", {"path":"src/calculator.py"}); await call("workspace.edit", {"path":"src/calculator.py","old_text":"return a - b","new_text":"return a + b"}); await call("workspace.diff", {})
            return SimpleNamespace(final_output="fixed", context_wrapper=SimpleNamespace(usage={}))
    backend=OpenAIAgentsBackend(reg, AgentsSdkModelConfig(model="m",api_key="k"), runner=Runner())
    from lhas.executors.protocol import ExecutionRequest
    result=asyncio.run(InnerAgentExecutor(backend, allowed_side_effect_capabilities=["workspace.edit"], db=db).execute(ExecutionRequest(task_id="t",run_id="r",attempt_id="a",attempt_number=1,task={"objective":"x","allowed_capabilities":["workspace.read","workspace.edit","workspace.diff"]})))
    assert result.status.value == "SUCCESS" and "workspace_patch" in result.artifacts and "+    return a + b" in result.artifacts["workspace_patch"]["diff"]
    from lhas.persistence.event_store import EventStore
    from lhas.domain.enums import EventType
    types=[e.event_type for e in EventStore(db).list_for_run("r")]
    assert EventType.WORKSPACE_EDIT_STARTED in types and EventType.WORKSPACE_EDIT_COMPLETED in types

def test_auto_staging_success_is_disjoint_and_source_unchanged(tmp_path):
    source=source_repo(tmp_path); before=(source / "src/calculator.py").read_bytes(); stage=StagedWorkspace.create(source)
    assert stage.root.exists() and stage.root != source and (stage.root / "src/calculator.py").exists()
    assert (source / "src/calculator.py").read_bytes() == before

def test_diff_runtime_hard_limit_and_patch_artifact_through_executor(tmp_path):
    source=source_repo(tmp_path); stage=StagedWorkspace.create(source, tmp_path / "stage", WorkspaceLimits(max_diff_bytes=100)); reg=ToolRegistry(); register_staged_workspace_tools(reg, stage, CommandPolicy())
    class Runner:
        async def run(self, agent, input, **kwargs):
            ctx=SimpleNamespace(tool_call_id="c")
            async def call(name,args):
                tool=next(x for x in agent.tools if x.name == name); return await tool.on_invoke_tool(ctx,json.dumps(args))
            await call("workspace.edit", {"path":"src/calculator.py","old_text":"return a - b","new_text":"return a + b"})
            await call("workspace.diff", {"max_diff_bytes": 99999999})
            return SimpleNamespace(final_output="fixed", context_wrapper=SimpleNamespace(usage={}))
    from lhas.executors.protocol import ExecutionRequest
    result=asyncio.run(InnerAgentExecutor(OpenAIAgentsBackend(reg, AgentsSdkModelConfig(model="m",api_key="k"),runner=Runner()), allowed_side_effect_capabilities=["workspace.edit"]).execute(ExecutionRequest(task_id="t",run_id="r",attempt_id="a",attempt_number=1,task={"objective":"x","allowed_capabilities":["workspace.edit","workspace.diff"],"allowed_side_effect_capabilities":["workspace.edit"]})))
    # Requesting an oversized limit is clamped by the staged workspace hard bound.
    patch=asyncio.run(stage.diff(max_diff_bytes=99999999)); assert len(patch["diff"].encode()) <= 100 and patch["truncated"] is True
    assert result.status.value == "SUCCESS" and len(result.artifacts["workspace_patch"]["diff"].encode()) <= 100 and result.artifacts["workspace_patch"]["truncated"] is True

def test_canonical_e4_eval_through_inner_executor(tmp_path):
    source=source_repo(tmp_path); source_sha=hashlib.sha256((source / "src/calculator.py").read_bytes()).hexdigest(); stage=StagedWorkspace.create(source); reg=ToolRegistry(); register_staged_workspace_tools(reg, stage, CommandPolicy())
    calls=[]; cli_calls=[]; outcomes=[]
    async def fake_cli(argv, cwd=".", timeout_seconds=None):
        cli_calls.append(argv); exit_code=1 if len(cli_calls) == 1 else 0; outcomes.append(exit_code)
        return ({"exit_code":exit_code,"stdout":"","stderr":"","timed_out":False,"duration_ms":1,"stdout_truncated":False,"stderr_truncated":False}, None)
    reg.resolve("cli.exec").cli.execute=fake_cli
    class Runner:
        async def run(self, agent, input, **kwargs):
            ctx=SimpleNamespace(tool_call_id="canonical")
            async def call(name,args):
                calls.append(name); tool=next(x for x in agent.tools if x.name == name); return await tool.on_invoke_tool(ctx,json.dumps(args))
            await call("workspace.read", {"path":"src/calculator.py"})
            before=await call("cli.exec", {"argv":["test"]}); assert before["output"]["exit_code"] == 1
            await call("workspace.search", {"query":"return a - b"})
            edit=await call("workspace.edit", {"path":"src/calculator.py","old_text":"return a - b","new_text":"return a + b"}); assert edit["status"] == "SUCCESS"
            after=await call("cli.exec", {"argv":["test"]}); assert after["output"]["exit_code"] == 0
            diff=await call("workspace.diff", {}); assert diff["output"]["files_changed"] == 1
            return SimpleNamespace(final_output="candidate", context_wrapper=SimpleNamespace(usage={}))
    from lhas.executors.protocol import ExecutionRequest
    result=asyncio.run(InnerAgentExecutor(OpenAIAgentsBackend(reg, AgentsSdkModelConfig(model="m",api_key="k"),runner=Runner()), allowed_side_effect_capabilities=["workspace.edit"]).execute(ExecutionRequest(task_id="t",run_id="r",attempt_id="a",attempt_number=1,task={"objective":"fix","allowed_capabilities":["workspace.read","workspace.search","workspace.edit","workspace.diff","cli.exec"],"allowed_side_effect_capabilities":["workspace.edit"]})))
    assert result.status.value == "SUCCESS" and len(calls) == 6
    assert result.artifacts["workspace_patch"]["files_changed"] == 1 and "+    return a + b" in result.artifacts["workspace_patch"]["diff"]
    assert outcomes == [1,0]
    assert hashlib.sha256((source / "src/calculator.py").read_bytes()).hexdigest() == source_sha
