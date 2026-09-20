# Phase 4 / Attempt6 Public Evidence

This directory is a sanitized public evidence bundle for the README claims.
It contains aggregate results and experiment identity hashes only; it does not
contain provider credentials, prompts, raw provider content, or execution
traces.

## Scope

- 6/6 valid real-provider runs, with a controlled fault and a 20-call provider budget.
- Legacy bounded recovery: validator-backed recovery `1/3`.
- Odys V2: validator-backed recovery `3/3`.
- Legacy first replan: `19–20` calls.
- Odys V2 first replan: `2–5` calls.
- Odys V2 remaining budget at first replan: `15–18 / 20` (`75%–90%`).
- Attempt5→Attempt6 V2 convergence: redundant post-success execution `8→0` across `3/3` V2 convergence runs.

This is a scoped controlled-fault experiment demonstrating a recovery
mechanism. It is not a general benchmark, a broad task/model generalization
result, or a same-quality cost-saving claim when baseline and V2 outcomes
differ.

The public JSON files are intentionally sanitized summaries, not replacements
for the historical raw experiment bundle.
