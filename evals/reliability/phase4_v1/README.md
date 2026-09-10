# Phase 4.1 Controlled Benchmark Freeze

This directory freezes `phase4-v1`. It defines methodology only; it contains
no official headline or ablation results. P4.2 is the next allowed node.

## Research question

Can verifier-backed workflow control and selective recovery improve reliable
task completion under controlled failures compared with a minimal agent
runtime, and what additional execution cost does that reliability require?

The protocol does not encode an expected winner or target percentage.

## Headline and ablation

The headline has exactly 60 tasks in six canonical families, 10 tasks per
family, three repeats, and two configurations: `minimal` and `odys_p3`.
That is 360 runs. The ablation is the fixed 12-task subset in `ablation.json`,
two tasks per family, three repeats, and four configurations, for 144 runs.

`minimal` is Context → Model → Tool → Observation. It receives no completion
authority, verifier authority, typed TaskGraph recovery, failure provenance,
selective repair, macro replan, or durable workflow recovery. `odys_p3` adds
the frozen P3 machinery and explicitly uses `SIMPLE_DEPENDENCY`. Both use the
same task, fixture, capability set, budgets, fault, and external validator.

## Primary metric denominators

- Verified Completion Rate = verified completions / valid runs.
- False Completion Rate = false completion claims / valid runs.
- Recovery Success Rate = eventually externally verified recovery runs /
  runs where eligible recovery was required or attempted.
- Cost per Verified Completion = total measured execution cost / verified
  completions. Monetary cost is `NOT_MEASURED` when unavailable; it is never
  silently reported as zero.
- Duplicate Side Effect Rate = runs with an unintended duplicate externally
  observable side effect / valid runs.
- Lost Work Rate = eligible recovery runs with lost or unnecessarily redone
  pre-failure valid work / eligible recovery runs.

Invalid runs are separate from task failures. Missing secondary metrics are
`NOT_MEASURED`, not zero. Repeats are reported per task as 0/3, 1/3, 2/3, or
3/3 plus failure consistency; no unjustified pass@k formula is used.

## Fairness and faults

`external-observable-v1` is one configuration-agnostic validator shared by
both headline configurations. It scores workspace/artifact state, tests,
expected effects, durable evidence where declared, side-effect counts, and
dependency/order properties. It does not accept self-report, a completion
string, or configuration-specific internal status as proof.

Faults are deterministic, configuration-neutral, and resettable. A fault is
triggered at the same logical point for both configurations. Changing any
frozen input changes `protocol_hash`; raw results must carry that hash and all
other identities in `schemas/result.schema.json`.

## Reporting and versioning

Reports include all tasks, valid and invalid runs, raw counts and percentages,
per-family and overall metrics, failure/recovery breakdowns, configuration
identities, and visible `NOT_MEASURED` values. Failed runs are not deleted.
Calibration output is not headline output. A methodology defect requires a
new protocol version; historical raw results are never overwritten.
