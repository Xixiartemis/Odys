# Odys Canonical Architecture

Status: **FROZEN**

Canonical freeze: `docs/ARCHITECTURE_FREEZE.md`

Product definition: **Odys is a Reliable & Efficient Long-Horizon Agent Runtime.**

North Star: **Use the least reliability machinery necessary to achieve a verified outcome.**

## Five logical layers

```text
┌──────────────────────────────────────────────────────────┐
│ 1. Product Surfaces                                      │
│ CLI · API · TUI · future Web/Desktop                    │
├──────────────────────────────────────────────────────────┤
│ 2. Capability Runtime                                    │
│ Tools · MCP · Skills · Retrieval · Browser/Web           │
│ Code/Shell · Providers · Sandbox · Delegation adapters  │
├──────────────────────────────────────────────────────────┤
│ 3. Native Minimal Agent Runtime                          │
│ Context → Model → Tool → Observation → State → Next turn│
├──────────────────────────────────────────────────────────┤
│ 4. Verified Workflow Runtime                             │
│ Typed TaskGraph · Preconditions · Acceptance · Evidence  │
│ Dependencies · Selective Repair · Macro Replan           │
├──────────────────────────────────────────────────────────┤
│ 5. Adaptive Reliability Control Plane                    │
│ Task/Run/Attempt · CompletionAuthority · Validation      │
│ Recovery · Checkpoint/Resume · Runtime Truth · Liveness  │
│ Reliability level · Budget · Cost                        │
└──────────────────────────────────────────────────────────┘
```

### Layer 1 — Product Surfaces

CLI, API, TUI, and future Web/Desktop integrations expose intent and durable state. They do not own execution semantics. Consumer chat surfaces and messaging gateways are deferred.

### Layer 2 — Capability Runtime

Answers: **What can the agent do?**

Tools, MCP, Skills, memory retrieval, browser/web/search, code execution, shell, provider adapters, sandbox adapters, and delegation adapters live here. Capabilities are progressively disclosed and provide bounded operations/evidence. They do not own Task lifecycle, workflow truth, completion, or recovery.

### Layer 3 — Native Minimal Agent Runtime

Answers: **How should the current bounded unit of work be executed?**

```text
Context → Model → Tool → Observation → State → next Model turn
```

This layer owns model invocation, tool dispatch, observation, bounded context assembly, in-attempt state, and micro-planning. The executing model chooses reads, searches, commands, patches, and local test loops. Odys must not introduce a second central micro-planner.

### Layer 4 — Verified Workflow Runtime

Answers: **What verified outcome must happen next?**

It owns macro decomposition, dependencies, milestones, preconditions, expected effects, acceptance criteria, evidence, capabilities, risk, workflow budget, checkpoint/recovery policy, selective repair, and macro replan.

```text
PLANNED → READY → RUNNING → CLAIMED_COMPLETE → VERIFIED
```

`CLAIMED_COMPLETE` is non-authoritative. Only externally supported acceptance evidence may produce `VERIFIED`.

Failure repair is local first, then affected-subgraph repair, then macro replan only when necessary.

### Layer 5 — Adaptive Reliability Control Plane

This layer owns Task/Run/Attempt, CompletionAuthority, validation, failure taxonomy and provenance, recovery, checkpoint/resume, Runtime Truth, liveness, reliability level, budget, cost accounting, and durable delegation semantics.

Reliability intensity is selected explicitly and deterministically until controlled ablation justifies a more advanced router.

## Frozen ownership

| Concern | Canonical owner |
| --- | --- |
| Runtime and execution lifecycle | Odys |
| Native model/tool loop | Native Minimal Agent Runtime |
| Macro workflow and verified transitions | Verified Workflow Runtime |
| Micro-planning and individual tool choices | Executing agent/model |
| Capability implementation | Capability Runtime |
| Completion claim | Agent/model as evidence |
| Completion acceptance | CompletionAuthority + Validator |
| Failure provenance and recovery | Adaptive Reliability Control Plane |
| Durable delegation semantics | Odys |

> Odys decides what verified outcome must be achieved next; the agent decides how to achieve it.

## Compatibility boundary

The historical Outer Runtime / Inner Agent boundary remains a compatibility and baseline integration boundary, but is not the desired final runtime ownership model. Existing `AgentExecutor`, `InnerAgentExecutor`, and `InnerAgentBackend` code may remain during migration.

Odys must not become:

```text
Odys full runtime
      ↓
another full agent runtime
      ↓
model
```

Avoid competing planner, retry, context, completion, and persistence authorities.

## Adaptive levels

| Level | Contract |
| --- | --- |
| FAST | Native Minimal Agent Runtime + Runtime Truth + basic budget/events |
| GUARDED | FAST + CompletionAuthority + independent validation |
| DURABLE | GUARDED + Verified Workflow + checkpoint + failure provenance + selective recovery + macro replan + liveness |
| MULTI_AGENT | DURABLE + durable delegation + dependency scheduling + child ownership + result verification |

Every task uses the least reliability machinery sufficient for a verified outcome. Do not build an ML/RL router before controlled ablation.

## Durable state and model context

> Durable State != Prompt State.

Odys may preserve complete events, workflow state, attempts, failures, checkpoints, memory, and evidence. Each model turn receives only the current goal/step, relevant verified state and evidence, relevant failure context and memory, and currently allowed capabilities. Progressive disclosure and selective context projection are mandatory defaults.

## Runtime Truth and liveness

Runtime Truth records configured and effective provider/model/route identity without exposing credentials. Liveness is durable business-progress evidence, not proof of process ownership. `ACTIVE` means recent durable progress; any in-flight operation is last-known unless a lease/fencing mechanism proves current ownership.

## Completion and failure lineage

```text
Executor terminal failure
        ↓
Attempt.error_type
        ↓
FailureReport.failure_type
        ↓
RecoveryPolicy input
```

Mappings must be explicit. Specific causes must not silently collapse into generic failures such as `EMPTY_RESULT`.

## Open-source boundary

Provider SDKs, the official MCP SDK, OpenTelemetry, and container primitives are directly reusable behind Odys adapters. LangGraph, Temporal, LongHorizon-Harness, Pi, and Hermes are references, baselines, foreign executors, or future deployment adapters—not canonical Odys core owners. See ADR 0002.

## Metrics

Primary success metric: **Verified Completion Rate**.

Primary efficiency metric: **Cost per Verified Completion**.

Repeated Reliability / `pass^k` and workflow metrics defined in `docs/ARCHITECTURE_FREEZE.md` are tracked when available. Missing values remain `NOT_MEASURED`.

## Architecture change rule

Major changes require a reproducible limitation, evidence the frozen architecture cannot reasonably support it, benchmark/production evidence, alternatives, migration cost, a superseding ADR, and claim-impact analysis.

> EXTEND, DO NOT REWRITE.
