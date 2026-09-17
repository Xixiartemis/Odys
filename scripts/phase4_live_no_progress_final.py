"""02G final runner closure for the Phase 4 no-progress experiment.

This module is intentionally separate from the historical 02E runner. It
uses the normal Phase4Runner -> P45BenchmarkExecutor -> runtime-factory path
and puts one independent ``PhaseEffectPolicy`` instance into every RunSpec. The
preflight and smoke modes never construct a real provider.
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
from evals.reliability.p46_provider import (
    CHEAP_CREDENTIAL_ENV,
    CHEAP_MODEL,
    FROZEN_PROVIDER,
    create_cheap_model_provider,
    provider_identity,
)
from evals.reliability.run_phase4 import (
    ConfigLoader,
    FixtureManager,
    Phase4Runner,
    ProtocolSnapshot,
    RunSpec,
)
from scripts.phase4_live_no_progress_escalation import FAULT_ID, PROTOCOL_ROOT
from scripts.phase4_live_no_progress_parity import (
    _DeterministicProvider,
    _fixture_registry,
    _task as _parity_task,
)


EXPERIMENT_ID = "phase4-live-no-progress-escalation-02g"
EXPECTED_POLICY_ID = "phase4-effect-policy-v1"
REPEATS = 3
ARMS = (
    ("baseline", "LEGACY_BOUNDED"),
    ("v2", "NO_PROGRESS_AWARE"),
)
EXPECTED_RUNS = len(ARMS) * REPEATS
DEFAULT_OUTPUT = REPO_ROOT / "results" / EXPERIMENT_ID
_EXPERIMENT_ONLY_KEYS = frozenset(
    {
        "_experiment_macro_replan_enabled",
        "_phase_effect_policy",
        "escalation_trigger_policy",
        "experiment_arm",
    }
)


def _experiment_registry() -> FixtureRegistry:
    return _fixture_registry()


def _task(snapshot: ProtocolSnapshot) -> dict[str, Any]:
    task = dict(_parity_task(snapshot, experiment_id=EXPERIMENT_ID))
    task["benchmark_version"] = EXPERIMENT_ID
    return task


def _build_specs(
    snapshot: ProtocolSnapshot,
    *,
    task: dict[str, Any] | None = None,
) -> tuple[tuple[RunSpec, ...], dict[str, PhaseEffectPolicy]]:
    task = task or _task(snapshot)
    loader = ConfigLoader(snapshot)
    specs: list[RunSpec] = []
    policies: dict[str, PhaseEffectPolicy] = {}
    base_config = dict(loader.load("odys_p3"))
    for arm, policy_name in ARMS:
        for repeat in range(1, REPEATS + 1):
            policy = PhaseEffectPolicy()
            config = dict(base_config)
            config["_experiment_macro_replan_enabled"] = True
            config["_phase_effect_policy"] = policy
            config["escalation_trigger_policy"] = policy_name
            config["experiment_arm"] = arm
            spec = RunSpec(
                task=task,
                config=config,
                repeat_index=repeat,
                arm_id=arm,
            )
            specs.append(spec)
            policies[spec.run_id] = policy
    return tuple(specs), policies


def _effective_config_diff(left: dict[str, Any], right: dict[str, Any]) -> set[str]:
    keys = set(left) | set(right)
    return {
        key
        for key in keys
        if key not in _EXPERIMENT_ONLY_KEYS and left.get(key) != right.get(key)
    }


def _preflight(output: Path) -> dict[str, Any]:
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    task = _task(snapshot)
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"OUTPUT_COLLISION:{output}")
    if snapshot.fault_by_id.get(FAULT_ID) is None:
        raise RuntimeError(f"FROZEN_FAULT_NOT_FOUND:{FAULT_ID}")

    specs, policies = _build_specs(snapshot, task=task)
    if len(specs) != EXPECTED_RUNS:
        raise RuntimeError(f"EXPECTED_RUN_COUNT_MISMATCH:{len(specs)}")
    if len(policies) != EXPECTED_RUNS or len({id(item) for item in policies.values()}) != EXPECTED_RUNS:
        raise RuntimeError("EFFECT_POLICY_INSTANCE_COLLISION")

    configs = [spec.config for spec in specs]
    if any(config.get("_phase_effect_policy") is None for config in configs):
        raise RuntimeError("EFFECT_POLICY_NOT_IN_RUNSPEC")
    baseline = next(config for config in configs if config["experiment_arm"] == "baseline")
    v2 = next(config for config in configs if config["experiment_arm"] == "v2")
    if _effective_config_diff(baseline, v2):
        raise RuntimeError(
            f"UNEXPECTED_EFFECTIVE_CONFIG_DIFF:{sorted(_effective_config_diff(baseline, v2))}"
        )
    if any(policy.policy_id != EXPECTED_POLICY_ID for policy in policies.values()):
        raise RuntimeError("EFFECT_POLICY_ID_MISMATCH")

    executor_source = sys.modules["evals.reliability.p45_executor"]
    factory_source = sys.modules["evals.reliability.p46_provider"]
    p45_text = Path(executor_source.__file__).read_text(encoding="utf-8")
    p46_text = Path(factory_source.__file__).read_text(encoding="utf-8")
    real_runner_config_contains_policy = (
        'request.config.get("_phase_effect_policy")' in p45_text
        and 'config.get("_phase_effect_policy")' in p46_text
    )
    if not real_runner_config_contains_policy:
        raise RuntimeError("REAL_RUNNER_EFFECT_POLICY_WIRING_MISSING")

    default_executor = P45BenchmarkExecutor(
        factory_type="real", experiment_macro_replan_enabled=True
    )
    default_executor.configure_frozen_budget(snapshot.protocol["budgets"])
    if default_executor._frozen_budgets["max_replan_attempts"] != 1:
        raise RuntimeError("EXPERIMENT_REPLAN_OPT_IN_MISSING")

    return {
        "experiment_id": EXPERIMENT_ID,
        "base_sha": _git_head(),
        "protocol_hash": snapshot.protocol_hash,
        "task_id": task["task_id"],
        "fault_id": FAULT_ID,
        "expected_runs": EXPECTED_RUNS,
        "baseline_policy": "LEGACY_BOUNDED",
        "v2_policy": "NO_PROGRESS_AWARE",
        "effective_config_diff_except_primary_variable": sorted(
            _effective_config_diff(baseline, v2)
        ),
        "shared_effect_policy_implementation": True,
        "effect_policy_id": EXPECTED_POLICY_ID,
        "all_runs_have_effect_policy": all(
            config.get("_phase_effect_policy") is not None for config in configs
        ),
        "policy_instance_count": len(policies),
        "unique_policy_instance_count": len({id(item) for item in policies.values()}),
        "real_runner_config_contains_phase_effect_policy": real_runner_config_contains_policy,
        "provider_executed": False,
        "output_created": output.exists(),
    }


def _git_head() -> str:
    import subprocess

    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


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


async def _smoke(output: Path) -> dict[str, Any]:
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"OUTPUT_COLLISION:{output}")
    specs, policies = _build_specs(snapshot)
    output.mkdir(parents=True, exist_ok=True)
    # Keep the exact 02D/02F production-path construction for every run,
    # while giving each run its own deterministic provider and policy.  This
    # avoids sharing mutable provider phase state across independent RunSpecs.
    counts: dict[str, int] = {"valid": 0, "invalid": 0}
    for spec in specs:
        provider = _DeterministicProvider(policies[spec.run_id])
        runner = _build_runner(
            snapshot,
            output,
            provider=provider,
        )
        counts = await runner.run((spec,))
    raw = [
        json.loads(line)
        for line in (output / "raw.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    traces = {
        record["run_id"]: record
        for record in (json.loads(line) for line in (output / "traces.jsonl").read_text(encoding="utf-8").splitlines())
    }
    by_run = {record["benchmark_run_id"]: record for record in raw}
    baseline = [record for record in raw if "::baseline::" in record["benchmark_run_id"]]
    v2 = [record for record in raw if "::v2::" in record["benchmark_run_id"]]
    all_events = [
        event
        for trace in traces.values()
        for event in trace.get("execution_trace", [])
    ]
    report = {
        "experiment_id": EXPERIMENT_ID,
        "provider_executed": False,
        "planned_runs": EXPECTED_RUNS,
        "valid_runs": counts["valid"],
        "invalid_runs": counts["invalid"],
        "policy_instance_count": len(policies),
        "unique_policy_instance_count": len({id(item) for item in policies.values()}),
        "all_runs_have_effect_policy": all(
            config.get("_phase_effect_policy") is not None
            for spec in specs
            for config in (spec.config,)
        ),
        "initial_alternate_mutation_denied": any(
            item["phase"] == "initial" and item["alternate_effect"] and not item["allowed"]
            for policy in policies.values()
            for item in policy.denied
        ),
        "local_repair_alternate_mutation_denied": any(
            item["phase"] == "local_repair" and item["alternate_effect"] and not item["allowed"]
            for policy in policies.values()
            for item in policy.denied
        ),
        "post_replan_alternate_mutation_allowed": any(
            item["phase"] == "post_replan" and item["alternate_effect"] and item["allowed"]
            for policy in policies.values()
            for item in policy.allowed
        ),
        "baseline_final_validator_accepted": sum(
            record["runtime_environment"]["validation"]["final_acceptance_status"] == "ACCEPTED"
            for record in baseline
        ) == REPEATS,
        "v2_final_validator_accepted": sum(
            record["runtime_environment"]["validation"]["final_acceptance_status"] == "ACCEPTED"
            for record in v2
        ) == REPEATS,
        "fault_trigger_index_1": all(
            next(
                event["metadata"].get("trigger_index")
                for event in traces[run_id].get("execution_trace", [])
                if event.get("event_type") == "FAULT_TRIGGERED"
            ) == 1
            for run_id in by_run
        ),
        "recovery_events_observed": sum(
            event.get("event_type") == "REPLAN_ACCEPTED" for event in all_events
        ),
    }
    if not (
        report["valid_runs"] == EXPECTED_RUNS
        and report["invalid_runs"] == 0
        and report["initial_alternate_mutation_denied"]
        and report["local_repair_alternate_mutation_denied"]
        and report["post_replan_alternate_mutation_allowed"]
        and report["baseline_final_validator_accepted"]
        and report["v2_final_validator_accepted"]
        and report["fault_trigger_index_1"]
    ):
        raise RuntimeError(f"02G_SMOKE_FAILED:{json.dumps(report, sort_keys=True)}")
    (output / "qualification.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


async def _execute(output: Path) -> dict[str, Any]:
    import os

    if not os.environ.get(CHEAP_CREDENTIAL_ENV, "").strip():
        raise RuntimeError(f"CREDENTIAL_REQUIRED_BEFORE_RUN:{CHEAP_CREDENTIAL_ENV}")
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"OUTPUT_COLLISION:{output}")
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    task = _task(snapshot)
    specs, policies = _build_specs(snapshot, task=task)
    identity_provider = create_cheap_model_provider()
    identity = provider_identity(identity_provider, expected_model=CHEAP_MODEL)
    output.mkdir(parents=True, exist_ok=True)
    (output / "provider_identity.json").write_text(
        json.dumps(identity, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "benchmark_identity.json").write_text(
        json.dumps(
            {
                "benchmark_version": EXPERIMENT_ID,
                "protocol_hash": snapshot.protocol_hash,
                "model_identity": CHEAP_MODEL,
                "provider_identity": FROZEN_PROVIDER,
                "credential_value_recorded": False,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    counts: dict[str, int] = {"valid": 0, "invalid": 0}
    for spec in specs:
        provider = create_cheap_model_provider()
        runner = _build_runner(snapshot, output, provider=provider)
        counts = await runner.run((spec,))
    report = {
        "experiment_id": EXPERIMENT_ID,
        "planned_runs": EXPECTED_RUNS,
        "valid_runs": counts["valid"],
        "invalid_runs": counts["invalid"],
        "total_runs": counts["valid"] + counts["invalid"],
        "provider_executed": True,
    }
    (output / "summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--preflight", action="store_true")
    group.add_argument("--smoke", action="store_true")
    group.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if args.preflight:
        print(json.dumps(_preflight(args.output), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.smoke:
        report = asyncio.run(_smoke(args.output))
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        print("PHASE4_02G_PROVIDER_FREE_SMOKE_COMPLETE")
        print(f"RESULT_PATH={args.output}")
        return 0
    report = asyncio.run(_execute(args.output))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    print("PHASE4_02G_EXECUTION_COMPLETE")
    print(f"RESULT_PATH={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
