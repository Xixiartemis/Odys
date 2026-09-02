# Odys Architecture Freeze

Status: **ACCEPTED / FROZEN**

Task: `ODYS-ARCHITECTURE-FREEZE-01`

This document freezes Odys's long-term architecture and development order. Future implementation work must conform to these decisions unless reproducible benchmark or production evidence justifies an ADR-level change.

## Product definition and North Star

Odys is **a Reliable & Efficient Long-Horizon Agent Runtime**.

> Use the least reliability machinery necessary to achieve a verified outcome.

Research objective:

> Can task-adaptive, verifier-backed workflow control improve reliable long-horizon completion and recovery while approaching lightweight-runtime cost on easy tasks and always-on-harness reliability on difficult tasks?

Primary metrics:

- **Verified Completion Rate**
- **Cost per Verified Completion**
- **Repeated Reliability / pass^k**

Odys is not primarily a wrapper around another full agent, a clone of Hermes or Pi, a LangGraph or Temporal application, an evaluation-only harness, or a consumer chatbot.

## Frozen runtime ownership

> Odys owns execution semantics.

Odys must remain the owner of:

- Task, Run, and Attempt lifecycle;
- the Native model/tool/observation loop;
- workflow state and verified transitions;
- CompletionAuthority and Validator;
- failure provenance and classification;
- recovery, selective repair, and macro replan;
- checkpoint/resume semantics;
- Runtime Truth and liveness;
- budget and cost accounting;
- durable delegation semantics.

No dependency may silently become the owner of these semantics.

## Canonical five-layer architecture

### Layer 1 — Product Surfaces

CLI, API, TUI, and future Web/Desktop integrations. This layer presents intent and state; it does not own execution semantics.

### Layer 2 — Capability Runtime

Answers: **What can the agent do?**

Contains tools, MCP, Skills, memory retrieval, browser, web/search, code execution, shell, sandbox adapters, provider adapters, and delegation adapters. Capabilities are progressively disclosed; every tool, Skill, memory, and capability must not be injected into every request.

### Layer 3 — Native Minimal Agent Runtime

Answers: **How should the current bounded unit of work be executed?**

```text
Context → Model → Tool → Observation → State → next Model turn
```

The executing model owns micro-planning: which file to read, symbol to search, command to run, or patch to make. Odys must not add a second central micro-planner.

### Layer 4 — Verified Workflow Runtime

Answers: **What verified outcome must happen next?**

This layer owns macro orchestration. A canonical typed TaskGraph node should evolve toward:

```text
step_id
goal
dependencies
preconditions
expected_effects
acceptance_criteria
evidence
capabilities
risk_class
budget
checkpoint_policy
recovery_policy
status
```

Canonical transition:

```text
PLANNED → READY → RUNNING → CLAIMED_COMPLETE → VERIFIED
```

Failure and repair:

```text
RUNNING / CLAIMED_COMPLETE
        ↓
FAILED
        ↓
Failure provenance
        ↓
Local repair
        ↓
Affected-subgraph repair
        ↓
Macro replan only when necessary
```

> Agent says done != Workflow VERIFIED.

Only externally supported acceptance evidence may move a required node to `VERIFIED`.

### Layer 5 — Adaptive Reliability Control Plane

Owns Task/Run/Attempt, CompletionAuthority, validation, failure taxonomy, recovery, checkpoint/resume, Runtime Truth, liveness, reliability level, cost, and budget. Reliability intensity becomes adaptive only after controlled ablation evidence exists.

## Planning ownership

There are exactly two planning scales:

- **Macro planning — Odys owned:** decomposition, dependencies, milestones, acceptance, workflow repair, and macro replan.
- **Micro planning — agent owned:** grep, read, edit, command execution, test runs, and traceback inspection.

> Odys decides what verified outcome must be achieved next; the agent decides how to achieve it.

Do not create `Outer Planner → Inner Planner → Subagent Planner` as competing planning authorities.

## Compatibility boundary

The historical Outer Runtime / Inner Agent boundary remains a compatibility and baseline integration boundary, but is not the desired final runtime ownership model. Existing `InnerAgentExecutor`, `InnerAgentBackend`, and `AgentExecutor` code may remain during migration.

Target:

```text
Odys Runtime
      │
 ┌────┴──────┐
 │           │
Workflow   Capabilities
 │           │
 └────┬──────┘
      ↓
Native Agent Loop
```

Odys must not become a full runtime delegating its semantics to another full runtime.

## Open-source reuse policy

| Project/component | Strategy | Permitted ownership |
| --- | --- | --- |
| Official provider SDKs | REUSE DIRECTLY as transport adapters | HTTP, authentication transport, streaming, provider protocol |
| Official MCP Python SDK | REUSE DIRECTLY behind an Odys adapter | MCP wire protocol only |
| OpenTelemetry | REUSE DIRECTLY for export/integration | Observability export, never execution truth |
| Docker/container primitives | REUSE DIRECTLY through sandbox adapters | OS isolation mechanics |
| LangGraph | ADAPTER/REFERENCE/BASELINE ONLY | Never canonical Workflow Runtime |
| Temporal | FUTURE DEPLOYMENT ADAPTER ONLY | Never current research-core semantics |
| LongHorizon-Harness | REFERENCE/BASELINE ONLY | Never Odys reliability core |
| Pi | LIGHTWEIGHT BASELINE / FOREIGN EXECUTOR / REFERENCE | Never Odys NativeAgentKernel |
| Hermes | CAPABILITY REFERENCE / EXTERNAL BASELINE / FOREIGN EXECUTOR | Never Odys execution lifecycle |

