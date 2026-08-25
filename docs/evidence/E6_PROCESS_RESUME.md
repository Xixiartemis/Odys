# E6-C Crash-Window-Aware Manual Process Resume

Harness: `HV-1.2`
Scope: deterministic outer-run recovery only; no live provider, automatic crash
discovery, lease, dynamic replan, or provider conversation restoration.

## CRASH_WINDOW_MODEL

The deterministic matrix covers W1–W8: run-start/workspace publication,
pre-attempt, running attempt, post-executor/pre-validation, post-validation
pass, post-validation fail, post-recovery decision/checkpoint, and a built
next-attempt context before executor start. Tests inject `ProcessDeath` only
through the optional `CrashPoint` lifecycle hook; production defaults to a
no-op injector.

Workspace binding uses a two-phase `CREATING` → `OPEN` protocol. The binding
is written before filesystem publication with a deterministic session identity
and root. Resume completes an absent root, promotes a strictly validated
existing root, and rejects corrupt/identity-mismatched partial state. It never
scans arbitrary filesystem paths.

## RESUME_STATE_MACHINE

`ResumeDecisionService.inspect()` reads Task, Run, Attempt, ValidationResult,
FailureReport, RecoveryAction, Checkpoint, ContextSnapshot, and workspace
binding repositories. `decide()` derives one action (`INITIALIZE_WORKSPACE`,
`START_FIRST_ATTEMPT`, `RECOVER_INTERRUPTED_ATTEMPT`,
`VALIDATE_COMPLETED_ATTEMPT`, `COMPLETE_FROM_PERSISTED_VALIDATION`,
classification/recovery continuation, `START_NEXT_ATTEMPT`, or
`RETURN_TERMINAL`). `resume_run()` executes one action and re-inspects durable
state, so a crash after any persisted phase cannot cause that phase to repeat.

Events are audit evidence, not the sole state authority: a crash can happen
after a DB row commits but before its next event append, or after an event
append but before the next row update. Resume decisions therefore consult
durable repositories first.

## IDEMPOTENCY_CONTRACT

Validation, failure reports, recovery actions, context snapshots, and
checkpoints are looked up per attempt before creation. A terminal resume is a
no-op. The matrix asserts no duplicate attempts or durable phase records,
zero executor calls after a persisted validation pass, and zero validator calls
after a persisted validation pass. Sequential repeated resume is covered;
concurrent ownership is intentionally out of scope.

## DESIGN_DECISIONS

`workspace_sessions` is the database registry binding one `run_id` and
`task_id` to a durable session root and manifest `session_id`. Resume resolves
the root only through this binding, then validates manifest identity, baseline
integrity, source identity, and canonical paths before exposing `work/`.

## Resume semantics

`RecoveringOrchestrator.resume_run(run_id)` is explicit and idempotent for
terminal runs. A persisted `RUNNING` run is continued from its latest durable
phase: completed executor results are validated from `Attempt.executor_result`,
persisted validation is reused, and a running/interrupted attempt is marked
`CRASHED` with `PROCESS_INTERRUPTED` before validator-before-retry. A bounded
`WORKSPACE_RECOVERY_STATE` summary (changed paths/counts and patch hash, never
raw diff) is the only workspace recovery evidence persisted to events and
checkpoints. Remaining retry budget continues normally through CP-3 until
completion, escalation, failure, or max attempts.

Events are audit evidence, not the sole state authority: a crash can happen
after a DB row commits but before its next event append, or after an event
append but before the next row update. Resume decisions therefore consult
durable repositories first.

## TRADEOFFS

- Repository lookups avoid duplicate phase records without requiring SQLite
  ALTER migrations; historical contradictory rows are resolved deterministically
  by latest creation order.
- A deterministic lifecycle hook makes crash windows reproducible in tests
  without production `if TEST_CRASH` branches.

## FAILED_APPROACHES

- Repeating the old `_resume_retry_once()` path would discard remaining retry
  budget after the first resumed attempt.
- Treating event presence as state authority would mis-handle a crash between
  row commit and event append.
- Reconstructing work from event history would lose the baseline/work boundary;
  durable workspace state remains authoritative.

## Deterministic evidence

`tests/test_e6_process_resume.py` and `tests/test_e6_crash_matrix.py` cover:

- process A edits `work/src/calculator.py` and disappears; process B reopens,
  validates, completes with one crashed attempt, and makes zero executor calls;
- the same restart first fails validation, writes a checkpoint, reconstructs
  CP-3, executes attempt 2 in the same workspace, and completes;
- a manifest/run identity mismatch is rejected before workspace use;
- all W1–W8 crash points, CREATING-root absent/valid/corrupt handling,
  per-phase idempotency, and a three-attempt resume/recovery continuation.

The tests use separate `Database` instances against the same SQLite file to
model the process boundary. They assert attempt state, recovery/checkpoint
events, workspace binding state, CP-3 reconstruction, no raw diff persistence,
and terminal resume idempotency.

## KNOWN_LIMITATIONS

- No process restart discovery, side-effect replay, or provider-internal
  conversation restoration is implemented.
- Exactly-once semantics are guaranteed only for the tested isolated local
  workspace fixture. HTTP writes, browser actions, emails, external database
  mutations, and MCP side effects require operation-level idempotency keys or
  receipts in a later phase.

## NEXT_HYPOTHESIS

After lifecycle recovery is stable, E7 can introduce dynamic macro replanning
and test whether Harness recovery plus replan improves complex long-task
completion compared with Inner Agent alone.
