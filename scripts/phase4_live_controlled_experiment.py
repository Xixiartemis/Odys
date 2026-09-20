"""Prepare and execute the Phase 4 Recovery Control Plane V2 live experiment.

This is experiment infrastructure, not a benchmark/runtime implementation.  It
uses the frozen Phase 4 snapshot and the existing ``Phase4Runner`` /
``P45BenchmarkExecutor`` path.  The ``--preflight`` mode is deliberately
provider-free and is suitable for local verification before a human runs the
live command with credentials.

The experiment has three isolated arms:

* CLEAN_CONTROL: odys_p3, no fault mutation;
* BASELINE_FAULT: minimal, frozen CWR-06 / FAIL_TOOL_ON_CALL_1;
* V2_FAULT: odys_p3, the same frozen CWR-06 / FAIL_TOOL_ON_CALL_1.

The clean arm is implemented as an execution-harness adapter: it preserves the
frozen task and fixture definitions while replacing only the fault application
with an auditable no-op.  No benchmark input file is changed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Running a script from ``scripts/`` does not put the repository root or its
# ``src/`` package directory on sys.path. Keep the execution command portable
# across a clean checkout and do not rely on an installed editable package.
_SCRIPT_REPO_ROOT = Path(__file__).resolve().parents[1]
for _import_root in (_SCRIPT_REPO_ROOT, _SCRIPT_REPO_ROOT / "src"):
    if str(_import_root) not in sys.path:
        sys.path.insert(0, str(_import_root))

from evals.reliability.fixture_packages.registry import FixtureRegistry
from evals.reliability.p45_executor import P45BenchmarkExecutor
from evals.reliability.p46_provider import (
    CHEAP_CREDENTIAL_ENV,
    CHEAP_MODEL,
    CHEAP_BASE_URL_ENV,
    FROZEN_ENDPOINT,
    FROZEN_MAX_TOKENS,
    FROZEN_PROVIDER,
    FROZEN_SEED,
    FROZEN_TEMPERATURE,
    RealLLMProvider,
    provider_identity,
)
from evals.reliability.run_phase4 import (
    FaultContext,
    FaultPlan,
    FixtureManager,
    Phase4Runner,
    ProtocolSnapshot,
    RunSpec,
    ConfigLoader,
)


EXPERIMENT_ID = "phase4-live-controlled-experiment-01"
PROTOCOL_ROOT = Path("evals/reliability/phase4_v1")
DEFAULT_OUTPUT = Path("results/phase4_live_controlled_experiment_01")
TASK_ID = "CWR-06"
FAULT_ID = "FAIL_TOOL_ON_CALL_1"
REPEATS = 3
CONFIGS = ("minimal", "odys_p3")
EXPECTED_FACTORY_NAMES = (
    "RealLLMMinimalRuntimeFactory",
    "RealLLMOdysRuntimeFactory",
)
REPAIR_PHASES = frozenset({"repair", "recovery"})
MUTATION_CAPABILITIES = frozenset(
    {"workspace.edit", "workspace.edit_lines", "workspace.restore"}
)
EFFECTIVE_CONFIG_ALLOWED_DIFFS = frozenset(
    {"config", "harness_variant", "runtime_source"}
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _tree_digest(root: Path) -> str:
    """Hash a workspace tree by relative path and file bytes."""
    entries: list[dict[str, str]] = []
    if root.exists():
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            entries.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": _sha256_bytes(path.read_bytes()),
                }
            )
    return _sha256_bytes(_canonical(entries).encode("utf-8"))


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_status(repo_root: Path) -> list[str]:
    return [line for line in _git(repo_root, "status", "--porcelain=v1").splitlines() if line]


def _find_task(snapshot: ProtocolSnapshot, task_id: str) -> dict[str, Any]:
    for task in snapshot.tasks:
        if task.get("task_id") == task_id:
            return dict(task)
    raise RuntimeError(f"FROZEN_TASK_NOT_FOUND:{task_id}")


def _frozen_initial_workspace_digest(task_id: str) -> str:
    registry = FixtureRegistry()
    fixture = registry.get(task_id)
    with tempfile.TemporaryDirectory(prefix="phase4-live-initial-") as name:
        workspace = Path(name)
        fixture.setup(workspace)
        digest = _tree_digest(workspace)
        fixture.reset(workspace)
        return digest


def _credential_present() -> bool:
    # This function is called only by a human-side execution.  It returns a
    # boolean and never exposes the credential value.
    value = os.environ.get(CHEAP_CREDENTIAL_ENV)
    return bool(value and value.strip())


class _NoFaultInjected(RuntimeError):
    """Internal marker used to make the clean arm's skipped injection explicit."""


class _FixtureFacade:
    """Delegate fixture behavior while optionally suppressing fault mutation."""

    def __init__(self, delegate: Any, audit: dict[str, dict[str, Any]], *, no_fault: bool):
        self._delegate = delegate
        self._audit = audit
        self._no_fault = no_fault

    def setup(self, workspace: Path) -> Any:
        result = self._delegate.setup(workspace)
        self._audit[str(workspace)] = {
            "workspace": str(workspace),
            "initial_tree_hash": _tree_digest(workspace),
            "fault_application": "NONE" if self._no_fault else FAULT_ID,
        }
        return result

    def inject_fault(self, workspace: Path, fault_id: str) -> None:
        audit = self._audit.setdefault(str(workspace), {"workspace": str(workspace)})
        if self._no_fault:
            audit["fault_before_tree_hash"] = _tree_digest(workspace)
            audit["fault_after_tree_hash"] = audit["fault_before_tree_hash"]
            audit["fault_application"] = "NONE"
            # P45 records this as FAULT_INJECTION_FAILED and continues.  That
            # trace is the truthful representation of a clean arm: no fault was
            # injected and no fixture mutation occurred.
            raise _NoFaultInjected("CLEAN_CONTROL_NO_FAULT")
        before = _tree_digest(workspace)
        self._delegate.inject_fault(workspace, fault_id)
        audit["fault_before_tree_hash"] = before
        audit["fault_after_tree_hash"] = _tree_digest(workspace)
        audit["fault_application"] = fault_id

    def observe(self, workspace: Path) -> Any:
        result = self._delegate.observe(workspace)
        audit = self._audit.setdefault(str(workspace), {"workspace": str(workspace)})
        audit["observed_tree_hash"] = _tree_digest(workspace)
        return result

    def reset(self, workspace: Path) -> None:
        return self._delegate.reset(workspace)


