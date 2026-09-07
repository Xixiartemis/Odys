# Phase 2 Capability Conformance Matrix

**Generated from**: `7e6dcc227b0ae809a462bdf9a581d0c300f1fde3`
**Date**: 2026-09-07
**Status**: Canonical Phase 2 runtime audit

---

## 1. Matrix Columns

| Column | Type | Description |
|--------|------|-------------|
| `capability_id` | string | Stable ID from `CapabilityDefinition.id` |
| `source` | enum | `odys-runtime`, `mcp:<server>`, or `runtime` |
| `category` | string | Semantic category (workspace, execution, verification, source-control, environment, platform) |
| `explicit_capability_definition` | bool | True if declared in `default_capabilities()` or MCP adapter |
| `model_visible` | bool | Exposed to model via `tool_schemas()` |
| `runtime_available` | bool | Resolved as AVAILABLE in discovery |
| `preferred_tool` | string | Primary backend tool name |
| `backend_registered` | bool | Preferred tool exists in `ToolRegistry` |
| `input_schema` | ref | JSON Schema for input validation |
| `output_schema` | ref | JSON Schema for output validation |
| `permissions` | list | Required permission tuples |
| `risk_level` | enum | LOW / MEDIUM / HIGH |
| `workspace_scope` | enum | SOURCE_WORKSPACE / EXTERNAL |
| `contract_path` | string | `CapabilityRegistry → ToolContract → ToolRegistry → Tool` |
| `fail_closed_missing_backend` | bool | Capability unavailable when preferred_tool missing |
| `fail_closed_invalid_args` | bool | Invalid arguments produce FAILURE, not silent pass-through |
| `typed_tool_result` | bool | Returns `ToolResult` with typed status |
| `typed_evidence` | bool | Produces `ToolEvidence` on success |
| `evidence_capability_identity` | bool | Evidence matches invocation capability_id + tool_name |
| `direct_execute_bypass` | enum | `AUTHORIZED_INTERNAL_BACKEND_ONLY` or `VIOLATION` |
| `platform_support` | list | Supported platforms (WINDOWS, LINUX, MACOS) |
| `test_evidence` | string | Reference to test coverage |
| `status` | enum | PASS / FAIL / NOT_APPLICABLE / NOT_PROVEN |

---

## 2. Authority Rules

### 2.1 CapabilityDefinition = Semantic Authority

`CapabilityDefinition` (from `capability_registry.py`) is the **sole semantic authority** for what the runtime may offer. It declares:
- Identity (`id`, `name`, `category`)
- Schema contract (`input_schema`, `output_schema`)
- Platform binding (`platforms`)
- Permission requirements (`permissions`)
- Risk classification (`risk_level`, `workspace_scope`)
- Tool binding (`preferred_tool`, `fallback_tools`)
- Evidence type (`evidence_type`)

### 2.2 CapabilitySpec = Backend Descriptor Only

`CapabilitySpec` (from `planning/models.py`) is a **lightweight backend descriptor** attached to each concrete `Tool` instance. It carries:
- `name`, `description`, `input_schema`, `output_schema`
- `risk_level`, `side_effect`, `requires_human_approval`
- `origin` (native / mcp), `server_name`

**FORBIDDEN**: Synthesizing a `CapabilityDefinition` from a `CapabilitySpec`. The `_build_runtime_capability_registry()` function in `native/tools.py` explicitly documents this:

```python
def _build_runtime_capability_registry(registry) -> CapabilityRegistry:
    """Build the runtime view from the explicit core catalog only.

    Backend ``CapabilitySpec`` values are never promoted into semantic
    definitions here.  Adapter-specific definitions must be supplied by the
    adapter when constructing its own ``CapabilityRegistry``.
    """
    return CapabilityRegistry(registry, definitions=default_capabilities())
```

### 2.3 CapabilitySpec → CapabilityDefinition Promotion is Forbidden

The P2.3 integration boundary enforces that `CapabilitySpec` values from concrete tools are **never** promoted into `CapabilityDefinition` entries. Only:
1. `default_capabilities()` — the explicit core catalog
2. `mcp_capabilities()` — the MCP adapter conversion

produce `CapabilityDefinition` entries.

---

## 3. Execution Rules

### 3.1 Canonical Invocation Path

```
NativeToolDispatcher
  → CapabilityRegistry.get(capability_id)
  → Policy checks (allowed_capabilities, side_effect, delegation_budget)
  → ToolContract.invoke(request, runtime_context)
    → CapabilityRegistry.discover(context) — availability check
    → Draft202012Validator — input schema validation
    → Semantic argv prefix guard
    → ToolRegistry.resolve(tool_name)
    → tool.execute(request)
    → Draft202012Validator — output schema validation
    → ToolEvidence generation
  → ToolResult
```

### 3.2 Direct Tool.execute() Classification

Any direct `tool.execute()` call outside the `ToolContract.invoke()` path is classified:

