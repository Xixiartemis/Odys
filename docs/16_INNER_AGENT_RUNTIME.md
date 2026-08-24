# Odys Inner Agent Runtime (Phase E2)

Odys keeps the outer runtime authoritative: `Goal → Macro TaskGraph → Subgoal → InnerAgentExecutor → InnerAgentBackend → Agent SDK loop → Odys ToolRegistry → ToolResult → final claim → Outer Validator`. The inner backend may use multiple model turns and tool calls, but one outer Attempt remains one complete inner run.

`InnerAgentBackend` and its request/result models are provider-neutral. `ScriptedInnerAgentBackend` supports deterministic offline tests; `OpenAIAgentsBackend` is an optional OpenAI Agents SDK implementation. SDK tools are dynamically built only from the allow-listed, non-side-effect Odys capabilities. Tool failures are structured observations rather than immediate outer failures. The outer Validator still decides task completion.

The SDK backend requires `ODYS_AGENT_MODEL`; optional configuration includes `ODYS_AGENT_API_KEY`, `ODYS_AGENT_BASE_URL`, `ODYS_AGENT_API_MODE`, and `ODYS_AGENT_SDK_TRACING` (default false). No secrets or hidden reasoning are persisted. Workspace, shell, browser, MCP, memory, multi-agent, and dynamic macro replan remain out of scope.