class _RecordingFixtureRegistry:
    def __init__(self, *, no_fault: bool):
        self._base = FixtureRegistry()
        self._audit: dict[str, dict[str, Any]] = {}
        self._no_fault = no_fault

    def get(self, task_id: str) -> _FixtureFacade:
        return _FixtureFacade(
            self._base.get(task_id),
            self._audit,
            no_fault=self._no_fault,
        )

    @property
    def audit(self) -> list[dict[str, Any]]:
        return [dict(value) for _, value in sorted(self._audit.items())]


class _NoFaultInjector:
    """Runner-local fault plan for CLEAN_CONTROL; no frozen input is changed."""

    def plan_for(self, task: Mapping[str, Any]) -> FaultPlan:
        del task
        definition = {
            "fault_id": "NONE",
            "fault_type": "none",
            "trigger": "never",
            "trigger_count": 0,
            "deterministic_seed": 0,
            "observable_effect": "no fault mutation",
            "expected_runtime_visibility": "no injected fault",
        }
        return FaultPlan(
            fault_id="NONE",
            fault_type="none",
            trigger="never",
            trigger_count=0,
            deterministic_seed=0,
            definition=definition,
        )

    def context_for(self, task: Mapping[str, Any]) -> FaultContext:
        return FaultContext(self.plan_for(task))


@dataclass(frozen=True)
class Arm:
    name: str
    harness_variant: str
    config_name: str
    fault_id: str | None


ARMS = (
    Arm("clean_control", "RECOVERY_CONTROL_PLANE_V2", "odys_p3", None),
    Arm("baseline_fault", "BASELINE", "minimal", FAULT_ID),
    Arm("v2_fault", "RECOVERY_CONTROL_PLANE_V2", "odys_p3", FAULT_ID),
)


def _validate_selection(snapshot: ProtocolSnapshot, task_id: str, fault_id: str) -> dict[str, Any]:
    task = _find_task(snapshot, task_id)
    if task.get("fault_injection") != fault_id:
        raise RuntimeError(
            f"FROZEN_TASK_FAULT_MISMATCH:{task_id}:{task.get('fault_injection')}!={fault_id}"
        )
    if task.get("family") != "COMPLEX_WORKFLOW_REPLAN":
        raise RuntimeError(f"UNEXPECTED_TASK_FAMILY:{task_id}")
    for config_name in CONFIGS:
        ConfigLoader(snapshot).load(config_name)
    fault = snapshot.fault_by_id.get(fault_id)
    if fault is None:
        raise RuntimeError(f"FROZEN_FAULT_NOT_FOUND:{fault_id}")
    return {"task": task, "fault": dict(fault)}


def _preflight(repo_root: Path, task_id: str, fault_id: str, output: Path) -> dict[str, Any]:
    snapshot = ProtocolSnapshot.load(repo_root / PROTOCOL_ROOT)
    selection = _validate_selection(snapshot, task_id, fault_id)
    source = inspect.getsource(P45BenchmarkExecutor.execute)
    factory_path_ok = all(name in source for name in EXPECTED_FACTORY_NAMES)
    if "P410IntegrationExecutor" in source:
        raise RuntimeError("FORBIDDEN_P410_EXECUTOR_REFERENCE")
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"OUTPUT_COLLISION:{output}")
    initial_hash = _frozen_initial_workspace_digest(task_id)
    return {
        "protocol_hash": snapshot.protocol_hash,
        "manifest_hash": snapshot.manifest_hash,
        "fault_set_hash": snapshot.fault_set_hash,
        "validator_hash": snapshot.validator_hash,
        "fixture_set_hash": snapshot.fixture_set_hash,
        "budget_identity": snapshot.budget_identity,
        "task_id": task_id,
        "fault_id": fault_id,
        "task_family": selection["task"]["family"],
        "fixture_id": selection["task"]["fixture_id"],
        "initial_workspace_hash": initial_hash,
        "configs": list(CONFIGS),
        "repeats": REPEATS,
        "expected_runs": len(ARMS) * REPEATS,
        "official_execution_path": factory_path_ok,
        "provider_executed": False,
        "result_created": False,
    }


