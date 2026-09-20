"""Prepare or execute one official-path P4 recovery proof.

This module is intentionally static.  The PowerShell wrapper supplies only
paths and the human-owned credential gate; it never generates Python source
or interpolates task identifiers.  ``--preflight`` performs selection and
wiring checks without constructing an executor or contacting a provider.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import sys
from pathlib import Path


# Make direct invocation use the official worktree's source tree even when
# the selected Python environment has another checkout installed editable.
_OFFICIAL_ROOT = Path(__file__).resolve().parents[1]
for _path in (_OFFICIAL_ROOT / "src", _OFFICIAL_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from evals.reliability.p46_launcher import (
    _load_executor_from_flag,
    _run_benchmark,
    load_benchmark_profile,
)
from evals.reliability.run_phase4 import ProtocolSnapshot, select_runs


# Selected by the offline scan of all 60 frozen tasks.  CWR-06 has a concrete
# workflow fixture and a declared LOCAL repair effect; its recovery decision
# can be proven without changing any frozen benchmark input.
SINGLE_PROOF_TASK = "CWR-06"
SINGLE_PROOF_CONFIG = "odys_p3"
SINGLE_PROOF_REPEAT = 1
EXPECTED_REPAIR_SCOPE = "LOCAL"
EXPECTED_RUNS = 1
EXPECTED_PROTOCOL_HASH = (
    "993eae04290fe683d40fcf845b9e7325b9572b227e78e7cb3090dd7ee37637c3"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Check the frozen selection and official wiring without execution.",
    )
    return parser


def _load_snapshot(repo_root: Path) -> ProtocolSnapshot:
    snapshot = ProtocolSnapshot.load(repo_root / "evals" / "reliability" / "phase4_v1")
    if snapshot.protocol_hash != EXPECTED_PROTOCOL_HASH:
        raise RuntimeError("PROTOCOL_HASH_MISMATCH")
    return snapshot


def select_single_proof_run(snapshot: ProtocolSnapshot):
    """Resolve exactly one frozen task/config/repeat through the normal API."""
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
    if task.get("fault_injection") != "FAIL_TOOL_ON_CALL_1":
        raise RuntimeError("SINGLE_PROOF_FAULT_BINDING_DRIFT")
    if task.get("expected_observable_effects", {}).get("repair_scope") != "local":
        raise RuntimeError("SINGLE_PROOF_ACCEPTANCE_SCOPE_DRIFT")
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
    api_budget = budgets.get("max_model_calls")
    if not isinstance(api_budget, int) or api_budget < 1:
        raise RuntimeError("FROZEN_API_BUDGET_INVALID")
    print(f"SINGLE_PROOF_TASK={SINGLE_PROOF_TASK}")
    print(f"RESOLVED_FROZEN_API_BUDGET={api_budget}")
    print(f"EXPECTED_REPAIR_SCOPE={EXPECTED_REPAIR_SCOPE}")
    print(f"MAX_ROOT_PROVIDER_CALLS={api_budget}")
    print("CONFIG=odys_p3")
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
    print("SINGLE_PROOF_EXECUTION_COMPLETE")
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
