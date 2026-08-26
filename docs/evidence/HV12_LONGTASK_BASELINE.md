# HV-1.2 Long-Task Recovery Baseline

Harness: `HV-1.2`
Evaluation: `HV12-DRY-001` (offline deterministic evidence; live remains manual)
Offline artifact: `evals/runs/HV12-DRY-001.json`
Canonical live artifact: `evals/runs/HV12-LIVE-001.json` (`FAIL`, immutable)
Fixture version: `HV12-SESSION-LIFECYCLE-1`

## PURPOSE

This baseline asks whether the frozen HV-1.2 outer Harness can finish a
multi-module SWE task after the Python process running the real Inner Agent is
forcibly terminated only after useful edits are durable in the workspace. A
new Python process then calls `RecoveringOrchestrator.resume_run(run_id)`.

The baseline measures recovery-only behavior. It does not add dynamic replan,
provider conversation restoration, or any new Harness capability.

## EXPERIMENT_DESIGN

The fixture is `evals/fixtures/hv12_session_lifecycle/`. It models tenant
scoping, cached sessions, tombstones, and delayed inbound messages across four
source modules and three tests. The initial fixture is red because the cache
key omits the tenant and the delayed-message route recreates a tombstoned
session. A correct behavioral repair is green without requiring a particular
patch shape.

The live path uses:

- `RecoveringOrchestrator` as the top-level loop;
- `RunWorkspaceManager` and `DurableWorkspaceSession`;
- a workspace-aware `InnerAgentExecutor`;
- the real `OpenAIAgentsBackend` with the explicit MiMo compatibility profile;
- a deterministic pytest validator;
- `max_attempts=3` and exactly 20 Inner-Agent turns per attempt.

The evaluator copies the fixture to a temporary immutable source root. The
durable session owns the separate `baseline/` and `work/` trees. Process A
opens the same file-backed SQLite database and runs the outer Harness. Process
B is a newly spawned Python process with a new database/workspace object graph;
it reopens the recorded run and calls `resume_run(run_id)`.

The offline dry run uses scripted executor behavior in the same outer state
machine. It is classified as `mode=deterministic_process_recovery_fixture`,
not as a live-model result.

## DRY_RUN_RESULT

The regenerated artifact is `PASS` from clean code commit
`a685f4e981bd8281875021e0393f6626976a8f87`. It records `initial_tests=FAIL`,
`final_tests=PASS`, stable durable mutation before OS-level termination,
`subprocess.Popen.terminate` for Process A, and a fresh Process B that calls
`resume_run(run_id)`. The durable attempt count is 1 before resume and 2 at
finalization, so `new_attempts_after_resume=2-1=1`; the post-resume executor
call list contains attempt 2. Validator-before-retry is observed with one
CP-3 retry, the workspace session id is reused, and all duplicate durable-row
counts are zero.

The artifact records both `repository_fixture_unchanged=true` and
`temporary_source_snapshot_unchanged=true`, with separate before/after hashes
for the copied source snapshot. It also records `evaluation_id`, `phase`,
`git_sha`, `harness_version`, and `fixture_version` at the top level.

## LIVE_RUN_RESULT

The first real-model run is retained unchanged as `HV12-LIVE-001` with
`mode=live_real_model`, model `mimo-v2.5`, code SHA
`7d63314ba08147042f841317da02589180c02b8f`, and `status=FAIL`. Its initial
tests were `FAIL` and final tests were `PASS`, while
`functional_validation_passed=true`, `agent_completion_passed=false`,
`outer_task_completed=false`, and `process_recovery_passed=false`.

The canonical live run had one durable attempt before resume and three total
attempts, yielding two post-resume attempts. Both post-resume attempts used
CP-3 and the executor call list was `[2, 3]`. The same workspace session was
reused, all duplicate durable-row counts were zero, both source immutability
checks were true, and `total_wall_duration_ms=412421`. Post-resume token
metrics are `null`/not available.

## OBSERVED_FAILURE

Recovery mechanics were observed: Process A was forcibly terminated after a
stable durable mutation, a fresh Process B called `resume_run(run_id)`, the
same workspace was reused, and CP-3 continuation occurred twice. The canonical
composite remains `process_recovery_passed=false` because the benchmark
requires final outer Run and Task completion. The run is intentionally not
turned into a PASS by the final pytest result.

Attempt 1 was `CRASHED` with `PROCESS_INTERRUPTED` and
`FORCED_PROCESS_TERMINATION`. Attempts 2 and 3 were `FAILED` with
`AGENT_TURN_LIMIT` and `TURN_LIMIT`, each at 20 turns. Only one validator call
occurred after resume. Attempt 2's post-attempt pytest state was
`NOT_MEASURED`; only the final workspace after Attempt 3 is known to pass
pytest.

## ROOT_CAUSE

HV-1.2 `ResumeDecisionService` validates completed attempts without validation
and performs workspace recovery validation for `PROCESS_INTERRUPTED`. Failed
or timed-out attempts instead enter failure classification without first
validating whether the durable workspace already satisfies the acceptance
criteria. Consequently, an executor failure can prevent the Outer Validator
from arbitrating useful durable workspace state before recovery consumes
another attempt.

This is a harness finding, not a claim that Attempt 2 was already correct.
Its post-attempt pytest state was not measured.

## SECONDARY_FINDING

The post-resume agent made 57 tool calls with 16 tool failures, a derived
failure rate of `16/57 ≈ 28.07%`. The safe tool-failure counts include
`EDIT_TARGET_NOT_FOUND=15` and `EDIT_TARGET_AMBIGUOUS=1`. These are derived
from the canonical attempt records; no raw transcript or credentials are
persisted.

