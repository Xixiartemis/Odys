# HV12-LIVE-001 Forensic Closeout

Canonical artifact: `evals/runs/HV12-LIVE-001.json`  
Evaluation: `HV12-LIVE-001`  
Model: `mimo-v2.5`  
Harness: `HV-1.2`  
Code SHA: `7d63314ba08147042f841317da02589180c02b8f`

## Canonical facts

The first real-model HV-1.2 run is preserved as `status=FAIL`. The initial
fixture tests failed and the final fixture tests passed, so
`functional_validation_passed=true`. The outer task did not complete and the
canonical composite `process_recovery_passed=false`; the run is not relabeled
as a pass.

- Process A was forcibly terminated after a stable durable workspace mutation.
- Process B was a new process, called `resume_run(run_id)`, and reused the same
  workspace session.
- Attempts 2 and 3 both used CP-3 and both ended with `AGENT_TURN_LIMIT`.
- The post-resume executor call list was `[2, 3]`.
- There was one validator call after resume.
- Duplicate attempts, validations, failure reports, recovery actions, and
  checkpoints were all zero.
- The repository fixture and temporary source snapshot were unchanged.

The canonical run retained `agent_completion_passed=false`,
`outer_task_completed=false`, and `process_recovery_passed=false` even though
final pytest passed. These are separate outcome dimensions.

## Attempt forensics

| Attempt | Status | Error type | Termination | Context | Turns | Tool calls | Tool failures |
|---:|---|---|---|---|---:|---:|---:|
| 1 | `CRASHED` | `PROCESS_INTERRUPTED` | `FORCED_PROCESS_TERMINATION` | `CP-2` | N/A | N/A | N/A |
| 2 | `FAILED` | `AGENT_TURN_LIMIT` | `TURN_LIMIT` | `CP-3` | 20 | 27 | 9 |
| 3 | `FAILED` | `AGENT_TURN_LIMIT` | `TURN_LIMIT` | `CP-3` | 20 | 30 | 7 |

Derived from the canonical attempt records:

- post-resume tool calls: `27 + 30 = 57`;
- post-resume tool failures: `9 + 7 = 16`;
- tool failure rate: `16 / 57 ≈ 28.07%`;
- `EDIT_TARGET_NOT_FOUND=15`;
- `EDIT_TARGET_AMBIGUOUS=1`.

These are derived metrics, not additional persisted runtime facts. Attempt 2's
post-attempt pytest state was **NOT_MEASURED**. The only known passing test
state is the final workspace state after Attempt 3.

## Recovery mechanics versus canonical outcome

Recovery mechanics evidence is positive: the forced kill was observed, a new
process started, `resume_run` was used, the same workspace was reused, CP-3
continuation occurred twice, and duplicate durable lifecycle rows were zero.

The canonical composite remains `process_recovery_passed=false` because this
benchmark requires final outer Run and Task completion. The mechanics evidence
must not be substituted for that composite.

## Root-cause classification

### Facts

The durable state shows a forced process interruption after useful workspace
mutation, successful same-workspace resume, two post-resume executor attempts,
two CP-3 contexts, no duplicate durable lifecycle rows, and final pytest PASS.
The outer task remained incomplete. Attempts 2 and 3 ended at the 20-turn
`AGENT_TURN_LIMIT`. Only one validator call occurred after resume.

### Harness finding

HV-1.2 `ResumeDecisionService` validates completed attempts without validation
and performs workspace recovery validation for `PROCESS_INTERRUPTED`. Failed
or timed-out attempts instead enter failure classification without first
validating whether the durable workspace already satisfies the acceptance
criteria.

Therefore, an executor failure can prevent the Outer Validator from discovering
that the workspace already contains useful or sufficient work before recovery
consumes another attempt. This is a harness finding, not a claim that Attempt 2
was already correct; its post-attempt pytest state was not measured.

## Secondary finding and limitation

The real-model agent spent all post-resume turns on edit attempts, with
`EDIT_TARGET_NOT_FOUND` as the dominant safe tool failure. The evidence supports
an edit-target robustness problem, but it does not establish that this was the
only cause of the incomplete outer task. No raw transcript or credentials are
part of the evidence.

## Next hypothesis

E6-D / HV-1.3 should introduce post-non-success outcome arbitration:

```text
Agent modifies workspace
→ Agent ends AGENT_TURN_LIMIT
→ Outer Validator runs
→ Validator PASS
→ Task COMPLETED
→ no unnecessary next Attempt
```

If the validator fails, the existing failure-classification, recovery,
checkpoint, and next-attempt path should remain available. Dynamic Replan is out
of scope.

## Immutability policy

`HV12-LIVE-001` is canonical and immutable. It must not be retried or rewritten.
Any future live comparison requires a new evaluation ID.
