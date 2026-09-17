"""02H timeout-isolated provider-free gate for the final live experiment.

02G is retained as invalid historical evidence.  This module does not rerun
it.  It builds a new live-experiment task with an explicit 900-second root
deadline and verifies that the native provider ceiling remains 300 seconds.
The timeout regression uses only an in-process simulated SDK timeout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for import_root in (REPO_ROOT, REPO_ROOT / "src"):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from evals.reliability.effect_policy import PhaseEffectPolicy
from evals.reliability.fixture_packages.registry import FixtureRegistry
from evals.reliability.p45_executor import P45BenchmarkExecutor
from evals.reliability.p46_provider import CHEAP_MODEL, FROZEN_PROVIDER
from evals.reliability.run_phase4 import (
    FixtureManager,
    Phase4Runner,
    ProtocolSnapshot,
    RunSpec,
)
from lhas.execution_control import (
    ExecutionControlError,
    ExecutionControlToken,
    await_with_control,
)
from lhas.native.runtime import ProviderFailureClassifier
from scripts.phase4_live_no_progress_escalation import FAULT_ID, PROTOCOL_ROOT
from scripts.phase4_live_no_progress_final import (
    EXPECTED_POLICY_ID,
    EXPECTED_RUNS,
    ARMS,
    _build_specs,
    _effective_config_diff,
    _experiment_registry,
    _git_head,
)
from scripts.phase4_live_no_progress_parity import (
    _DeterministicProvider,
    _task as _parity_task,
)


EXPERIMENT_ID = "phase4-live-no-progress-escalation-02h"
ROOT_TIMEOUT_SECONDS = 900.0
PROVIDER_TIMEOUT_CEILING_SECONDS = 300.0
DEFAULT_OUTPUT = REPO_ROOT / "results" / EXPERIMENT_ID


def _task(snapshot: ProtocolSnapshot) -> dict[str, Any]:
    task = dict(_parity_task(snapshot, experiment_id=EXPERIMENT_ID))
    task["benchmark_version"] = EXPERIMENT_ID
    task["timeout_seconds"] = ROOT_TIMEOUT_SECONDS
    return task


def _build_runner(
    snapshot: ProtocolSnapshot,
    output: Path,
    *,
    provider: Any,
) -> Phase4Runner:
    executor = P45BenchmarkExecutor(
        fixture_registry=_experiment_registry(),
        factory_type="real",
        provider=provider,
        expected_model=CHEAP_MODEL,
        experiment_macro_replan_enabled=True,
    )
    return Phase4Runner(
        snapshot,
        output_dir=output,
        executor=executor,
        fixture_manager=FixtureManager(snapshot),
        model=CHEAP_MODEL,
        provider=FROZEN_PROVIDER,
        benchmark_version=EXPERIMENT_ID,
        repo_root=REPO_ROOT,
        trace_path=output / "traces.jsonl",
        require_trace=True,
    )


def _preflight(output: Path) -> dict[str, Any]:
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    task = _task(snapshot)
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"OUTPUT_COLLISION:{output}")
    if snapshot.fault_by_id.get(FAULT_ID) is None:
        raise RuntimeError(f"FROZEN_FAULT_NOT_FOUND:{FAULT_ID}")

    specs, policies = _build_specs(snapshot, task=task)
    configs = [spec.config for spec in specs]
    baseline = next(config for config in configs if config["experiment_arm"] == "baseline")
    v2 = next(config for config in configs if config["experiment_arm"] == "v2")
    effective_diff = _effective_config_diff(baseline, v2)
    provider_ceiling = min(max(float(task["timeout_seconds"]), 0.1), 300.0)
    if len(specs) != EXPECTED_RUNS or len(policies) != EXPECTED_RUNS:
        raise RuntimeError("EXPECTED_RUN_COUNT_MISMATCH")
    if len({id(policy) for policy in policies.values()}) != EXPECTED_RUNS:
        raise RuntimeError("EFFECT_POLICY_INSTANCE_COLLISION")
    if any(config.get("_phase_effect_policy") is None for config in configs):
        raise RuntimeError("EFFECT_POLICY_NOT_IN_RUNSPEC")
    if any(policy.policy_id != EXPECTED_POLICY_ID for policy in policies.values()):
        raise RuntimeError("EFFECT_POLICY_ID_MISMATCH")
    if effective_diff:
        raise RuntimeError(f"UNEXPECTED_EFFECTIVE_CONFIG_DIFF:{sorted(effective_diff)}")
    if task["timeout_seconds"] != ROOT_TIMEOUT_SECONDS:
        raise RuntimeError("ROOT_TIMEOUT_NOT_ISOLATED")
    if provider_ceiling != PROVIDER_TIMEOUT_CEILING_SECONDS:
        raise RuntimeError("PROVIDER_TIMEOUT_CEILING_NOT_ISOLATED")

    return {
        "experiment_id": EXPERIMENT_ID,
        "base_sha": _git_head(),
        "protocol_hash": snapshot.protocol_hash,
        "expected_runs": EXPECTED_RUNS,
        "shared_effect_policy": True,
        "effect_policy_id": EXPECTED_POLICY_ID,
        "policy_instance_count": len(policies),
        "unique_effect_policies": len({id(policy) for policy in policies.values()}),
        "all_runs_have_effect_policy": all(
            config.get("_phase_effect_policy") is not None for config in configs
        ),
        "baseline_policy": dict(ARMS)["baseline"],
        "v2_policy": dict(ARMS)["v2"],
        "other_effective_config_diff": sorted(effective_diff),
        "root_timeout_seconds": task["timeout_seconds"],
        "provider_timeout_ceiling_seconds": provider_ceiling,
        "root_timeout_gt_provider_timeout": task["timeout_seconds"] > provider_ceiling,
        "provider_executed": False,
        "output_created": output.exists(),
    }


async def _timeout_regression() -> dict[str, Any]:
    from openai import APITimeoutError
    import httpx

    request = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
    loop = asyncio.get_running_loop()
    loop_errors: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    try:
        async def sdk_timeout() -> None:
            raise APITimeoutError(request=request)

        control = ExecutionControlToken("02h-provider-timeout", timeout_seconds=5)
        try:
            await await_with_control(
                sdk_timeout(),
                control=control,
                local_ceiling=1.0,
                timeout_failure_type="PROVIDER_TIMEOUT",
                source="provider",
            )
        except APITimeoutError as exc:
            classified = ProviderFailureClassifier.classify(exc).value
        else:  # pragma: no cover - the simulated timeout must raise
            classified = "NO_FAILURE"

        started = asyncio.Event()

        async def late_sdk_timeout() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise APITimeoutError(request=request)

        root = ExecutionControlToken("02h-root-race", timeout_seconds=0.02)
        race_task = asyncio.create_task(
            await_with_control(
                late_sdk_timeout(),
                control=root,
                local_ceiling=1.0,
                timeout_failure_type="PROVIDER_TIMEOUT",
                source="provider",
            )
        )
        await started.wait()
        try:
            await race_task
        except ExecutionControlError as exc:
            root_failure = exc.failure_type
        finally:
            if not race_task.done():
                race_task.cancel()
                await asyncio.gather(race_task, return_exceptions=True)
        await asyncio.sleep(0)
        return {
            "simulated_provider_timeout": True,
            "unretrieved_task_exception": bool(loop_errors),
            "failure_classification": classified,
            "root_control_remains_consistent": root_failure == "ROOT_DEADLINE_EXCEEDED",
        }
    finally:
        loop.set_exception_handler(previous_handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--preflight", action="store_true")
    group.add_argument("--timeout-regression", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if args.preflight:
        print(json.dumps(_preflight(args.output), indent=2, sort_keys=True))
        return 0
    report = asyncio.run(_timeout_regression())
    print(json.dumps(report, indent=2, sort_keys=True))
    if not (
        report["simulated_provider_timeout"]
        and not report["unretrieved_task_exception"]
        and report["failure_classification"] == "PROVIDER_TIMEOUT"
        and report["root_control_remains_consistent"]
    ):
        return 1
    print("PHASE4_02H_TIMEOUT_REGRESSION_COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
