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
    assert HARNESS_VERSION == "HV-0.8"

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
