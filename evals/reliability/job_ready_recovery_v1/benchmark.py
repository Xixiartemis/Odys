"""Job-ready recovery proof benchmark.

This namespace is deliberately separate from ``phase4_v1``.  It reuses the
official :class:`P45BenchmarkExecutor`, the real runtime factories, and the
shared :class:`ExternalObservableValidator`, while defining one small,
versioned fixture whose acceptance state is observable and repairable.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from evals.reliability.p45_executor import P45BenchmarkExecutor
from evals.reliability.p46_provider import (
    CHEAP_MODEL,
    FROZEN_PROVIDER,
    create_cheap_model_provider,
    provider_identity,
)
from evals.reliability.run_phase4 import (
    ConfigLoader,
    FixtureManager,
    Phase4Runner,
    ProtocolError,
    ProtocolSnapshot,
    RunSpec,
    canonical_json,
    select_runs,
)
from evals.reliability.fixture_packages.base import BaseFixture


ROOT = Path(__file__).resolve().parent
JOB_READY_VERSION = "job-ready-recovery-v1"
TASK_ID = "recovery-proof-01"
FAULT_ID = "JOB_READY_PRE_ACCEPTANCE_FAILURE"
FIXTURE_ID = "fixture-job-ready-recovery-v1"
STATE_FILE = "state.json"
INITIAL_CONTENT = '{"status":"PENDING","version":1}\n'
BROKEN_CONTENT = '{"status":"BROKEN","version":1}\n'
TARGET_CONTENT = '{"status":"READY","version":1}\n'
TARGET_HASH = "bbbbb0009b8c4e5e810508ee2d28d58ef9f778db678dd7de7dcaabc688e7e6ca"
SUPPORTED_CONFIGS = ("minimal", "odys_p3")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _document_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _input_values(root: Path) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for relative in (
        "protocol.json",
        "manifest.json",
        "faults.json",
        "validators.json",
        "fixtures/catalog.json",
        "ablation.json",
        "schemas/result.schema.json",
    ):
        values[relative] = _read_json(root / relative)
    for path in sorted((root / "configs").glob("*.json")):
        values[f"configs/{path.name}"] = _read_json(path)
    return values


def _protocol_hash(root: Path) -> str:
    return _document_hash(_input_values(root))


def job_ready_config_hash(snapshot: ProtocolSnapshot) -> str:
    """Return the deterministic identity of this benchmark configuration."""
    return _document_hash(
        {
            "benchmark_version": JOB_READY_VERSION,
            "configs": {
                name: snapshot.configs[name]
                for name in SUPPORTED_CONFIGS
            },
            "budgets": snapshot.protocol["budgets"],
        }
    )


def validate_job_ready_protocol(root: Path = ROOT) -> dict[str, Any]:
    """Validate the small benchmark's own invariants fail-closed."""
    root = Path(root).resolve()
    protocol = _read_json(root / "protocol.json")
    manifest = _read_json(root / "manifest.json")
    faults = _read_json(root / "faults.json")
    validators = _read_json(root / "validators.json")
    fixtures = _read_json(root / "fixtures" / "catalog.json")
    configs = {
        path.stem: _read_json(path)
        for path in sorted((root / "configs").glob("*.json"))
    }
    tasks = manifest.get("tasks")
    if protocol.get("benchmark_namespace") != JOB_READY_VERSION:
        raise ProtocolError("job-ready benchmark namespace drift")
    if manifest.get("benchmark_namespace") != JOB_READY_VERSION:
        raise ProtocolError("job-ready manifest namespace drift")
    if protocol.get("benchmark_version") != "phase4-v1":
        raise ProtocolError("job-ready base protocol drift")
    if not isinstance(tasks, list) or len(tasks) != 1:
        raise ProtocolError("job-ready task count drift")
    task = tasks[0]
    if task.get("task_id") != TASK_ID or task.get("benchmark_version") != "phase4-v1":
        raise ProtocolError("job-ready task identity drift")
    if task.get("fixture_id") != FIXTURE_ID or task.get("fixture_version") != "1":
        raise ProtocolError("job-ready fixture binding drift")
    if task.get("fault_injection") != FAULT_ID:
        raise ProtocolError("job-ready fault binding drift")
    if task.get("fault_timing") != "before acceptance":
        raise ProtocolError("job-ready fault timing drift")
    if task.get("expected_observable_effects") != {"checksum": TARGET_HASH}:
        raise ProtocolError("job-ready acceptance state drift")
    if task.get("max_turns") != 20 or task.get("max_model_calls") != 20:
        raise ProtocolError("job-ready budget drift")
    if task.get("timeout_seconds") != 900:
        raise ProtocolError("job-ready timeout drift")
    if not isinstance(faults.get("faults"), list) or len(faults["faults"]) != 1:
        raise ProtocolError("job-ready fault count drift")
    fault = faults["faults"][0]
    if fault.get("fault_id") != FAULT_ID or fault.get("fault_type") != "tool_failure":
        raise ProtocolError("job-ready fault definition drift")
    if FAULT_ID not in {fault.get("fault_id") for fault in faults["faults"]}:
        raise ProtocolError("job-ready fault unresolved")
    if FIXTURE_ID not in fixtures.get("fixtures", {}):
        raise ProtocolError("job-ready fixture unresolved")
    if task.get("validator_id") != "external-observable-v1":
        raise ProtocolError("job-ready validator binding drift")
    if "external-observable-v1" not in validators:
        raise ProtocolError("job-ready validator unresolved")
    if set(configs) != set(SUPPORTED_CONFIGS):
        raise ProtocolError("job-ready config set drift")
    tool_set = {"workspace.edit"}
    if any(config.get("tool_capability_set") != sorted(tool_set) for config in configs.values()):
        raise ProtocolError("job-ready tool fairness drift")
    minimal = configs["minimal"].get("features", {})
    if any(bool(minimal.get(name)) for name in (
        "completion_authority",
        "workflow_verifier",
        "failure_provenance",
        "selective_repair",
        "macro_replan",
        "durable_workflow_recovery",
    )):
        raise ProtocolError("job-ready minimal exposes recovery machinery")
    odys = configs["odys_p3"].get("features", {})
    if not all(bool(odys.get(name)) for name in (
        "completion_authority",
        "workflow_verifier",
        "failure_provenance",
        "selective_repair",
        "durable_workflow_recovery",
    )):
        raise ProtocolError("job-ready odys recovery features missing")
    headline = protocol.get("headline", {})
    if headline != {
        "tasks": 1,
        "configs": list(SUPPORTED_CONFIGS),
        "repeats": 3,
        "total_runs": 6,
    }:
        raise ProtocolError("job-ready execution matrix drift")
    return {
        "benchmark_version": JOB_READY_VERSION,
        "base_protocol_version": protocol["benchmark_version"],
        "task_count": 1,
        "config_count": 2,
        "protocol_hash": _protocol_hash(root),
    }


