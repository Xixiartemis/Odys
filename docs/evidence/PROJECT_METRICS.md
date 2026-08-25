# Project Metrics Ledger

| Phase | Harness | Tests | CI | Primary Capability | Deterministic Eval | Live Eval | Known Data Gaps |
|---|---|---:|---|---|---|---|---|
| E2 | HV-0.7 | 171 passed (repository closeout record) | N/A | Inner Agent backend | Not recorded as a benchmark | Not recorded | Historical tokens/duration/success rate unavailable |
| E3 | HV-0.8 | 188 passed (repository closeout record) | N/A | Read-only Workspace and Safe CLI | Not recorded as a benchmark | NOT_RUN | Historical tokens/duration/success rate unavailable |
| E4 | HV-0.9 | 203 passed | SUCCESS (CI verified at `40d4c53`; final PR #8 merged as `0b981ff`) | Staged edit/test loop and candidate patch artifact | 1/1, one outer attempt, six inner calls, one file | NOT_RUN | Live metrics unavailable; implementation diff and final PR diff are separate in E4 summary |
| E5 | HV-1.0 | 228 passed locally | PASS (PR merge-result workflow) | Durable checkpoint and bounded CP-3 context reconstruction | 120 events, cursor 100, replay 20, 0.8333333333333334 replay reduction, restart equal | Two real MiMo fixture runs; functional 2/2, agent completion 0/2, both TURN_LIMIT | Tokens unavailable; no latency or termination improvement; scope is E5 calculator fixture calibration only |
| E6-B | HV-1.1 | 239 passed locally | Pending exact-head CI | Manual process resume with durable run/workspace binding | Two restart scenarios: validate-without-retry and CP-3 retry in same workspace | NOT_RUN | Historical baseline; superseded by E6-C crash-window evidence |
| E6-C | HV-1.2 | 251 passed locally | Pending exact-head CI | Crash-window-aware durable resume and generalized continuation | 10 deterministic crash-window fixtures, W1–W8 plus CREATING-root cases and 3-attempt continuation | NOT_RUN | No live provider or external side-effect exactly-once semantics |

Metric semantics are explicit: implementation and deterministic test facts are not benchmark claims. `scripts/e5_live_model_smoke.py` is manual-only and CI remains offline. Missing historical fields remain N/A rather than being estimated.

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