def _build_manifest(
    repo_root: Path,
    snapshot: ProtocolSnapshot,
    task: dict[str, Any],
    fault: dict[str, Any],
    initial_workspace_hash: str,
    *,
    task_id: str,
    fault_id: str,
) -> dict[str, Any]:
    return {
        "experiment_id": EXPERIMENT_ID,
        "created_at": _utc_now(),
        "repo_sha": _git(repo_root, "rev-parse", "HEAD"),
        "repo_root": str(repo_root),
        "tracked_worktree_status": _git_status(repo_root),
        "protocol_root": str((repo_root / PROTOCOL_ROOT).resolve()),
        "base_protocol_version": snapshot.protocol["benchmark_version"],
        "protocol_hash": snapshot.protocol_hash,
        "manifest_hash": snapshot.manifest_hash,
        "fault_set_hash": snapshot.fault_set_hash,
        "validator_hash": snapshot.validator_hash,
        "fixture_set_hash": snapshot.fixture_set_hash,
        "budget_identity": snapshot.budget_identity,
        "task_prompt": {
            "source": "frozen phase4_v1 manifest",
            "task_id": task_id,
            "title": task["title"],
            "objective": task["objective"],
            "acceptance_criteria": list(task["acceptance_criteria"]),
            "required_capabilities": list(task["required_capabilities"]),
        },
        "task_id": task_id,
        "fixture_id": task["fixture_id"],
        "fixture_version": task["fixture_version"],
        "initial_workspace_tree_hash": initial_workspace_hash,
        "fault_artifact": {
            "fault_id": fault_id,
            "definition": fault,
            "same_for": ["baseline_fault", "v2_fault"],
        },
        "model_identity": CHEAP_MODEL,
        "provider_identity": FROZEN_PROVIDER,
        "endpoint": FROZEN_ENDPOINT,
        "model_parameters": {
            "temperature": FROZEN_TEMPERATURE,
            "max_tokens": FROZEN_MAX_TOKENS,
            "seed": FROZEN_SEED,
        },
        "credential_env": CHEAP_CREDENTIAL_ENV,
        "credential_value_recorded": False,
        "provider_retry_count": 0,
        "sdk_retry_policy": "AsyncOpenAI(max_retries=0)",
        "context_limit": "NOT_MEASURED",
        "timeout_seconds": snapshot.protocol["budgets"]["timeout_seconds"],
        "api_call_budget": snapshot.protocol["budgets"]["max_model_calls"],
        "repair_budget": 1,
        "wall_clock_budget_seconds": snapshot.protocol["budgets"]["timeout_seconds"],
        "tool_capabilities": list(snapshot.protocol["fairness"]["tool_capability_set"]),
        "validator": {
            "id": snapshot.protocol["shared_validator_id"],
            "hash": snapshot.validator_hash,
            "authority": "external validator only",
        },
        "arms": [
            {
                "name": arm.name,
                "harness_variant": arm.harness_variant,
                "config": arm.config_name,
                "fault_id": arm.fault_id,
                "fault_mode": "NONE" if arm.fault_id is None else "FROZEN_FAULT",
            }
            for arm in ARMS
        ],
        "repeat_count": REPEATS,
        "allowed_primary_variable": "harness_variant",
        "notes": [
            "CLEAN_CONTROL uses an execution-local no-op fault adapter; frozen task and fixture files are unchanged.",
            "BASELINE_FAULT and V2_FAULT share the exact frozen task, fault, workspace seed, provider, model, budget, and validator.",
            "No metric, acceptance rule, protocol, manifest, fault, fixture, or validator definition is rewritten.",
        ],
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_manifest(output: Path, manifest: dict[str, Any]) -> str:
    payload = _canonical(manifest).encode("utf-8")
    digest = _sha256_bytes(payload)
    _write_json(output / "experiment_manifest.json", manifest)
    (output / "experiment_manifest.sha256").write_text(digest + "\n", encoding="utf-8")
    return digest


def _build_provider() -> RealLLMProvider:
    """Build the same P46 provider adapter with SDK retries explicitly off."""
    key = os.environ.get(CHEAP_CREDENTIAL_ENV)
    if not key or not key.strip():
        raise RuntimeError(f"CREDENTIAL_REQUIRED_BEFORE_RUN:{CHEAP_CREDENTIAL_ENV}")
    base_url = os.environ.get(CHEAP_BASE_URL_ENV, FROZEN_ENDPOINT)
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key=key,
        base_url=base_url,
        max_retries=0,
    )
    return RealLLMProvider(
        model=CHEAP_MODEL,
        api_key=key,
        base_url=base_url,
        temperature=FROZEN_TEMPERATURE,
        max_tokens=FROZEN_MAX_TOKENS,
        seed=FROZEN_SEED,
        provider_id=FROZEN_PROVIDER,
        credential_route_id=CHEAP_CREDENTIAL_ENV,
        client=client,
        expected_model=CHEAP_MODEL,
    )


def _write_arm_identity(
    arm_dir: Path,
    snapshot: ProtocolSnapshot,
    manifest_digest: str,
    arm: Arm,
    provider: RealLLMProvider,
) -> None:
    identity = provider_identity(provider, expected_model=CHEAP_MODEL)
    _write_json(arm_dir / "provider_identity.json", identity)
    _write_json(
        arm_dir / "benchmark_identity.json",
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
            "harness_variant": arm.harness_variant,
            "config": arm.config_name,
            "fault_id": arm.fault_id or "NONE",
            "experiment_manifest_digest": manifest_digest,
        },
    )


