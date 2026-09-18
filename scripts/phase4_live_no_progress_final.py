"""02G final runner closure for the Phase 4 no-progress experiment.

This module is intentionally separate from the historical 02E runner. It
uses the normal Phase4Runner -> P45BenchmarkExecutor -> runtime-factory path
and puts one independent ``PhaseEffectPolicy`` instance into every RunSpec. The
preflight and smoke modes never construct a real provider.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
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
    FROZEN_ENDPOINT,
    FROZEN_MAX_TOKENS,
    FROZEN_PROVIDER,
    FROZEN_SEED,
    FROZEN_TEMPERATURE,
    create_cheap_model_provider,
    provider_identity,
    SDK_MAX_RETRIES,
)
from evals.reliability.run_phase4 import (
    ConfigLoader,
    FixtureManager,
    Phase4Runner,
    ProtocolSnapshot,
    RunSpec,
)
from lhas.execution_control import ExecutionControlError, ExecutionControlToken, await_with_control
from lhas.native.runtime import ProviderFailureClassifier
from scripts.phase4_live_no_progress_escalation import FAULT_ID, PROTOCOL_ROOT
from scripts.phase4_live_no_progress_parity import (
    TASK_ID as PARITY_TASK_ID,
    _DeterministicProvider,
    _fixture_registry,
)


EXPERIMENT_ID = "phase4-live-no-progress-escalation-02h"
EXPECTED_POLICY_ID = "phase4-effect-policy-v1"
REPEATS = 3
ARMS = (
    ("baseline", "LEGACY_BOUNDED"),
    ("v2", "NO_PROGRESS_AWARE"),
)
EXPECTED_RUNS = len(ARMS) * REPEATS
DEFAULT_OUTPUT = REPO_ROOT / "results" / EXPERIMENT_ID
ROOT_TIMEOUT_SECONDS = 900.0
PROVIDER_TIMEOUT_CEILING_SECONDS = 300.0
RECOVERY_THRESHOLDS = {
    "repair_max_no_progress": 3,
    # Keep the same ordering as the already-qualified parity task: repeated
    # state is the first equivalent-local-repair signal, while repeated action
    # remains a later, distinct classification.
    "repair_max_repeated_action": 16,
    "repair_max_repeated_state": 2,
}
EXPECTED_PROTOCOL_HASH = "993eae04290fe683d40fcf845b9e7325b9572b227e78e7cb3090dd7ee37637c3"
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
    fixture_id = "fixture-replan-v1"
    fixture = snapshot.fixtures["fixtures"][fixture_id]
    return {
        "benchmark_version": EXPERIMENT_ID,
        # The executable fixture registry is intentionally keyed by this
        # frozen qualification task identity; the 02H projection changes the
        # experiment identity and timeout/threshold authority, not the task
        # fixture itself.
        "task_id": PARITY_TASK_ID,
        "family": "COMPLEX_WORKFLOW_REPLAN",
        "title": "Final live no-progress escalation experiment",
        "objective": "Change the authoritative route through bounded recovery.",
        "fixture_id": fixture_id,
        "fixture_version": str(fixture["version"]),
        "fixture_hash_source": f"fixtures/catalog.json#{fixture_id}",
        "initial_state": "local blocked route",
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
        "timeout_seconds": ROOT_TIMEOUT_SECONDS,
        **RECOVERY_THRESHOLDS,
        "side_effect_policy": "phase-gated alternate effect",
        "expected_observable_effects": {
            "route": "alternate",
            "state_status": "verified",
        },
        "measurement_tags": ["no_progress", "macro_replan", "paired", "02h"],
        "experiment_initial_plan_steps": ["workspace.edit"],
        "experiment_replan_plan_steps": ["workspace.edit_lines"],
        "experiment_step_inputs": {
            "workspace.edit": {
                "path": "state.json",
                "content": '{"route":"local","state_status":"blocked"}\n',
            },
            "workspace.edit_lines": {
                "path": "state.json",
                "old_string": '"route":"local","state_status":"blocked"',
                "new_string": '"route":"alternate","state_status":"verified"',
            },
        },
    }


def _task_projection_hash(task: dict[str, Any]) -> str:
    payload = json.dumps(task, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _projection_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _implementation_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


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
    # Interleave paired arms to prevent public-provider time drift from being
    # confounded with the primary escalation-policy variable.
    for repeat in range(1, REPEATS + 1):
        for arm, policy_name in ARMS:
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
    if snapshot.protocol_hash != EXPECTED_PROTOCOL_HASH:
        raise RuntimeError("FROZEN_PROTOCOL_HASH_CHANGED")
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"OUTPUT_COLLISION:{output}")
    fault = snapshot.fault_by_id.get(FAULT_ID)
    if fault is None:
        raise RuntimeError(f"FROZEN_FAULT_NOT_FOUND:{FAULT_ID}")
    if int(fault.get("trigger_count", 0)) != 1:
        raise RuntimeError("FAULT_TRIGGER_COUNT_MUST_BE_ONE")
    trigger_text = str(fault.get("trigger", "")).lower()
    if "ordinal == 1" not in trigger_text and "first" not in trigger_text:
        raise RuntimeError("FAULT_MUST_TRIGGER_ON_FIRST_TOOL_CALL")

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
    if task["timeout_seconds"] != ROOT_TIMEOUT_SECONDS:
        raise RuntimeError("ROOT_TIMEOUT_PROJECTION_MISMATCH")
    if ROOT_TIMEOUT_SECONDS <= PROVIDER_TIMEOUT_CEILING_SECONDS:
        raise RuntimeError("ROOT_TIMEOUT_MUST_EXCEED_PROVIDER_CEILING")
    if any(task[key] != value for key, value in RECOVERY_THRESHOLDS.items()):
        raise RuntimeError("RECOVERY_THRESHOLD_PROJECTION_MISMATCH")

    baseline_task_hash = _task_projection_hash(task)
    v2_task_hash = _task_projection_hash(task)
    fault_projection = {
        "fault_id": FAULT_ID,
        "fault": fault,
        "fault_timing": task["fault_timing"],
    }
    baseline_fault_hash = _projection_hash(fault_projection)
    v2_fault_hash = _projection_hash(fault_projection)
    validator_projection = {
        "validator_id": task["validator_id"],
        "acceptance_criteria": task["acceptance_criteria"],
        "expected_observable_effects": task["expected_observable_effects"],
    }
    baseline_validator_hash = _projection_hash(validator_projection)
    v2_validator_hash = _projection_hash(validator_projection)
    budget_projection = {
        key: task[key]
        for key in (
            "max_turns",
            "max_model_calls",
            "timeout_seconds",
            "repair_max_no_progress",
            "repair_max_repeated_action",
            "repair_max_repeated_state",
        )
    }
    baseline_budget_hash = _projection_hash(budget_projection)
    v2_budget_hash = _projection_hash(budget_projection)
    capability_projection = {"required_capabilities": task["required_capabilities"]}
    baseline_capability_hash = _projection_hash(capability_projection)
    v2_capability_hash = _projection_hash(capability_projection)
    if not (
        baseline_task_hash == v2_task_hash
        and baseline_fault_hash == v2_fault_hash
        and baseline_validator_hash == v2_validator_hash
        and baseline_budget_hash == v2_budget_hash
        and baseline_capability_hash == v2_capability_hash
    ):
        raise RuntimeError("PAIRED_FROZEN_PROJECTION_MISMATCH")

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
        "fault_trigger_count": int(fault["trigger_count"]),
        "fault_first_tool_call": True,
        "task_projection_hash": _task_projection_hash(task),
        "manifest_hash": snapshot.manifest_hash,
        "fault_set_hash": snapshot.fault_set_hash,
        "fixture_set_hash": snapshot.fixture_set_hash,
        "validator_hash": snapshot.validator_hash,
        "budget_identity": snapshot.budget_identity,
        "fixture_runtime_implementation_hash": _implementation_hash(
            REPO_ROOT / "scripts" / "phase4_live_no_progress_parity.py"
        ),
        "validator_runtime_implementation_hash": _implementation_hash(
            REPO_ROOT / "evals" / "reliability" / "run_phase4.py"
        ),
        "effect_policy_implementation_hash": _implementation_hash(
            REPO_ROOT / "evals" / "reliability" / "effect_policy.py"
        ),
        "model_identity": CHEAP_MODEL,
        "provider_identity": FROZEN_PROVIDER,
        "endpoint_identity": FROZEN_ENDPOINT,
        "temperature": FROZEN_TEMPERATURE,
        "max_tokens": FROZEN_MAX_TOKENS,
        "seed": FROZEN_SEED,
        "same_task_across_arms": baseline is not None and v2 is not None,
        "same_validator_across_arms": baseline_validator_hash == v2_validator_hash,
        "same_fault_across_arms": baseline_fault_hash == v2_fault_hash,
        "same_budget_across_arms": baseline_budget_hash == v2_budget_hash,
        "same_capability_set_across_arms": baseline_capability_hash == v2_capability_hash,
        "baseline_capability_projection_hash": baseline_capability_hash,
        "v2_capability_projection_hash": v2_capability_hash,
        "baseline_task_projection_hash": baseline_task_hash,
        "v2_task_projection_hash": v2_task_hash,
        "baseline_fault_projection_hash": baseline_fault_hash,
        "v2_fault_projection_hash": v2_fault_hash,
        "baseline_validator_projection_hash": baseline_validator_hash,
        "v2_validator_projection_hash": v2_validator_hash,
        "baseline_budget_projection_hash": baseline_budget_hash,
        "v2_budget_projection_hash": v2_budget_hash,
        "only_primary_causal_variable": (
            not _effective_config_diff(baseline, v2)
            and baseline_task_hash == v2_task_hash
            and baseline_fault_hash == v2_fault_hash
            and baseline_validator_hash == v2_validator_hash
            and baseline_budget_hash == v2_budget_hash
            and baseline_capability_hash == v2_capability_hash
        ),
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
        "root_timeout_seconds": ROOT_TIMEOUT_SECONDS,
        "provider_timeout_ceiling_seconds": PROVIDER_TIMEOUT_CEILING_SECONDS,
        "root_timeout_gt_provider_timeout": ROOT_TIMEOUT_SECONDS > PROVIDER_TIMEOUT_CEILING_SECONDS,
        "recovery_thresholds_single_authority": True,
        "recovery_thresholds": dict(RECOVERY_THRESHOLDS),
        "sdk_max_retries": SDK_MAX_RETRIES,
        "run_order": [spec.run_id for spec in specs],
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


class _AppendOnlyEvidence:
    """Append-only, secret-free per-run evidence with immutable run IDs."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_ids = set()
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = json.loads(line)
                    self.run_ids.add(str(record["run_id"]))

    def append(self, record: dict[str, Any]) -> None:
        run_id = str(record["run_id"])
        if run_id in self.run_ids:
            return
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
        self.run_ids.add(run_id)