## NEXT_HYPOTHESIS

E6-D / HV-1.3 should introduce post-non-success outcome arbitration:

```text
Agent modifies workspace
→ Agent ends AGENT_TURN_LIMIT
→ Outer Validator runs
→ Validator PASS
→ Task COMPLETED
→ no unnecessary next Attempt
```

If Validator FAILs, failure classification, recovery, checkpointing, and the
next-attempt path should remain available. Dynamic Replan remains out of scope.

## CRASH_TRIGGER

The parent polls only local SQLite rows, the workspace manifest, the baseline,
and the work tree. It waits for all of the following:

1. the work tree differs from the immutable baseline;
2. at least one changed path is under `src/`;
3. the work-tree hash is identical across two poll observations.

It then uses OS-level `subprocess.Popen.terminate()` (or the recorded
`Popen.kill()` fallback) on Process A. No in-process exception simulates the
canonical live crash. If the observation window expires without a durable
mutation, the result records `CRASH_TRIGGER_NOT_REACHED` and is not treated as
a successful recovery experiment.

Immediately after termination, the result records only safe facts: run and
attempt status, session identity, changed paths/counts, patch hash, source
tree hash, and work-tree hash. Raw diff text is never persisted.

## SUCCESS_CRITERIA

Strict final success requires all of:

- initial pytest is `FAIL`;
- final pytest is `PASS`;
- final Run and Task are `COMPLETED`;
- the final validator patch is non-empty;
- the repository fixture remains unchanged;
- the temporary copied source snapshot remains unchanged;
- the same durable workspace session id is observed before and after restart;
- Process A was forcibly terminated after the stable mutation trigger;
- Process B completed through `resume_run(run_id)`.

The result keeps these dimensions separate:
`functional_validation_passed`, `agent_completion_passed`,
`outer_task_completed`, and `process_recovery_passed`. An Agent completion
claim is never treated as authoritative over the validator.

## METRIC_SCHEMA

Each attempt records, where durable evidence exists:

- attempt/Inner status, termination status, duration, turn and tool counts;
- capability-level tool counts and failure counts;
- safe token usage fields (`null` when provider usage is unavailable);
- completion-claim presence, context policy, checkpoint use, and checkpoint creation.

Outer metrics record process instances, Process A pid and termination
mechanism, crash trigger, crash-time changed files and patch hash, attempts
before/after restart using the observed durable pre-resume count, crashed
attempts, resume validation, checkpoint and CP-3 counts, workspace-session
reuse, post-resume executor/validator calls, duplicate durable records, the
two source immutability checks, and wall duration.

The pre-crash Inner trace is allowed to be incomplete. If the backend had not
returned before the OS termination, turn/tool metrics are `null` rather than
fabricated from a missing trace. Workspace recovery state remains
authoritative and is represented by bounded paths/counts/hashes only.

## DESIGN_DECISIONS

- The evaluation uses the existing frozen outer state machine; it does not
  bypass the Outer Harness by calling `InnerAgentExecutor` as the loop.
- The live worker exposes only `workspace.list`, `workspace.read`,
  `workspace.search`, `workspace.edit`, `workspace.diff`, and `cli.exec`.
  `workspace.edit` is the only side-effect capability. The CLI policy allows
  pytest commands only.
- The source checkout is never handed to the Agent. A temporary source copy
  and the durable session baseline/work split make source immutability
  independently checkable.
- Worker A and Worker B are launched with the active evaluation interpreter
  (`sys.executable`); live executor calls are counted by an evaluation-only
  recorder shared by factory-created executors in a resumed process.
- Completion claims are extracted from the persisted `ExecutionResult.raw`
  `InnerAgentResult` when present. Termination status preserves forced process
  termination, turn-limit, successful completion, and stable failure types.
- Validator-before-retry is exercised by running pytest during recovery before
  CP-3 and a possible next attempt are created.
- The live result path is exclusive: a pre-existing
  `HV12-LIVE-001.json` is not overwritten.

## TRADEOFFS

The deterministic crash trigger favors a reproducible observation window over
capturing every last provider trace event. A process can disappear between a
workspace write and the backend's returned trace; the evidence therefore
separates incomplete trace accounting from durable workspace recovery.

The fixture validator runs pytest as a bounded local command and stores only
exit/status facts. This preserves behavioral authority without putting raw
stdout/stderr into the evidence file.

## KNOWN_LIMITATIONS

- The live evaluator is manual-only and requires `ODYS_AGENT_MODEL` and
  `ODYS_AGENT_API_KEY`. CI and the deterministic test suite never call a live
  provider.
- No provider-internal conversation state is restored across the process
  boundary; only durable Harness/workspace state is resumed.
- External side effects and concurrent resume ownership are outside this
  local fixture's scope.
- Dynamic replan is intentionally absent; this is a recovery-only HV-1.2
  baseline and does not enter E7.

## DRY_RUN_NEXT_HYPOTHESIS

If HV-1.2 resumes and completes but needs repeated recovery attempts because
the underlying strategy is wrong, E7 Dynamic Replan is justified. If state is
lost across the process boundary, fix the recovery Harness before E7. If the
run completes cleanly, preserve it as the Recovery-Only baseline for later E7
comparison.

## RUN_POLICY

Implementation and deterministic verification must use no live model/API.
Review the code and dry-run artifact first. Only after explicit approval may
the user manually execute exactly one live run:

```text
ODYS_AGENT_MODEL=<model> ODYS_AGENT_API_KEY=<key> \
python scripts/hv12_longtask_recovery.py --mode live_real_model
```

The deterministic implementation did not create the canonical live artifact.
`HV12-LIVE-001` is immutable and must not be retried; any future live
comparison requires a new evaluation ID.