def _provider_records(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    env = record.get("runtime_environment")
    accounting = env.get("execution_accounting") if isinstance(env, Mapping) else None
    records = accounting.get("provider_call_records") if isinstance(accounting, Mapping) else None
    return [dict(item) for item in records if isinstance(item, Mapping)] if isinstance(records, list) else []


def _known_sum(records: list[Mapping[str, Any]], key: str) -> int | str:
    values = [item.get(key) for item in records if item.get("provider_call", True) is not False]
    if not values or any(not isinstance(value, int) for value in values):
        return "NOT_MEASURED"
    return sum(values)


def _trace_events(trace_record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = trace_record.get("execution_trace")
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _trace_repair_provider_calls(events: list[Mapping[str, Any]]) -> int:
    """Count provider responses inside durable repair intervals.

    This is a fallback/consistency check for provider telemetry.  It never
    infers a repair from a final success alone: a repair interval must be
    explicitly opened by ``REPAIR_STARTED``.
    """
    active = False
    count = 0
    for event in events:
        event_type = event.get("event_type")
        if event_type == "REPAIR_STARTED":
            active = True
        elif event_type == "REPAIR_COMPLETED":
            active = False
        elif active and event_type == "PROVIDER_RESPONSE_SUCCESS":
            count += 1
    return count


def _trace_event_count(events: list[Mapping[str, Any]], event_type: str) -> int:
    return sum(1 for event in events if event.get("event_type") == event_type)


def _mutation_evidence(
    accounting: Mapping[str, Any], events: list[Mapping[str, Any]]
) -> tuple[int, str, int, int, bool]:
    """Count committed mutation observations without trusting a stale flag.

    ``workspace.edit_lines`` in the live artifact reported
    ``observed_mutation=false`` while its bounded result said
    ``replaced=true``.  The bounded result is stronger evidence for this
    aggregation purpose.  Trace fallback is keyed by invocation id so an
    invocation is never double-counted.
    """
    accounting_ids: set[str] = set()
    for invocation in accounting.get("tool_invocations", []) if isinstance(accounting, Mapping) else []:
        if not isinstance(invocation, Mapping) or invocation.get("capability") not in MUTATION_CAPABILITIES:
            continue
        bounded = invocation.get("bounded_output")
        replaced = isinstance(bounded, Mapping) and bounded.get("replaced") is True
        committed = invocation.get("result_status") == "SUCCESS" and (
            invocation.get("observed_mutation") is True or replaced
        )
        if not committed:
            continue
        identity = str(invocation.get("invocation_id") or f"accounting:{len(accounting_ids)}")
        accounting_ids.add(identity)

    trace_ids: set[str] = set()
    for event in events:
        if event.get("event_type") not in {"TOOL_CALL_OBSERVED", "TOOL_CALL_COMPLETED"}:
            continue
        metadata = event.get("metadata")
        if not isinstance(metadata, Mapping) or metadata.get("capability") not in MUTATION_CAPABILITIES:
            continue
        bounded = metadata.get("bounded_output")
        replaced = isinstance(bounded, Mapping) and bounded.get("replaced") is True
        if metadata.get("observed_mutation") is not True and not replaced:
            continue
        invocation_id = str(metadata.get("invocation_id") or "")
        trace_ids.add(invocation_id or f"trace:{len(trace_ids)}")
    count = len(accounting_ids | trace_ids)
    consistent = accounting_ids == trace_ids
    if count:
        evidence = "OBSERVED_COMMITTED_MUTATION" if consistent else "ACCOUNTING_TRACE_MISMATCH"
    else:
        evidence = "NO_COMMITTED_MUTATION_OBSERVED"
    return count, evidence, len(accounting_ids), len(trace_ids), consistent


def _metric_run(record: Mapping[str, Any], trace_record: Mapping[str, Any] | None) -> dict[str, Any]:
    events = _trace_events(trace_record or {})
    event_types = [str(item.get("event_type")) for item in events]
    records = _provider_records(record)
    env = record.get("runtime_environment")
    accounting = env.get("execution_accounting") if isinstance(env, Mapping) else {}
    recovery = env.get("recovery") if isinstance(env, Mapping) else {}
    validation = env.get("validation") if isinstance(env, Mapping) else {}
    input_tokens = _known_sum(records, "input_tokens")
    output_tokens = _known_sum(records, "output_tokens")
    total_tokens = _known_sum(records, "total_tokens")
    phase_recovery = [
        item
        for item in records
        if item.get("phase") in REPAIR_PHASES and item.get("provider_call", True) is not False
    ]
    trace_repair_calls = _trace_repair_provider_calls(events)
    repair_telemetry_consistent = not events or len(phase_recovery) == trace_repair_calls
    input_sequence = [item.get("input_tokens") for item in records if isinstance(item.get("input_tokens"), int)]
    initial_acceptance = validation.get("acceptance_status") if isinstance(validation, Mapping) else None
    final_acceptance = validation.get("final_acceptance_status") if isinstance(validation, Mapping) else None
    false_completion = bool(
        isinstance(validation, Mapping)
        and validation.get("false_completion_detected")
    )
    tool_invocations = (accounting or {}).get("tool_invocations", []) if isinstance(accounting, Mapping) else []
    mutation_calls, mutation_evidence, mutation_accounting_count, mutation_trace_count, mutation_telemetry_consistent = _mutation_evidence(accounting, events)
    recovery_scope = (recovery or {}).get("repair_scope") if isinstance(recovery, Mapping) else None
    recovery_attempted = bool((recovery or {}).get("recovery_attempted")) or _trace_event_count(events, "REPAIR_STARTED") > 0
    recovery_success = bool((recovery or {}).get("recovery_success")) or _trace_event_count(events, "VERIFICATION_PASSED") > 0
    return {
        "run_id": record.get("benchmark_run_id"),
        "task_id": record.get("task_id"),
        "config": record.get("configuration"),
        "fault_id": record.get("fault_id"),
        "repeat": record.get("repeat_index"),
        "validity": record.get("validity"),
        "final_validator_result": final_acceptance or initial_acceptance or "NOT_MEASURED",
        "final_state": "VERIFIED" if record.get("verified_completion") else "FAILED",
        "model_turns": len([item for item in records if item.get("provider_call", True) is not False]),
        "repair_turns": len(phase_recovery) if repair_telemetry_consistent else "NOT_MEASURED",
        "repair_provider_calls": len(phase_recovery),
        "repair_trace_provider_calls": trace_repair_calls,
        "repair_telemetry_consistent": repair_telemetry_consistent,
        "local_repair_calls": len(phase_recovery) if recovery_scope == "LOCAL" and repair_telemetry_consistent else ("NOT_MEASURED" if recovery_scope == "LOCAL" else 0),
        "macro_replans": int(record.get("replan_count") or 0),
        "post_replan_executions": len([event for event in event_types if "POST_REPLAN" in event]),
        "validation_calls": event_types.count("VALIDATION_RESULT"),
        "provider_calls": int((accounting or {}).get("provider_calls", record.get("model_calls", 0)) or 0),
        "provider_retries": 0,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "max_context_tokens": max(input_sequence) if input_sequence else "NOT_MEASURED",
        "context_input_tokens": input_sequence,
        "context_growth": "MEASURED_SEQUENCE" if input_sequence else "NOT_MEASURED",
        "tool_calls": int(record.get("tool_calls") or 0),
        "mutation_calls": mutation_calls,
        "mutation_evidence": mutation_evidence,
        "mutation_accounting_count": mutation_accounting_count,
        "mutation_trace_count": mutation_trace_count,
        "mutation_telemetry_consistent": mutation_telemetry_consistent,
        "duplicate_mutations": int(record.get("duplicate_side_effect_count") or 0),
        "wall_clock_ms": round(float(record.get("wall_time_seconds")) * 1000, 3) if isinstance(record.get("wall_time_seconds"), (int, float)) else "NOT_MEASURED",
        "false_completion_attempts": 1 if false_completion else 0,
        "recovery_eligible": bool((recovery or {}).get("recovery_required")),
        "recovery_attempted": recovery_attempted,
        "recovery_success": recovery_success,
        "recovery_start_event": "FAILURE_DETECTED" if "FAILURE_DETECTED" in event_types else None,
        "recovery_end_event": "VERIFICATION_PASSED" if "VERIFICATION_PASSED" in event_types else ("VERIFICATION_FAILED" if "VERIFICATION_FAILED" in event_types else None),
        "recovery_token_cost": _known_sum(phase_recovery, "total_tokens"),
        "provider_call_records": records,
    }


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"EXPECTED_JSON_OBJECT:{path}")
    return value


def _file_sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _arm_metrics(arm_dir: Path) -> dict[str, Any]:
    raw = _load_jsonl(arm_dir / "raw.jsonl")
    invalid = _load_jsonl(arm_dir / "invalid.jsonl")
    traces = {str(item.get("run_id")): item for item in _load_jsonl(arm_dir / "traces.jsonl")}
    runs = [_metric_run(record, traces.get(str(record.get("benchmark_run_id")))) for record in raw]
    return {
        "arm": arm_dir.name,
        "raw_runs": len(raw),
        "invalid_runs": len(invalid),
        "runs": runs,
        "workspace_audit": _load_jsonl(arm_dir / "workspace-audit.jsonl"),
        "identity": _load_json(arm_dir / "benchmark_identity.json") if (arm_dir / "benchmark_identity.json").exists() else {},
        "runtime_sources": sorted(
            {
                str((item.get("runtime_environment") or {}).get("runtime_source"))
                for item in raw
                if isinstance(item.get("runtime_environment"), Mapping)
                and (item.get("runtime_environment") or {}).get("runtime_source")
            }
        ),
        "trace_event_counts": {
            event_type: sum(
                _trace_event_count(_trace_events(trace), event_type)
                for trace in traces.values()
            )
            for event_type in sorted(
                {
                    str(event.get("event_type"))
                    for trace in traces.values()
                    for event in _trace_events(trace)
                    if event.get("event_type")
                }
            )
        },
    }


def _aggregate_arm(arm: Mapping[str, Any]) -> dict[str, Any]:
    runs = list(arm.get("runs", []))
    valid = [run for run in runs if run.get("validity") != "INVALID_RUN"]

    def sum_field(field: str) -> int | str:
        values = [item.get(field) for item in runs]
        if not values or any(not isinstance(value, int) for value in values):
            return "NOT_MEASURED"
        return sum(values)
    fault_ids = sorted({str(run.get("fault_id")) for run in runs if run.get("fault_id") is not None})
    trace_counts = dict(arm.get("trace_event_counts", {}))
    if fault_ids == ["NONE"]:
        fault_injection_classification = "SKIPPED"
    elif trace_counts.get("FAULT_TRIGGERED", 0) >= len(runs) and runs:
        fault_injection_classification = "TRIGGERED"
    elif trace_counts.get("FAULT_INJECTED", 0) >= len(runs) and runs:
        fault_injection_classification = "ARMED_ONLY"
    else:
        fault_injection_classification = "NOT_MEASURED"
    fault_triggered = (
        "YES"
        if trace_counts.get("FAULT_TRIGGERED", 0) >= len(runs) and runs
        else "NOT_AVAILABLE_FOR_OLD_RUN"
        if trace_counts.get("FAULT_TRIGGERED", 0) == 0
        else "PARTIAL"
    )
    return {
        "arm": arm.get("arm"),
        "runs": len(runs),
        "invalid_runs": int(arm.get("invalid_runs", 0)),
        "final_validator_results": {str(item): sum(1 for run in runs if run.get("final_validator_result") == item) for item in sorted({run.get("final_validator_result") for run in runs})},
        "model_turns": sum_field("model_turns"),
        "repair_turns": sum_field("repair_turns"),
        "local_repair_calls": sum_field("local_repair_calls"),
        "macro_replans": sum_field("macro_replans"),
        "post_replan_executions": sum_field("post_replan_executions"),
        "validation_calls": sum_field("validation_calls"),
        "provider_calls": sum_field("provider_calls"),
        "provider_retries": sum_field("provider_retries"),
        "input_tokens": sum_field("input_tokens"),
        "output_tokens": sum_field("output_tokens"),
        "total_tokens": sum_field("total_tokens"),
        "max_context_tokens": max((run.get("max_context_tokens") for run in runs if isinstance(run.get("max_context_tokens"), int)), default="NOT_MEASURED"),
        "context_growth": "MEASURED_SEQUENCE" if any(run.get("context_growth") == "MEASURED_SEQUENCE" for run in runs) else "NOT_MEASURED",
        "tool_calls": sum_field("tool_calls"),
        "mutation_calls": sum_field("mutation_calls"),
        "duplicate_mutations": sum_field("duplicate_mutations"),
        "mutation_evidence": sorted({str(run.get("mutation_evidence")) for run in runs}),
        "mutation_aggregation": "TRACE_CONSISTENT" if all(run.get("mutation_telemetry_consistent") for run in runs) else "NOT_MEASURED",
        "repair_aggregation": "TRACE_CONSISTENT" if all(run.get("repair_telemetry_consistent") for run in runs) else "NOT_MEASURED",
        "local_repair_aggregation": "TRACE_CONSISTENT" if all(
            run.get("repair_telemetry_consistent")
            for run in runs
            if run.get("local_repair_calls") != 0
        ) else "NOT_MEASURED",
        "duplicate_mutation_evidence": "NOT_PROVEN",
        "wall_clock_ms": sum_field("wall_clock_ms"),
        "false_completion_attempts": sum_field("false_completion_attempts"),
        "recovery_eligible": sum(1 for run in runs if run.get("recovery_eligible")),
        "recovery_attempted": sum(1 for run in runs if run.get("recovery_attempted")),
        "recovery_success": sum(1 for run in runs if run.get("recovery_success")),
        "verified_completion_rate": round(sum(run.get("final_state") == "VERIFIED" for run in valid) / len(valid), 6) if valid else "NOT_MEASURED",
        "false_completion_attempt_rate": round(sum(bool(run.get("false_completion_attempts")) for run in valid) / len(valid), 6) if valid else "NOT_MEASURED",
        "duplicate_mutation_rate": "NOT_PROVEN",
        "recovery_success_rate": (
            round(sum(1 for run in runs if run.get("recovery_success")) / sum(1 for run in runs if run.get("recovery_attempted")), 6)
            if sum(1 for run in runs if run.get("recovery_attempted")) else "NOT_MEASURED"
        ),
        "fault_ids": fault_ids,
        "fault_injection_classification": fault_injection_classification,
        "fault_armed_recorded": "YES" if trace_counts.get("FAULT_INJECTED", 0) >= len(runs) and runs else "NO",
        "fault_triggered_recorded": fault_triggered,
        "trace_event_counts": trace_counts,
    }


def _effective_config_diff(arms: list[Mapping[str, Any]]) -> dict[str, Any]:
    by_name = {str(arm.get("arm")): arm for arm in arms}
    baseline = by_name.get("baseline_fault", {})
    v2 = by_name.get("v2_fault", {})
    baseline_identity = baseline.get("identity", {})
    v2_identity = v2.get("identity", {})
    observed: dict[str, tuple[Any, Any]] = {
        key: (baseline_identity.get(key), v2_identity.get(key))
        for key in sorted(set(baseline_identity) | set(v2_identity))
    }
    observed["runtime_source"] = (
        baseline.get("runtime_sources", []),
        v2.get("runtime_sources", []),
    )
    differences = {
        key: {"baseline": left, "v2": right}
        for key, (left, right) in observed.items()
        if left != right
    }
    unallowed = sorted(set(differences) - EFFECTIVE_CONFIG_ALLOWED_DIFFS)
    return {
        "status": "PASS" if not unallowed else "FAIL",
        "allowed_difference_fields": sorted(EFFECTIVE_CONFIG_ALLOWED_DIFFS),
        "differences": differences,
        "unallowed_differences": unallowed,
        "scope": "captured benchmark identity and runtime-source fields only",
    }


def _write_experiment_report(output: Path, manifest: Mapping[str, Any], arms: list[Mapping[str, Any]], digest: str) -> None:
    aggregate = {_arm["arm"]: _aggregate_arm(_arm) for _arm in arms}
    baseline = aggregate["baseline_fault"]
    v2 = aggregate["v2_fault"]
    outcome_comparable = baseline["verified_completion_rate"] == v2["verified_completion_rate"]
    if outcome_comparable:
        paired = {
            "cost_comparison": "COMPARABLE",
            "reason": None,
            "avoided_model_turns": baseline["model_turns"] - v2["model_turns"] if isinstance(baseline["model_turns"], int) and isinstance(v2["model_turns"], int) else "NOT_MEASURED",
            "avoided_repair_turns": baseline["repair_turns"] - v2["repair_turns"] if isinstance(baseline["repair_turns"], int) and isinstance(v2["repair_turns"], int) else "NOT_MEASURED",
            "avoided_provider_calls": baseline["provider_calls"] - v2["provider_calls"] if isinstance(baseline["provider_calls"], int) and isinstance(v2["provider_calls"], int) else "NOT_MEASURED",
            "tokens_saved": baseline["total_tokens"] - v2["total_tokens"] if isinstance(baseline["total_tokens"], int) and isinstance(v2["total_tokens"], int) else "NOT_MEASURED",
            "wall_clock_saved_ms": baseline["wall_clock_ms"] - v2["wall_clock_ms"] if isinstance(baseline["wall_clock_ms"], (int, float)) and isinstance(v2["wall_clock_ms"], (int, float)) else "NOT_MEASURED",
        }
    else:
        paired = {
            "cost_comparison": "NOT_COMPARABLE",
            "reason": "OUTCOME_MISMATCH",
            "avoided_model_turns": "NOT_COMPARABLE",
            "avoided_repair_turns": "NOT_COMPARABLE",
            "avoided_provider_calls": "NOT_COMPARABLE",
            "tokens_saved": "NOT_COMPARABLE",
            "wall_clock_saved_ms": "NOT_COMPARABLE",
        }
    effective_config_diff = _effective_config_diff(arms)
    summary = {
        "experiment_id": EXPERIMENT_ID,
        "manifest_digest": digest,
        "repo_sha": manifest["repo_sha"],
        "real_provider_executed": True,
        "provider_retry_count": 0,
        "arms": aggregate,
        "paired_baseline_fault_vs_v2_fault": paired,
        "experiment_classification": "LOCAL_RECOVERY_SMOKE",
        "fault_none_classification": "SKIPPED",
        "effective_config_diff": effective_config_diff,
        "clean_harness_overhead_percent": "NOT_MEASURED",
        "live_context_token_growth_class": "MEASURED_SEQUENCE" if any(arm.get("context_growth") == "MEASURED_SEQUENCE" for arm in aggregate.values()) else "NOT_MEASURED",
        "context_linear_growth_eliminated": "NOT_CLAIMED",
        "only_validator_can_verify": True,
    }
    _write_json(output / "experiment-summary.json", summary)
    lines = [
        f"# {EXPERIMENT_ID}",
        "",
        "> Generated only after the human-run real-provider experiment. This report contains observed execution evidence; unavailable provider telemetry remains `NOT_MEASURED`.",
        "",
        f"- Repository SHA: `{manifest['repo_sha']}`",
        f"- Manifest digest: `{digest}`",
        f"- Protocol hash: `{manifest['protocol_hash']}`",
        f"- Model/provider: `{manifest['model_identity']}` / `{manifest['provider_identity']}`",
        "- SDK retries: `0` (explicit `AsyncOpenAI(max_retries=0)`).",
        "- Completion authority: external validator only; model claims are not verification.",
        "",
        "## Arms",
        "",
        "| Arm | Config | Runs | Invalid | Verified rate | Provider calls | Total tokens | Recovery attempted | Recovery success |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("clean_control", "baseline_fault", "v2_fault"):
        item = aggregate[name]
        lines.append(
            f"| {name} | {next(arm['config'] for arm in manifest['arms'] if arm['name'] == name)} | {item['runs']} | {item['invalid_runs']} | {item['verified_completion_rate']} | {item['provider_calls']} | {item['total_tokens']} | {item['recovery_attempted']} | {item['recovery_success']} |"
        )
    lines.extend(
        [
            "",
            "## Paired deltas",
            "",
            "The following are computed only when both source measurements are numeric:",
            "",
            f"- Avoided model turns: `{paired['avoided_model_turns']}`",
            f"- Avoided repair turns: `{paired['avoided_repair_turns']}`",
            f"- Avoided provider calls: `{paired['avoided_provider_calls']}`",
            f"- Tokens saved: `{paired['tokens_saved']}`",
            f"- Wall-clock saved (ms): `{paired['wall_clock_saved_ms']}`",
            "- Clean harness overhead: `NOT_MEASURED` (no matched baseline-clean arm was run).",
            "- Experiment classification: `LOCAL_RECOVERY_SMOKE` (CWR-06 does not exercise no-progress escalation or macro replan).",
            f"- Cost comparison: `{paired['cost_comparison']}` (reason: `{paired['reason'] or 'NONE'}`).",
            f"- Effective config diff check: `{effective_config_diff['status']}` over captured identity/runtime-source fields; allowed differences: `{', '.join(sorted(EFFECTIVE_CONFIG_ALLOWED_DIFFS))}`.",
            "- CLEAN_CONTROL fault classification: `SKIPPED`; the historical trace may contain the legacy `FAULT_INJECTION_FAILED` marker because no fault was requested.",
            "- Historical runs do not contain `FAULT_TRIGGERED`; this report records `NOT_AVAILABLE_FOR_OLD_RUN` and does not infer trigger occurrence from later behavior.",
            "- Duplicate mutation rate: `NOT_PROVEN`; zero reported duplicates is not treated as proof of absence.",
            "- Context claim: real input-token sequences are recorded; no linear-growth-eliminated claim is made from this artifact alone.",
            "",
            "## Artifact paths",
            "",
            "- `experiment_manifest.json` and `experiment_manifest.sha256`",
            "- `clean_control/`, `baseline_fault/`, `v2_fault/` each contain raw/invalid/trace/identity artifacts",
            "- `experiment-summary.json`",
        ]
    )
    (output / "PHASE4_LIVE_CONTROLLED_EXPERIMENT_01.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _run_arm(
    repo_root: Path,
    snapshot: ProtocolSnapshot,
    task: dict[str, Any],
    arm: Arm,
    output: Path,
    manifest_digest: str,
) -> None:
    arm_dir = output / arm.name
    arm_dir.mkdir(parents=True, exist_ok=True)
    provider = _build_provider()
    _write_arm_identity(arm_dir, snapshot, manifest_digest, arm, provider)
    registry = _RecordingFixtureRegistry(no_fault=arm.fault_id is None)
    executor = P45BenchmarkExecutor(
        fixture_registry=registry,
        factory_type="real",
        trace_file=arm_dir / "executor-traces.jsonl",
        provider=provider,
        provider_identity=provider_identity(provider, expected_model=CHEAP_MODEL),
        expected_model=CHEAP_MODEL,
    )
    runner = Phase4Runner(
        snapshot,
        output_dir=arm_dir,
        executor=executor,
        fixture_manager=FixtureManager(snapshot),
        model=CHEAP_MODEL,
        provider=FROZEN_PROVIDER,
        benchmark_version=EXPERIMENT_ID,
        benchmark_config_hash=manifest_digest,
        repo_root=repo_root,
        trace_path=arm_dir / "traces.jsonl",
        require_trace=True,
    )
    if arm.fault_id is None:
        runner.injector = _NoFaultInjector()
    config = ConfigLoader(snapshot).load(arm.config_name)
    arm_task = dict(task)
    if arm.fault_id is None:
        # This is an in-memory execution-condition projection. The committed
        # frozen task remains unchanged; the clean arm's declared condition is
        # explicitly fault NONE.
        arm_task["fault_injection"] = "NONE"
    specs = tuple(
        RunSpec(task=arm_task, config=config, repeat_index=repeat)
        for repeat in range(1, REPEATS + 1)
    )
    counts = await runner.run(specs)
    _write_jsonl(arm_dir / "workspace-audit.jsonl", registry.audit)
    _write_json(
        arm_dir / "arm-summary.json",
        {
            "arm": arm.name,
            "harness_variant": arm.harness_variant,
            "config": arm.config_name,
            "fault_id": arm.fault_id or "NONE",
            "planned_runs": REPEATS,
            "valid_runs": counts["valid"],
            "invalid_runs": counts["invalid"],
            "provider_retry_count": 0,
        },
    )
    close = getattr(getattr(provider, "_inner", None), "client", None)
    close_method = getattr(close, "close", None)
    if callable(close_method):
        await close_method()


def _write_jsonl(path: Path, records: list[Mapping[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(dict(item), ensure_ascii=False, sort_keys=True) + "\n" for item in records),
        encoding="utf-8",
    )


def execute(repo_root: Path, output: Path, task_id: str, fault_id: str) -> None:
    if not _credential_present():
        print(f"CHEAP_CREDENTIAL=MISSING ({CHEAP_CREDENTIAL_ENV})")
        raise SystemExit(2)
    print("CHEAP_CREDENTIAL=SET")
    preflight = _preflight(repo_root, task_id, fault_id, output)
    snapshot = ProtocolSnapshot.load(repo_root / PROTOCOL_ROOT)
    selection = _validate_selection(snapshot, task_id, fault_id)
    task = selection["task"]
    fault = selection["fault"]
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"OUTPUT_COLLISION:{output}")
    output.mkdir(parents=True, exist_ok=True)
    manifest = _build_manifest(
        repo_root,
        snapshot,
        task,
        fault,
        preflight["initial_workspace_hash"],
        task_id=task_id,
        fault_id=fault_id,
    )
    manifest_digest = _write_manifest(output, manifest)
    for arm in ARMS:
        asyncio.run(_run_arm(repo_root, snapshot, task, arm, output, manifest_digest))
    arms = [_arm_metrics(output / arm.name) for arm in ARMS]
    _write_experiment_report(output, manifest, arms, manifest_digest)
    status = _git_status(repo_root)
    if status:
        raise RuntimeError(f"WORKTREE_CHANGED_AFTER_EXPERIMENT:{status}")
    print("EXPERIMENT_EXECUTION_COMPLETE")
    print(f"RESULT_PATH={output}")


def reaggregate_existing(repo_root: Path, output: Path) -> None:
    """Regenerate derived evidence from an existing run without provider access.

    Raw/traces/invalid artifacts are inputs and are never rewritten.  This mode
    exists specifically to correct evidence semantics after a live run while
    preserving the original experiment bundle and its recorded repository SHA.
    """
    manifest_path = output / "experiment_manifest.json"
    digest_path = output / "experiment_manifest.sha256"
    if not manifest_path.exists() or not digest_path.exists():
        raise RuntimeError(f"EXISTING_EXPERIMENT_MANIFEST_REQUIRED:{output}")
    manifest = _load_json(manifest_path)
    digest = digest_path.read_text(encoding="utf-8").strip()
    if digest != _sha256_bytes(_canonical(manifest).encode("utf-8")):
        raise RuntimeError("EXPERIMENT_MANIFEST_DIGEST_MISMATCH")
    arms = [_arm_metrics(output / arm.name) for arm in ARMS]
    _write_experiment_report(output, manifest, arms, digest)
    inputs: dict[str, str] = {}
    for arm in ARMS:
        arm_dir = output / arm.name
        for name in (
            "raw.jsonl",
            "traces.jsonl",
            "executor-traces.jsonl",
            "workspace-audit.jsonl",
            "invalid.jsonl",
        ):
            path = arm_dir / name
            if path.exists():
                inputs[str(path.relative_to(output))] = _file_sha256(path)
    _write_json(
        output / "aggregation-audit.json",
        {
            "mode": "OFFLINE_REAGGREGATION",
            "existing_provider_runs_reused": True,
            "provider_executed": False,
            "raw_and_trace_inputs_unchanged_at_reaggregation": True,
            "input_sha256": inputs,
            "experiment_classification": "LOCAL_RECOVERY_SMOKE",
            "production_semantics_changed": False,
        },
    )
    print("OFFLINE_REAGGREGATION_COMPLETE")
    print(f"RESULT_PATH={output}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=EXPERIMENT_ID)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--task-id", default=TASK_ID)
    parser.add_argument("--fault-id", default=FAULT_ID)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--reaggregate", action="store_true")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    repo_root = args.repo_root.resolve()
    output = args.output if args.output.is_absolute() else (repo_root / args.output)
    if args.reaggregate:
        reaggregate_existing(repo_root, output)
        return 0
    result = _preflight(repo_root, args.task_id, args.fault_id, output)
    if args.preflight:
        print("PREFLIGHT=PASS")
        print(f"TASK_ID={result['task_id']}")
        print(f"FAULT_ID={result['fault_id']}")
        print(f"CONFIGS={','.join(result['configs'])}")
        print(f"REPEATS={result['repeats']}")
        print(f"EXPECTED_RUNS={result['expected_runs']}")
        print(f"PROTOCOL_HASH={result['protocol_hash']}")
        print(f"OFFICIAL_EXECUTION_PATH={'YES' if result['official_execution_path'] else 'NO'}")
        print("PROVIDER_EXECUTED_DURING_PREFLIGHT=NO")
        print("RESULT_CREATED_DURING_PREFLIGHT=NO")
        return 0
    execute(repo_root, output, args.task_id, args.fault_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
