# Hermes → Odys architecture map

Status: **Historical upstream research reference.** Hermes is architectural inspiration, not an Odys runtime dependency; canonical ownership is defined in `docs/01_ARCHITECTURE.md`.

Baseline: NousResearch/hermes-agent at
`3f315e46fede84ed4e6c8cfdbd00a13618e68986` (MIT).

| Hermes subsystem | Upstream source files reviewed | Odys target | Treatment |
|---|---|---|---|
| General agent loop | `run_agent.py`, `agent/conversation_loop.py`, `agent/agent_init.py` | `src/lhas/agent/kernel.py`, `models.py` | REIMPLEMENTED |
| Prompt/context construction and compression | `agent/prompt_builder.py`, `agent/context_compressor.py`, `agent/prompt_caching.py` | `src/lhas/agent/context.py`; existing `context_builder.py`, `checkpoint.py` | REIMPLEMENTED; Odys CP-0..CP-3 retained |
| Provider runtime resolution | `providers/base.py`, `providers/__init__.py` | `src/lhas/agent/provider.py`; existing `inner_agent/provider_compat.py` | REIMPLEMENTED + ADAPTER boundary |
| Tool registry/toolsets | `tools/registry.py`, `toolsets.py` | existing `src/lhas/tools/registry.py`; new `agent/toolsets.py` | REIMPLEMENTED around Odys-native registry |
| Skill discovery/progressive loading | `agent/skill_utils.py`, `agent/prompt_builder.py`, `tools/skills_tool.py` | `src/lhas/skills/`, `.odys/skills/` | REIMPLEMENTED |
| Memory provider boundary | `agent/memory_provider.py`, `tools/memory_tool.py` | `src/lhas/memory.py` | REIMPLEMENTED |
| Session storage and lexical search | `hermes_state.py`, `hermes_state_schema.py`, `hermes_state_search.py` | `platform_models.py`, `persistence/platform_repositories.py` | REIMPLEMENTED in existing Odys SQLite |
| MCP stdio discovery/calls | `tools/mcp_tool.py`, `tools/mcp_schema_cache.py` | `src/lhas/mcp/` | REIMPLEMENTED as bounded JSON-RPC stdio |
| Subagent delegation | `tools/delegate_tool.py`, `tools/async_delegation.py`, `tools/delegation_output_schema.py` | `src/lhas/agent/delegation.py` | REIMPLEMENTED with durable Child Task/Run/Attempt |
| Plugin architecture | `hermes_cli/agent_plugins.py`, `plugins/plugin_storage.py`, `plugins/plugin_utils.py` | future plugin adapter boundary; MCP metadata includes origin | REFERENCE_ONLY |
| Hermes CLI/gateway/dashboard/messaging | `cli.py`, `gateway/`, `tui_gateway/`, messaging plugins | none | REFERENCE_ONLY / explicitly excluded |

Totals for this phase:

- COPIED: 0 Hermes source files
- MODIFIED: 0 Hermes source files
- REIMPLEMENTED: 9 subsystem concepts
- REFERENCE_ONLY: plugin architecture and excluded surfaces