| Pattern | Classification |
|---------|---------------|
| `NativeToolDispatcher` → `ToolContract.invoke()` → `tool.execute()` | **AUTHORIZED** |
| `MCPToolAdapter.execute()` (called via ToolContract) | **AUTHORIZED** |
| `tool.execute()` called directly without ToolContract | **VIOLATION** |
| Test fakes (`tools/fakes.py`) | **AUTHORIZED_INTERNAL_BACKEND_ONLY** |

The `NativeToolDispatcher.dispatch()` method enforces this: all execution goes through `self.tool_contract.invoke()`. The docstring explicitly states:

> All normal tool invocations MUST route through `tool_contract.invoke()`.
> Direct `tool.execute()` calls are forbidden — use the contract boundary.

---

## 4. Skills Boundary

### 4.1 Skills Are NOT Executable Capabilities

Skills (from `skills/registry.py`) are **declarative metadata documents** that:
- Discover `SKILL.md` files from configured roots
- Parse frontmatter (`name`, `description`, `required_capabilities`, `optional_capabilities`, `acceptance_contract`)
- Declare which capabilities they **require** or **optionally consume**
- Produce `SkillCapabilityReport` for availability validation

### 4.2 Skills Execute Zero Tools

`SkillRegistry.discover()` reads filesystem metadata only. `SkillRegistry.view()` reads file content only. Neither mutates the `CapabilityRegistry`, executes any `Tool`, or changes task lifecycle.

### 4.3 Skills Consume Capability State

Skills reference capabilities by ID in `required_capabilities` and `optional_capabilities`. The `SkillCapabilityReport` validates whether these are available in a given `CapabilityRegistry`, but does **not** create, modify, or remove any capability.

**EXECUTABLE_CAPABILITY_COUNT_FROM_SKILLS = 0**

---

## 5. MCP Boundary

### 5.1 MCP Discovery → Explicit CapabilityDefinition

The MCP path is:

```
MCPManager.discover_tools() → list[MCPToolInfo]
  → mcp_tool_to_capability(info) → CapabilityDefinition
  → merge_capability_definitions(core, mcp) → single registry
```

### 5.2 MCP Capability ID Pattern

MCP capabilities use the pattern: `mcp.<server>.<remote_tool>`

However, the actual implementation uses `info.name` directly as the capability ID (not a triple-qualified name). The `source` field is set to `mcp:<server_name>` and `category` is set to `mcp.<server_name>`.

### 5.3 MCP Constraints

- **No double prefix**: MCP tools register under `info.name` in both `ToolRegistry` and `CapabilityDefinition`
- **No invented output schema**: MCP tools use `{"type": "object"}` as output schema (the honest declaration since MCP tools/call returns arbitrary JSON)
- **Backend binding**: `preferred_tool = info.name` ensures `MCPToolAdapter` (registered under `info.name`) resolves correctly
- **Collision prevention**: `merge_capability_definitions()` raises `ValueError` if any MCP definition ID collides with a core ID

---

## 6. Platform Boundary

### 6.1 Platform Capabilities in default_capabilities()

Three platform capabilities are declared in the core catalog:

| Capability | Description | Risk | Permissions |
|------------|-------------|------|-------------|
| `platform.prepare` | Prepare bounded platform evidence for a worker step | LOW | `platform.execute` |
| `platform.delegate` | Create a durable child Task, Run and Attempt | HIGH | `platform.delegate` |
| `platform.finalize` | Finalize bounded platform evidence with a reviewer step | LOW | `platform.execute` |

### 6.2 Platform Properties

- All three have **explicit `CapabilityDefinition`** entries in `default_capabilities()`
- All three are **agent-visible** (exposed via `tool_schemas()` when allowed)
- All three cross the **ToolContract** boundary (invoked via `NativeToolDispatcher.dispatch()` → `ToolContract.invoke()`)
- `platform.delegate` has `retryable=False` and risk_level=HIGH
- `platform.prepare` and `platform.finalize` have risk_level=LOW

### 6.3 Platform in NativeToolDispatcher

`platform.delegate` is classified as `SideEffectClass.DELEGATION` in `_side_effect_class()`. It has a delegation budget check:

```python
if call.name == "platform.delegate" and len(snapshot.delegation_dependencies) >= request.budget.max_delegations:
    return self._finish_denied(invocation, "DELEGATION_BUDGET_EXHAUSTED")
```

---

## 7. Capability Conformance Matrix

### 7.1 Static Builtin Capabilities (15 total)

