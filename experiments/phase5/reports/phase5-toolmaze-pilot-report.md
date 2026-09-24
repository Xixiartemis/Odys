# Phase5 ToolMaze Pilot Report

Experiment ID: phase5-pilot-001
Generated: 2026-09-23T06:17:33.128750+00:00

## Configuration

- Benchmark: toolmaze
- Revision: ef0798a
- Model: dry-run-model (dry-run)
- Provider: dry-run
- Tasks: 5
- Arms: 6
- Total trials: 30

## Results Summary

- All pairings valid: True
- Total violations: 0
- Firewall clean: YES

## Firewall Audit

- RUNTIME_HIDDEN_GROUND_TRUTH_ACCESS: NO
- RUNTIME_ORACLE_ACCESS: NO
- RUNTIME_FINAL_JUDGE_ACCESS: NO
- runtime_active: False
- runtime_terminated: True
- violation_count: 0

## Trial Breakdown

| Task | Topology | Mode | Arms | Pairing |
|------|----------|------|------|---------|
| C1_task_001_P0 | C1 | P0 | 6 | PASS |
| C1_task_001_P1 | C1 | P1 | 6 | PASS |
| C1_task_001_P2 | C1 | P2 | 6 | PASS |
| C1_task_001_P3 | C1 | P3 | 6 | PASS |
| C1_task_001_P4 | C1 | P4 | 6 | PASS |

## Artifact Structure

```
D:\projects\odys\experiments\phase5\runs\phase5-pilot-001/
  experiment_manifest.json
  paired_manifests/
    <task_id>_paired.json
  raw/<trial_id>/raw_artifact.json
  benchmark/<trial_id>/native_result.json
  audits/
```

## Statistics

- Total artifact files: 96
- Paired manifests: 5

## REAL_PROVIDER_EXECUTED=NO

This is a provider-free dry-run validating the experiment pipeline.
No real model calls were made.