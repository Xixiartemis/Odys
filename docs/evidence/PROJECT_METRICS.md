# Project Metrics Ledger

| Phase | Harness | Tests | CI | Primary Capability | Deterministic Eval | Live Eval | Known Data Gaps |
|---|---|---:|---|---|---|---|---|
| E2 | HV-0.7 | 171 passed (repository closeout record) | N/A | Inner Agent backend | Not recorded as a benchmark | Not recorded | Historical tokens/duration/success rate unavailable |
| E3 | HV-0.8 | 188 passed (repository closeout record) | N/A | Read-only Workspace and Safe CLI | Not recorded as a benchmark | SKIPPED_CONFIG | Historical tokens/duration/success rate unavailable |
| E4 | HV-0.9 | 203 passed | SUCCESS (CI verified at `40d4c53`; final PR #8 merged as `0b981ff`) | Staged edit/test loop and candidate patch artifact | 1/1, one outer attempt, six inner calls, one file | SKIPPED_CONFIG | Live metrics unavailable; implementation diff and final PR diff are separate in E4 summary |
| E5 | HV-1.0 | 210 passed | PENDING | Durable checkpoint and bounded CP-3 context reconstruction | 120 events, cursor 100, replay 20, 0.8333333333333334 replay reduction, restart equal | SKIPPED_CONFIG | CI for current PR and live token/duration metrics unavailable |

Metric semantics are explicit: implementation and deterministic test facts are not benchmark claims. E5 live evaluation is currently `SKIPPED_CONFIG`; `scripts/e5_live_model_smoke.py` is manual-only. Missing historical fields remain N/A rather than being estimated.
