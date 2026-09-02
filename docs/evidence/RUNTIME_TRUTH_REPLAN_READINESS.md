# Runtime Truth / Provider Recovery / Macro Replan Readiness

This document records the deterministic control-plane contract for the
pre-benchmark patch. No live Hermes or stochastic MiMo comparison is included.

## RuntimeTarget ownership map

`RuntimeTarget` is the immutable composite of `provider_id`, `model_id`,
`endpoint_identity`, `credential_route_id`, and `route_type`. A
`RuntimeTargetController` binds that identity to one `run_id` (and optional
workspace `session_id`). `NativeAgentKernel` copies configured/effective
targets into the durable execution snapshot and into every model-turn event.
Secrets and raw endpoint URLs are not persisted.

## Provider switch state machine

`REQUESTED → PENDING → COMMITTED` is accepted only after the expected-current
target and run/session ownership guards pass. A rejected confirmation produces
`REQUESTED → PENDING → FAILED`; the previous effective target remains
authoritative. A switch for one run cannot mutate another run's binding.

## Provider failure / recovery state machine

Provider responses are classified as `TRANSIENT_RATE_LIMIT`,
`QUOTA_EXHAUSTED`, `BILLING_OR_CREDIT_EXHAUSTED`, `AUTH_INVALID`,
`PROVIDER_UNAVAILABLE`, `PROVIDER_TIMEOUT`,
`MALFORMED_PROVIDER_RESPONSE`, or `UNKNOWN_PROVIDER_FAILURE`.

Transient failures may be controlled-retry eligible. Monthly quota, billing,
and invalid-auth failures are not same-route retryable; bounded route health is
persisted and the task parks as `BLOCKED_PROVIDER`. Recovery is an explicit
`resume_task` / `resume_run` control intent after migration, never a guessed
user message.

## Provider migration resume trace

`meaningful workspace mutation → durable native snapshot / TaskGraph state →
provider A QUOTA_EXHAUSTED → route health QUOTA_BLOCKED → task
BLOCKED_PROVIDER → explicit target switch → resume_task → same native Attempt
snapshot → unfinished node`. Reconciliation remains authoritative, so a
completed side effect is not blindly replayed.

## Macro replan call chain

`NativeAgentKernel → ReplanSignalRepository → MacroReplanService → existing
Planner.create_plan → canonical Plan/PlanStep update → TaskGraphScheduler →
worker`. The planner proposes; the service accepts/rejects and preserves valid
completed nodes. Validator rejection remains under `CompletionAuthority` and
the outer validator.

## Plan version / stale-plan invariant

Accepted replans increment `Plan.version` and record invalidated nodes and
signal IDs. Workers carry the plan version they were created against. A worker
whose persisted plan version no longer matches returns `STALE_PLAN` before tool
execution. Invalidated nodes are retained as `STALE` audit records and are not
scheduler-executable.

## Benchmark family matrix

| Family | Scenario | Primary control |
|---|---|---|
| execution_state_recovery | existing native recovery | durable snapshot/reconciliation |
| completion_integrity | existing completion control | outer validator authority |
| delegation_lifecycle | existing delegation control | provenance/delivery idempotency |
| runtime_target_truth | `runtime_target_truth` | explicit provider evidence/fallback |
| provider_quota_exhaustion | `provider_quota_exhaustion` | zero useless same-route retries |
| complex_workflow_replan | `complex_workflow_replan` | revised TaskGraph, not tool retry |

## Remaining external runtime dependencies

Live request evidence still requires configured provider credentials and an
actual provider endpoint. Hermes observations, live token/cost/wall-time
metrics, and stochastic MiMo comparisons are intentionally deferred.

