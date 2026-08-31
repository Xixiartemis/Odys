# E7-A Tool-Aware Recovery & Verification

Task: `ODYS-E7A-TOOL-AWARE-RECOVERY-01`

Harness: `HV-1.5`

Deterministic evaluation: `HV15-E7A-DRY-001`

## Scope and evidence boundary

E7-A addresses the measured HV12/HV13 tool-recovery bottleneck without adding
Dynamic Macro Replan, a new planner, more attempts, or more Inner-Agent turns.
The canonical HV12/HV13 live artifacts remain byte-identical. No live model or
MiMo call was made.

`HV15-E7A-DRY-001` is deterministic dry evidence over the frozen
`HV12-SESSION-LIFECYCLE-1` fixture. Its legacy arm executes the same edit
implementation with safe normalization disabled; its E7-A arm enables the new
policy. This is a controlled mechanism comparison, not a historical replay and
not evidence of a stochastic success-rate improvement.

## Implementation contract

`workspace.edit` now resolves targets in this order:

1. one exact occurrence: mutate that exact range;
2. multiple exact occurrences: `EDIT_TARGET_AMBIGUOUS`;
3. no exact occurrence: compare whole-line candidates after normalizing only
   CRLF/LF and trailing horizontal whitespace;
4. one normalized candidate: mutate that bounded range;
5. zero or multiple normalized candidates: fail without mutation.

Leading whitespace, internal whitespace, line order, and non-whitespace text
are never fuzzed. Partial-line approximate matching is not allowed. Successful
edits report match mode, candidate count, line range, and before/after hashes;
`workspace.diff` remains the mutation audit. A byte-identical result is
`NO_CHANGE` and is not written.

Failures carry a safe category and actionable bounded diagnostics for
`EDIT_TARGET_NOT_FOUND`, `EDIT_TARGET_AMBIGUOUS`, `INVALID_EDIT_RANGE`,
`NO_CHANGE`, stale versions, and workspace path errors. Candidate text and raw
arguments are not persisted.

## Attempt-local recovery and verification call chain

```text
Inner Agent
→ FunctionTool adapter
→ attempt-local ToolAwareObserver
→ ToolRegistry / workspace or CLI tool
→ structured ToolResult
→ bounded observation summary
→ durable INNER_AGENT_TOOL_OBSERVATION
→ CP-3 safe projection on a later attempt
```

Consecutive `workspace.edit` failures are grouped by capability and safe
failure category even when argument hashes differ. Exact repeats remain
separately countable. On the second similar failure the observation emits
`strategy_change_required`; a later read/search or successful reconstructed
edit emits `strategy_change_observed`. Counts are capped at 100 and marked
truncated. The adapter performs no hidden retry, and the existing turn budget
is unchanged.

The verification path is:

```text
meaningful edit
→ workspace.diff or workspace.read observation
→ allow-listed argv-only cli.exec pytest
→ real PASS / FAIL / ERROR observation
→ Agent continues or returns a candidate claim
→ Outer Validator independently accepts or rejects the Task
```

`COMMAND_NOT_ALLOWED` exposes only the bounded allowed argv prefixes and the
explicit-prefix policy category. `WORKSPACE_PATH_ESCAPE` exposes only the
workspace-relative path/cwd rule; it does not reveal the absolute workspace
root. Neither policy was widened, and shell execution remains absent.

## Safe metrics

The new projector derives workspace edit calls/successes/failures, edit failure
categories, repeated failures, observed strategy changes, read/search after an
edit failure, CLI calls/failures/categories, pytest executions and outcomes,
the first pytest tool-call index, and total tool calls/failures. Attempt turns
remain sourced from the existing Inner-Agent lifecycle events. No transcript,
reasoning, arguments, command output, credentials, or secrets enter these
metrics.

## Regression risk map

| Risk | Deterministic coverage |
|---|---|
| Exact behavior regresses | exact success and exact audit fields |
| Safe formatting drift remains brittle | trailing-space and CRLF unique normalization |
| Fuzzy edit hits a wrong location | partial-line rejection and unchanged file assertion |
| Duplicate targets mutate silently | exact and normalized ambiguity failures |
| Mutation and evidence diverge | file bytes plus `workspace.diff` agreement |
| No-op is treated as success | explicit `NO_CHANGE`, no write |
| Invalid line range is opaque | structured `INVALID_EDIT_RANGE` diagnostics |
| Repeated variants evade detection | exact-repeat and category-similar repeat tests |
| Recovery is not observable | read-after-failure and strategy-change event tests |
| Adapter retries behind the Agent | one requested failure produces one observation |
| Counters grow without bound | repeat cap and truncation test |
| CLI denial is weakened | denied git command; pytest-only policy unchanged |
| Path escape leaks or succeeds | denial, relative-only feedback, root non-disclosure |
| pytest result is fabricated | real local pytest FAIL, edit/diff, then real PASS |
| pytest bypasses acceptance | prompt contract plus existing validator-authority regression |
| E6-D arbitration regresses | full `test_e6d_outcome_arbitration.py` suite |

## Deterministic evaluation result

Both arms repaired the same two files and passed final functional validation.
Each arm ran through `RecoveringOrchestrator`, persisted an Attempt and
Validation, and reached `Run=COMPLETED` / `Task=COMPLETED` only after one real
`FixturePytestValidator` call. The controlled difference is the edit policy and
resulting tool sequence.

| Metric | Legacy exact-only arm | E7-A arm |
|---|---:|---:|
| `workspace.edit` calls | 2 | 1 |
| `workspace.edit` failures | 2 | 0 |
| Edit failure rate | 100% | 0% |
| Repeated `EDIT_TARGET_NOT_FOUND` observations | 1 | 0 |
| Total tool calls | 9 | 8 |
| Total tool failures | 2 | 0 |
| Tool failure rate | 22.2222% | 0% |
| First pytest turn/tool-call | 9 | 8 |
| Final functional validation | PASS | PASS |
| Outer Task result | COMPLETED | COMPLETED |

One scripted tool call is defined as one deterministic turn in this dry
comparison. The fixture, HV12 canonical JSON, HV13 canonical JSON, and HV13
claim marker remained unchanged.

## Known limitations and live readiness

- Safe normalization intentionally handles only whole-line newline and trailing
  whitespace differences. Other drift still requires read/search/edit-lines.
- Strategy adaptation is observable guidance, not an autonomous planner. A
  model may still spend the remaining fixed turn budget poorly.
- The dry sequence is deterministic and does not measure provider latency,
  tokens, model judgment, or success rate.
- This compact experiment uses the real Outer lifecycle but does not reproduce
  the HV13 process-kill/resume path already covered elsewhere.

The tool, safety, regression, and deterministic evidence gates are sufficient
to prepare a new, separately identified E7-A real-model protocol after human
review. No live execution is authorized or performed by this change.
