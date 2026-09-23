# Odys Phase 5 — Research Hypothesis

## Primary Hypothesis

**H1**: Odys recovery mechanisms (observable progress + recovery budget policy)
improve perturbation recovery rate (PRR) and reduce recovery cost (RC) compared
to baseline approaches (bare, retry-only, validator-only) on the ToolMaze benchmark.

## Secondary Hypotheses

**H2**: Observable progress signals improve detection latency for implicit
perturbations (P3/P4) compared to explicit perturbations (P1/P2).

**H3**: Recovery budget policy reduces wasted recovery attempts on permanent
perturbations (P2/P4) without degrading performance on transient perturbations (P1/P3).

**H4**: The advantage of full Odys over baseline arms increases with task
complexity (C1 → C4).

## Null Hypotheses

**H0_1**: No significant difference in PRR between A3 (ODYS_FULL) and A0 (BARE).

**H0_2**: No significant difference in RC between A3 and A1 (RETRY_ONLY).

## Metrics

- **TSR** (Task Success Rate): primary benchmark-native metric
- **PRR** (Perturbation Recovery Rate): hit-conditioned recovery rate
- **RC** (Recovery Cost): all-sample normalized recovery burden

## Experimental Design

- 6 arms: A0–A5 (ablation study)
- ToolMaze benchmark: C1–C4 × P0–P4
- 20 tasks per condition (pilot), full dataset for final
- Paired trials: identical task/model/budget, only policy differs

## Constraints

- Do not modify recovery thresholds after seeing results
- Do not modify prompts after seeing results
- Do not modify scoring after seeing results
- Any modification requires new experiment ID + new hypothesis