Provider SDKs must not own Task lifecycle, completion, recovery, workflow, or Runtime Truth.

Canonical MCP path:

```text
Official MCP SDK
      ↓
Odys MCP Adapter
      ↓
Capability Registry
      ↓
Policy / risk metadata
      ↓
Native Tool Dispatcher
```

Do not implement the MCP wire protocol manually.

Canonical telemetry distinction:

```text
EventStore = durable execution truth
OpenTelemetry = observability/export
```

Critical Task/Workflow truth must not be reconstructed only from spans.

Sandbox/container runtimes own isolation mechanics. Odys owns workspace policy, capability policy, protected paths, and side-effect classification.

## Evidence-safe competitor positioning

- **Pi:** a durable lightweight agent harness/runtime; use it as a serious lightweight baseline and architecture reference.
- **Hermes:** an assistant-capable runtime with durable multi-agent workflow/Kanban; use it as a capability reference, external baseline, or foreign executor.
- **Odys:** researches verifier-backed workflow transitions, failure provenance, selective recovery, adaptive reliability intensity, and cost per verified completion as first-class runtime semantics.

Competitor capabilities must be described from current evidence. Unsupported absence claims and single-feature uniqueness claims are prohibited, as are superiority claims without paired evidence.

## Skill architecture

A Skill is richer than a prompt file:

```text
Skill
= Procedural Knowledge
+ Capability Declaration
+ Optional Workflow Template
+ Acceptance Contract
```

A Skill may declare procedures, required capabilities, preconditions, acceptance criteria, and bounded recovery guidance. It must not create a competing runtime.

## Context architecture

> Durable State != Prompt State.

Odys may persist the full EventStore, TaskGraph, Attempt history, FailureReports, checkpoints, memory, and evidence. Each model turn receives only the current goal and workflow step, relevant verified state/evidence/failure context/memory, and currently allowed capabilities.

Long-context pressure is solved through selective context projection, not deletion of durable history.

## Adaptive reliability levels

- **FAST:** Minimal Agent Runtime + Runtime Truth + basic budget/events.
- **GUARDED:** FAST + CompletionAuthority + independent validation.
- **DURABLE:** GUARDED + Verified Workflow + checkpoint + failure provenance + selective recovery + macro replan + liveness.
- **MULTI_AGENT:** DURABLE + durable delegation + dependency scheduling + child ownership + result verification.

Initial selection policy must be explicit and deterministic. Do not build an ML/RL router before ablation evidence exists.

## Frozen development order

### Phase 0 — Architecture Freeze

Documentation only. This phase is completed by this freeze.

### Phase 1 — Basic Native Vertical Slice

```text
Model
→ Tool
→ Observation
→ Multiple Turns
→ Completion Claim
→ CompletionAuthority
→ Validator
→ VERIFIED
```

This is the immediate engineering gate. Do not add major capability systems until the real native chain passes live.

### Phase 2 — Minimum Capability Parity

Implement only what controlled comparison requires: P0 read/write/edit/shell, P1 official MCP adapter, P2 Skill loading/progressive disclosure, P3 compaction/selective context, and P4 token/cost accounting. Browser, memory, and delegation wait for benchmark demand.

### Phase 3 — Verified Workflow V1

Implement a typed TaskGraph with dependencies, preconditions, acceptance, evidence, verified transitions, local repair, affected-descendant invalidation, and macro replan. Remain HTN-compatible, but do not build a full HTN solver.

### Phase 4 — Controlled Ablation

Compare FAST vs GUARDED vs STATEFUL/WORKFLOW vs DURABLE under the same model, task, tools, environment, validator, and budget policy. Stop adding framework features during the ablation.

### Phase 5 — Adaptive Reliability

Only after Phase 4. Begin with deterministic features such as estimated horizon, dependency depth, statefulness, verification availability, side-effect risk, and delegation need. No ML/RL initially.

### Phase 6 — Capability Expansion

Add memory, browser, web/search, delegation, richer Skills, and sandbox backends only from benchmark or product demand.

### Phase 7 — Productionization

Only after research semantics stabilize. Reconsider Temporal backends, distributed workers, PostgreSQL, queues, multi-tenant services, Web/Desktop, and gateway integrations as deployment questions that do not redefine core semantics.

## Research metrics

Workflow metrics include:

```text
dependency_violation_rate
stale_plan_execution_rate
verified_transition_rate
false_completion_rate
repair_scope
lost_work_after_failure
duplicate_side_effect_rate
checkpoint_recovery_rate
workflow_completion_ratio
control_overhead_ratio
```

Unavailable values remain `NOT_MEASURED`. Never fabricate metrics.

## Non-goals until Phase 7

No full consumer assistant clone; Telegram/Discord ecosystem; custom MCP wire protocol; custom tracing protocol; custom container runtime; LangGraph or Temporal migration; Pi kernel embedding; Hermes AIAgent embedding; RL reliability router; full HTN planner; distributed microservices rewrite; or language rewrite.

## Architecture change policy

A core change requires:

1. a reproducible limitation or failure;
2. evidence that the frozen architecture cannot reasonably support it;
3. benchmark or production evidence;
4. alternatives considered;
5. migration cost;
6. an ADR;
7. explicit claim impact.

A cleaner-looking framework, popular library, model suggestion, or speculative feature is insufficient.

> EXTEND, DO NOT REWRITE.
