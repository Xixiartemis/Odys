# Odys Phase 5 — Methodology

## Benchmark Selection

Primary: ToolMaze (arXiv:2606.05806)
- Measures dynamic replanning and anomaly recovery in LLM agents
- C1–C4 topology classes with P0–P4 perturbation modes
- Native evaluator: subset-matching judge with complexity-aware scoring

Secondary: ToolSandbox (Apple)
- Milestone-based progress validation
- World-state snapshots for offline analysis

## Experimental Arms

| Arm | Policy | Recovery | Validator | Progress | Budget Policy |
|-----|--------|----------|-----------|----------|---------------|
| A0 | BARE | ✗ | ✗ | ✗ | ✗ |
| A1 | RETRY_ONLY | ✓ (simple) | ✗ | ✗ | ✗ |
| A2 | VALIDATOR_ONLY | ✗ | ✓ | ✗ | ✗ |
| A3 | ODYS_FULL | ✓ | ✓ | ✓ | ✓ |
| A4 | ODYS_MINUS_OBS_PROGRESS | ✓ | ✓ | ✗ | ✓ |
| A5 | ODYS_MINUS_RECOVERY_BUDGET | ✓ | ✓ | ✓ | ✗ |

## Fairness Controls

All arms share identical:
- Model ID and provider
- Task prompt and visible tools
- Root execution budget (turns, model calls, tokens, deadline)
- Environment state
- Benchmark evaluator

Only control policy differs.

## Artifact Schema

Each trial produces:
- trial_manifest.json (immutable identity)
- raw_artifact.json (runtime events, no hidden labels)
- native_result.json (benchmark-native scoring)
- classification.json (VALID/INVALID_INFRA/EXCLUDED)

## Firewall

Runtime cannot access:
- Oracle solution DAG
- Hidden perturbation labels
- Benchmark judge results
- ToolSandbox target milestones

Offline evaluation runs only after runtime termination.