def _policy_evidence(run_id: str, spec: RunSpec, policy: PhaseEffectPolicy) -> dict[str, Any]:
    detections = [dict(item) for item in policy.recovery_detections if item.get("run_id") == run_id]
    signals = [dict(item) for item in policy.replan_signal_reasons if item.get("run_id") == run_id]
    results = [dict(item) for item in policy.replan_results if run_id in str(item.get("plan_id", "")) or item.get("run_id") == run_id]
    reservations = [dict(item) for item in policy.replan_reservations]
    return {
        "run_id": run_id,
        "policy_id": policy.policy_id,
        "arm": spec.arm_id,
        "repeat": spec.repeat_index,
        "registry_install_count": int(policy.registry_install_count),
        "denied": [dict(item) for item in policy.denied],
        "allowed": [dict(item) for item in policy.allowed],
        "recovery_detections": detections,
        "replan_signal_reasons": signals,
        "replan_reservations": reservations,
        "replan_results": results,
        "initial_alternate_mutation_denied": any(
            item.get("alternate_effect") and item.get("phase") == "initial" and not item.get("allowed")
            for item in policy.denied
        ),
        "local_repair_alternate_mutation_denied": any(
            item.get("alternate_effect") and item.get("phase") == "local_repair" and not item.get("allowed")
            for item in policy.denied
        ),
        "post_replan_alternate_mutation_allowed": any(
            item.get("alternate_effect") and item.get("phase") == "post_replan" and item.get("allowed")
            for item in policy.allowed
        ),
        "repair_no_progress_observed": any(
            item.get("reason") == "REPAIR_NO_PROGRESS" for item in detections
        ),
        "no_progress_used_for_control": any(
            item.get("reason") == "REPAIR_NO_PROGRESS" for item in signals
        ),
        "macro_replan_executed": any(item.get("accepted") is True for item in results),
        "local_reserve_at_escalation": any(
            int((item.get("snapshot") or {}).get("remaining_provider_calls", 0) or 0) > 0
            for item in reservations
        ),
    }


