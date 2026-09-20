"""Compatibility CLI for the canonical 02H final-live infrastructure gate.

The executable implementation lives in ``phase4_live_no_progress_final.py``.
This name remains available for the already-reviewed 02H command, while every
mode now shares one task/spec/runner authority.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from scripts.phase4_live_no_progress_final import (
    DEFAULT_OUTPUT,
    EXPECTED_POLICY_ID,
    EXPECTED_RUNS,
    EXPERIMENT_ID,
    PROVIDER_TIMEOUT_CEILING_SECONDS,
    ROOT_TIMEOUT_SECONDS,
    _execute,
    _preflight as _canonical_preflight,
    _smoke,
    _timeout_regression,
)


def _preflight(output: Path) -> dict:
    report = _canonical_preflight(output)
    return {
        **report,
        "shared_effect_policy": report["shared_effect_policy_implementation"],
        "unique_effect_policies": report["unique_policy_instance_count"],
        "other_effective_config_diff": report["effective_config_diff_except_primary_variable"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--preflight", action="store_true")
    group.add_argument("--timeout-regression", action="store_true")
    group.add_argument("--smoke", action="store_true")
    group.add_argument("--execute", action="store_true")
    group.add_argument("--offline-execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if args.preflight:
        print(json.dumps(_preflight(args.output), indent=2, sort_keys=True))
        return 0
    if args.timeout_regression:
        report = asyncio.run(_timeout_regression())
        print(json.dumps(report, indent=2, sort_keys=True))
        return int(
            not (
                report["simulated_provider_timeout"]
                and not report["unretrieved_task_exception"]
                and report["failure_classification"] == "PROVIDER_TIMEOUT"
                and report["root_control_remains_consistent"]
            )
        )
    if args.smoke:
        report = asyncio.run(_smoke(args.output))
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    report = asyncio.run(
        _execute(
            args.output,
            resume=args.resume,
            offline=args.offline_execute,
        )
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
