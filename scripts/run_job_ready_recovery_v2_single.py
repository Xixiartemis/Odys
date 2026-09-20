"""Prepare and execute one real-provider Odys V2 recovery proof."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from evals.reliability.job_ready_recovery_v2 import (
    build_runner, create_live_executor, load_snapshot, select_single_odys_run,
    validate_job_ready_protocol, write_identity_artifacts,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one job-ready V2 Odys recovery proof")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"OUTPUT_COLLISION: {output}")
    snapshot = load_snapshot()
    validate_job_ready_protocol(snapshot.root)
    runs = select_single_odys_run(snapshot)
    executor, identity = create_live_executor()
    write_identity_artifacts(output, snapshot=snapshot, provider_identity_record=identity)
    counts = asyncio.run(build_runner(snapshot, executor=executor, output_dir=output, repo_root=repo_root).run(runs))
    if counts != {"valid": 1, "invalid": 0}:
        raise SystemExit(f"SINGLE_PROOF_GATE_FAILED: {counts}")
    print("JOB_READY_V2_SINGLE_EXECUTION_COMPLETE")
    print(f"RESULT_PATH={output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
