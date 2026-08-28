# HV13 Long-Task Validation

## PURPOSE

HV13 is the first controlled validation protocol for the HV-1.3 process-level
long-task path. It asks whether useful durable workspace progress survives an
actual child-process termination, a fresh Python process, and `resume_run()`;
whether the resumed repair can reach a non-success executor outcome; and
whether the outer validator can arbitrate that outcome into final Task
completion.

The deterministic artifact is `evals/runs/HV13-DRY-001.json`. The future live
artifact is reserved as `evals/runs/HV13-LIVE-001.json` but was not created by
this task.

## BASELINE

HV13 is compared to `HV12-LIVE-001` under HV-1.2. That historical artifact is
unchanged and remains the canonical real-model baseline: its final pytest was
`PASS`, but its outer task did not complete. HV13 does not rewrite,
reinterpret, or merge that result with the deterministic experiment.

## CONTROLLED_VARIABLES

The experiment reuses the exact `HV12-SESSION-LIFECYCLE-1` fixture and the same
task objective: tenant-scoped session isolation, no recreation of deleted or
tombstoned sessions by delayed messages, and valid active/new-session behavior.
The initial fixture test is red and the final functional acceptance is green.

The execution contract remains `MAX_ATTEMPTS=3`, `INNER_TURN_BUDGET=20`, the
capabilities `workspace.list`, `workspace.read`, `workspace.search`,
`workspace.edit`, `workspace.diff`, and `cli.exec`, with `workspace.edit` as the
only side effect. CLI use is restricted to pytest. The crash policy is
`DURABLE_MUTATION_STABLE`: the parent observes a changed source file and a
stable workspace hash across two polls before terminating Process A.

## DRY_RUN

`HV13-DRY-001` uses two actual child Python processes. Process A makes the
useful partial `session_store.py` repair and remains alive until the parent
terminates it. Process B is a new Python process and calls `resume_run(run_id)`
against the same SQLite database and workspace session.

The recovered Attempt 1 is validated and fails because the delayed-message
repair is incomplete. Recovery creates a checkpoint and starts Attempt 2 with
CP-3. Attempt 2 completes the remaining deterministic workspace repair but
returns `FAILURE / AGENT_TURN_LIMIT` intentionally. The now-green workspace is
then validated through `VALIDATE_NON_SUCCESS_ATTEMPT`; the validator passes,
the Run and Task complete, and Attempt 2 remains `FAILED`. Attempt 3 does not
exist.

Recorded deterministic shape:

| Field | Value |
|---|---|
| Initial pytest | `FAIL` |
| Final pytest | `PASS` |
| Process A termination | forced, after `DURABLE_MUTATION_STABLE` |
| Fresh Process B / same workspace | `true` / `true` |
| Attempts | 2 total; Attempt 3 absent |
| Attempt 1 | `CRASHED / PROCESS_INTERRUPTED` |
| Attempt 2 | `FAILED / AGENT_TURN_LIMIT` |
| CP-3 attempts | 1 |
| Executor calls after resume | `[2]` |
| Validator calls after resume | 2 |
| Run / Task | `COMPLETED / COMPLETED` |
| Source and temporary snapshot unchanged | `true / true` |
| Duplicate validation/report/action/checkpoint rows | `0 / 0 / 0 / 0` |

## SUCCESS_CONTRACT

Canonical long-horizon `PASS` requires all of the following independently
observable facts: initial pytest failure, forced process termination, fresh
Process B creation, reuse of the same durable workspace, `resume_run()`
invocation, final pytest success, Run and Task completion, unchanged source
repository and temporary source snapshot, a non-empty final patch, and zero
duplicate durable lifecycle rows.

The four historical booleans remain separate measurements:
`functional_validation_passed`, `agent_completion_passed`,
`outer_task_completed`, and `process_recovery_passed`. In this dry run they are
`true`, `false`, `true`, and `true`, respectively. `long_horizon_result` is
`PASS` because the long-horizon contract passed even though the executor did
not provide a completion claim.

## ARBITRATION_OBSERVATION

Arbitration is detected from durable lifecycle evidence, specifically a
`VALIDATION_STARTED` event with `outcome_arbitration=true`; it is not inferred
from final status alone. HV13-DRY-001 records:

- `outcome_arbitration_observed=true`
- `outcome_arbitration_attempt_number=2`
- `outcome_arbitration_executor_status=FAILED`
- `outcome_arbitration_error_type=AGENT_TURN_LIMIT`
- `outcome_arbitration_validation_passed=true`
- `next_attempt_suppressed_after_arbitration=true`
- `attempts_after_arbitration=0`

This is a deterministic observation of the E6-D path, not a causal claim that
HV-1.3 improves stochastic live success rates.

## LIVE_ONE_SHOT_POLICY

Live execution is not part of this preparation task. When explicitly invoked,
HV13 first requires `ODYS_AGENT_MODEL` and `ODYS_AGENT_API_KEY`, verifies the
HV-1.3 harness configuration, refuses if either the canonical live result or
the canonical claim marker already exists, and atomically creates
`evals/runs/HV13-LIVE-001.claim.json` before starting Process A. Missing
configuration returns `SKIPPED_CONFIG` without consuming the live ID or claim
marker. There is no force-rerun escape hatch.

Only bounded summaries may be persisted. API keys, authorization headers,
provider transcripts, hidden reasoning, raw full diffs, full tool arguments,
and unsafe stdout/stderr are excluded. If a process dies before trace
persistence, trace metrics are `null` or marked incomplete rather than
fabricated as zero.

## RESULT_INTERPRETATION

For a future live run, `long_horizon_result=PASS` does not require arbitration
to occur. A normal executor completion followed by validator acceptance can
also pass the long-horizon contract, while recording
`outcome_arbitration_observed=false`. A live final pytest pass with an
incomplete outer Task is `FAIL`; a final pytest failure is also `FAIL` but is
not called an arbitration failure without durable arbitration evidence. If the
stable crash trigger is never reached, the result is `INCONCLUSIVE` with reason
`CRASH_TRIGGER_NOT_REACHED`.

Any live observation is a single-run paired observation against HV12, not a
success-rate or causal improvement claim.

## KNOWN_LIMITATIONS

The dry executor is deterministic and does not measure provider latency,
tokens, or stochastic model behavior. It proves the process boundary and
non-success arbitration contract only for this fixture. The live protocol is
one-shot, so it cannot establish a benchmark trend or generalize from one
paired run. Provider-internal conversation state is not restored across the
process boundary.

## NEXT_DECISION

Review the dry artifact, test evidence, and safety guards before authorizing at
most one explicit HV13 live execution. Do not execute a live model as part of
this preparation change, and do not start E7 Dynamic Replan.
