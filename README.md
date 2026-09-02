# Odys — Reliable & Efficient Long-Horizon Agent Runtime

Odys is an adaptive agent runtime for reliably completing long, complex tasks with verifiable outcomes and controlled execution cost.

> Odys is a reliable and efficient runtime for long-horizon AI agents. It combines a minimal model–tool loop with extensible capabilities and progressively activates validation, checkpointing, recovery, replanning, and durable workflow execution only when a task requires them. Its optimization target is not minimum raw token usage, but low cost per verified completion.

Odys combines five logical architectural layers:

```text
Product / Interface
        ↓
Capability Layer
        ↓
Minimal Agent Runtime
        ↓
Verified Workflow Runtime
        ↓
Adaptive Reliability Control Plane
```

- **Product / Interface** — CLI, API, TUI, and future integrations. Product surfaces are not the current differentiation.
- **Capability Layer** — tools, MCP, Skills, memory, browser/search, code and shell execution, providers, sandboxing, and delegation capabilities. Capabilities are progressively loaded rather than injected into every model context.
- **Minimal Agent Runtime** — `Context → Model → Tool → Observation → State → Next turn`. It owns model invocation, tool calling, context assembly, session state, compaction, and micro-planning.
- **Verified Workflow Runtime** — typed workflow state, dependencies, preconditions, acceptance, evidence, verified transitions, selective repair, and macro replan.
- **Adaptive Reliability Control Plane** — Task/Run/Attempt, CompletionAuthority, validation, failure provenance, recovery, checkpoint/resume, Runtime Truth, liveness, durable delegation, budgets, and cost accounting.

## Ownership

> Odys decides what outcome must be achieved next; the agent decides how to achieve it.

Odys owns execution lifecycle, macro workflow decomposition, acceptance criteria, and completion authority. The executing agent owns micro-planning such as reading files, searching symbols, running commands, editing code, and rerunning tests. Odys does not centrally plan every tool call.

The historical Outer Runtime / Inner Agent architecture remains a compatibility boundary during migration, but it is not the desired final ownership model. Capabilities do not own a competing long-horizon runtime.

## Adaptive Harness

Every task should use the cheapest sufficient reliability level:

| Level | Mode | Runtime contract |
| --- | --- | --- |
| 0 | **FAST** | Minimal loop + Runtime Truth + lightweight events/budget |
| 1 | **GUARDED** | FAST + completion claim + Validator / CompletionAuthority |
| 2 | **DURABLE** | Macro plan + workflow + checkpoint + recovery + replan + liveness + durable state |
| 3 | **MULTI_AGENT** | DURABLE + durable child lifecycle + dependency scheduling + failure propagation |

The full reliability control plane is not activated by default. A validator-accepted GUARDED task finishes immediately; stronger recovery activates only when evidence requires it.

## Capability strategy

Capabilities are added according to benchmark and user requirements, not to clone another assistant product:

1. P0 — read, write, edit, shell/bash
2. P1 — generic provider abstraction and MCP
3. P2 — Skills with progressive disclosure
4. P3 — context compaction and retrieval
5. P4 — durable memory
6. P5 — delegation
7. P6 — browser, web, and search

Telegram, Discord, WhatsApp, avatars, consumer-chat UX, desktop-assistant UX, and a large gateway ecosystem are deferred unless a measured task requires them.

## Context and efficiency

> Durable State != Prompt State

Odys persists rich workflow, event, memory, and recovery state, then selects only relevant working context for the model. Entire conversations, event histories, workflows, Skills, tools, memories, and failure histories must not be injected on every turn.

The primary success metric is **Verified Completion Rate**. The primary efficiency metric is **Cost per Verified Completion**:

```text
total execution cost / validator-accepted tasks
```

Token counts, model/tool calls, dead-end and recovery turns, human intervention, wall time, and provider cost are tracked when available. Missing measurements remain `NOT_MEASURED`.

## Benchmark philosophy

Future paired comparisons target Pi, Hermes, and Odys using—where technically possible—the same model, task, repository, capabilities, validator, and similar budget. The research question is whether adaptive reliability improves verified long-horizon completion while keeping cost per verified completion close to lightweight runtimes. Odys does not claim superiority without paired live evidence.

Pi informs minimal loops, clean layering, low context overhead, progressive capabilities, and model autonomy. Hermes informs Skills, Memory, MCP, tools, browser/web, providers, delegation, and assistant capability ecosystems. They are architectural inspiration, not runtime dependencies. Odys explicitly makes CompletionAuthority, execution-state recovery, Runtime Truth, liveness, durable delegation, macro workflow replan, and adaptive reliability first-class runtime concerns.

## Current status and claim boundary

Odys currently has a Native Agent Kernel; durable Task/Run/Attempt state; CompletionAuthority and validation; failure classification and recovery; and Runtime Truth and liveness mechanisms. It is building toward adaptive reliable long-horizon execution.

It is not yet claimed to outperform Pi or Hermes, solve arbitrary long-horizon workflows, be production-ready, or be token-optimal.

The immediate gate is the Basic Native Vertical Slice:

```text
Model → Tool → Observation → More Turns → Completion Claim → Validator → ACCEPTED
```

> Benchmark-driven development begins after the basic Native vertical slice. New mechanisms should normally start from a reproducible task or failure, establish baseline evidence, state a mechanism hypothesis, add deterministic regression coverage, and then run a paired live experiment.

## Quick start

```bash
uv sync --extra dev
uv run lhas init-db
uv run pytest
```

Canonical direction and architecture are documented in [`docs/ARCHITECTURE_FREEZE.md`](docs/ARCHITECTURE_FREEZE.md), [`docs/PRODUCT_DIRECTION.md`](docs/PRODUCT_DIRECTION.md), [`docs/01_ARCHITECTURE.md`](docs/01_ARCHITECTURE.md), and [`docs/14_ROADMAP.md`](docs/14_ROADMAP.md). Historical experiments and task records remain preserved under `docs/evidence/`, `experiments/`, and `tasks/`.
