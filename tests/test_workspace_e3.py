import asyncio
from pathlib import Path
from lhas.workspace import LocalReadOnlyWorkspace, CommandPolicy, CommandRule, register_workspace_tools
from lhas.workspace.errors import WorkspacePathEscape
from lhas.tools.registry import ToolRegistry
from lhas.tools.protocol import ToolRequest

def make_repo(tmp_path):
    (tmp_path / "src").mkdir(); (tmp_path / "tests").mkdir()
    (tmp_path / "README.md").write_text("Demo repository\n", encoding="utf-8")
    (tmp_path / "src" / "calculator.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (tmp_path / "tests" / "test_calculator.py").write_text("assert add(1,2) == 3\n", encoding="utf-8")
    return tmp_path
def req(cap, args): return ToolRequest(tool_call_id="c", task_id="t", run_id="r", attempt_id="a", capability=cap, arguments=args)

def test_workspace_list_read_search_and_empty(tmp_path):
    ws=LocalReadOnlyWorkspace(make_repo(tmp_path))
    listed=asyncio.run(ws.list_files())
    assert any(x["relative_path"] == "README.md" for x in listed["entries"])
    read=asyncio.run(ws.read_file("src/calculator.py")); assert "return a - b" in read["content"]
    found=asyncio.run(ws.search_text("return a - b")); assert found["matches"][0]["line"] == 2
    assert asyncio.run(ws.search_text("not-present"))["matches"] == []

def test_workspace_path_and_symlink_escape(tmp_path):
    ws=LocalReadOnlyWorkspace(make_repo(tmp_path))
    for value in ("../secret", str(tmp_path.parent / "secret"), "C:\\outside", "\\\\server\\share"):
        try: ws.resolve_path(value); assert False
        except WorkspacePathEscape: pass
    outside=tmp_path.parent / "outside-secret"; outside.write_text("secret", encoding="utf-8")
    try:
        (tmp_path / "link").symlink_to(outside)
    except (OSError, NotImplementedError): return
    try: ws.resolve_path("link"); assert False
    except WorkspacePathEscape: pass

def test_workspace_binary_and_read_limit(tmp_path):
    make_repo(tmp_path); (tmp_path / "blob").write_bytes(b"a\x00b")
    ws=LocalReadOnlyWorkspace(tmp_path)
    from lhas.workspace.errors import BinaryFileError
    try: asyncio.run(ws.read_file("blob")); assert False
    except BinaryFileError: pass

def test_workspace_tools_registry(tmp_path):
    reg=ToolRegistry(); register_workspace_tools(reg, LocalReadOnlyWorkspace(make_repo(tmp_path)), CommandPolicy())
    assert set(reg.list_capabilities()) == {"workspace.list", "workspace.read", "workspace.search", "cli.exec"}
    result=asyncio.run(reg.resolve("workspace.search").execute(req("workspace.search", {"query":"return a - b"})))
    assert result.status.value == "SUCCESS" and result.output["matches"]

def test_safe_cli_allowed_and_nonzero_observation(tmp_path):
    ws=LocalReadOnlyWorkspace(make_repo(tmp_path)); policy=CommandPolicy([CommandRule(["git", "status"], True)])
    from lhas.workspace.tools import SafeCliTool
    tool=SafeCliTool(ws, policy)
    result=asyncio.run(tool.execute(req("cli.exec", {"argv":["git", "status", "--short"]})))
    assert result.status.value == "SUCCESS" and "exit_code" in result.output

def test_safe_cli_denied_escape_and_shell_composition(tmp_path):
    from lhas.workspace.tools import SafeCliTool
    tool=SafeCliTool(LocalReadOnlyWorkspace(tmp_path), CommandPolicy([CommandRule(["git", "status"], True)]))
    denied=asyncio.run(tool.execute(req("cli.exec", {"argv":["git", "push"]}))); assert denied.error_type == "COMMAND_NOT_ALLOWED"
    composed=asyncio.run(tool.execute(req("cli.exec", {"argv":["git", "status", "&&", "whoami"]}))); assert composed.error_type in {"INVALID_ARGUMENTS", "COMMAND_NOT_ALLOWED"}
    escape=asyncio.run(tool.execute(req("cli.exec", {"argv":["git", "status"], "cwd":".."}))); assert escape.error_type == "WORKSPACE_PATH_ESCAPE"

def test_safe_cli_secret_environment_filter(monkeypatch, tmp_path):
    from lhas.workspace.safe_cli import SafeCli
    monkeypatch.setenv("ODYS_AGENT_API_KEY", "secret")
    cli=SafeCli(LocalReadOnlyWorkspace(tmp_path), CommandPolicy())
    assert "ODYS_AGENT_API_KEY" not in cli._env()

def test_harness_version():
    from lhas import HARNESS_VERSION
    assert HARNESS_VERSION == "HV-1.0"

def test_inner_agent_function_tools_workspace_sequence(tmp_path):
    from types import SimpleNamespace
    from lhas.inner_agent import OpenAIAgentsBackend, AgentsSdkModelConfig
    ws=LocalReadOnlyWorkspace(make_repo(tmp_path)); reg=ToolRegistry(); register_workspace_tools(reg, ws, CommandPolicy([CommandRule(["git", "status"], True)]))
    class Runner:
        async def run(self, agent, input, **kwargs):
            ctx=SimpleNamespace(tool_call_id="call")
            for name, args in [("workspace.list", {}), ("workspace.search", {"query":"return a - b"}), ("workspace.read", {"path":"src/calculator.py"}), ("cli.exec", {"argv":["git", "status", "--short"]})]:
                tool=next(x for x in agent.tools if x.name == name)
                await kwargs["hooks"].on_tool_start(ctx, agent, tool)
                observed=await tool.on_invoke_tool(ctx, __import__("json").dumps(args))
                assert "usage" not in observed
                await kwargs["hooks"].on_tool_end(ctx, agent, tool, observed)
            return SimpleNamespace(final_output="diagnosed", context_wrapper=SimpleNamespace(usage={}))
    from lhas.inner_agent.models import InnerAgentRequest
    request=InnerAgentRequest(task_id="t", run_id="r", attempt_id="a", objective="inspect", allowed_capabilities=["workspace.list","workspace.search","workspace.read","cli.exec"])
    result=asyncio.run(OpenAIAgentsBackend(reg, AgentsSdkModelConfig(model="m", api_key="k"), runner=Runner()).run(request))
    assert result.status.value == "SUCCESS" and result.tool_call_count == 4

def test_failing_command_is_successful_observation(tmp_path):
    from lhas.workspace.tools import SafeCliTool
    ws=LocalReadOnlyWorkspace(make_repo(tmp_path)); tool=SafeCliTool(ws, CommandPolicy([CommandRule(["git", "diff"], True)]))
    result=asyncio.run(tool.execute(req("cli.exec", {"argv":["git", "diff", "--no-such-file"]})))
    assert result.status.value == "SUCCESS" and result.output["exit_code"] != 0

def test_discovered_symlink_is_never_searched_or_statted(tmp_path):
    outside=tmp_path.parent / "outside.txt"; outside.write_text("TOP_SECRET_MARKER", encoding="utf-8")
    try: (tmp_path / "leak.txt").symlink_to(outside)
    except (OSError, NotImplementedError): return
    ws=LocalReadOnlyWorkspace(tmp_path)
    assert asyncio.run(ws.search_text("TOP_SECRET_MARKER"))["matches"] == []
    assert all(item["relative_path"] != "leak.txt" for item in asyncio.run(ws.list_files())["entries"])

def test_real_read_limit_with_small_injected_limit(tmp_path):
    from lhas.workspace.models import WorkspaceLimits
    (tmp_path / "large.txt").write_text("0123456789", encoding="utf-8")
    result=asyncio.run(LocalReadOnlyWorkspace(tmp_path, WorkspaceLimits(max_read_bytes=4, hard_max_read_bytes=8)).read_file("large.txt"))
    assert result["truncated"] is True and len(result["content"]) < 10

def test_capability_schemas_are_strict(tmp_path):
    reg=ToolRegistry(); register_workspace_tools(reg, LocalReadOnlyWorkspace(tmp_path), CommandPolicy())
    assert "path" in reg.resolve("workspace.read").capability.input_schema["required"]
    assert "query" in reg.resolve("workspace.search").capability.input_schema["required"]
    assert "argv" in reg.resolve("cli.exec").capability.input_schema["required"]
    assert all(reg.resolve(x).capability.input_schema["additionalProperties"] is False for x in reg.list_capabilities())

def test_search_hard_match_and_context_limits(tmp_path):
    from lhas.workspace.models import WorkspaceLimits
    (tmp_path / "many.txt").write_text("needle\n" * 120, encoding="utf-8")
    ws=LocalReadOnlyWorkspace(tmp_path, WorkspaceLimits(max_search_matches=3, max_context_lines=2))
    result=asyncio.run(ws.search_text("needle", max_matches=1000000, context_lines=10000))
    assert len(result["matches"]) <= 3 and result["truncated"] is True
    assert len(result["matches"][0]["before"]) <= 2 and len(result["matches"][0]["after"]) <= 2

def test_spawn_error_has_structured_type(tmp_path):
    from lhas.workspace.tools import SafeCliTool
    tool=SafeCliTool(LocalReadOnlyWorkspace(tmp_path), CommandPolicy([CommandRule(["definitely-not-a-command"], True)]))
    result=asyncio.run(tool.execute(req("cli.exec", {"argv":["definitely-not-a-command"]})))
    assert result.status.value == "FAILURE" and result.error_type == "SPAWN_ERROR"

def test_timeout_and_output_limits_with_injected_process(monkeypatch, tmp_path):
    from lhas.workspace.safe_cli import SafeCli
    class Proc:
        returncode=0
        async def communicate(self): return b"x" * 20, b"y" * 20
        def kill(self): self.killed=True
    async def spawn(*args, **kwargs): return Proc()
    monkeypatch.setattr("lhas.workspace.safe_cli.asyncio.create_subprocess_exec", spawn)
    cli=SafeCli(LocalReadOnlyWorkspace(tmp_path), CommandPolicy([CommandRule(["fake"], True)]), max_output_bytes=5)
    result,error=asyncio.run(cli.execute(["fake"])); assert error is None and result["stdout_truncated"] and result["stderr_truncated"] and len(result["stdout"]) <= 5
    async def timeout(awaitable, timeout):
        if hasattr(awaitable, "close"): awaitable.close()
        raise asyncio.TimeoutError
    monkeypatch.setattr("lhas.workspace.safe_cli.asyncio.wait_for", timeout)
    result,error=asyncio.run(cli.execute(["fake"])); assert error == "COMMAND_TIMEOUT"

def test_nonzero_observation_can_continue_same_inner_run(tmp_path):
    from types import SimpleNamespace
    from lhas.inner_agent import OpenAIAgentsBackend, AgentsSdkModelConfig
    ws=LocalReadOnlyWorkspace(make_repo(tmp_path)); reg=ToolRegistry(); register_workspace_tools(reg, ws, CommandPolicy([CommandRule(["git", "diff"], True)]))
    class Runner:
        async def run(self, agent, input, **kwargs):
            ctx=SimpleNamespace(tool_call_id="c")
            cli=next(x for x in agent.tools if x.name == "cli.exec")
            observed=await cli.on_invoke_tool(ctx, '{"argv":["git","diff","--bad"]}')
            assert observed["status"] == "SUCCESS" and observed["output"]["exit_code"] != 0
            search=next(x for x in agent.tools if x.name == "workspace.search")
            read=next(x for x in agent.tools if x.name == "workspace.read")
            assert (await search.on_invoke_tool(ctx, '{"query":"return a - b"}'))["status"] == "SUCCESS"
            assert (await read.on_invoke_tool(ctx, '{"path":"src/calculator.py"}'))["status"] == "SUCCESS"
            return SimpleNamespace(final_output="continued", context_wrapper=SimpleNamespace(usage={}))
    from lhas.inner_agent.models import InnerAgentRequest
    result=asyncio.run(OpenAIAgentsBackend(reg, AgentsSdkModelConfig(model="m", api_key="k"), runner=Runner()).run(InnerAgentRequest(task_id="t",run_id="r",attempt_id="a",objective="x",allowed_capabilities=["cli.exec","workspace.search","workspace.read"])))
    assert result.status.value == "SUCCESS"
