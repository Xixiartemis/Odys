# ADR 0004 — Adaptive Reliability Levels

Status: **ACCEPTED / FROZEN**

## Context

Always-on heavy reliability increases overhead on easy tasks, while lightweight execution may be insufficient for stateful, risky, or long-running work. Adaptive control must be measured rather than assumed.

## Decision

Freeze four conceptual levels:

- **FAST:** Native Minimal Agent Runtime, Runtime Truth, and basic budget/events.
- **GUARDED:** FAST plus CompletionAuthority and independent validation.
- **DURABLE:** GUARDED plus Verified Workflow, checkpoint, failure provenance, selective recovery, macro replan, and liveness.
- **MULTI_AGENT:** DURABLE plus durable delegation, dependency scheduling, child ownership, and result verification.

Every task uses the least reliability machinery sufficient for a verified outcome. Initial routing is explicit and deterministic. Controlled ablation precedes adaptive routing. ML/RL routing is prohibited until evidence justifies it.

## Consequences

- Easy tasks can retain lightweight-runtime cost characteristics.
- Strong mechanisms activate only when task requirements justify them.
- Level selection and overhead become measurable policy decisions.
- The runtime must preserve compatible truth and completion semantics across levels.

## Alternatives Rejected

- Activating the full harness for every task.
- Removing verification to minimize raw token use.
- Building an ML/RL classifier before controlled ablation.
- Selecting levels from unmeasured intuition or provider-specific behavior.

## Conditions for Reconsideration

Change levels or routing only after Phase 4 controlled ablation, reproducible evidence, explicit alternatives, migration cost, a superseding ADR, and analysis of Verified Completion Rate and Cost per Verified Completion.
