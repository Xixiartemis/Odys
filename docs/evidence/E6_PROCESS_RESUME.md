# E6-B Manual Process Resume

Harness: `HV-1.1`
Scope: deterministic outer-run recovery only; no live provider, automatic crash
discovery, lease, dynamic replan, or provider conversation restoration.

## Durable identity

`workspace_sessions` is the database registry binding one `run_id` and
`task_id` to a durable session root and manifest `session_id`. Resume resolves
the root only through this binding, then validates manifest identity, baseline
integrity, source identity, and canonical paths before exposing `work/`.

## Resume semantics

`RecoveringOrchestrator.resume_run(run_id)` is explicit and idempotent for
terminal runs. A persisted `RUNNING` run requires its latest attempt to be
`RUNNING`; resume marks it `CRASHED` with the stable classification
`PROCESS_INTERRUPTED`, records a bounded `WORKSPACE_RECOVERY_STATE` summary
(changed paths/counts and patch hash, never raw diff), validates the durable
candidate, and either completes without an executor call or performs a normal
CP-3 retry in the same durable workspace.

## Deterministic evidence

`tests/test_e6_process_resume.py` covers three file-backed scenarios:

- process A edits `work/src/calculator.py` and disappears; process B reopens,
  validates, completes with one crashed attempt, and makes zero executor calls;
- the same restart first fails validation, writes a checkpoint, reconstructs
  CP-3, executes attempt 2 in the same workspace, and completes;
- a manifest/run identity mismatch is rejected before workspace use.

The tests use separate `Database` instances against the same SQLite file to
model the process boundary. They assert attempt state, recovery/checkpoint
events, workspace binding state, CP-3 reconstruction, no raw diff persistence,
and terminal resume idempotency.
