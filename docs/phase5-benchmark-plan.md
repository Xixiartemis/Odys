# Phase 5 Recovery Generalization Benchmark Plan

Status: design only. No Phase 5 provider execution is authorized by this
document.

## Purpose and boundary

Phase 5 measures whether the recovery control plane generalizes beyond the
single controlled Phase 4 task. The ordinary benchmark score and the
controlled fault-injection benchmark remain separate artifacts and separate
claims.

The first qualification target is at least 30 independent
task-condition pairs. A publishable target is 50–100 valid pairs, with at
least 10 distinct tasks and at least three meaningful fault conditions. At
least one task family must contain tasks not authored specifically for Odys.

## Matrix

Each pair uses the same task, model, tool set, injected fault, validator,
root provider budget, timeout, and supported temperature/seed. Arms are:

| Arm | Description |
| --- | --- |
| A | Bare Agent / minimal loop |
| B | Legacy bounded recovery |
| C | Odys no-progress-aware recovery |
| D | Optional verified-state plus Odys harness |

Task categories should include multi-step coding/taskgraph work, file
mutation workflows, validation-driven tasks, and dependency/subgraph tasks.

Fault conditions should cover NO_PROGRESS or denied effect,
REPEATED_ACTION, STATE_OSCILLATION, VALIDATOR_REJECTION, TOOL_FAILURE,
malformed provider output, transient provider failure, stale-plan/state
conflict, and uncertain or partial side effects. Each pair must record the
fault identity and trigger status; an untriggered case is not causal evidence.

## Primary measurements

1. Validator-backed completion rate.
2. Recovery success conditional on the injected fault.
3. Provider calls and total tokens.
4. Tool calls and wall-clock time.
5. Time/calls to escalation.
6. Remaining recovery budget at escalation.
7. Redundant post-success actions.
8. Unnecessary replan rate.
9. Unsafe or duplicate side-effect rate.
10. Invalid infrastructure rate.

The evidence layer must keep action observations, effect observations, and
external validator observations separate. `VERIFIED` is emitted only by the
authoritative validator.

## Failure taxonomy

Every invalid or unsuccessful pair should be classified as one primary
failure: provider failure, budget exhaustion, no-progress not detected,
false-positive escalation, unchanged replan, plan-to-execution divergence,
tool execution failure, mutation without acceptance, validator rejection,
side-effect uncertainty, or infrastructure invalidation.

## Reporting rules

Retain pair identity across arms. For binary outcomes report numerator,
denominator, and a confidence interval; do not report a broad improvement
percentage from three paired runs. For cost, compare calls/tokens primarily
among validator-equivalent outcomes. If baseline fails and Odys passes, the
difference is a completion/recovery effect, not equal-quality cost saving.
For both-pass pairs report paired deltas with median, IQR, and a paired
bootstrap interval.

## Public benchmark and model generalization

After the controlled matrix is stable, run a reproducible subset of a public
agent/coding benchmark with deterministic fault injection. Do not claim
improvement on the public benchmark unless its own task set is used.

Only after the single-model result is stable should a smaller paired subset
be repeated on a second model family. This is a generalization check, not a
prerequisite for the first resume publication.

## Execution gate

No expensive run starts until the provider-free Phase 4/P4.5 convergence gate
proves: one successful post-replan mutation, `VALIDATE_CANDIDATE`, external
acceptance, durable `PlanStep=VERIFIED`, durable `Plan=COMPLETED`, no
redundant post-success calls, and no root repair-budget bypass. Frozen Phase
4 task inputs remain unchanged.
