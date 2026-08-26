# E6-D Outcome Arbitration

Harness target: `HV-1.3`  
Base: `a821df5793afb5ed705c3238dac3165265319c89`  
Scope: deterministic workspace-backed `FAILED` / `TIMED_OUT` attempts

## PURPOSE

E6-D closes the lifecycle gap exposed by `HV12-LIVE-001`: an executor can end
with `FAILURE`, `TIMEOUT`, or `AGENT_TURN_LIMIT` after making useful durable
workspace changes, while the Outer Task remains incomplete because validation
was bypassed. The Outer Validator is now allowed to arbitrate durable
workspace outcome before failure recovery consumes another attempt.

No live model was executed. `evals/runs/HV12-LIVE-001.json` remains immutable,
and this deterministic fixture is not a reconstruction of its unmeasured
Attempt 2 state.

## REAL_BASELINE_INPUT

The historical live baseline had final pytest `PASS` but
`outer_task_completed=false` and `process_recovery_passed=false`. Attempts 2
and 3 ended with `AGENT_TURN_LIMIT`; only one validator call occurred after
resume. E6-D uses that outcome distinction as motivation only. It does not
claim that the historical Attempt 2 workspace would have passed validation.

## STATE_MACHINE_BEFORE

For workspace-backed `FAILED` / `TIMED_OUT` attempts, HV-1.2 performed:

```text
non-success executor outcome
→ failure classification
→ recovery action
→ checkpoint
→ CP-3 / next Attempt
```

An absent validation result did not stop this path, and an existing failure
report could cause recovery to continue without an outcome arbitration step.
Non-workspace executions retained their prior behavior.

## STATE_MACHINE_AFTER

For workspace-backed `FAILED` / `TIMED_OUT` attempts only:

```text
validation absent
→ VALIDATE_NON_SUCCESS_ATTEMPT
→ Validation PASS → COMPLETE_FROM_PERSISTED_VALIDATION
                 → Run COMPLETED / Task COMPLETED
                 → no recovery action, checkpoint, or next Attempt
→ Validation FAIL → existing classification / recovery / checkpoint / CP-3 path
```

Validation is checked before `FailureReport`, so an older persisted report does
not bypass arbitration. `CRASHED` attempts, including `PROCESS_INTERRUPTED`,
retain existing recovery semantics. Non-workspace executions do not gain an
implicit validator call.

## INVARIANTS

- Executor outcome and task outcome are distinct.
- A passing post-non-success validation does not rewrite `Attempt.status`.
- `ExecutionResult.FAILURE` / `TIMEOUT` is not converted to `SUCCESS`.
- A passing validation completes Run and Task without requiring a
  `FailureReport`, `RecoveryAction`, checkpoint, or next Attempt.
- A failing validation preserves classification, recovery, checkpoint, and CP-3
  behavior.
- Validation persistence is idempotent across crashes and resume.
- Historical live evidence is not modified and no live model is called.

## TEST_MATRIX

| Scenario | Deterministic assertion |
|---|---|
| Failed executor + Validator PASS | Attempt remains `FAILED`; Run/Task complete; no Attempt 3, report, action, or Attempt-2 checkpoint |
| Failed executor + Validator FAIL | Validation, classification, recovery, checkpoint, and CP-3 next attempt remain active |
| Timed-out executor + Validator PASS | Attempt remains `TIMED_OUT`; Run/Task complete; no retry |
| Timed-out executor + Validator FAIL | Existing failure/recovery path remains active |
| Non-workspace failure | Existing behavior preserved; no implicit validator call |
| W-A: crash before arbitration validation | Fresh resume validates exactly once and completes |
| W-B: crash after validation PASS | Fresh resume completes without executor or validator rerun |
| W-C: crash after validation FAIL | Fresh resume classifies and continues recovery without duplicate rows |
| W-D: pre-existing report, absent validation | Validation takes priority, PASS completes without retry |

The primary regression models the live failure shape with Attempt 2 returning
`ExecutionResult.FAILURE`, `error_type=AGENT_TURN_LIMIT`, and a validator-PASS
workspace. Attempt 2 remains `FAILED`, Run and Task complete, and Attempt 3 is
absent.

W-A through W-D explicitly persist `AttemptStatus.FAILED` with
`error_type=AGENT_TURN_LIMIT` before the fresh resume. Their assertions inspect
the durable pre-resume validation/report state and the bounded
`outcome_arbitration=true` validation-start payload, so these windows do not
exercise the pre-existing completed-attempt validation path.

## METRICS

Measured from the deterministic E6-D test matrix:

| Metric | Value |
|---|---:|
| `test_count` | 271 |
| `new_tests` | 9 |
| `arbitration_pass_scenarios` | 5 |
| `arbitration_fail_scenarios` | 3 |
| `timeout_scenarios` | 2 |
| `restart_scenarios` | 4 |
| `duplicate_validation_rows` | 0 |
| `duplicate_failure_reports` | 0 |
| `duplicate_recovery_actions` | 0 |
| `duplicate_checkpoints` | 0 |
| `unnecessary_attempts_avoided` | 5 scenario-level next-attempt suppressions |
| `validator_calls` | 11 across the nine E6-D scenarios |
| `executor_calls_avoided` | 5 scenario-level post-arbitration executor calls |

These are deterministic test-fixture measurements, not live-model improvement
claims. Token and provider metrics are not applicable (`null` /
`not_available`).

## DESIGN_DECISIONS

The Outer Validator remains the final acceptance authority. Arbitration is
limited to durable workspace-backed `FAILED` / `TIMED_OUT` attempts so generic
non-workspace executor contracts and existing `CRASHED` recovery semantics do
not change implicitly.

The persisted executor result is passed to the existing validator machinery.
Timeouts without a concrete result use the existing safe synthesized result;
the implementation never fabricates executor success.

## TRADEOFFS

Arbitration adds a bounded validator call after a non-success executor outcome.
This may validate an incomplete workspace, but it can avoid an unnecessary
retry when durable work already satisfies acceptance. A validation failure
continues through the existing failure and recovery pipeline.

## FAILED_APPROACHES

The pre-E6-D route classified workspace-backed executor failures before asking
the Outer Validator to inspect the durable workspace. That ordering allowed a
turn-limited executor outcome to consume another recovery attempt even when
the workspace might already be acceptable.

E6-D does not claim that the historical HV12 Attempt 2 was correct because its
post-attempt pytest state was `NOT_MEASURED`.

## KNOWN_LIMITATIONS

The scenarios use deterministic workspace fixtures and do not measure provider
latency, tokens, or live-model success-rate changes. Arbitrating a non-success
outcome is implemented for workspace-backed `FAILED` / `TIMED_OUT` states;
arbitrary `CRASHED` states and Dynamic Replan remain out of scope.

## NEXT_HYPOTHESIS

After review and merge, a new live evaluation ID may test whether post-
non-success outcome arbitration reduces unnecessary retries while preserving
validator authority. `HV12-LIVE-001` remains canonical and must not be rerun.
