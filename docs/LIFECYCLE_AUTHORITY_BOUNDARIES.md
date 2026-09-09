# Lifecycle Authority Boundaries — PlanStep/Task/Run/Attempt

## Identity Chain

```
PlanStep.task_id → Task.id → Run.task_id → Attempt.run_id
```

- `PlanStep.task_id` is set at dispatch time (`service.py:317`)
- `Run.task_id` is set at `Orchestrator._create_run()` (`orchestrator.py:388`)
- `Attempt.run_id` is set at `Orchestrator._create_attempt()` (`orchestrator.py:401`)
- No `run_id` field on PlanStep — traversal requires: Step→Task→Run

## Authority Boundaries

| Entity   | Authority                | Controlled By                                    | Statuses                                              |
|----------|--------------------------|--------------------------------------------------|-------------------------------------------------------|
| PlanStep | `transition_step()`      | PlanExecutionService (via transition_step only)  | PENDING→RUNNING→CLAIMED_COMPLETE→(VERIFIED/WAITING)   |
| Task     | Orchestrator             | Orchestrator.execute_task                        | CREATED→RUNNING→COMPLETED/ESCALATED/FAILED             |
| Run      | Orchestrator             | Orchestrator._run_executor                       | CREATED→RUNNING→COMPLETED/ESCALATED/FAILED             |
| Attempt  | Orchestrator             | Orchestrator._finalize_attempt                   | PENDING→RUNNING→COMPLETED/FAILED/TIMED_OUT/CRASHED     |

### Completion Authority
- **Who decides 'done'**: Run COMPLETED → step CLAIMED_COMPLETE (service.py:342)
- Attempt/Run/Task completion has NO direct effect on step status
- Step reaches CLAIMED_COMPLETE only after orchestrator returns a COMPLETED run

### Verification Authority
- **Who decides 'verified'**: `workflow_verifier.verify(step, plan, events)` (service.py:347)
- Without verifier: step → WAITING_FOR_VERIFICATION (fail-closed default)
- With verifier accepted: step → VERIFIED
- With verifier rejected: step → CLASSIFIED_FAILURE
- **No lower-level success bypasses verification**: Attempt SUCCESS, Run SUCCESS,
  and Tool SUCCESS all stop at CLAIMED_COMPLETE. Only the explicit verification
  seam promotes to VERIFIED.

## Invariant Proofs

### Invariant 1: Attempt SUCCESS ≠ Step VERIFIED
- Attempt COMPLETED is persisted by `Orchestrator._finalize_attempt()` (orchestrator.py:306)
- This has zero side effects on PlanStep status
- Step status is controlled exclusively by `transition_step()` in `service.py`

### Invariant 2: Run SUCCESS ≠ Step VERIFIED
- Run COMPLETED triggers `transition_step(step, CLAIMED_COMPLETE, "run_completed")`
- This is one status BEFORE VERIFIED — the verification seam is the gate

### Invariant 3: Tool SUCCESS ≠ Step VERIFIED
- Tool SUCCESS → ExecutionStatus.SUCCESS → Attempt COMPLETED → Run COMPLETED
- Chain stops at step CLAIMED_COMPLETE
- `transition_step(step, VERIFIED, ...)` only called when `workflow_verifier.verify().accepted`

### Invariant 4: All step transitions go through transition_step()
- `transition_step()` is the sole entry point for step status changes
- Every call emits a `STEP_STATE_TRANSITION` event with full provenance
- Direct `step.status = X` assignment is only used for initial construction

## Restart/Reload Behavior

- CLAIMED_COMPLETE persists to DB via `plans.update(plan)` (service.py:355)
- On process restart, `PlanRepository.get()` restores step with CLAIMED_COMPLETE status
- The scheduler skips CLAIMED_COMPLETE steps (treats as already active)
- Plan completion check includes CLAIMED_COMPLETE: `plan.steps all in {CLAIMED_COMPLETE, COMPLETED, VERIFIED, STALE}`
- Steps in CLAIMED_COMPLETE can be transitioned to VERIFIED on reload (deferred verification)
- Steps in WAITING_FOR_VERIFICATION are detected by the service and plan status is set accordingly
