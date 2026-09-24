# ToolMaze Adapter Report

Generated: 2026-09-23T07:15:00Z

## Adapter Status=READY

- Source: Official ToolMaze HuggingFace dataset
- Data path: `experiments/phase5/benchmarks/toolmaze/data/perturbed_tasks/`
- Adapter: `src/lhas/phase5/toolmaze_adapter.py`

## Task Count=2000

| Topology | Tasks | Perturbation Modes |
|----------|-------|-------------------|
| C1 | 500 | P0, P1, P2, P3, P4 |
| C2 | 500 | P0, P1, P2, P3, P4 |
| C3 | 500 | P0, P1, P2, P3, P4 |
| C4 | 500 | P0, P1, P2, P3, P4 |

Per mode: 400 tasks each (100 per topology)

## Condition Coverage=FULL

All C1-C4 x P0-P4 combinations present:
- C1/P0 through C4/P4: 20 conditions
- 100 tasks per condition

## Firewall Result=PASS

```
RUNTIME_HIDDEN_GROUND_TRUTH_ACCESS=NO
RUNTIME_ORACLE_ACCESS=NO
RUNTIME_FINAL_JUDGE_ACCESS=NO
```

Hidden fields stripped from runtime tasks:
- `expected_result` (ground truth tool calls)
- `execution_trace` (oracle execution path)
- `valid_paths` (C2-C4 alternative paths with oracle traces)
- `alternative_tools` (C2-C4 alternative tool definitions)
- `perturbation_point` (where perturbation was injected)

## Smoke Test (5 tasks)

| Task ID | Mode | Topology | TSR | Judge |
|---------|------|----------|-----|-------|
| C1_task_001_P0 | P0 | C1 | 1.0 | PASS |
| C1_task_001_P1 | P1 | C1 | 1.0 | PASS |
| C1_task_001_P2 | P2 | C1 | 0.0 | FAIL (permanent — graceful stop) |
| C1_task_001_P3 | P3 | C1 | 1.0 | PASS |
| C1_task_001_P4 | P4 | C1 | 0.0 | FAIL (permanent — graceful stop) |

Note: P2/P4 fail in simulated evaluation because the oracle trace does not
demonstrate graceful stop behavior. This is expected — real agents must
detect permanent perturbations and stop gracefully.

## TOOLMAZE_SMOKE_PASS=YES
## REAL_PROVIDER_EXECUTED=NO
