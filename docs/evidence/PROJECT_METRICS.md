# Project Metrics Ledger

| Phase | Harness | Tests | CI | Primary Capability | Deterministic Eval | Live Eval | Known Data Gaps |
|---|---|---:|---|---|---|---|---|
| E2 | HV-0.7 | 171 passed (repository closeout record) | N/A | Inner Agent backend | Not recorded as a benchmark | Not recorded | Historical tokens/duration/success rate unavailable |
| E3 | HV-0.8 | 188 passed (repository closeout record) | N/A | Read-only Workspace and Safe CLI | Not recorded as a benchmark | SKIPPED_CONFIG | Historical tokens/duration/success rate unavailable |
| E4 | HV-0.9 | 203 passed | SUCCESS at 40d4c53; current patch pending | Staged edit/test loop and candidate patch artifact | 1/1, one outer attempt, six inner calls, one file | SKIPPED_CONFIG | Current patch CI and live metrics pending/unavailable |

Metric semantics are explicit: implementation and deterministic test facts are not benchmark claims. Missing historical fields remain N/A rather than being estimated.
