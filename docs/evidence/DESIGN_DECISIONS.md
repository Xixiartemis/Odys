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

## DD-HV13-LIVE-001-TOOL-AWARE-RECOVERY

**Design decision.** Preserve HV13-LIVE-001 as a canonical `FAIL` and retain
the Outer Validator as acceptance authority. The two real-run arbitration
events correctly returned false for invalid durable workspace state; Outcome
Arbitration must not be characterized as failed merely because the overall run
failed.

**Tradeoffs.** Post-non-success validation adds a bounded validator step but
prevents executor status from becoming an unverified task outcome. Lower-level
tool recovery work should be measured before introducing macro replanning, so
the next phase prioritizes edit ergonomics and verification feedback while
deferring broader planning changes.

**Failed approaches.** Across the two post-resume attempts, exact-target
`workspace.edit` failed 17 of 20 calls. Attempt 3 also failed both `cli.exec`
calls. Repeating these operations without sufficient adaptation consumed two
20-turn budgets and did not produce a valid final workspace. This measured
bottleneck is not asserted to be the only cause.

**Known limitations.** The canonical artifact intentionally omits raw diff,
stdout/stderr, tool arguments, and model transcript, so it cannot identify the
exact final failing assertion or incorrect code. Its
`next_attempt_suppressed_after_arbitration` field records absence of a later
attempt, not causal retry suppression; Attempt 3 had already reached
`max_attempts=3` and validation was false. The HV12/HV13 comparison is a
single-run paired observation and provides no stochastic success-rate estimate.

**Next hypothesis.** E7-A — Tool-Aware Recovery & Verification should test:

1. whether less brittle edit targeting reduces `EDIT_TARGET_NOT_FOUND`;
2. whether repeated edit failure triggers a strategy change;
3. whether an early edit → pytest → observe loop improves verification;
4. whether CLI policy and path errors provide actionable recovery feedback.

E7-B — Dynamic Macro Replan remains deferred until lower-level tool robustness
is measured.

## DD-HV14-CLI-ALPHA-VERTICAL-SLICE

**Design decision.** Make `odys` the primary Typer entry point while retaining
`lhas` as a compatibility alias. The CLI is an application adapter over
`RecoveringOrchestrator`, durable workspace binding, InnerAgentExecutor,
ToolRegistry, explicit Outer Validation, EventStore, and CP-3. Product commands
do not duplicate lifecycle or recovery state machines.

**Design decision.** The user-supplied verification command is parsed to an
explicit argv, executed without a shell in the staged workspace, and installed
as an exact Inner Agent command policy. Validator exit code, rather than a
model completion claim, is authoritative for Alpha functional acceptance.

**Design decision.** Keep exact-target `workspace.edit` compatible and add
version-checked `workspace.edit_lines` plus structured recovery metadata.
Repeated failure adaptation uses only a SHA-256 argument signature and safe
error identity. CP-3 receives bounded aggregate failure memory, never raw tool
arguments.

**Tradeoffs.** Exact command policy limits exploratory CLI flexibility and may
require the user to choose a verification command that covers the full goal.
Line editing requires a fresh file SHA and an extra read, but removes ambiguous
substring targeting and makes stale edits explicit. Polling persisted state at
four Hz is less immediate than in-process callbacks but keeps UI failure
outside correctness.

**Failed approaches.** The first offline vertical slice resolved `pytest` from
an unrelated system Python because PATH did not explicitly prioritize the
active Odys virtual environment. SafeCli now prepends the allowed
`VIRTUAL_ENV/Scripts` or `bin` directory without changing argv or command
policy. The first deterministic fixture repair corrected tenant-key isolation
but left delayed-message recreation in the router; the final offline scenario
repairs and validates both independent defects.

**Known limitations.** Alpha supports one repository, one durable workspace,
one sequential agent, and one explicit verification argv per Run. The
deterministic offline profile is fixture-oriented product evidence, not a
general autonomous coding benchmark. Terminal UI is polling-based; validation
stdout/stderr is bounded; ordinary demos are rerunnable and are not canonical
evidence.

**Next hypothesis.** Measure E7-A tool ergonomics and verification discipline
on additional non-canonical product demos before any new canonical real-model
run. E7-B Dynamic Macro Replan remains deferred and is not implemented here.
