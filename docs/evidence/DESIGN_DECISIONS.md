# Evidence Design Decisions

## DD-E5-INNER-FAILURE-NOT-TASK-FAILURE

**Context.** In both preserved E5 MiMo calibration runs, the staged calculator fixture reached `FAIL → PASS` and the outer validator accepted the final patch, while the Inner Agent exhausted its 20-turn budget without a completion claim.

**Decision.** The outer validator remains the authority for functional validation. `InnerAgentResult.FAILURE` caused by `TURN_LIMIT` is recorded as an agent-lifecycle failure and is not relabeled as task failure when the validator independently confirms the required patch and tests. Conversely, it is not relabeled as agent success.

**Evidence boundary.** This distinction is an E5 observation, not a runtime behavior change. The two runs are reported with separate functional-validation and agent-completion rates, both scoped to the E5 calculator fixture.

**Future input.** E6 may test reconstruction and validation of useful state from a failed or interrupted Inner Agent: a validated PASS should finish without retry, while a validated FAIL should recover, retry, or resume. No E6 implementation is included here.

## Deferred efficiency hypothesis

`tool_use_behavior` and acceptance-based early stopping remain future efficiency optimizations. They were not enabled or inferred from these runs because both runs still terminated at the 20-turn limit.

## DD-HV12-LIVE-001-OUTCOME-ARBITRATION

**Design decision.** HV12-LIVE-001 demonstrates that executor outcome and task
outcome must remain separate. A durable workspace can contain useful work even
when the Inner Agent ends with `FAILURE`, `TIMEOUT`, or `TURN_LIMIT`. The
Outer Validator must arbitrate task completion before failure recovery consumes
another attempt when durable workspace state may satisfy acceptance criteria.

**Tradeoff.** Running validation after a non-successful executor outcome adds a
bounded validator call and may spend time on a workspace that is still
incomplete. It can also avoid an unnecessary retry when the durable patch is
already sufficient. The validator remains the acceptance authority; the Inner
Agent completion claim is not upgraded by this decision.

**Failed approach.** In the canonical HV-1.2 run, Attempts 2 and 3 ended at
`AGENT_TURN_LIMIT` and entered failure/recovery handling. The durable workspace
was not validator-arbitrated after each non-success outcome before another
attempt was consumed. Attempt 2's post-attempt pytest state was not measured,
so this closeout does not claim that Attempt 2 was already correct.

**Known limitation.** HV12-LIVE-001 proves the lifecycle gap and records final
pytest PASS, but it does not prove which intermediate workspace state would
have passed acceptance. No runtime semantics are changed in this closeout, and
Dynamic Replan remains out of scope.

**Next hypothesis.** E6-D / HV-1.3 should implement post-non-success outcome
arbitration:

```text
Inner Agent FAILURE / TIMEOUT / TURN_LIMIT
→ Outer Validator
→ Validator PASS → Task COMPLETED without an unnecessary next Attempt
→ Validator FAIL → classify failure, recover, checkpoint, next Attempt
```

## DD-E5-CP3-RETRY-RECONSTRUCTION

Retry attempts now reconstruct context from the latest durable checkpoint and
events after its cursor, then pass the structured state through
`ContextBuilder(policy=CP-3)`. The first attempt retains the ordinary initial
context path. ContextBuilder remains the sole context assembly boundary; this
does not implement process restart or resume.

Recent history is an explicit allowlisted projection. Unknown event payloads
are not copied into executor context, and reconstruction failures emit only a
bounded error type in `CONTEXT_RECONSTRUCTION_FAILED`.

## DD-E6D-POST-NON-SUCCESS-OUTCOME-ARBITRATION

**Design decision.** For durable workspace-backed `FAILED` and `TIMED_OUT`
attempts, the Outer Validator arbitrates the current workspace before failure
classification and recovery. A passing validation completes Run and Task while
the Attempt retains its authoritative executor status and persisted result.

**Tradeoff.** This adds one bounded validator call after a non-success outcome,
but may avoid an unnecessary retry when durable work already satisfies the
acceptance criteria. A validation failure follows the existing classifier,
recovery, checkpoint, and CP-3 path.

**Failed approach.** HV-1.2 classified workspace-backed executor failures
before validation, allowing `AGENT_TURN_LIMIT` to consume another attempt even
when the durable workspace might already be sufficient. E6-D changes the
ordering without changing executor budgets, tool policy, provider behavior, or
CP-3 reconstruction semantics.

**Known limitation.** This arbitration is intentionally limited to workspace-
backed `FAILED` / `TIMED_OUT` attempts. `CRASHED` / `PROCESS_INTERRUPTED`
recovery and non-workspace execution retain their existing behavior. The
historical HV12 Attempt 2 post-state remains `NOT_MEASURED`.

**Next hypothesis.** A future live evaluation with a new ID can measure whether
post-non-success validation avoids unnecessary retries while preserving the
Outer Validator as acceptance authority. Dynamic Replan remains out of scope.
