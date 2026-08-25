# Project Metrics Ledger

| Phase | Harness | Tests | CI | Primary Capability | Deterministic Eval | Live Eval | Known Data Gaps |
|---|---|---:|---|---|---|---|---|
| E2 | HV-0.7 | 171 passed (repository closeout record) | N/A | Inner Agent backend | Not recorded as a benchmark | Not recorded | Historical tokens/duration/success rate unavailable |
| E3 | HV-0.8 | 188 passed (repository closeout record) | N/A | Read-only Workspace and Safe CLI | Not recorded as a benchmark | SKIPPED_CONFIG | Historical tokens/duration/success rate unavailable |
| E4 | HV-0.9 | 203 passed | SUCCESS (CI verified at `40d4c53`; final PR #8 merged as `0b981ff`) | Staged edit/test loop and candidate patch artifact | 1/1, one outer attempt, six inner calls, one file | SKIPPED_CONFIG | Live metrics unavailable; implementation diff and final PR diff are separate in E4 summary |
| E5 | HV-1.0 | 225 passed | PASS | Durable checkpoint and bounded CP-3 context reconstruction | 120 events, cursor 100, replay 20, 0.8333333333333334 replay reduction, restart equal | Two real MiMo fixture runs; functional 2/2, agent completion 0/2, both TURN_LIMIT | Tokens unavailable; no latency or termination improvement; scope is E5 calculator fixture calibration only |

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