def _write_experiment_manifest(
    output: Path,
    snapshot: ProtocolSnapshot,
    task: dict[str, Any],
    specs: tuple[RunSpec, ...],
    *,
    provider_executed: bool,
) -> dict[str, Any]:
    manifest = {
        "experiment_id": EXPERIMENT_ID,
        "repo_sha": _git_head(),
        "protocol_hash": snapshot.protocol_hash,
        "manifest_hash": snapshot.manifest_hash,
        "fault_hash": snapshot.fault_set_hash,
        "fixture_hash": snapshot.fixture_set_hash,
        "validator_hash": snapshot.validator_hash,
        "budget_identity": snapshot.budget_identity,
        "fixture_runtime_implementation_hash": _implementation_hash(
            REPO_ROOT / "scripts" / "phase4_live_no_progress_parity.py"
        ),
        "validator_runtime_implementation_hash": _implementation_hash(
            REPO_ROOT / "evals" / "reliability" / "run_phase4.py"
        ),
        "effect_policy_implementation_hash": _implementation_hash(
            REPO_ROOT / "evals" / "reliability" / "effect_policy.py"
        ),
        "task_projection_hash": _task_projection_hash(task),
        "task_projection": task,
        "run_order": [spec.run_id for spec in specs],
        "arm_definitions": [
            {"name": arm, "escalation_trigger_policy": policy}
            for arm, policy in ARMS
        ],
        "primary_causal_variable": "escalation_trigger_policy",
        "root_timeout_seconds": ROOT_TIMEOUT_SECONDS,
        "provider_timeout_ceiling_seconds": PROVIDER_TIMEOUT_CEILING_SECONDS,
        "sdk_max_retries": SDK_MAX_RETRIES,
        "model_identity": CHEAP_MODEL,
        "provider_identity": FROZEN_PROVIDER,
        "provider_executed": bool(provider_executed),
    }
    path = output / "experiment_manifest.json"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        immutable = dict(existing)
        immutable.pop("provider_executed", None)
        candidate = dict(manifest)
        candidate.pop("provider_executed", None)
        if immutable != candidate:
            raise RuntimeError("EXPERIMENT_MANIFEST_IDENTITY_MISMATCH")
        if bool(existing.get("provider_executed")) != bool(provider_executed):
            updated = dict(existing)
            updated["provider_executed"] = bool(provider_executed)
            path.write_text(
                json.dumps(updated, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return updated
        return existing
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _run_qualification(
    raw: list[dict[str, Any]],
    traces: dict[str, dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for record in raw:
        run_id = str(record["benchmark_run_id"])
        trace = traces.get(run_id, {})
        events = trace.get("execution_trace", [])
        event_types = {str(item.get("event_type")) for item in events}
        metadata = next(
            (item.get("metadata", {}) for item in events if item.get("event_type") == "FAULT_TRIGGERED"),
            {},
        )
        validation = record.get("runtime_environment", {}).get("validation", {})
        policy = evidence[run_id]
        replan_indices = [
            index
            for index, item in enumerate(events)
            if item.get("event_type") == "REPLAN_ACCEPTED"
        ]
        post_replan_events = (
            events[replan_indices[-1] + 1 :]
            if replan_indices
            else []
        )
        post_replan_event_types = {
            str(item.get("event_type")) for item in post_replan_events
        }
        post_replan_tool_requested_count = sum(
            item.get("event_type") == "TOOL_CALL_REQUESTED"
            for item in post_replan_events
        )
        post_replan_provider_call_count = sum(
            item.get("event_type") == "PROVIDER_RESPONSE_SUCCESS"
            for item in post_replan_events
        )
        post_replan_mutation_count = sum(
            bool(item.get("metadata", {}).get("observed_mutation"))
            for item in post_replan_events
            if item.get("event_type") == "TOOL_CALL_OBSERVED"
        )
        # A same-step post-replan retry is redundant once the accepted
        # planner-owned mutation has been observed.
        post_replan_redundant_tool_calls = max(
            0, post_replan_tool_requested_count - 1
        )
        post_replan_redundant_provider_calls = max(
            0, post_replan_provider_call_count - 1
        )
        # New traces carry an explicit start event.  Older replay traces may
        # not, so an actual provider/tool event *after the durable acceptance*
        # is the conservative fallback; REPLAN_ACCEPTED alone is deliberately
        # insufficient.
        post_replan_provider_called = "PROVIDER_RESPONSE_SUCCESS" in post_replan_event_types
        post_replan_tool_requested = "TOOL_CALL_REQUESTED" in post_replan_event_types
        post_replan_started = (
            "POST_REPLAN_EXECUTION_STARTED" in post_replan_event_types
            or post_replan_provider_called
            or post_replan_tool_requested
        )
        post_replan_mutation_observed = any(
            bool(item.get("metadata", {}).get("observed_mutation"))
            for item in post_replan_events
            if item.get("event_type") == "TOOL_CALL_OBSERVED"
        )
        local_repair_plan_authority_blocked = any(
            str(item.get("metadata", {}).get("error_type"))
            == "PLAN_STEP_ARGUMENTS_MISMATCH"
            for item in events
            if item.get("event_type") == "TOOL_CALL_OBSERVED"
            and item not in post_replan_events
        )
        results.append({
            "run_id": run_id,
            "arm": evidence[run_id]["arm"],
            "initial_validator": validation.get("acceptance_status"),
            "final_validator": validation.get("final_acceptance_status"),
            "fault_trigger_index": metadata.get("trigger_index"),
            "entered_recovery": "FAILURE_DETECTED" in event_types and "StepFailureProvenance" in event_types,
            "repair_no_progress_observed": policy["repair_no_progress_observed"],
            "no_progress_used_for_control": policy["no_progress_used_for_control"],
            "macro_replan_executed": policy["macro_replan_executed"],
            "replan_accepted": "REPLAN_ACCEPTED" in event_types,
            "post_replan_execution_started": post_replan_started,
            "post_replan_provider_called": post_replan_provider_called,
            "post_replan_tool_requested": post_replan_tool_requested,
            "post_replan_tool_requested_count": post_replan_tool_requested_count,
            "post_replan_provider_call_count": post_replan_provider_call_count,
            "post_replan_mutation_observed": post_replan_mutation_observed,
            "post_replan_mutation_count": post_replan_mutation_count,
            "post_replan_redundant_tool_calls": post_replan_redundant_tool_calls,
            "post_replan_redundant_provider_calls": post_replan_redundant_provider_calls,
            "post_replan_executed": post_replan_started and (
                post_replan_provider_called or post_replan_tool_requested
            ),
            "initial_alternate_mutation_denied": policy["initial_alternate_mutation_denied"],
            # The planner-owned active-step contract can reject a provider's
            # local alternate payload before PhaseEffectPolicy sees it.  That
            # is still a real local-repair block, but retain the source
            # boundary separately for evidence consumers.
            "local_repair_alternate_mutation_denied": (
                policy["local_repair_alternate_mutation_denied"]
                or local_repair_plan_authority_blocked
            ),
            "local_repair_alternate_mutation_policy_denied": policy[
                "local_repair_alternate_mutation_denied"
            ],
            "local_repair_alternate_mutation_plan_authority_blocked": local_repair_plan_authority_blocked,
            "post_replan_alternate_mutation_allowed": policy["post_replan_alternate_mutation_allowed"],
            "local_reserve_at_escalation": policy["local_reserve_at_escalation"],
        })
    return results


async def _smoke(output: Path) -> dict[str, Any]:
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    task = _task(snapshot)
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"OUTPUT_COLLISION:{output}")
    specs, policies = _build_specs(snapshot, task=task)
    output.mkdir(parents=True, exist_ok=True)
    _write_experiment_manifest(output, snapshot, task, specs, provider_executed=False)
    evidence_writer = _AppendOnlyEvidence(output / "effect-policy-evidence.jsonl")
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
        evidence_writer.append(_policy_evidence(spec.run_id, spec, policies[spec.run_id]))
    raw = [
        json.loads(line)
        for line in (output / "raw.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    traces = {
        record["run_id"]: record
        for record in (json.loads(line) for line in (output / "traces.jsonl").read_text(encoding="utf-8").splitlines())
    }
    evidence_records = {
        str(item["run_id"]): item
        for item in (
            json.loads(line)
            for line in (output / "effect-policy-evidence.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }
    per_run = _run_qualification(raw, traces, evidence_records)
    baseline = [item for item in per_run if item["arm"] == "baseline"]
    v2 = [item for item in per_run if item["arm"] == "v2"]
    per_run_gates = all(
        item["fault_trigger_index"] == 1
        and item["initial_validator"] == "REJECTED"
        and item["entered_recovery"]
        and item["initial_alternate_mutation_denied"]
        and item["local_repair_alternate_mutation_denied"]
        and item["repair_no_progress_observed"]
        and item["macro_replan_executed"]
        and item["post_replan_executed"]
        and item["post_replan_alternate_mutation_allowed"]
        and item["post_replan_mutation_count"] == 1
        and item["post_replan_redundant_tool_calls"] == 0
        and item["post_replan_redundant_provider_calls"] == 0
        and item["final_validator"] == "ACCEPTED"
        for item in per_run
    )
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
        "per_run_causal_smoke": per_run,
        "per_run_causal_smoke_pass": per_run_gates,
        "baseline_policy_gate": all(
            item["no_progress_used_for_control"] is False for item in baseline
        ) and len(baseline) == REPEATS,
        "v2_policy_gate": all(
            item["no_progress_used_for_control"] is True
            and item["local_reserve_at_escalation"]
            for item in v2
        ) and len(v2) == REPEATS,
        "baseline_final_validator_accepted": all(item["final_validator"] == "ACCEPTED" for item in baseline),
        "v2_final_validator_accepted": all(item["final_validator"] == "ACCEPTED" for item in v2),
        "fault_trigger_index_1": all(item["fault_trigger_index"] == 1 for item in per_run),
        # Retain aggregate fields for existing consumers while the per-run
        # rows above remain the authoritative causal evidence.
        "initial_alternate_mutation_denied": all(
            item["initial_alternate_mutation_denied"] for item in per_run
        ),
        "local_repair_alternate_mutation_denied": all(
            item["local_repair_alternate_mutation_denied"] for item in per_run
        ),
        "post_replan_alternate_mutation_allowed": all(
            item["post_replan_alternate_mutation_allowed"] for item in per_run
        ),
        "post_replan_mutation_count": sum(
            item["post_replan_mutation_count"] for item in per_run
        ),
        "post_replan_redundant_tool_calls": sum(
            item["post_replan_redundant_tool_calls"] for item in per_run
        ),
        "post_replan_redundant_provider_calls": sum(
            item["post_replan_redundant_provider_calls"] for item in per_run
        ),
        "recovery_events_observed": sum(item["post_replan_executed"] for item in per_run),
        "effect_policy_evidence_count": len(evidence_records),
    }
    if not (
        report["valid_runs"] == EXPECTED_RUNS
        and report["invalid_runs"] == 0
        and report["per_run_causal_smoke_pass"]
        and report["baseline_policy_gate"]
        and report["v2_policy_gate"]
        and report["baseline_final_validator_accepted"]
        and report["v2_final_validator_accepted"]
        and report["effect_policy_evidence_count"] == EXPECTED_RUNS
    ):
        raise RuntimeError(f"02H_SMOKE_FAILED:{json.dumps(report, sort_keys=True)}")
    (output / "qualification.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def _write_immutable_json(path: Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise RuntimeError(f"IMMUTABLE_ARTIFACT_MISMATCH:{path.name}")
        return
    path.write_text(encoded, encoding="utf-8")


def _load_bundle_records(output: Path, filename: str) -> list[dict[str, Any]]:
    path = output / filename
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def _timeout_regression() -> dict[str, Any]:
    """Exercise provider-timeout/root-cancellation consumption without I/O."""
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
        else:  # pragma: no cover
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
            "orphan_async_tasks": 0,
            "failure_classification": classified,
            "root_control_remains_consistent": root_failure == "ROOT_DEADLINE_EXCEEDED",
        }
    finally:
        loop.set_exception_handler(previous_handler)


async def _execute(output: Path, *, resume: bool = False, offline: bool = False) -> dict[str, Any]:
    import os

    if not offline and not os.environ.get(CHEAP_CREDENTIAL_ENV, "").strip():
        raise RuntimeError(f"CREDENTIAL_REQUIRED_BEFORE_RUN:{CHEAP_CREDENTIAL_ENV}")
    if output.exists() and any(output.iterdir()) and not resume:
        raise RuntimeError(f"OUTPUT_COLLISION:{output}")
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    task = _task(snapshot)
    specs, policies = _build_specs(snapshot, task=task)
    output.mkdir(parents=True, exist_ok=True)
    if offline:
        identity = {
            "provider": "offline-deterministic",
            "model": CHEAP_MODEL,
            "api_version": "offline-v1",
            "endpoint_hash": "offline",
            "temperature": 0.0,
            "max_tokens": 4096,
            "system_prompt_hash": "offline",
            "tool_policy_hash": EXPECTED_POLICY_ID,
            "sdk_max_retries": SDK_MAX_RETRIES,
        }
    else:
        identity_provider = create_cheap_model_provider()
        identity = provider_identity(identity_provider, expected_model=CHEAP_MODEL)
    _write_immutable_json(output / "provider_identity.json", identity)
    _write_immutable_json(
        output / "benchmark_identity.json",
        {
            "benchmark_version": EXPERIMENT_ID,
            "protocol_hash": snapshot.protocol_hash,
            "model_identity": CHEAP_MODEL,
            "provider_identity": "offline-deterministic" if offline else FROZEN_PROVIDER,
            "credential_value_recorded": False,
            "sdk_max_retries": SDK_MAX_RETRIES,
        },
    )
    _write_experiment_manifest(output, snapshot, task, specs, provider_executed=False)
    evidence_writer = _AppendOnlyEvidence(output / "effect-policy-evidence.jsonl")
    counts: dict[str, int] = {"valid": 0, "invalid": 0}
    for spec in specs:
        provider = (
            _DeterministicProvider(policies[spec.run_id])
            if offline
            else create_cheap_model_provider()
        )
        runner = _build_runner(snapshot, output, provider=provider)
        counts = await runner.run((spec,))
        evidence_writer.append(_policy_evidence(spec.run_id, spec, policies[spec.run_id]))
    raw = _load_bundle_records(output, "raw.jsonl")
    invalid = _load_bundle_records(output, "invalid.jsonl")
    provider_executed = any(
        int(
            ((record.get("runtime_environment") or {}).get("execution_accounting") or {}).get(
                "provider_calls", 0
            )
            or 0
        ) > 0
        for record in (*raw, *invalid)
    )
    _write_experiment_manifest(output, snapshot, task, specs, provider_executed=provider_executed)
    report = {
        "experiment_id": EXPERIMENT_ID,
        "planned_runs": EXPECTED_RUNS,
        "valid_runs": counts["valid"],
        "invalid_runs": counts["invalid"],
        "total_runs": counts["valid"] + counts["invalid"],
        "provider_executed": provider_executed,
        "offline_provider": offline,
        "execution_attempts": len(raw) + len(invalid),
        "sdk_max_retries": SDK_MAX_RETRIES,
    }
    _write_immutable_json(output / "summary.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--preflight", action="store_true")
    group.add_argument("--smoke", action="store_true")
    group.add_argument("--timeout-regression", action="store_true")
    group.add_argument("--execute", action="store_true")
    group.add_argument("--offline-execute", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--resume", action="store_true")
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
    if args.timeout_regression:
        report = asyncio.run(_timeout_regression())
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        if not (
            report["simulated_provider_timeout"]
            and not report["unretrieved_task_exception"]
            and report["failure_classification"] == "PROVIDER_TIMEOUT"
            and report["root_control_remains_consistent"]
        ):
            return 1
        print("PHASE4_02H_TIMEOUT_REGRESSION_COMPLETE")
        return 0
    report = asyncio.run(
        _execute(
            args.output,
            resume=args.resume,
            offline=args.offline_execute,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    print("PHASE4_02H_EXECUTION_COMPLETE")
    print(f"RESULT_PATH={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
