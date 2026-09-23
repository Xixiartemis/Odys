# PHASE5 ENVIRONMENT FREEZE REPORT

Generated: 2026-09-23T07:00:00Z

## BENCHMARKS_DOWNLOADED=YES

| Benchmark | Repository | Commit SHA | Status |
|-----------|-----------|------------|--------|
| ToolMaze | https://github.com/Zhudongsheng75/ToolMaze | ef0798aa7f31ac9b33403254b1ef76e8673305fa | FROZEN |
| ToolSandbox | https://github.com/apple/ToolSandbox | c8571d7854316d2e1c5f288e59fe1e34e53f6dd1 | FROZEN |
| Terminal-Bench | N/A | N/A | EXTENSION_ONLY |
| TUA-Bench | N/A | N/A | EXTENSION_ONLY |

## VERSIONS_FROZEN=YES

- ToolMaze: no release tags; pinned to commit ef0798a
- ToolSandbox: no release tags; pinned to commit c8571d7
- Dataset source: https://huggingface.co/datasets/dongsheng/ToolMaze

## HASHES_RECORDED=YES

- Dataset hash (SHA-256): `9fcd7d7ec3c06afcee098877cca1ae8c29b87a1102be4b365a1055f824a76bc8`
- Evaluator hash (SHA-256): `412f9c3615bfd997af903f6514f716be94a774140975b82c8ff2fd16b6547833`
- Lock file: `experiments/phase5/manifests/benchmark-lock.json`

## RUNTIME_FIREWALL_READY=YES

- RUNTIME_HIDDEN_GROUND_TRUTH_ACCESS=NO
- RUNTIME_ORACLE_ACCESS=NO
- RUNTIME_FINAL_JUDGE_ACCESS=NO

### Firewall Design

Raw task JSON files contain hidden fields:
- `expected_result` — ground truth tool calls
- `execution_trace` — oracle execution path
- `perturbation_point` — where perturbation was injected

These are present in the source data (as required by the benchmark).
The ToolMazeAdapter strips all hidden fields before passing tasks to the runtime.
The `offline_native_evaluate()` method uses hidden data only after runtime termination.

## Dataset Statistics

| Category | Task Count | Perturbation Modes |
|----------|-----------|-------------------|
| C1 | 500 | P0, P1, P2, P3, P4 |
| C2 | 500 | P0, P1, P2, P3, P4 |
| C3 | 500 | P0, P1, P2, P3, P4 |
| C4 | 500 | P0, P1, P2, P3, P4 |
| **Total** | **2000** | **5 modes x 4 topologies** |

Total JSON files in dataset: 2801

## Lock File Location

```
experiments/phase5/manifests/benchmark-lock.json
```