| # | capability_id | category | preferred_tool | risk | permissions | model_visible | status |
|---|---------------|----------|----------------|------|-------------|---------------|--------|
| 1 | `cli.exec` | execution | `cli.exec` | LOW | `process.execute` | YES | PASS |
| 2 | `environment.inspect` | environment | `cli.exec` | LOW | `environment.read` | YES | PASS |
| 3 | `git.diff` | source-control | `cli.exec` | LOW | `process.execute` | YES | PASS |
| 4 | `git.status` | source-control | `cli.exec` | LOW | `process.execute` | YES | PASS |
| 5 | `platform.delegate` | platform | `platform.delegate` | HIGH | `platform.delegate` | YES | PASS |
| 6 | `platform.finalize` | platform | `platform.finalize` | LOW | `platform.execute` | YES | PASS |
| 7 | `platform.prepare` | platform | `platform.prepare` | LOW | `platform.execute` | YES | PASS |
| 8 | `test.run` | verification | `cli.exec` | LOW | `process.execute` | YES | PASS |
| 9 | `workspace.diff` | workspace | `workspace.diff` | LOW | `workspace.read` | YES | PASS |
| 10 | `workspace.edit` | workspace | `workspace.edit` | MEDIUM | `workspace.write` | YES | PASS |
| 11 | `workspace.edit_lines` | workspace | `workspace.edit_lines` | MEDIUM | `workspace.write` | YES | PASS |
| 12 | `workspace.list` | workspace | `workspace.list` | LOW | `workspace.read` | YES | PASS |
| 13 | `workspace.read` | workspace | `workspace.read` | LOW | `workspace.read` | YES | PASS |
| 14 | `workspace.restore` | workspace | `workspace.restore` | MEDIUM | `workspace.write` | YES | PASS |
| 15 | `workspace.search` | workspace | `workspace.search` | LOW | `workspace.read` | YES | PASS |

### 7.2 Platform Capabilities (3 of 15)

The 3 platform capabilities (`platform.prepare`, `platform.delegate`, `platform.finalize`) are a subset of the 15 builtin capabilities, not a separate count.

### 7.3 MCP Capabilities

MCP capabilities are **dynamic** — they depend on configured MCP servers at runtime. The pattern is:
- **ID**: `info.name` (from `MCPToolInfo.name`)
- **Category**: `mcp.<server_name>`
- **Source**: `mcp:<server_name>`
- **Output schema**: `{"type": "object"}` (honest generic)

No MCP capabilities are statically enumerable from the codebase.

### 7.4 Skill-Produced Capabilities

**COUNT = 0** — Skills declare capability requirements but produce no executable capabilities.

---

## 8. Conformance Verification Matrix

| Property | workspace.* | cli.exec | git.* | test.run | environment.inspect | platform.* | mcp.* |
|----------|-------------|----------|-------|----------|--------------------|-----------|-------|
| Explicit CapabilityDefinition | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ (adapter) |
| Model-visible schema | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| CapabilityRegistry.discover() | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| ToolContract.validate_request() | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| ToolContract.invoke() | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Input schema validation | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Output schema validation | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Typed ToolResult | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Typed ToolEvidence | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Evidence identity check | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Semantic argv guard | — | — | ✅ | — | — | — | — |
| Fail-closed missing backend | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Fail-closed invalid args | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| CapabilitySpec ≠ CapabilityDefinition | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

---

## 9. Boundary Violations Audit

### 9.1 No Violations Found

The merged runtime at `7e6dcc2` correctly enforces:

1. **Authority boundary**: `CapabilityDefinition` is the sole semantic authority; `CapabilitySpec` is never promoted
2. **Execution boundary**: All tool calls route through `ToolContract.invoke()`; direct `tool.execute()` is forbidden
3. **Skills boundary**: Skills execute zero tools and produce zero capabilities
4. **MCP boundary**: MCP tools go through explicit `CapabilityDefinition` conversion with collision prevention
5. **Platform boundary**: All platform capabilities have explicit definitions and cross ToolContract

### 9.2 Known Design Decisions (Not Violations)

- `workspace.search` has a generic `{"type": "object"}` output schema (honest: search results vary)
- `workspace.edit_lines` and `workspace.restore` have generic `{"type": "object"}` output schemas
- `cli.exec`, `test.run`, `git.*`, `environment.inspect` have generic `{"type": "object"}` output schemas
- MCP tools always use `{"type": "object"}` output schema (MCP protocol doesn't expose schemas)
- `git.status` and `git.diff` share the `cli.exec` backend with semantic argv prefix guards
- `test.run` also shares the `cli.exec` backend

---

## 10. Summary Counts

| Metric | Count |
|--------|-------|
| BUILTIN_CAPABILITY_COUNT | 15 |
| PLATFORM_CAPABILITY_COUNT (subset) | 3 |
| MCP_CAPABILITY_PATTERN | `info.name` (dynamic, from `mcp.<server>.<remote_tool>` convention) |
| SKILLS_EXECUTABLE_CAPABILITY_COUNT | 0 |
| TOTAL_EXECUTABLE_STATIC_CAPABILITIES | 15 |
| TOTAL_PASS | 15 |
| TOTAL_FAIL | 0 |
| TOTAL_NOT_PROVEN | 0 |
