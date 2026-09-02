# ADR 0003 — Verified Workflow Semantics

Status: **ACCEPTED / FROZEN**

## Context

Long-horizon progress cannot be inferred from an agent's final text or from tool activity alone. Workflow repair must preserve verified work, invalidate affected descendants, and avoid unnecessary global replanning.

## Decision

Odys owns a typed Verified Workflow Runtime. TaskGraph nodes evolve toward explicit goal, dependencies, preconditions, expected effects, acceptance criteria, evidence, capabilities, risk, budget, checkpoint/recovery policy, and status.

The canonical success transition is:

```text
PLANNED → READY → RUNNING → CLAIMED_COMPLETE → VERIFIED
```

`CLAIMED_COMPLETE` is non-authoritative. Only externally supported acceptance evidence may produce `VERIFIED`.

Failure handling preserves provenance and applies local repair first, then affected-subgraph repair, and macro replan only when necessary. Macro planning is Odys-owned; micro-planning remains agent-owned.

## Consequences

- Agent completion and workflow verification remain separate facts.
- Workflow evidence and invalidation rules must be durable and testable.
- Selective repair can reduce lost work and repeated side effects.
- V1 requires typed graph semantics but not a full HTN solver.

## Alternatives Rejected

- Treating agent success text as workflow completion.
- Replanning the entire workflow after every local failure.
- Central planning of every grep/read/edit/tool call.
- A full HTN solver before V1 evidence requires one.
- Outsourcing canonical workflow state and transitions to LangGraph or Temporal.

## Conditions for Reconsideration

Reconsider transition or repair semantics only with reproducible workflow failures, evidence that typed graph semantics are insufficient, paired benchmark results, alternatives and migration analysis, a superseding ADR, and claim-impact review.
