# HV13-LIVE-001 Forensic Closeout

Canonical artifact: `evals/runs/HV13-LIVE-001.json`

Canonical SHA256: `3e6ecf1b718d2e8f0a292fa4cf8f1d2d71c455128f3d0a23fabad81079840bb5`

Evaluation: `HV13-LIVE-001`

Model: `mimo-v2.5`

Harness: `HV-1.3`

Code SHA: `dcde4d28139f07bb5c1639683d6fe45f4c8770e5`

## Canonical facts

The canonical real-model result is `status=FAIL` and
`long_horizon_result=FAIL`. Initial pytest and final pytest both failed. The
artifact therefore retains all of the following as `false`:

- `functional_validation_passed`;
- `agent_completion_passed`;
- `outer_task_completed`;
- `process_recovery_passed`;
- `process_recovery_mechanics_passed`.

Process A was forcibly terminated after a stable durable workspace mutation.
A fresh Process B invoked resume, reused the same durable workspace session,
and continued with two CP-3 attempts. The source repository, repository
fixture, and temporary source snapshot were unchanged. There were three
attempts in total, two CP-3 attempts, and zero duplicate validations, failure
reports, recovery actions, or checkpoints.

These continuity facts do not change the canonical result. Final functional
state remained invalid, so neither recovery composite passed.

## Attempt forensics

| Attempt | Status | Error type | Context | Turns | Tool calls | Tool failures |
|---:|---|---|---|---:|---:|---:|
| 1 | `CRASHED` | `PROCESS_INTERRUPTED` | `CP-2` | N/A | N/A | N/A |
| 2 | `FAILED` | `AGENT_TURN_LIMIT` | `CP-3` | 20 | 29 | 7 |
| 3 | `FAILED` | `AGENT_TURN_LIMIT` | `CP-3` | 20 | 29 | 12 |

Attempt 1 has an intentionally incomplete pre-crash tool trace. Durable
mutation was observed before termination, and `src/session_store.py` was
recorded as changed at the crash boundary. Tool-call and token totals for this
attempt are unavailable and are not inferred.

### Attempt 2

Calls by capability:

- `workspace.diff=2`;
- `workspace.edit=9`;
- `workspace.list=1`;
- `workspace.read=14`;
- `workspace.search=3`.

All seven failures came from `workspace.edit`, and all were
`EDIT_TARGET_NOT_FOUND`. The tool failure rate was `7 / 29 = 24.14%`; the edit
failure rate was `7 / 9 = 77.78%`.

### Attempt 3

Calls by capability:

- `cli.exec=2`;
- `workspace.diff=2`;
- `workspace.edit=11`;
- `workspace.list=1`;
- `workspace.read=13`.

Failures were `workspace.edit=10` and `cli.exec=2`. Failure types were
`EDIT_TARGET_NOT_FOUND=10`, `COMMAND_NOT_ALLOWED=1`, and
`WORKSPACE_PATH_ESCAPE=1`. The tool failure rate was
`12 / 29 = 41.38%`; the edit failure rate was `10 / 11 = 90.91%`.

### Post-resume aggregate

The two post-resume attempts consumed 40 turns and made 58 tool calls, of
which 19 failed: `19 / 58 = 32.76%`. They made 20 `workspace.edit` calls, of
which 17 failed: `17 / 20 = 85.00%`. Both `cli.exec` calls failed.

Input, output, and total token metrics remain `null` / not available. No token
metrics are estimated.

## Outcome Arbitration finding

`outcome_arbitration_observed=true` with two durable events:

| Attempt | Executor status | Error type | Validator | Later attempt exists |
|---:|---|---|---|---|
| 2 | `FAILED` | `AGENT_TURN_LIMIT` | `false` | `true` |
| 3 | `FAILED` | `AGENT_TURN_LIMIT` | `false` | `false` |

HV-1.3 Outcome Arbitration was exercised twice in the real-model run. It
correctly rejected the invalid workspace both times and did not falsely
complete the task. Outcome Arbitration did not fail.

This differs from HV12-LIVE-001, where final pytest passed while the outer task
still failed. In HV13-LIVE-001, final pytest remained failing and both
arbitration validations correctly returned false.

## Instrumentation semantic limitation

The latest Attempt 3 observation records
`next_attempt_suppressed_after_arbitration=true`. This does not establish
causal retry suppression: Attempt 3 already reached `max_attempts=3`, and its
validation failed. The current field observes only that no later attempt
exists.

Future instrumentation should distinguish the direct observation
`later_attempt_exists` from the causal claim
`retry_suppressed_by_successful_arbitration`. The canonical JSON remains
unchanged.

## HV12 versus HV13 single-run paired observation

**SINGLE-RUN PAIRED OBSERVATION.** These are two individual stochastic runs,
not a success-rate estimate.

| Metric | HV12-LIVE-001 (HV-1.2) | HV13-LIVE-001 (HV-1.3) |
|---|---:|---:|
| Final pytest | `PASS` | `FAIL` |
| Outer task | `FAIL` | `FAIL` |
| Post-resume turns | 40 | 40 |
| Tool calls | 57 | 58 |
| Tool failures | 16 | 19 |
| `workspace.edit` calls | 22 | 20 |
| `workspace.edit` failures | 16 | 17 |

This pair does not support a claim that HV-1.3 reduced or worsened stochastic
success rate.

## Root-cause boundary

The evidence supports these findings:

1. Process termination/restart continuity worked.
2. Durable workspace reuse worked.
3. CP-3 continuation worked.
4. Outcome Arbitration ran twice and correctly rejected invalid state.
5. Both post-resume attempts exhausted their 20-turn budgets.
6. `workspace.edit` robustness is a major measured bottleneck.
7. Attempt 3 failed both `cli.exec` calls.
8. Final functional state remained invalid.

The evidence does not retain the exact final failing pytest assertion, exact
incorrect final code, raw model reasoning, raw transcript, diff, or stdout.
It therefore does not support a more specific code diagnosis, a claim that
edit failures were the only cause, or a claim that Dynamic Replan alone would
have solved the task.

## Next hypothesis

**E7-A — Tool-Aware Recovery & Verification**

- **H1:** Exact-target `workspace.edit` is too brittle for long-horizon
  stochastic repair.
- **H2:** Repeated `EDIT_TARGET_NOT_FOUND` consumes turn budget without
  sufficient strategy adaptation.
- **H3:** The agent does not establish a reliable early
  edit → pytest → observe loop.
- **H4:** CLI policy/path feedback is insufficiently actionable for recovery.

Before Dynamic Macro Replan, measure whether better edit ergonomics and
verification discipline reduce tool failure and enable functional completion.
E7-B — Dynamic Macro Replan remains a future phase after lower-level tool
robustness is measured; it is not started by this closeout.

## Immutability policy

`HV13-LIVE-001` is canonical and immutable. The live run must not be retried,
and its JSON and claim marker must not be deleted, modified, or regenerated.
