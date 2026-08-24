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

The full deterministic suite contains 210 passing tests. Live model evaluation is `SKIPPED_CONFIG`; no token or duration claim is made.

## Design Decisions

- Checkpoint is durable state, not Memory; no transcript, raw file content, stdout/stderr, secrets, or reasoning is persisted.
- Event cursor enables incremental replay instead of full-history replay.
- `WorkingStateProjector` consumes only bounded safe observation events.
- `ContextBuilder` remains the sole Outer→Executor context assembly boundary; reconstruction only supplies structured CP-3 inputs.
- UTF-8 byte budgets and deterministic SHA-256 make reconstruction comparable and bounded.
- E5 does not implement process resume, subprocess continuation, or workspace restoration after a crash.

## Problems Found During Review

The implementation required an explicit checkpoint cursor contract, safe observation events rather than raw tool results, integrity verification on reload, and separate context metrics so model-visible sections remain free of accounting metadata.

## Trade-offs

Working state is intentionally a compact projection. It can omit historical detail, but it avoids unbounded prompt growth and keeps the recovery boundary explainable.

## Known Limitations

There is no automatic process resume, crash-time workspace recreation, LLM summarization, vector memory, or live-model benchmark. Existing orchestrator checkpoint timing is terminal-attempt based; full process lifecycle resume is deferred.

## Next Hypothesis

E6 should test process-level resume semantics using durable checkpoints without replaying or duplicating completed side effects.

## Roadmap Decision Record

After E4, the project deliberately prioritized Checkpoint and Context Reconstruction over Human Apply, MCP, Skills, and Pi. This tests the core Long-Horizon hypothesis before adding peripheral capabilities.

## Metric Semantics

`IMPLEMENTED` means code exists, `TESTED` means deterministic assertions pass, `DETERMINISTIC_EVAL` means the canonical fixture above, `LIVE_MODEL_EVAL` is skipped, and `BENCHMARK` is not claimed. No token or cost reduction is inferred from event replay reduction.
