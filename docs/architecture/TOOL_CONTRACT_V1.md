# Tool Contract V1

Status: **Phase 2 foundation**.

Tool Contract V1 fixes the boundary between the existing Capability Registry
and the existing Tool Registry. It does not introduce a second runtime,
planner, retry loop, durable state machine, or persistence schema.

## Concepts

- **Capability = WHAT** an agent requests, such as `workspace.read` or
  `test.run`.
- **Tool = HOW** Odys performs that request. Concrete tools remain owned by
  `ToolRegistry`.
- **ToolRequest = invocation contract**, carrying identity, arguments,
  workspace/context reference, timeout, and metadata.
- **ToolResult = execution result**, with status, output, artifacts, usage,
  error fields, metadata, and optional `ToolEvidence`.
- **ToolEvidence = execution evidence**, identifying the capability, concrete
  tool, source, summary, and artifact references. It is not
  `CompletionAuthority` evidence.

Tool `SUCCESS` is an observation/result and does not mean `Task COMPLETED`.
The existing Validator and CompletionAuthority remain the completion boundary.

## Invocation contract

`ToolRequest` now carries the following V1 fields:

`tool_call_id`, `task_id`, `run_id`, `attempt_id`, `capability_id`,
`tool_name`, `arguments`, `context`, `workspace_ref`, `timeout_seconds`, and
`metadata`.

The legacy `capability` field remains synchronized for existing callers. New
contract callers provide both `capability_id` and `tool_name`; mismatched
identity is rejected.

Before `Tool.execute()` the contract deterministically:

1. requires invocation identity;
2. resolves the capability and fail-closes unknown capabilities;
3. requires known platform and an actual backend in the concrete
   `ToolRegistry`;
4. checks that the requested tool is an allowed declared binding;
5. validates `arguments` against the capability JSON Schema; and
6. computes `effective_timeout = min(requested, capability limit)` or the
   capability limit when no request timeout is supplied.

The mature `jsonschema` validator is reused for input and output schemas.
Schema diagnostics contain only bounded path/validator information, not the
invalid raw value.

After one existing Tool call, a successful output is validated against the
capability output schema. A mismatch becomes `OUTPUT_VALIDATION_FAILED`, with
safe diagnostics and the original result's artifacts/usage retained. Existing
failure and approval statuses remain unchanged, including concrete
`COMMAND_NOT_ALLOWED` behavior from Safe CLI.

## Error taxonomy

The stable V1 contract codes are:

`INVALID_ARGUMENT`, `CAPABILITY_UNAVAILABLE`, `TOOL_NOT_FOUND`,
`POLICY_REJECTED`, `PERMISSION_DENIED`, `TIMEOUT`, `EXECUTION_FAILED`,
`OUTPUT_VALIDATION_FAILED`, `CANCELLED`, and `INTERNAL_ERROR`.

Existing concrete tool codes such as `COMMAND_NOT_ALLOWED`,
`WORKSPACE_PATH_ESCAPE`, `COMMAND_TIMEOUT`, `SPAWN_ERROR`, and `TOOL_ERROR`
remain representable and are not replaced by string matching or broad
normalization.

The contract exposes `retry_allowed` and `retry_reason` from the capability's
`retryable` declaration. It does not perform retries; Recovery/Orchestrator
remain responsible for that decision.

## Binding and availability

The invocation path is:

```text
Agent
  ↓
Capability request
  ↓
CapabilityRegistry
  ↓
availability + binding
  ↓
Tool Contract validation
  ↓
ToolRegistry
  ↓
Tool.execute()
  ↓
output validation
  ↓
ToolResult + ToolEvidence
  ↓
Validator / CompletionAuthority (existing boundary)
```

`CapabilityRegistry` remains non-executing. Declaration-only discovery may
use its static catalog for diagnostics, but `ToolContract` always requires a
concrete `ToolRegistry` and derives runtime availability from that registry.
Unknown platforms and missing backends fail closed. A request cannot name a
different tool than the capability's explicit preferred/fallback binding.

The contract does not move command allowlists into the capability layer.
`cli.exec` and Safe CLI continue to enforce the existing command policy, so a
dangerous or unallowlisted command remains `COMMAND_NOT_ALLOWED`.

`CapabilitySelectionMetrics` remains the only deterministic selection
observation helper. Tool Contract adds no second metrics authority or
persistence path.