def load_snapshot(root: Path = ROOT) -> ProtocolSnapshot:
    """Load this namespace into the official runner's snapshot contract."""
    root = Path(root).resolve()
    validate_job_ready_protocol(root)
    protocol = _read_json(root / "protocol.json")
    manifest = _read_json(root / "manifest.json")
    faults = _read_json(root / "faults.json")
    validators = _read_json(root / "validators.json")
    fixtures = _read_json(root / "fixtures" / "catalog.json")
    ablation = _read_json(root / "ablation.json")
    configs = {
        path.stem: _read_json(path)
        for path in sorted((root / "configs").glob("*.json"))
    }
    return ProtocolSnapshot(
        root=root,
        protocol=protocol,
        manifest=manifest,
        faults=faults,
        validators=validators,
        fixtures=fixtures,
        ablation=ablation,
        configs=configs,
        protocol_hash=_protocol_hash(root),
        manifest_hash=_document_hash(manifest),
        fault_set_hash=_document_hash(faults),
        validator_hash=_document_hash(validators),
        fixture_set_hash=_document_hash(fixtures),
        budget_identity=_document_hash(protocol["budgets"]),
    )


class JobReadyFixture(BaseFixture):
    """One mutable state file whose exact content hash is externally checked."""

    task_id = TASK_ID
    family = "RECOVERY_EFFECTIVENESS"
    fixture_id = FIXTURE_ID
    fault_ids = [FAULT_ID]

    def setup(self, workspace_dir: Path) -> dict[str, Any]:
        self._write_text(workspace_dir / STATE_FILE, INITIAL_CONTENT)
        return {"state_file": STATE_FILE, "initial_status": "PENDING"}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        if fault_id != FAULT_ID:
            raise ValueError(f"unknown job-ready fault: {fault_id}")
        self._write_text(workspace_dir / STATE_FILE, BROKEN_CONTENT)

    def observe(self, workspace_dir: Path) -> dict[str, Any]:
        path = workspace_dir / STATE_FILE
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        state: Any = None
        if content:
            state = json.loads(content).get("status")
        return {
            "state": state,
            "checksum": hashlib.sha256(content.encode("utf-8")).hexdigest()
            if content
            else "",
            "state_path": STATE_FILE,
        }

    def reset(self, workspace_dir: Path) -> None:
        return None


