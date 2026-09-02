# ADR 0001 — Runtime Ownership

Status: **ACCEPTED / FROZEN**

## Context

Odys historically integrated Outer Runtime, Inner Agent, AgentExecutor, and external agent SDK boundaries. Those integrations can blur ownership of turns, tools, workflow state, completion, retries, and recovery.

## Decision

Odys owns execution semantics: Task/Run/Attempt, the Native model/tool loop, verified workflow transitions, CompletionAuthority, failure provenance, recovery, selective repair, macro replan, checkpoint/resume, Runtime Truth, liveness, budget/cost, and durable delegation.

External executors remain compatibility and baseline integrations. They must not become the canonical owner of Odys lifecycle or completion semantics.

Planning has exactly two authorities: Odys owns macro outcomes and workflow repair; the executing agent owns micro-planning and tool choices.

## Consequences

- Native behavior and reliability claims remain attributable to Odys.
- Compatibility adapters may duplicate some legacy boundaries during migration.
- New dependencies require narrow ownership contracts.
- Completion claims remain evidence until CompletionAuthority/Validator accepts them.

## Alternatives Rejected

- A giant outer harness supervising a black-box full inner runtime.
- An external agent SDK owning the canonical model/tool loop.
- Nested outer, inner, and subagent planners as competing authorities.
- Capability systems owning independent Task, retry, or completion semantics.

## Conditions for Reconsideration

Reconsider only with a reproducible limitation, evidence the frozen ownership cannot support it, benchmark or production evidence, alternatives and migration cost, a superseding ADR, and explicit claim impact.
