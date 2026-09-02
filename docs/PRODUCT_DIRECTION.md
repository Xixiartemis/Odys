# Odys Product Direction

Status: **Canonical**

Architecture authority: `docs/ARCHITECTURE_FREEZE.md`

## North Star

Odys is a Reliable & Efficient Long-Horizon Agent Runtime: capable enough to perform real assistant and agent tasks, while progressively enabling stronger reliability mechanisms only when task complexity requires them.

The product objective is **high verified completion rate at low cost per verified completion**, not minimum raw token usage.

## Non-goals

Odys is not currently trying to:

- clone Hermes as a product or reproduce every assistant surface;
- prioritize Telegram, Discord, WhatsApp, desktop UX, avatars, consumer chat, or a gateway ecosystem;
- centrally plan every model/tool action;
- activate the full durability harness for every task;
- build speculative infrastructure without a reproducible requirement;
- claim superiority over Pi or Hermes without paired live evidence;
- claim production readiness, arbitrary-workflow reliability, or token optimality.

## Architecture ownership

The canonical five logical layers are defined in `docs/01_ARCHITECTURE.md`:

1. Product Surfaces
2. Capability Runtime
3. Native Minimal Agent Runtime
4. Verified Workflow Runtime
5. Adaptive Reliability Control Plane

Odys owns execution lifecycle and semantics. Verified Workflow owns macro workflow state and transitions; Adaptive Reliability owns validation, failure provenance, recovery, truth, liveness, and cost. Capabilities provide operations and evidence. The executing agent owns micro-planning.

> Odys decides what outcome must be achieved next; the agent decides how to achieve it.

The historical Outer Runtime / Inner Agent architecture is a migration compatibility boundary, not the final ownership model.

## Adaptive Harness levels

- **FAST (Level 0):** minimal model/tool loop, Runtime Truth, lightweight events and budget.
- **GUARDED (Level 1):** FAST plus Completion Claim and Validator/CompletionAuthority.
- **DURABLE (Level 2):** macro plan, workflow, checkpoints, recovery, replan, liveness, and durable state.
- **MULTI_AGENT (Level 3):** DURABLE plus durable child lifecycle, dependency scheduling, result delivery, failure propagation, and macro replan.

Every task uses the cheapest sufficient level. Explicit policy selects the level first; an ML routing classifier is deferred until evidence justifies it.

## Capability roadmap

Capabilities are prioritized by benchmark and task demand:

| Priority | Capability |
| --- | --- |
| P0 | read, write, edit, shell/bash |
| P1 | generic provider abstraction, official MCP adapter |
| P2 | Skills and progressive disclosure |
| P3 | context compaction and selective context |
| P4 | token and cost accounting |
| Later | memory, browser/web/search, delegation, richer Skills, sandbox backends when benchmarks require them |

Pi is a durable lightweight agent harness/runtime and a serious lightweight baseline, foreign-executor target, and architecture reference. Hermes is an assistant-capable runtime with durable multi-agent workflow/Kanban and is a capability reference, external baseline, and foreign-executor target. Neither is an Odys core runtime dependency.

Odys researches verifier-backed workflow transitions, failure provenance, selective recovery, adaptive reliability intensity, and cost per verified completion as first-class runtime semantics. This is a research focus, not a superiority claim.

## Context philosophy

> Durable State != Prompt State

Durable state may include workflow, events, memory, checkpoints, validation and failure evidence. A context-selection stage sends only relevant working context to the model. Full conversations, histories, workflows, tools, Skills, memories, and failures are not loaded on every turn. Progressive disclosure is the default.

## Benchmark strategy

Future paired comparisons target Pi, Hermes, and Odys with, where technically possible:

- the same model and provider;
- the same task and repository;
- the same tools/capabilities;
- the same validation criteria;
- similar budgets and fault boundaries.

The research question is:

> Can adaptive reliability controls improve long-horizon verified completion while keeping cost per verified completion close to lightweight agent runtimes?

Expected metrics include task success, validator acceptance, tokens, model/tool calls, dead-end turns, recovery rate, false completion, stale-plan execution, duplicate side effects, human intervention, wall time, and cost per verified completion. Missing metrics remain `NOT_MEASURED`.

No superiority claim is permitted before paired live evidence exists.

## Efficiency objective

Primary success metric: **Verified Completion Rate**.

Primary efficiency metric: **Cost per Verified Completion**:

```text
total execution cost / validator-accepted tasks
```

Raw token reduction is an input metric, not the optimization target. When available, measurements distinguish fresh input tokens, cached input tokens, output tokens, model calls, tool calls, dead-end turns, redundant tool calls, recovery turns, human interventions, wall time, and provider cost.

Repeated Reliability / `pass^k` is also a frozen primary research metric. Missing measurements remain `NOT_MEASURED`.

## Development gates

### Phase 0 — Architecture Freeze

Freeze architecture, ownership, reuse policy, workflow semantics, reliability levels, and development order. Documentation only.

### Phase 1 — Basic Native Vertical Slice

Prove:

```text
Model → Tool → Observation → Multiple Turns → Completion Claim → CompletionAuthority → Validator → VERIFIED
```

This is the immediate engineering gate. Do not build a full Hermes-style Capability Layer before it passes live.

### Phase 2 — Minimum Capability Parity

Add only controlled-comparison requirements: read/write/edit/shell, official MCP adapter, Skill loading/progressive disclosure, selective context, and token/cost accounting.

### Phase 3 — Verified Workflow V1

Implement typed TaskGraph dependencies, preconditions, acceptance, evidence, verified transitions, local repair, affected-descendant invalidation, and macro replan. Remain HTN-compatible without building a full HTN solver.

### Phase 4 — Controlled Ablation

Compare FAST vs GUARDED vs STATEFUL/WORKFLOW vs DURABLE under the same model, task, tools, environment, validator, and budget policy.

### Phase 5 — Adaptive Reliability

After ablation evidence, route deterministically using horizon, dependency depth, statefulness, verification availability, side-effect risk, and delegation need. No ML/RL initially.

### Phase 6 — Capability Expansion

Add memory, browser, web/search, delegation, richer Skills, and sandbox backends from benchmark or product demand.

### Phase 7 — Productionization

Only after research semantics stabilize, reconsider Temporal backends, distributed workers, PostgreSQL, queues, multi-tenant services, Web/Desktop, and gateway integrations without redefining core semantics.

## Development rule

> Benchmark-driven development after the basic Native vertical slice.

New reliability or capability mechanisms should normally require:

1. a reproducible task or failure;
2. baseline evidence;
3. a mechanism hypothesis;
4. implementation;
5. deterministic regression;
6. a paired live experiment;
7. a measured result.

Do not build speculative infrastructure without a task or failure that requires it.

## Current claim boundary

Allowed now:

- Odys has a Native Agent Kernel.
- Odys explicitly models durable Task/Run/Attempt state.
- Odys has CompletionAuthority and validation mechanisms.
- Odys has failure classification and recovery mechanisms.
- Odys has Runtime Truth and liveness mechanisms.
- Odys is building toward adaptive reliable long-horizon execution.

Not allowed without future live evidence:

- Odys outperforms Pi or Hermes.
- Odys solves long-horizon agents.
- Odys is production-ready or token-optimal.
- Odys reliably completes arbitrary workflows.
