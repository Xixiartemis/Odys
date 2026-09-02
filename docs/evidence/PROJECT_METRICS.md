# Project Metrics Ledger

| Phase | Harness | Tests | CI | Primary Capability | Deterministic Eval | Live Eval | Known Data Gaps |
|---|---|---:|---|---|---|---|---|
| E2 | HV-0.7 | 171 passed (repository closeout record) | N/A | Inner Agent backend | Not recorded as a benchmark | Not recorded | Historical tokens/duration/success rate unavailable |
| E3 | HV-0.8 | 188 passed (repository closeout record) | N/A | Read-only Workspace and Safe CLI | Not recorded as a benchmark | NOT_RUN | Historical tokens/duration/success rate unavailable |
| E4 | HV-0.9 | 203 passed | SUCCESS (CI verified at `40d4c53`; final PR #8 merged as `0b981ff`) | Staged edit/test loop and candidate patch artifact | 1/1, one outer attempt, six inner calls, one file | NOT_RUN | Live metrics unavailable; implementation diff and final PR diff are separate in E4 summary |
| E5 | HV-1.0 | 228 passed locally | PASS (PR merge-result workflow) | Durable checkpoint and bounded CP-3 context reconstruction | 120 events, cursor 100, replay 20, 0.8333333333333334 replay reduction, restart equal | Two real MiMo fixture runs; functional 2/2, agent completion 0/2, both TURN_LIMIT | Tokens unavailable; no latency or termination improvement; scope is E5 calculator fixture calibration only |
| E6-B | HV-1.1 | 239 passed locally | PASS (exact-head push CI) | Manual process resume with durable run/workspace binding | Two restart scenarios: validate-without-retry and CP-3 retry in same workspace | NOT_RUN | Historical baseline; superseded by E6-C crash-window evidence |
| E6-C | HV-1.2 | 254 passed locally | PASS (exact-head push CI) | Crash-window-aware durable resume and generalized continuation | 10 crash windows recovered; 3 CREATING-root scenarios; 3-attempt continuation; W5/W7 closeout contracts | NOT_RUN | No live provider or external side-effect exactly-once semantics |
| HV12-LIVE-001 | HV-1.2 | 262 passed locally | PASS (exact-head CI for code/evidence branch) | Real-model long-task process recovery baseline | Real model; canonical status FAIL; functional repair PASS; outer completion FAIL; 3 attempts; 2 post-resume attempts; 40 post-resume turns; 57 post-resume tool calls; 16 post-resume tool failures; CP-3=2; same workspace; duplicate durable records=0; wall duration 412421 ms | MiMo v2.5, `real_model=true` | Tokens unavailable (`null`/not available); Attempt 2 post-state not measured |
| E6-D | HV-1.3 | 271 passed locally | NOT_RUN (local implementation) | Post-non-success outcome arbitration for durable workspace attempts | 5 arbitration-pass scenarios; 3 arbitration-fail scenarios; 2 timeout scenarios; 4 restart windows; duplicate validation/report/action/checkpoint rows 0; 5 scenario-level unnecessary attempts avoided | NOT_RUN | Deterministic only; no live improvement claim; historical Attempt 2 remains NOT_MEASURED |
| HV13-DRY-001 | HV-1.3 | 287 passed locally | NOT_RUN (local preflight correction) | Controlled real-process long-task validation harness with safe live evidence preflight | Clean code SHA `194dfff`; initial pytest FAIL; forced stable-mutation termination; fresh Process B; same workspace; 2 attempts; Attempt 1 CRASHED/PROCESS_INTERRUPTED; Attempt 2 FAILED/AGENT_TURN_LIMIT; final pytest PASS; Run/Task COMPLETED; CP-3=1; 1 bounded arbitration event, latest Attempt 2 PASS; Attempt 3 absent; historical recovery=true; mechanics=true; duplicate durable rows=0; safe attempt metrics retained; source and temporary snapshot unchanged | NOT_RUN | Deterministic only; completion claim is durable-evidence-derived; live result/claim not created; no causal improvement claim |
| HV13-LIVE-001 | HV-1.3 | 287 passed locally | See PR #13 exact-head checks | Real-model long-task recovery with post-non-success Outcome Arbitration | Canonical status FAIL; initial/final pytest FAIL; 3 attempts; CP-3=2; 40 post-resume turns; 58 tool calls; 19 failures; edit failures 17/20; 2 arbitration-fail events; same workspace; duplicate durable rows=0; source and temporary snapshot unchanged | MiMo v2.5, `live_real_model`; canonical FAIL | Single run only; tokens unavailable; raw diff/stdout/transcript and exact final failing assertion intentionally not retained; no success-rate claim |
| CLI Alpha | HV-1.4 | 321 passed locally; 34 new | NOT_RUN (local implementation; no push) | Product vertical slice: durable CLI run/resume/inspect, explicit command validation, Rich event projection, edit-line recovery, CP-3 failure memory | 5/5 primary commands; 9 edit-line safety cases; 2 CP-3 failure-memory cases; 2 offline end-to-end scenarios; interrupted resume reused exact session/root; source unchanged | NOT_RUN; offline deterministic provider only | Product evidence, not canonical benchmark; no real-model performance claim; Dynamic Macro Replan not implemented |
| Agent Platform Foundation | HV-1.5 | 367 passed locally; 46 new | NOT_RUN (local implementation; no push) | Unified AgentKernel, Root/Planner/Worker roles, durable child delegation, Skills/Memory/Session/Knowledge/Toolsets/MCP stdio and ContextAssembler | 5 roles; 6 toolsets; 2 bundled skills; 3 memory scenarios; 3 session scenarios including FTS5; 2 knowledge scenarios; 1 project-context scenario; 1 MCP stdio scenario covering discovery/call/registry bridge; 1 durable delegation scenario; 2 offline E2E routes | NOT_RUN; scripted kernels and fake stdio MCP only | Platform skeleton evidence, not a model benchmark; no live model, dynamic replan, HTTP MCP, vector DB, parallel worker, or external side-effect claim |

Metric semantics are explicit: implementation and deterministic test facts are not benchmark claims. `scripts/e5_live_model_smoke.py` is manual-only and CI remains offline. Missing historical fields remain N/A rather than being estimated.

## HV12-LIVE-001 canonical metric row

| Metric | Value |
|---|---:|
| Harness | HV-1.2 |
| Model | MiMo v2.5 |
| Real model | `true` |
| Canonical status | `FAIL` |
| Functional repair | `PASS` |
| Outer task completion | `FAIL` |
| Attempts | 3 |
| Post-resume attempts | 2 |
| Post-resume turns | 40 |
| Post-resume tool calls | 57 |
| Post-resume tool failures | 16 |
| Tool failure rate | `16/57 ≈ 28.07%` |
| CP-3 attempts | 2 |
| Same workspace | `true` |
| Duplicate durable records | 0 |
| Wall duration (ms) | 412421 |
| Token usage | `null` / not available |

The 57 calls, 16 failures, 28.07% rate, and edit-target counts are derived
from the immutable canonical attempt records. The canonical `FAIL` is retained
even though the final functional test status is `PASS`, because the outer task
did not complete.

## HV13-LIVE-001 canonical metric row

| Metric | Value |
|---|---:|
| Harness | HV-1.3 |
| Model | MiMo v2.5 |
| Canonical status | `FAIL` |
| Initial pytest | `FAIL` |
| Final pytest | `FAIL` |
| Functional validation | `false` |
| Agent completion | `false` |
| Outer task completion | `false` |
| Attempts | 3 |
| CP-3 attempts | 2 |
| Post-resume turns | 40 |
| Post-resume tool calls | 58 |
| Post-resume tool failures | 19 |
| Tool failure rate | `19/58 = 32.76%` |
| `workspace.edit` calls / failures | 20 / 17 |
| `workspace.edit` failure rate | `17/20 = 85.00%` |
| `cli.exec` calls / failures | 2 / 2 |
| Outcome Arbitration events / passes | 2 / 0 |
| Same workspace | `true` |
| Duplicate durable records | 0 |
| Source repository unchanged | `true` |
| Token usage | `null` / not available |

Outcome Arbitration correctly rejected invalid state after Attempts 2 and 3.
The latest `next_attempt_suppressed_after_arbitration=true` field observes no
later attempt; it does not prove causal suppression because Attempt 3 was
already the configured maximum and validation was false.

### HV12/HV13 single-run comparison warning

**SINGLE-RUN PAIRED OBSERVATION:** HV12 ended with final pytest PASS, 57 calls,
and 16 failures; HV13 ended with final pytest FAIL, 58 calls, and 19 failures.
Both used 40 post-resume turns and both outer tasks failed. These two stochastic
runs do not establish that HV-1.3 reduced or worsened success rate.

## E6 final PR scale

Measured from `origin/main` to the final E6 PR head after the closeout patch:
the final PR has **5 commits**, **29 changed files**, **2039 additions**, and
**176 deletions**. The deterministic evidence contains 13 traceable scenarios:
10 crash windows plus 3 CREATING-root binding cases.

## E5 real-run comparison

The two preserved real MiMo calibration runs are comparable only within the E5 calculator fixture. They demonstrate functional repair, not a general benchmark:

| Metric | E5-LIVE-004 | E5-LIVE-005 |
|---|---:|---:|
| Tool calls | 26 | 23 |
| CLI calls | 12 | 5 |
| Tool failures | 9 (`types` not available) | 5 |
| Inner turns | 20 | 20 |
| Functional validation | PASS | PASS |
| Agent completion | FAIL | FAIL |
| Termination | TURN_LIMIT | TURN_LIMIT |
| Duration (ms) | 235391 | 252234 |

This comparison shows fewer tool/CLI calls but no latency or termination improvement. It does not support a success-rate, cost, or general SWE-performance claim.

## E5 engineering scope and CI terminology

The initial checkpoint-core change is retained as `checkpoint_core_initial_diff` in the E5 summary (`14` files, `+277/-9`). It is not the final E5 engineering scale. The summary also records `final_pr_changed_files`, `final_pr_additions`, `final_pr_deletions`, and `final_pr_commits`, measured from `origin/main` to the final E5 HEAD and including evidence files.

`LOCAL_HEAD_TESTS` refers to the local `uv run pytest` result. `PR_MERGE_RESULT_CI` refers to the pull-request workflow, which tests GitHub's merge result; it is not an exact-head checkout claim.
