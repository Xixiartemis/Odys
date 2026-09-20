"""Prepare/run the Phase 4 Experiment 02 no-progress paired diagnostic.

This module is intentionally separate from the frozen Phase 4 protocol.  It
uses the official ``Phase4Runner`` -> ``P45BenchmarkExecutor`` -> real Odys
runtime path, but supplies an experiment-local task projection and an
execution-local escalation policy.  ``--preflight`` is provider-free and does
not create the result directory; a human with credentials may use
``--execute`` later.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for import_root in (REPO_ROOT, REPO_ROOT / "src"):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

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


EXPERIMENT_ID = "phase4-live-no-progress-escalation-02"
PROTOCOL_ROOT = REPO_ROOT / "evals" / "reliability" / "phase4_v1"
DEFAULT_OUTPUT = REPO_ROOT / "results" / EXPERIMENT_ID
EXPERIMENT_TASK_ID = "P4E02-CWR-NP-01"
FAULT_ID = "FAIL_TOOL_ON_CALL_1"
REPEATS = 3
ARMS = (
    ("baseline", "LEGACY_BOUNDED"),
    ("v2", "NO_PROGRESS_AWARE"),
)
PRIMARY_VARIABLE = "escalation_trigger_policy"
EXPECTED_FACTORY_NAMES = (
    "RealLLMMinimalRuntimeFactory",
    "RealLLMOdysRuntimeFactory",
)
EXPERIMENT_ONLY_CONFIG_KEYS = frozenset(
    {
        "_experiment_macro_replan_enabled",
        "escalation_trigger_policy",
        "experiment_arm",
    }
)
EXPERIMENT_01_PATHS = (
    "scripts/phase4_live_controlled_experiment.py",
    "docs/evidence/PHASE4_LIVE_CONTROLLED_EXPERIMENT_01.md",
)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _credential_present() -> bool:
    value = os.environ.get(CHEAP_CREDENTIAL_ENV)
    return bool(value and value.strip())


def _frozen_experiment_01_unchanged() -> bool:
    for relative in EXPERIMENT_01_PATHS:
        current = REPO_ROOT / relative
        try:
            committed = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "show", f"HEAD:{relative}"],
                check=True,
                capture_output=True,
            ).stdout
        except subprocess.CalledProcessError:
            return False
        if not current.exists() or current.read_bytes() != committed:
            return False
    return True


def _committed_file_unchanged(relative: str) -> bool:
    current = REPO_ROOT / relative
    if not current.exists():
        return False
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "diff", "--quiet", "HEAD", "--", relative]
    )
    # Git's comparison honors the repository's line-ending attributes; a
    # byte-for-byte comparison would falsely flag a clean CRLF worktree.
    return result.returncode == 0


def _experiment_task(snapshot: ProtocolSnapshot) -> dict[str, Any]:
    """Return a task projection; no frozen manifest file is modified."""
    fixture_id = "fixture-replan-v1"
    fixture = snapshot.fixtures["fixtures"][fixture_id]
    return {
        "benchmark_version": EXPERIMENT_ID,
        "task_id": EXPERIMENT_TASK_ID,
        "family": "COMPLEX_WORKFLOW_REPLAN",
        "title": "No-progress local repair escalation",
        "objective": (
            "Repair the blocked state. A local repair may be syntactically "
            "successful without changing the observable route; when the "
            "local strategy makes no progress, use the alternate replan path."
        ),
        "fixture_id": fixture_id,
        "fixture_version": str(fixture["version"]),
        "fixture_hash_source": f"fixtures/catalog.json#{fixture_id}",
        "initial_state": "blocked route with local strategy available",
        "required_capabilities": ["workspace.edit", "workspace.edit_lines"],
        "acceptance_criteria": [
            "observable state has route=alternate",
            "observable state has state_status=verified",
        ],
        "validator_id": "external-observable-v1",
        "fault_injection": FAULT_ID,
        "fault_timing": "first native tool dispatch",
        "max_turns": 20,
        "max_model_calls": 20,
        "timeout_seconds": 900,
        "side_effect_policy": "bounded local repair with macro escalation",
        "expected_observable_effects": {
            "route": "alternate",
            "state_status": "verified",
        },
        "measurement_tags": ["no_progress", "macro_replan", "paired"],
        # These are consumed only by the experiment opt-in coordinator.  The
        # initial and alternate strategies use the same allowed capability set
        # and differ only in strategy after replan.
        "experiment_initial_plan_steps": ["workspace.edit"],
        "experiment_replan_plan_steps": ["workspace.edit_lines"],
        "experiment_step_inputs": {
            "workspace.edit": {
                "path": "state.json",
                "content": '{"route":"local","state_status":"blocked"}\n',
            },
            "workspace.edit_lines": {
                "path": "state.json",
                "old_string": '"route":"local"',
                "new_string": '"route":"alternate","state_status":"verified"',
            },
        },
    }


class _NoProgressFixture:
    """Small experiment-only fixture; frozen fixture files remain untouched."""

    task_id = EXPERIMENT_TASK_ID

    @staticmethod
    def _path(workspace: Path) -> Path:
        return workspace / "state.json"

    def setup(self, workspace: Path) -> None:
        workspace.mkdir(parents=True, exist_ok=True)
        self._path(workspace).write_text(
            '{"route":"local","state_status":"blocked"}\n',
            encoding="utf-8",
        )

    def inject_fault(self, workspace: Path, fault_id: str) -> None:
        state = json.loads(self._path(workspace).read_text(encoding="utf-8"))
        state["fault_armed"] = fault_id
        self._path(workspace).write_text(
            json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    def observe(self, workspace: Path) -> dict[str, Any]:
        state = json.loads(self._path(workspace).read_text(encoding="utf-8"))
        return {
            "route": state.get("route"),
            "state_status": state.get("state_status"),
            "fault_armed": state.get("fault_armed"),
        }

    def reset(self, workspace: Path) -> None:
        self._path(workspace).unlink(missing_ok=True)


def _experiment_registry() -> FixtureRegistry:
    registry = FixtureRegistry()
    # The registry is an execution adapter registry, not the frozen fixture
    # catalog.  The task projection binds its identity to the frozen catalog
    # entry while the experiment owns this separate deterministic workspace.
    registry._registry[EXPERIMENT_TASK_ID] = _NoProgressFixture  # type: ignore[attr-defined]
    return registry


def _arm_config(snapshot: ProtocolSnapshot, policy: str, arm: str) -> dict[str, Any]:
    config = dict(ConfigLoader(snapshot).load("odys_p3"))
    config["_experiment_macro_replan_enabled"] = True
    config["escalation_trigger_policy"] = policy
    config["experiment_arm"] = arm
    return config


def _effective_config_diff(left: dict[str, Any], right: dict[str, Any]) -> set[str]:
    keys = set(left) | set(right)
    return {
        key
        for key in keys
        if key not in EXPERIMENT_ONLY_CONFIG_KEYS and left.get(key) != right.get(key)
    }


def _preflight(output: Path) -> dict[str, Any]:
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    task = _experiment_task(snapshot)
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"OUTPUT_COLLISION:{output}")
    fault = snapshot.fault_by_id.get(FAULT_ID)
    if fault is None:
        raise RuntimeError(f"FROZEN_FAULT_NOT_FOUND:{FAULT_ID}")
    if fault.get("trigger_count") != 1 or "tool_call" not in str(fault.get("trigger", "")):
        raise RuntimeError("FAULT_TRIGGER_CONTRACT_INVALID")

    baseline = _arm_config(snapshot, "LEGACY_BOUNDED", "baseline")
    v2 = _arm_config(snapshot, "NO_PROGRESS_AWARE", "v2")
    if _effective_config_diff(baseline, v2):
        raise RuntimeError(f"UNEXPECTED_EFFECTIVE_CONFIG_DIFF:{sorted(_effective_config_diff(baseline, v2))}")

    default_executor = P45BenchmarkExecutor(factory_type="real")
    default_executor.configure_frozen_budget(snapshot.protocol["budgets"])
    opt_in_executor = P45BenchmarkExecutor(
        factory_type="real", experiment_macro_replan_enabled=True
    )
    opt_in_executor.configure_frozen_budget(snapshot.protocol["budgets"])
    default_budget = default_executor._frozen_budgets  # offline config proof
    opt_in_budget = opt_in_executor._frozen_budgets
    factory_source = inspect.getsource(P45BenchmarkExecutor.execute)
    recovery_source = inspect.getsource(P45BenchmarkExecutor.recover_after_validation)
    return {
        "experiment_id": EXPERIMENT_ID,
        "base_sha": _git("rev-parse", "HEAD"),
        "protocol_hash": snapshot.protocol_hash,
        "manifest_hash": snapshot.manifest_hash,
        "fault_set_hash": snapshot.fault_set_hash,
        "validator_hash": snapshot.validator_hash,
        "fixture_set_hash": snapshot.fixture_set_hash,
        "budget_identity": snapshot.budget_identity,
        "experiment_01_unchanged": _frozen_experiment_01_unchanged(),
        "official_default_replan_behavior_preserved": (
            default_budget["max_replan_attempts"] == 0
            and "max_replan_attempts" in default_budget
        ),
        "experiment_opt_in_replan_enabled": opt_in_budget["max_replan_attempts"] == 1,
        "baseline_config": "odys_p3",
        "v2_config": "odys_p3",
        "primary_variable": PRIMARY_VARIABLE,
        "baseline_policy": baseline["escalation_trigger_policy"],
        "v2_policy": v2["escalation_trigger_policy"],
        "recovery_budgets_identical": baseline["_experiment_macro_replan_enabled"]
        == v2["_experiment_macro_replan_enabled"],
        "effective_config_diff": sorted(_effective_config_diff(baseline, v2)),
        "fault_trigger_telemetry_ready": all(
            marker in factory_source
            for marker in ("FAULT_ARMED", "FAULT_TRIGGERED", "fault_trigger_index")
        ),
        "progress_fingerprint_telemetry_ready": all(
            marker in recovery_source or marker in inspect.getsource(P45BenchmarkExecutor)
            for marker in ("escalation_trigger_policy", "_experiment_macro_replan_enabled")
        ),
        "validator_unchanged": _committed_file_unchanged(
            "evals/reliability/phase4_v1/validators.json"
        ),
        "only_validator_can_verify": "ExternalObservableValidator" in inspect.getsource(
            sys.modules["evals.reliability.run_phase4"]
        ),
        "official_execution_path": all(name in factory_source for name in EXPECTED_FACTORY_NAMES)
        and "P410IntegrationExecutor" not in factory_source,
        "provider_executed": False,
        "result_created": False,
        "output_exists_after_preflight": output.exists(),
        "task_id": task["task_id"],
        "fault_id": fault["fault_id"],
        "expected_runs": len(ARMS) * REPEATS,
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _build_manifest(snapshot: ProtocolSnapshot, task: dict[str, Any], output: Path) -> dict[str, Any]:
    budget = dict(snapshot.protocol["budgets"])
    return {
        "experiment_id": EXPERIMENT_ID,
        "repo_sha": _git("rev-parse", "HEAD"),
        "base_protocol_version": snapshot.protocol["benchmark_version"],
        "protocol_hash": snapshot.protocol_hash,
        "manifest_hash": snapshot.manifest_hash,
        "fault_set_hash": snapshot.fault_set_hash,
        "validator_hash": snapshot.validator_hash,
        "fixture_set_hash": snapshot.fixture_set_hash,
        "budget_identity": snapshot.budget_identity,
        "task_projection": task,
        "fault_definition": snapshot.fault_by_id[FAULT_ID],
        "arms": [
            {
                "arm": arm,
                "config": "odys_p3",
                "escalation_trigger_policy": policy,
                "experiment_macro_replan_enabled": True,
                "max_replan_attempts": 1,
                "repeats": REPEATS,
                "shared_budget": budget,
            }
            for arm, policy in ARMS
        ],
        "primary_variable": PRIMARY_VARIABLE,
        "model_identity": CHEAP_MODEL,
        "provider_identity": FROZEN_PROVIDER,
        "credential_env": CHEAP_CREDENTIAL_ENV,
        "credential_value_recorded": False,
        "output_path": str(output),
        "notes": [
            "Experiment-only task projection; frozen protocol inputs are not rewritten.",
            "Both arms use odys_p3 and the same opt-in macro-replan capability and budgets.",
            "LEGACY_BOUNDED observes no-progress but does not use it as an early control decision.",
            "NO_PROGRESS_AWARE consumes the typed signal and may escalate while local reserve remains.",
            "A result mismatch must be reported as COST_COMPARISON=NOT_COMPARABLE / OUTCOME_MISMATCH.",
        ],
    }


async def _execute(output: Path) -> None:
    if not _credential_present():
        raise RuntimeError(f"CREDENTIAL_REQUIRED_BEFORE_RUN:{CHEAP_CREDENTIAL_ENV}")
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"OUTPUT_COLLISION:{output}")
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    task = _experiment_task(snapshot)
    registry = _experiment_registry()
    provider = create_cheap_model_provider()
    identity = provider_identity(provider, expected_model=CHEAP_MODEL)
    executor = P45BenchmarkExecutor(
        fixture_registry=registry,
        factory_type="real",
        provider=provider,
        provider_identity=identity,
        expected_model=CHEAP_MODEL,
        experiment_macro_replan_enabled=True,
    )
    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "experiment_manifest.json", _build_manifest(snapshot, task, output))
    _write_json(output / "provider_identity.json", identity)
    _write_json(
        output / "benchmark_identity.json",
        {
            "benchmark_version": EXPERIMENT_ID,
            "base_protocol_version": snapshot.protocol["benchmark_version"],
            "protocol_hash": snapshot.protocol_hash,
            "manifest_hash": snapshot.manifest_hash,
            "fault_set_hash": snapshot.fault_set_hash,
            "validator_hash": snapshot.validator_hash,
            "fixture_set_hash": snapshot.fixture_set_hash,
            "budget_identity": snapshot.budget_identity,
            "model_identity": CHEAP_MODEL,
            "provider_identity": FROZEN_PROVIDER,
            "credential_value_recorded": False,
        },
    )
    specs = []
    base_config = ConfigLoader(snapshot).load("odys_p3")
    for arm, policy in ARMS:
        config = dict(base_config)
        config["_experiment_macro_replan_enabled"] = True
        config["escalation_trigger_policy"] = policy
        config["experiment_arm"] = arm
        for repeat in range(1, REPEATS + 1):
            specs.append(RunSpec(task=task, config=config, repeat_index=repeat, arm_id=arm))
    runner = Phase4Runner(
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
    counts = await runner.run(specs)
    _write_json(
        output / "summary.json",
        {
            "experiment_id": EXPERIMENT_ID,
            "planned_runs": len(specs),
            "valid_runs": counts["valid"],
            "invalid_runs": counts["invalid"],
            "total_runs": counts["valid"] + counts["invalid"],
            "provider_executed": True,
        },
    )
    print("EXPERIMENT_02_EXECUTION_COMPLETE")
    print(f"RESULT_PATH={output}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if args.preflight == args.execute:
        parser.error("choose exactly one of --preflight or --execute")
    if args.preflight:
        report = _preflight(args.output)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    import asyncio

    asyncio.run(_execute(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
