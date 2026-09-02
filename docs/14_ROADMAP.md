# Odys Roadmap

Status: **FROZEN**

Canonical authority: `docs/ARCHITECTURE_FREEZE.md`

The order below must not change without the evidence and ADR process defined by the architecture freeze.

## Phase 0 — Architecture Freeze

Freeze runtime ownership, five-layer architecture, planning ownership, open-source reuse, verified workflow semantics, adaptive reliability levels, metrics, non-goals, and development order.

Deliverable: documentation only.

## Phase 1 — Basic Native Vertical Slice

Current highest priority:

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

The real Native path must demonstrate provider/model target truth, tool execution, model-visible observations, multiple turns, non-authoritative completion claim, independent validation, and verified acceptance. Test/live runtime identity and failure provenance must be explicit.

Do not add a major capability system or build a full Hermes-style Capability Layer before this passes live.

## Phase 2 — Minimum Capability Parity

Implement only capabilities required for controlled comparison:

```text
P0  read / write / edit / shell
P1  official MCP adapter
P2  Skill loading and progressive disclosure
P3  context compaction and selective context
P4  token and cost accounting
```

Memory, browser, web/search, and delegation wait for selected benchmark demand. Do not build Telegram, Discord, Desktop assistant, or gateway surfaces.

## Phase 3 — Verified Workflow V1

Implement the canonical typed TaskGraph with:

- dependencies;
- preconditions;
- expected effects;
- acceptance criteria;
- evidence;
- verified transitions;
- local repair;
- affected-descendant invalidation;
- macro replan only when necessary.

Remain HTN-compatible, but do not build a full HTN solver.

## Phase 4 — Controlled Ablation

Stop adding framework features and compare:

```text
FAST
vs GUARDED
vs STATEFUL / WORKFLOW
vs DURABLE
```

Hold model, task, tools, environment, validator, and budget policy constant. Measure mechanism contribution, control overhead, Verified Completion Rate, Cost per Verified Completion, and Repeated Reliability.

## Phase 5 — Adaptive Reliability

Only after Phase 4 provides data. Begin with explicit deterministic routing features:

- estimated horizon;
- dependency depth;
- statefulness;
- verification availability;
- side-effect risk;
- parallelism/delegation need.

Select FAST, GUARDED, DURABLE, or MULTI_AGENT. Do not use ML/RL initially.

## Phase 6 — Capability Expansion

Add only from benchmark/product demand:

- memory;
- browser and web/search;
- durable delegation;
- richer Skills;
- sandbox backends.

## Phase 7 — Productionization

Only after research semantics are stable, reconsider:

- Temporal as an optional execution backend;
- distributed workers;
- PostgreSQL and queues;
- multi-tenant services;
- Web/Desktop;
- gateway integrations.

These are deployment questions and must not redefine core execution semantics.

## Architecture-change gate

```text
reproducible limitation/failure
→ evidence current architecture cannot reasonably support it
→ benchmark or production evidence
→ alternatives considered
→ migration cost
→ ADR
→ explicit claim impact
```

> EXTEND, DO NOT REWRITE.

## Historical milestone map

Earlier LHAS Phase A–H and Odys E1–E7 labels remain preserved in `tasks/`, `experiments/`, and `docs/evidence/`. They are historical implementation records and do not override this frozen roadmap.
