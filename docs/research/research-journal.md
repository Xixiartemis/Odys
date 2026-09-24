# Odys Phase 5 — Research Journal

Auto-generated experiment log. Each entry records a concrete action taken.

---

## Entry 1: Phase5-01 Environment Freeze

- **Date**: 2026-09-23 06:18 UTC
- **Commit**: 601df7c (main)
- **Action**: Download and freeze ToolMaze + ToolSandbox benchmarks
- **Command**: `git clone` + `huggingface_hub.snapshot_download`
- **Result**:
  - ToolMaze commit: ef0798aa7f31ac9b33403254b1ef76e8673305fa
  - ToolSandbox commit: c8571d7854316d2e1c5f288e59fe1e34e53f6dd1
  - Dataset: 2801 JSON files, 2000 perturbed tasks (C1-C4 x P0-P4)
  - Dataset hash: 9fcd7d7ec3c06afcee098877cca1ae8c29b87a1102be4b365a1055f824a76bc8
  - Evaluator hash: 412f9c3615bfd997af903f6514f716be94a774140975b82c8ff2fd16b6547833
- **Artifacts**: experiments/phase5/manifests/benchmark-lock.json
- **Unexpected findings**: None

---

## Entry 2: Phase5-02 ToolMaze Adapter

- **Date**: 2026-09-23 06:18 UTC
- **Commit**: 601df7c (main)
- **Action**: Wire ToolMazeAdapter to official frozen data
- **Command**: `python -c "from lhas.phase5.toolmaze_adapter import ToolMazeAdapter; ..."`
- **Result**:
  - 2000 tasks loaded successfully
  - Hidden field leakage: NONE (verified)
  - 5-task smoke test: P0/P1/P3 pass, P2/P4 fail (expected for permanent perturbations)
  - Firewall: RUNTIME_HIDDEN_GROUND_TRUTH_ACCESS=NO
- **Artifacts**: experiments/phase5/manifests/toolmaze-adapter-report.md
- **Unexpected findings**: P2/P4 fail in simulated eval because oracle trace lacks graceful stop

---

## Entry 3: Phase5-03 Six-Arm Controller + Pairing Validation

- **Date**: 2026-09-23 06:18 UTC
- **Commit**: 601df7c (main)
- **Action**: Implement ExperimentPairValidator and paired_trial_manifest generation
- **Command**: Provider-free tests in tests/test_phase5_falsification.py
- **Result**:
  - 56/56 tests pass
  - T2 (identical conditions across arms): PASS
  - T15 (paired trial identity reconciliation): PASS
  - T16 (provider/tool accounting): PASS
- **Artifacts**: tests/test_phase5_falsification.py
- **Unexpected findings**: None

---

## Entry 4: Phase5-04 Pilot Experiment (Dry-Run)

- **Date**: 2026-09-23 06:18 UTC
- **Commit**: 601df7c (main)
- **Action**: Run pilot experiment — 5 tasks x 6 arms = 30 trials
- **Command**: `python -c "from lhas.phase5.pilot_runner import PilotRunner; ..."`
- **Result**:
  - 30 trials completed
  - All pairings valid: YES
  - Total violations: 0
  - Firewall clean: YES
  - 96 artifact files generated
- **Artifacts**: experiments/phase5/runs/phase5-pilot-001/
- **REAL_PROVIDER_EXECUTED**: NO
- **Unexpected findings**: None

---

## Entry 5: Phase5-05 Research Documentation

- **Date**: 2026-09-23 06:18 UTC
- **Commit**: 601df7c (main)
- **Action**: Create research documentation
- **Result**:
  - docs/research/hypothesis.md
  - docs/research/methodology.md
  - docs/research/limitations.md
  - docs/research/research-journal.md (this file)
- **Unexpected findings**: None

---

## Phase 4 Regression

- **Date**: 2026-09-23 06:18 UTC
- **Action**: Verify Phase 4 core tests still pass
- **Command**: `pytest tests/test_recovery_policy.py tests/test_phase4_protocol_validation.py ...`
- **Result**: 36/36 Phase 4 core tests pass (61 total across selected files)
- **PHASE4_CORE_CHANGED**: NO
