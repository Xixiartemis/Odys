# Capability Registry V1

Status: **Phase 2 foundation**. This document defines the first narrow
capability catalog; it does not authorize a live model run or change the
command policy.

## Ownership and boundary

The Capability Registry belongs to the Odys Runtime. A capability describes
what an agent may request, while a Tool describes how Odys performs that
request. The existing `ToolRegistry` remains the owner of concrete Tool
instances and execution. The V1 registry only declares metadata, discovers
availability, and resolves an explicit binding to an existing tool.

This distinction prevents a second execution path. For example,
`test.run`, `git.status`, `git.diff`, and `environment.inspect` describe
different operations but bind to the existing `cli.exec` tool. Workspace
capabilities bind to the existing workspace tools.

## V1 catalog

The stable IDs are:

`workspace.read`, `workspace.list`, `workspace.edit`, `workspace.diff`,
`test.run`, `git.status`, `git.diff`, and `environment.inspect`.

Every definition carries its category, version, JSON input/output schemas,
supported platforms, permissions, risk level, workspace scope, timeout,
retry behavior, preferred/fallback tools, availability, source, and evidence
type. These are declarations, not a new policy or execution authority.

## Discovery and availability

`CapabilityRegistry.list_all()`, `list_available(context)`, and
`get(capability_id)` are the runtime-facing interface. `discover(context)`
provides the context-specific status record used by diagnostics. A concrete
runtime supplies its normalized platform and the names exposed by its
existing `ToolRegistry`.

Known Windows, Linux, and macOS contexts are filtered against the declared
platform set and available tool bindings. A missing backend is
`UNAVAILABLE` with a structured reason (`MISSING_TOOL_BINDING`). An unknown
platform is `UNKNOWN` and returns no available capabilities; it never falls
back to the complete catalog.

No executable discovery is performed. Registration is explicit and
deterministic, and repeated discovery with the same context produces the
same ordered result.

## Safety and future consumers

Permissions, risk, and `SOURCE_WORKSPACE` scope are explicit in each V1
definition. `test.run` remains subject to the existing process execution and
command allowlist. The registry does not modify `COMMAND_NOT_ALLOWED`, tool
schemas, budgets, validation, completion authority, recovery, or persistence.

Future MCP, Skills, and employee/agent integrations should consume this same
registry rather than creating parallel capability models. They may add
explicit registrations later, but V1 contains only the eight capabilities
above. No new runtime, planner, metrics authority, or web framework is
introduced here.
