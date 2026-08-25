# E5 Evidence Report

## What

E5 introduces durable, append-only execution checkpoints, safe event-observation projection, incremental event cursors, CP-3 bounded context reconstruction, context SHA-256 metrics, and restart-safe checkpoint reload.

## Why

Checkpoint is durable execution state, not long-term memory. Replaying every historical event into an agent would grow context with run length. E5 projects safe working state and replays only events after the latest checkpoint.

## Architecture

```text
EventStore -> CheckpointService -> WorkingStateProjector
                         |-> CheckpointRepository (append-only)
Checkpoint + event delta -> ContextReconstructionService -> ContextBuilder (sole boundary) -> CP-3 snapshot
```

## Quantitative Evidence

Canonical `E5-CONTEXT-001` used 120 safe history events, checkpoint cursor 100, and replayed 20 delta events. It selected 20 recent events, dropped 0 within the configured recent-event window, produced 4,420 bytes of history input and a 2,249-byte context under a 4,096-byte budget. Replay reduction was `1 - 20/120 = 0.8333333333333334`; this is event replay reduction, not token reduction. One checkpoint was created and restart reconstruction was equal.

The full deterministic suite contains 228 passing tests. Live model evaluation is `CALIBRATION_RECORDED`; benchmark status is `NOT_RUN`, and no token or duration claim is made.

The later real run `E5-LIVE-004` is preserved as immutable evidence: the model repaired the fixture (`FAIL → PASS`) but reached the 20-turn limit before producing a completion claim. Its closeout classification and the separate functional/agent/task state decision are recorded in `E5_LIVE_004_CLOSEOUT.md`; it is not relabeled as success.

## Real Model Calibration

The preserved MiMo calibration progression is recorded as evidence, not as a benchmark:

- **A — authentication:** the initial live probe failed authentication.
- **B — provider connection:** a subsequent probe failed at provider connection.
- **C — MiMo semantics:** the provider required explicit OpenAI-compatible request/reasoning compatibility handling.
- **D — profile:** the `mimo` profile made the provider mode and model configuration explicit.
- **E — sustained autonomous SWE:** the first sustained run inspected, edited, tested, and validated the calculator fixture.
- **F — bounded completion:** both E5-LIVE-004 and E5-LIVE-005 achieved `FAIL → PASS` functional validation but ended at the 20-turn limit without an agent completion claim.

Design findings from this progression:

1. OpenAI-compatible APIs do not guarantee identical multi-turn semantics.
2. A bounded compatibility layer is safer than embedding provider behavior in planning or validation.
3. Functional validation, agent completion, and harness/task state are separate dimensions.
4. Inner `FINAL` is not equivalent to Task `COMPLETED`.
5. Inner `FAILURE` is not equivalent to Task `FAILURE` when the outer validator passes.
6. Prompt guidance reduced waste but did not solve termination.
7. Acceptance-based early stopping is a future efficiency hypothesis only; it was not implemented or claimed here.

The exact run comparison is in `PROJECT_METRICS.md`; E5-LIVE-004 remains byte-for-byte immutable and E5-LIVE-005 records only the supplied sanitized run facts, with unavailable fields left null.

Evidence manifest (SHA-256):

- `evals/runs/E5-LIVE-004.json`: `E838EB668DCC6FD21A489833C2830D43FA7E546CA8C40C43DE950471BFBF8EB8`
- `evals/runs/E5-LIVE-005.json`: `EA622D7C36E36EE3AEF6DC8EC0C54FB2A58314CE796AA284DB2BEDAB2D149B7E`

## Live Model Evaluation

`scripts/e5_live_model_smoke.py` is a manual entrypoint that uses the real `OpenAIAgentsBackend` and SDK Runner with a temporary staged fixture. It allocates the next exclusive `evals/runs/E5-LIVE-NNN.json` path only when configuration is present, records the selected provider profile and sanitized diagnostics, and never persists reasoning or credentials. Deterministic CI remains offline. The MiMo probe finding is recorded separately in `E5_PROVIDER_COMPAT_FINDING.md`; the phase evidence is the two-run calibration record and its benchmark status remains `NOT_RUN`.

## Design Decisions

- Checkpoint is durable state, not Memory; no transcript, raw file content, stdout/stderr, secrets, or reasoning is persisted.
- Event cursor enables incremental replay instead of full-history replay.
- `WorkingStateProjector` consumes only bounded safe observation events.
- `ContextBuilder` remains the sole Outer→Executor context assembly boundary; reconstruction only supplies structured CP-3 inputs.
- Retry attempts use `ContextReconstructionService` to feed checkpoint working state and a safe event delta back through `ContextBuilder(policy=CP-3)`; no second prompt builder exists.
- UTF-8 byte budgets and deterministic SHA-256 make reconstruction comparable and bounded.
- E5 does not implement process resume, subprocess continuation, or workspace restoration after a crash.

## Problems Found During Review

The implementation required an explicit checkpoint cursor contract, safe observation events rather than raw tool results, integrity verification on reload, and separate context metrics so model-visible sections remain free of accounting metadata.

## Trade-offs

Working state is intentionally a compact projection. It can omit historical detail, but it avoids unbounded prompt growth and keeps the recovery boundary explainable.

## Known Limitations

MiMo termination remains unreliable in this fixture: both real runs reached the 20-turn maximum. Tokens were unavailable, and the evidence is not a latency benchmark. There is no automatic process resume, crash-time workspace recreation, dynamic replan, formal benchmark suite, or larger-than-tiny fixture. Existing orchestrator checkpoint timing is terminal-attempt based; full process lifecycle resume is deferred.

## Next Hypothesis

When a failed or interrupted Inner Agent leaves useful state, reconstruct and validate that state: a validated PASS should finish without retry, while a validated FAIL should recover, retry, or resume. E6 may test this across process restart/resume without replaying or duplicating completed side effects. This is a hypothesis only; it is not implemented in E5.

## Roadmap Decision Record

After E4, the project deliberately prioritized Checkpoint and Context Reconstruction over Human Apply, MCP, Skills, and Pi. This tests the core Long-Horizon hypothesis before adding peripheral capabilities.

## Metric Semantics

`IMPLEMENTED` means code exists, `TESTED` means deterministic assertions pass, `DETERMINISTIC_EVAL` means the canonical fixture above, `LIVE_MODEL_EVAL` is skipped, and `BENCHMARK` is not claimed. No token or cost reduction is inferred from event replay reduction.
