"""Run the small Phase 4 Golden diagnostic through the official path.

This file is intentionally static Python.  The PowerShell wrapper only
supplies paths and the human-owned credential gate; it never generates Python
source, so Windows quoting cannot change task IDs or Python literals.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
from pathlib import Path

from evals.reliability.p46_launcher import (
    _load_executor_from_flag,
    _run_benchmark,
    load_benchmark_profile,
)
from evals.reliability.run_phase4 import ProtocolSnapshot, select_runs


GOLDEN_TASK_IDS = (
    "CWR-06",
    "CWR-01",
    "CWR-03",
)
GOLDEN_CONFIG_IDS = ("minimal", "odys_p3")
GOLDEN_REPEATS = (1, 2, 3)
EXPECTED_RUNS = len(GOLDEN_TASK_IDS) * len(GOLDEN_CONFIG_IDS) * len(GOLDEN_REPEATS)
EXPECTED_PROTOCOL_HASH = "993eae04290fe683d40fcf845b9e7325b9572b227e78e7cb3090dd7ee37637c3"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Validate selection and official wiring without constructing a provider.",
    )
    return parser


def _load_snapshot(repo_root: Path) -> ProtocolSnapshot:
    snapshot = ProtocolSnapshot.load(repo_root / "evals" / "reliability" / "phase4_v1")
    if snapshot.protocol_hash != EXPECTED_PROTOCOL_HASH:
        raise RuntimeError("PROTOCOL_HASH_MISMATCH")
    return snapshot


def select_golden_runs(snapshot: ProtocolSnapshot):
    """Select frozen manifest runs without creating a second run definition."""
    selected = tuple(
        run
        for run in select_runs(snapshot, headline=True)
        if run.task["task_id"] in GOLDEN_TASK_IDS
        and run.config["config_id"] in GOLDEN_CONFIG_IDS
        and run.repeat_index in GOLDEN_REPEATS
    )
    if len(selected) != EXPECTED_RUNS:
        raise RuntimeError(f"GOLDEN_SELECTION_COUNT_MISMATCH:{len(selected)}")
    if {run.task["task_id"] for run in selected} != set(GOLDEN_TASK_IDS):
        raise RuntimeError("GOLDEN_TASK_ID_RESOLUTION_FAILED")
    if {run.config["config_id"] for run in selected} != set(GOLDEN_CONFIG_IDS):
        raise RuntimeError("GOLDEN_CONFIG_RESOLUTION_FAILED")
    if {run.repeat_index for run in selected} != set(GOLDEN_REPEATS):
        raise RuntimeError("GOLDEN_REPEAT_RESOLUTION_FAILED")
    return selected


def _preflight(repo_root: Path, output: Path) -> int:
    snapshot = _load_snapshot(repo_root)
    selected = select_golden_runs(snapshot)
    profile = load_benchmark_profile("cheap_model", repo_root=repo_root)
    official_loader = inspect.getsource(_load_executor_from_flag)
    official_runner = inspect.getsource(_run_benchmark)
    if "create_cheap_executor" not in official_loader:
        raise RuntimeError("OFFICIAL_CHEAP_EXECUTOR_FACTORY_NOT_FOUND")
    if "Phase4Runner" not in official_runner:
        raise RuntimeError("OFFICIAL_PHASE4_RUNNER_NOT_FOUND")
    print("TASK_IDS=" + ",".join(GOLDEN_TASK_IDS))
    print("CONFIGS=" + ",".join(GOLDEN_CONFIG_IDS))
    print("REPEATS=" + str(len(GOLDEN_REPEATS)))
    print("EXPECTED_RUNS=" + str(len(selected)))
    print("PROFILE=" + "|".join((profile.benchmark_version, profile.model, profile.provider)))
    print("OFFICIAL_EXECUTION_PATH=YES")
    print("P410_EXECUTOR_USED=NO")
    print("PROVIDER_EXECUTED_DURING_PREFLIGHT=NO")
    print("OUTPUT_EXISTS_DURING_PREFLIGHT=" + ("YES" if output.exists() else "NO"))
    return 0


def _execute(repo_root: Path, output: Path) -> int:
    snapshot = _load_snapshot(repo_root)
    selected = select_golden_runs(snapshot)
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
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    output = args.output.resolve()
    if args.preflight:
        return _preflight(repo_root, output)
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"GOLDEN_OUTPUT_EXISTS:{output}")
    return _execute(repo_root, output)


if __name__ == "__main__":
    raise SystemExit(main())
