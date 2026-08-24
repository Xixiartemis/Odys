# E4 Evidence Report

## What

E4 adds an isolated `StagedWorkspace`, exact replacement editing, SHA optimistic locking, atomic writes, bounded diff/restore, explicit side-effect grants, mutation audit events, and a deterministic fail→edit→pass→diff loop.

## Why

The source repository must remain immutable while an agent investigates and proposes a fix. All mutation occurs in a temporary staging copy; the candidate patch is returned as an execution artifact for outer validation.

## Architecture

```text
Source -> StagedWorkspace -> Edit/Test/Diff -> ChangeSet Artifact -> Outer Validator
```

## Quantitative Evidence

- Full deterministic suite: 200 passed.
- Calculator scenario: 1 run, 1 success, 1 outer attempt, 6 inner tool calls, 1 modified file.
- Test observation before edit: FAIL; after edit: PASS.
- Source repository SHA remained unchanged.
- Live model evaluation: `SKIPPED_CONFIG`; no tokens or duration are claimed.

## Design Decisions

- Exact replacement is used instead of arbitrary `write_file`; zero or multiple matches fail safely.
- `expected_sha256` prevents edits based on stale observations.
- Writes use a flushed temporary sibling and atomic replace.
- Side-effect tools require both capability listing and explicit side-effect grant; human-approval tools remain blocked.
- Staging root isolation prevents source/stage overlap, and copy limits prevent unbounded staging.
- Nonzero test exit is a successful observation, so the agent can continue diagnosis.

## Problems Found During Review

- Side-effect grant was initially not wired through `InnerAgentExecutor`.
- Candidate artifact initially lacked the actual bounded diff.
- Staging root overlap could have allowed unsafe copy placement.
- Workspace mutation events were initially declared but not persisted.

## Trade-offs

Staging is not an OS sandbox. An explicitly allowed subprocess still runs with host OS permissions and may have filesystem side effects.

## Known Limitations

There is no source apply operation, file create/delete/rename, git mutation, human approval integration, or real-model evaluation in E4. Safe CLI remains policy-constrained subprocess execution rather than kernel isolation.

## Next Hypothesis

E5 should validate human-gated candidate patch application and approval/audit semantics without weakening source isolation.

## Metric Semantics

`IMPLEMENTED` describes code paths, `TESTED` describes deterministic assertions, `DETERMINISTIC_EVAL` describes the fixture loop above, and `LIVE_MODEL_EVAL` is skipped. No benchmark success rate, token count, or duration is inferred.
