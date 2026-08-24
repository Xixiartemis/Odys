import asyncio
from types import SimpleNamespace

from lhas.inner_agent import (
    AgentsSdkModelConfig, InnerAgentBackend, InnerAgentExecutor, InnerAgentRequest,
    InnerAgentResult, InnerAgentStatus, OdysAgentRunContext, OdysAgentsRunHooks,
    OpenAIAgentsBackend,
)
from lhas.inner_agent.tool_adapter import allowed_tools
from lhas.inner_agent.trace import InnerAgentTrace
from lhas.tools.fakes import FakeTool
from lhas.tools.protocol import ToolRequest, ToolResult, ToolResultStatus
from lhas.tools.registry import ToolRegistry
from lhas.planning.models import CapabilitySpec


def _request(**kw):
    values = {"task_id": "t", "run_id": "r", "attempt_id": "a", "objective": "x", "allowed_capabilities": []}
    values.update(kw)
    return InnerAgentRequest(**values)


def test_public_inner_agent_exports():
    from lhas.inner_agent import InnerAgentRequest, InnerAgentResult, InnerAgentBackend, InnerAgentExecutor, OpenAIAgentsBackend, OdysAgentRunContext
    assert all(x is not None for x in (InnerAgentRequest, InnerAgentResult, InnerAgentBackend, InnerAgentExecutor, OpenAIAgentsBackend, OdysAgentRunContext))


def test_public_planning_exports():
    from lhas.planning import PlanExecutionService, TaskGraphScheduler, build_step_dependency_context
    assert PlanExecutionService and TaskGraphScheduler and build_step_dependency_context


def test_hook_uses_public_tool_metadata():
    trace = InnerAgentTrace(); hooks = OdysAgentsRunHooks(trace)
    ctx = SimpleNamespace(tool_call_id="call-123"); tool = SimpleNamespace(name="repo.search")
    asyncio.run(hooks.on_llm_start(None, None, None, None)); asyncio.run(hooks.on_llm_end(None, None, None, None))
    asyncio.run(hooks.on_tool_start(ctx, None, tool)); asyncio.run(hooks.on_tool_end(ctx, None, tool, {"status": "SUCCESS"}))
    assert hooks.turn_count == 1 and hooks.tool_call_count == 1
    assert trace.items[-2]["tool_name"] == "repo.search" and trace.items[-2]["tool_call_id"] == "call-123"
    assert trace.items[-1]["tool_call_id"] == "call-123"


def test_tool_observation_excludes_usage_and_preserves_accounting():
    reg = ToolRegistry(); cap = CapabilitySpec(name="safe.a", description="a")
    reg.register(FakeTool(cap, lambda req: ToolResult(status=ToolResultStatus.FAILURE, error_type="NOT_FOUND", error_message="missing", usage={"requests": 2})))
    req = _request(allowed_capabilities=["safe.a"]); trace = InnerAgentTrace()
    tools, _ = allowed_tools(reg, req, trace)
    observed = asyncio.run(tools[0].on_invoke_tool(SimpleNamespace(tool_call_id="call-456", context={}), "{}"))
    assert observed["status"] == "FAILURE" and observed["error_type"] == "NOT_FOUND" and "usage" not in observed
    assert trace.items[-1]["usage"] == {"requests": 2}


def test_tool_adapter_preserves_tool_call_id():
    seen = []
    reg = ToolRegistry(); cap = CapabilitySpec(name="safe.a", description="a")
    reg.register(FakeTool(cap, lambda req: seen.append(req) or {"ok": True}))
    tools, _ = allowed_tools(reg, _request(allowed_capabilities=["safe.a"]))
    asyncio.run(tools[0].on_invoke_tool(SimpleNamespace(tool_call_id="call-456", context={}), "{}"))
    assert seen[0].tool_call_id == "call-456"


class _Provider:
    def __init__(self, **kw): self.kw = kw


class _RunConfig:
    def __init__(self, **kw): self.kw = kw


class _Runner:
    calls = []
    async def run(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return SimpleNamespace(final_output="done", context_wrapper=SimpleNamespace(usage={"requests": 3, "input_tokens": 100, "output_tokens": 40, "total_tokens": 140}))


def _run_backend(mode):
    runner = _Runner(); captured = {}
    def provider(**kw): captured["provider"] = kw; return _Provider(**kw)
    def run_config(**kw): captured["config"] = kw; return _RunConfig(**kw)
    backend = OpenAIAgentsBackend(ToolRegistry(), AgentsSdkModelConfig(model="m", api_key="k", base_url="https://example", api_mode=mode), runner=runner, provider_factory=provider, run_config_factory=run_config)
    result = asyncio.run(backend.run(_request(max_turns=4)))
    return result, captured, runner.calls[-1][1]


def test_provider_responses_routing():
    result, captured, kwargs = _run_backend("responses")
    assert captured["provider"]["use_responses"] is True and captured["provider"]["api_key"] == "k" and captured["provider"]["base_url"] == "https://example"
    assert kwargs["run_config"].kw["tracing_disabled"] is True


def test_provider_chat_completions_routing():
    _, captured, _ = _run_backend("chat_completions")
    assert captured["provider"]["use_responses"] is False


def test_runner_contract_and_context():
    _, _, kwargs = _run_backend("responses")
    assert isinstance(kwargs["context"], OdysAgentRunContext) and kwargs["max_turns"] == 4
    assert kwargs["hooks"] is not None and kwargs["run_config"].kw["trace_include_sensitive_data"] is False


def test_usage_mapping_from_context_wrapper():
    result, _, _ = _run_backend("responses")
    assert result.usage == {"requests": 3, "input_tokens": 100, "output_tokens": 40, "total_tokens": 140}


def test_explicit_empty_allowlist_denies_all():
    captured = []
    class Backend:
        name = "fake"
        async def run(self, request): captured.append(request); return InnerAgentResult(status=InnerAgentStatus.SUCCESS)
    from lhas.executors.protocol import ExecutionRequest
    task = {"objective": "x", "allowed_capabilities": ["safe.a"]}
    asyncio.run(InnerAgentExecutor(Backend(), allowed_capabilities=[]).execute(ExecutionRequest(task_id="t", run_id="r", attempt_id="a", attempt_number=1, task=task)))
    assert captured[0].allowed_capabilities == []