class JobReadyFixtureRegistry:
    """P45 fixture-registry adapter for the single versioned task."""

    def get(self, task_id: str) -> JobReadyFixture:
        if task_id != TASK_ID:
            raise KeyError(f"No job-ready fixture for task_id={task_id!r}")
        return JobReadyFixture()

    def all_task_ids(self) -> list[str]:
        return [TASK_ID]

    def __len__(self) -> int:
        return 1


def select_single_odys_run(snapshot: ProtocolSnapshot) -> tuple[RunSpec, ...]:
    """Select exactly the first live gate: one task, Odys, repeat one."""
    runs = select_runs(
        snapshot,
        task_id=TASK_ID,
        config_name="odys_p3",
        repeat_index=1,
    )
    if len(runs) != 1:
        raise ProtocolError("job-ready single proof selection drift")
    return runs


def create_live_executor() -> tuple[P45BenchmarkExecutor, dict[str, Any]]:
    """Create the real-provider P45 executor without executing a call."""
    provider = create_cheap_model_provider()
    identity = provider_identity(provider, expected_model=CHEAP_MODEL)
    executor = P45BenchmarkExecutor(
        fixture_registry=JobReadyFixtureRegistry(),
        factory_type="real",
        provider=provider,
        provider_identity=identity,
        expected_model=CHEAP_MODEL,
    )
    return executor, identity


def build_runner(
    snapshot: ProtocolSnapshot,
    *,
    executor: P45BenchmarkExecutor,
    output_dir: Path,
    repo_root: Path,
) -> Phase4Runner:
    """Build the standard Phase4Runner with the job-ready identity layer."""
    output_dir = Path(output_dir)
    return Phase4Runner(
        snapshot,
        output_dir=output_dir,
        executor=executor,
        fixture_manager=FixtureManager(snapshot),
        model=CHEAP_MODEL,
        provider=FROZEN_PROVIDER,
        benchmark_version=JOB_READY_VERSION,
        benchmark_config_hash=job_ready_config_hash(snapshot),
        repo_root=repo_root,
        trace_path=output_dir / "traces.jsonl",
        require_trace=True,
    )


def write_identity_artifacts(
    output_dir: Path,
    *,
    snapshot: ProtocolSnapshot,
    provider_identity_record: Mapping[str, Any],
) -> None:
    """Write only secret-free identity evidence for a live bundle."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    benchmark_identity = {
        "benchmark_version": JOB_READY_VERSION,
        "base_protocol_version": snapshot.protocol["benchmark_version"],
        "protocol_hash": snapshot.protocol_hash,
        "benchmark_config_hash": job_ready_config_hash(snapshot),
        "model_identity": CHEAP_MODEL,
        "provider_identity": FROZEN_PROVIDER,
        "validator_id": snapshot.protocol["shared_validator_id"],
    }
    (output_dir / "benchmark_identity.json").write_text(
        json.dumps(benchmark_identity, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "provider_identity.json").write_text(
        json.dumps(dict(provider_identity_record), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
