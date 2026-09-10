# P4.2 Benchmark Runner Contract

P4.2 implements the frozen `phase4-v1` protocol; it does not redesign it.
The runner may load the manifest, reset fixtures, adapt the four frozen
configurations, inject deterministic faults, execute a task, call the shared
validator, and write identity-stamped raw JSONL results. It must support
resume/interruption-safe batch execution and produce aggregation input.

The runner may not change the task set, ablation subset, headline
configurations, budgets, fairness contract, validator semantics, or fault
definitions without a new protocol version. Calibration output is separate
from official result directories.

Target CLI shape:

```text
python -m evals.reliability.run_phase4 --protocol phase4-v1 --config minimal --task CI-01 --repeat 1
python -m evals.reliability.run_phase4 --protocol phase4-v1 --headline
```

Every raw result must include the protocol, manifest, fixture, validator,
fault, model/provider, and repository identities defined by the frozen result
schema. Invalid runs are recorded separately and never silently counted as
task failures or deleted.
