"""Prepare or execute the bounded P4 single-proof v2 bundle.

The proof uses one frozen task whose validator-visible effect is a concrete
mutable side-effect count.  This file is static Python; the PowerShell
wrapper supplies only paths and performs the human-owned credential gate.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import sys
from pathlib import Path


_OFFICIAL_ROOT = Path(__file__).resolve().parents[1]
for _path in (_OFFICIAL_ROOT / "src", _OFFICIAL_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from evals.reliability.p46_launcher import (  # noqa: E402
    _load_executor_from_flag,
    _run_benchmark,
    load_benchmark_profile,
)
from evals.reliability.run_phase4 import ProtocolSnapshot, select_runs  # noqa: E402


SINGLE_PROOF_TASK = "ESR-04"
SINGLE_PROOF_CONFIG = "odys_p3"
SINGLE_PROOF_REPEAT = 1
EXPECTED_INITIAL_FAILURE_TYPE = "TOOL_CALL_BUDGET_EXHAUSTED"
EXPECTED_REPAIR_SCOPE = "LOCAL"
EXPECTED_RUNS = 1
EXPECTED_ROOT_API_BUDGET = 20
EXPECTED_PROTOCOL_HASH = (
    "993eae04290fe683d40fcf845b9e7325b9572b227e78e7cb3090dd7ee37637c3"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preflight", action="store_true")
    return parser


def _load_snapshot(repo_root: Path) -> ProtocolSnapshot:
    snapshot = ProtocolSnapshot.load(repo_root / "evals" / "reliability" / "phase4_v1")
    if snapshot.protocol_hash != EXPECTED_PROTOCOL_HASH:
        raise RuntimeError("PROTOCOL_HASH_MISMATCH")
    return snapshot


def select_single_proof_run(snapshot: ProtocolSnapshot):
    selected = tuple(
        run
        for run in select_runs(snapshot, headline=True)
        if run.task["task_id"] == SINGLE_PROOF_TASK
        and run.config["config_id"] == SINGLE_PROOF_CONFIG
        and run.repeat_index == SINGLE_PROOF_REPEAT
    )
    if len(selected) != EXPECTED_RUNS:
        raise RuntimeError(f"SINGLE_PROOF_SELECTION_COUNT_MISMATCH:{len(selected)}")
    task = selected[0].task
    if task.get("family") != "EXECUTION_STATE_RECOVERY":
        raise RuntimeError("SINGLE_PROOF_FAMILY_BINDING_DRIFT")
    if task.get("fault_injection") != "INTERRUPT_AFTER_EFFECT":
        raise RuntimeError("SINGLE_PROOF_FAULT_BINDING_DRIFT")
    if task.get("expected_observable_effects") != {"side_effect_count": 1}:
        raise RuntimeError("SINGLE_PROOF_ACCEPTANCE_EFFECT_DRIFT")
    return selected


def _preflight(repo_root: Path, output: Path) -> int:
    snapshot = _load_snapshot(repo_root)
    selected = select_single_proof_run(snapshot)
    profile = load_benchmark_profile("cheap_model", repo_root=repo_root)
    loader_source = inspect.getsource(_load_executor_from_flag)
    runner_source = inspect.getsource(_run_benchmark)
    if "create_cheap_executor" not in loader_source:
        raise RuntimeError("OFFICIAL_CHEAP_EXECUTOR_FACTORY_NOT_FOUND")
    if "Phase4Runner" not in runner_source:
        raise RuntimeError("OFFICIAL_PHASE4_RUNNER_NOT_FOUND")
    budgets = snapshot.protocol.get("budgets", {})
    if budgets.get("max_model_calls") != EXPECTED_ROOT_API_BUDGET:
        raise RuntimeError("FROZEN_ROOT_API_BUDGET_DRIFT")
    print(f"SINGLE_PROOF_TASK={SINGLE_PROOF_TASK}")
    print(f"EXPECTED_INITIAL_FAILURE_TYPE={EXPECTED_INITIAL_FAILURE_TYPE}")
    print(f"EXPECTED_RECOVERY_SCOPE={EXPECTED_REPAIR_SCOPE}")
    print(f"FROZEN_ROOT_API_BUDGET={EXPECTED_ROOT_API_BUDGET}")
    print(f"MAX_ROOT_PROVIDER_CALLS={EXPECTED_ROOT_API_BUDGET}")
    print(f"MODEL_IDENTITY={profile.model}")
    print(f"CONFIG={SINGLE_PROOF_CONFIG}")
    print("REPEATS=1")
    print(f"EXPECTED_RUNS={len(selected)}")
    print("OFFICIAL_EXECUTION_PATH=YES")
    print("P410_EXECUTOR_USED=NO")
    print("PROVIDER_EXECUTED_DURING_PREFLIGHT=NO")
    print("RESULT_CREATED_DURING_PREFLIGHT=" + ("YES" if output.exists() else "NO"))
    print("READY_FOR_HUMAN_EXECUTION=YES")
    return 0


def _execute(repo_root: Path, output: Path) -> int:
    snapshot = _load_snapshot(repo_root)
    selected = select_single_proof_run(snapshot)
    profile = load_benchmark_profile("cheap_model", repo_root=repo_root)
    executor = _load_executor_from_flag(None, profile)
    asyncio.run(
        _run_benchmark(
            runs=selected,
            output_dir=output,
            snapshot=snapshot,
            executor=executor,
            model=profile.model,
            provider=profile.provider,
            profile=profile,
            resume=False,
        )
    )
    print("SINGLE_PROOF_V2_EXECUTION_COMPLETE")
    print("RESULT_PATH=" + str(output))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    output = args.output.resolve()
    if args.preflight:
        return _preflight(repo_root, output)
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"SINGLE_PROOF_OUTPUT_EXISTS:{output}")
    return _execute(repo_root, output)


if __name__ == "__main__":
    raise SystemExit(main())
