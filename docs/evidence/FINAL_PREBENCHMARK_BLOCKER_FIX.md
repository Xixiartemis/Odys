# Final pre-benchmark blocker fix

Base: `8136e16d48ece8a9aebf597cee71cdcb91de4581`

## Runtime ownership

`ProductRuntime._build_orchestrator()` creates one `RuntimeTargetController` and one `CliNativeProviderFactory` for every native orchestrator. The workspace factory receives the durable `run_id`, reads the effective target, and constructs the provider from the exact secret-free composite:

`provider_id + model_id + endpoint_identity + credential_route_id + route_type`

Credential values remain in environment/configuration and never enter RuntimeTarget projections.

## Switch transaction

```text
REQUESTED -> PENDING -> build candidate -> validate candidate target
          -> install candidate -> durable COMMITTED
          -> rollback + FAILED on construction, validation, install, or commit error
```

The old provider remains installed and the durable effective target remains authoritative on failure. Expected-current and execution ownership guards reject stale or cross-run switches.

## Request-time truth

Before `provider.generate`, the kernel reads the durable effective target, records `actual_provider_target` in the `ExecutionSnapshot`, and requires exact equality. A mismatch emits `RUNTIME_TARGET_DIVERGENCE`, persists configured/effective/actual safe projections, and makes zero model calls.

## Migration resume

`resume_task(task_id, runtime_target=...)` performs the switch through the native kernel before reopening the blocked Task/Run/Attempt. The same native Attempt and snapshot are then resumed. The run-scoped factory rehydrates the committed target on every executor construction, so the old provider is not reconstructed from persisted CLI labels.

## Failure state machine

```text
provider error -> conservative taxonomy -> route health
QUOTA/BILLING/AUTH -> BLOCK_PROVIDER -> explicit target switch -> resume_task
429 rate evidence -> TRANSIENT_RATE_LIMIT -> bounded retry policy
ambiguous 429 -> UNKNOWN_PROVIDER_FAILURE -> fail-safe bounded recovery
```

Health is keyed by the complete RuntimeTarget composite and supports `HEALTHY`, `TRANSIENTLY_UNAVAILABLE`, `QUOTA_BLOCKED`, `AUTH_BLOCKED`, and `UNKNOWN`.

## Macro replan and stale protection

```text
native/tool/validator/child evidence
 -> durable ReplanSignal
 -> MacroReplanService
 -> existing Planner proposal
 -> semantic-equivalence acceptance
 -> canonical Plan/PlanStep version rN
 -> reload scheduler
 -> execute revised graph
 -> outer validation/completion authority
```

Completed nodes are preserved only when capability, normalized objective, normalized inputs, and dependency semantics produce the same stable semantic fingerprint. Linear and dependency execution abort the current iteration after acceptance and reload the authoritative plan. Workers capture immutable `(plan_id, plan_version, step_id)` at construction and fail closed before side effects when the persisted version changes.

## Deterministic adversarial coverage

`tests/test_final_prebenchmark_blockers.py` covers W1 invalid assumptions, W2 repeated dead ends, W3 validator rejection, W4 child failure delivery, W5 stale workers, and native CLI controller/factory wiring. `tests/native_kernel/test_runtime_truth_replan.py` covers 429 taxonomy, factory rollback, divergence, and same-Attempt provider migration.

## Remaining external dependencies

Live provider credentials, route-scoped migration environment variables, real endpoint availability, and Hermes comparison remain external to the deterministic gate. No live model or Hermes benchmark is executed by this patch.
