# Odys / Hermes boundary

Status: **Compatibility and provenance reference**

Canonical product ownership is defined in `docs/01_ARCHITECTURE.md`. Hermes is architectural inspiration, not a runtime dependency or a product Odys intends to clone.

## Authoritative Odys control plane

The following remain Odys-native and authoritative: Goal, Plan/TaskGraph,
Task, Run, Attempt, `RecoveringOrchestrator`, `RunWorkspaceManager`, durable
workspace, WorkingState, Checkpoint, CP-3, process resume, Validator, Outcome
Arbitration, FailureClassifier, RecoveryPolicy, EventStore, and Eval/Evidence.

There is no `HermesRun`, `HermesTask`, `HermesRecovery`, or parallel checkpoint
model. Agent Platform services call existing repositories and orchestrators.
Every delegated child is a normal Child Task with a normal Run and Attempt.

## Adopted boundaries

- `AgentKernel` is the shared role-neutral loop contract.
- `WorkerAgentKernelAdapter` exposes the existing `InnerAgentExecutor` path
  without deleting or changing its provider semantics.
- `ToolsetRegistry` expands names into the existing canonical `ToolRegistry`.
- MCP tools become ordinary ToolRegistry capabilities through
  `MCPToolAdapter`; agents do not use an MCP side dispatcher.
- `ContextAssembler` composes selected Session/Memory/Knowledge/Skill/Project
  inputs while existing `ContextBuilder` remains the CP-0..CP-3 historical
  reconstruction boundary.
- `RootAgentService` routes long goals into Planner→TaskGraph→Control Plane.
  It does not run an unbounded private long-horizon loop.

## Security and authority

Provider profiles contain no API key field. Session/Event/Delegation records
exclude hidden reasoning, raw provider transcripts, and raw tool arguments.
Child toolsets cannot exceed the parent, default spawn depth is one, and
non-root memory writes are denied. An AgentResult is only a completion claim;
the existing Validator decides Task completion, including existing Outcome
Arbitration behavior.

## Deferred boundaries

Dynamic Macro Replan, distributed/parallel workers, vector databases, remote
MCP HTTP, messaging gateways, browser/dashboard UI, cron, remote execution,
and external exactly-once side effects remain unimplemented.
