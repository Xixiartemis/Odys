# P4.3 Execution Plan

Official headline execution is 360 runs: 60 tasks × 3 repeats × 2
configurations. The preregistered ablation is 144 runs: 12 tasks × 3 repeats
× 4 configurations.

Order:

1. deterministic protocol smoke validation;
2. small calibration run, excluded from headline results;
3. freeze environment-only corrections before official execution;
4. headline run;
5. ablation run;
6. aggregation;
7. failure audit;
8. publication of raw results.

Once the first official headline run begins, frozen protocol inputs are
immutable. A methodology defect stops the run, creates a new benchmark
version, and invalidates affected raw results; historical raw results are not
overwritten.
