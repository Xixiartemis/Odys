"""Real Pilot Runner - executes real model trials through the canonical TrialExecutor.

``RealPilotRunner`` delegates ALL trial execution to the shared
``lhas.phase5.trial_executor.execute_trial()`` function - the same
canonical path used by ``CanaryRunner``.

Design contract
---------------
* Loads a frozen pilot manifest with experiment_id, task_ids, arm
  definitions, model config, and budgets.
* For each task x arm: calls ``execute_trial()`` - the single
  canonical execution path shared with CanaryRunner.
* Writes all artifacts per trial (raw, benchmark, derived, classification).
* Stratified sampling: ``select_20_tasks()`` picks one task from each
  C1-C4 x P0-P4 cell for the 20-task pilot.
* Pilot manifest has ``experiment_role=PIPELINE_PILOT``.
* A fresh ModelDriver is created per trial via ``model_driver_factory``
  to prevent state leakage between trials.
* Budget exhaustion is classified by TrialExecutor as ``VALID``
  (the task ran and exhausted its budget - that is a valid
  experimental outcome), not ``INVALID_INFRA``.

CANONICAL_EXPERIMENT_EXECUTOR=lhas.phase5.trial_executor.execute_trial

This runner does NOT independently create Phase5AgentCore, ExecutionEngine,
PolicyStrategy, EvidenceLedger, or observers - those are TrialExecutor
responsibilities.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .types import (
    BenchmarkName,
    BudgetConfig,
    ComparisonArm,
    ControlArm,
    GenerationConfig,
    NativeResult,
    PerturbationMode,
    RuntimeTask,
    TaskDescriptor,
    TopologyClass,
    TrialIdentity,
    TrialStatus,
)
from .toolmaze_adapter import ToolMazeAdapter
from .artifacts import ArtifactWriter
from .provenance import ProvenanceFreeze, arm_definitions_snapshot
from .control_arms import (
    ExperimentPairValidator,
)
from .trial_executor import execute_trial, TrialResult
from .runtime_validator import VALIDATOR_ID, VALIDATOR_VERSION, PublicEvidenceCompletionValidator
from .provider_lock import (
    provider_config_hash,
    validate_resolved_config,
)

logger = logging.getLogger(__name__)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")


def _write_json_once(path: Path, value: Any, *, allow_legacy_objects: bool = False) -> None:
    """Create a structured artifact exactly once; reruns use new attempts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = ArtifactWriter._structured(value)
    try:
        payload = json.dumps(normalized, indent=2, ensure_ascii=False)
    except TypeError:
        if not allow_legacy_objects:
            raise
        # Compatibility is limited to legacy test doubles in non-evidence
        # metadata; evidence/shadow artifacts remain strict structured JSON.
        payload = json.dumps(normalized, indent=2, ensure_ascii=False, default=str)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(payload)


def _write_text_once(path: Path, value: str) -> None:
    """Create a text artifact exactly once."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(value)


# -- Stratified sampling ----------------------------------------------

def select_20_tasks(
    tasks: Sequence[TaskDescriptor],
    *,
    seed: int = 42,
) -> list[TaskDescriptor]:
    """Select 20 tasks: one from each C1-C4 x P0-P4 cell.

    Stratified sampling ensures full coverage of the 20-cell grid
    (4 topology classes x 5 perturbation modes).  Fixed seed ensures
    determinism.

    Parameters
    ----------
    tasks : Sequence[TaskDescriptor]
        All available task descriptors.
    seed : int
        Random seed for deterministic selection within each cell.

    Returns
    -------
    list[TaskDescriptor]
        Exactly 20 tasks, one per cell.  If a cell is empty, a
        warning is logged and the cell is skipped.
    """
    rng = random.Random(seed)

    # Group tasks by (topology, perturbation_mode) cell
    cells: dict[tuple[str, str], list[TaskDescriptor]] = {}
    for task in tasks:
        topo = task.topology.value if task.topology else "C1"
        mode = task.perturbation_mode.value
        key = (topo, mode)
        if key not in cells:
            cells[key] = []
        cells[key].append(task)

    selected: list[TaskDescriptor] = []
    for topo_cls in TopologyClass:
        for perturb_mode in PerturbationMode:
            key = (topo_cls.value, perturb_mode.value)
            cell_tasks = cells.get(key, [])
            if cell_tasks:
                chosen = rng.choice(cell_tasks)
                selected.append(chosen)
            else:
                # Log empty cell but don't fail - some cells may be
                # legitimately empty in smaller datasets
                import sys
                print(
                    f"Warning: empty cell ({topo_cls.value}, {perturb_mode.value})",
                    file=sys.stderr,
                )

    return selected

# -- Pilot manifest schema --------------------------------------------

PILOT_MANIFEST_SCHEMA_VERSION = "phase5-real-pilot-002"
# Formal B0/B1 recovery-route experiment manifest (Experiment B).
B0B1_MANIFEST_SCHEMA_VERSION = "phase5-b0b1-experiment-001"


def build_pilot_manifest(
    *,
    experiment_id: str,
    task_ids: list[str],
    arm_definitions: dict[str, dict[str, Any]],
    model_config: GenerationConfig,
    budgets: dict[str, Any],
    benchmark_identity: dict[str, Any],
    seed: int = 42,
    task_entries: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Build a pilot manifest with the canonical schema.

    Parameters
    ----------
    experiment_id : str
        Unique experiment identifier.
    task_ids : list[str]
        Selected task IDs for the pilot.
    arm_definitions : dict
        Arm definitions snapshot.
    model_config : GenerationConfig
        Model generation configuration.
    budgets : dict
        Budget configuration.
    benchmark_identity : dict
        Benchmark provenance.
    seed : int
        Deterministic seed.

    Returns
    -------
    dict
        The pilot manifest.
    """
    manifest = {
        "schema_version": PILOT_MANIFEST_SCHEMA_VERSION,
        "experiment_role": "PIPELINE_PILOT",
        "experiment_id": experiment_id,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "benchmark": benchmark_identity,
        # Both field names for schema compatibility
        "selected_task_ids": sorted(task_ids),
        "tasks": task_entries if task_entries is not None else sorted(task_ids),
        "task_count": len(task_ids),
        # Both field names for schema compatibility
        "arm_definitions": arm_definitions,
        "arms": list(arm_definitions.keys()),
        "arm_count": len(arm_definitions),
        "total_trials": len(task_ids) * len(arm_definitions),
        "generation_config": (
            model_config.model_dump(mode="json")
            if hasattr(model_config, "model_dump")
            else model_config
        ),
        # Both field names for schema compatibility
        "budgets": {
            **budgets,
            "token_budget": budgets.get("token_budget", budgets.get("max_tokens")),
        },
        "root_budget": {
            **budgets,
            "token_budget": budgets.get("token_budget", budgets.get("max_tokens")),
        },
        "seed": seed,
        "stratified_sampling": {
            "method": "one_per_CxP_cell",
            "grid": "C1-C4 x P0-P4",
            "cells_total": 20,
            "cells_filled": len(task_ids),
            "seed": seed,
        },
    }
    manifest["manifest_hash"] = hashlib.sha256(
        _canonical_json(manifest)
    ).hexdigest()
    return manifest


# -- RealPilotRunner --------------------------------------------------

class RealPilotRunner:
    """Runs real model pilot experiments through the canonical TrialExecutor.

    Delegates ALL trial execution to ``lhas.phase5.trial_executor.execute_trial()``
    - the same canonical path used by ``CanaryRunner``.

    A fresh ``ModelDriver`` is created per trial via the injected
    ``model_driver_factory`` callable to prevent state leakage.

    Lifecycle::

        runner = RealPilotRunner(
            model_driver_factory=lambda: MyDriver(config),
            manifest_path="experiments/phase5/manifests/phase5-real-pilot-001.json",
        )
        results = runner.run_pilot()
    """

    def __init__(
        self,
        *,
        model_driver: Any = None,
        model_driver_factory: Optional[Callable[[], Any]] = None,
        manifest_path: Optional[Path] = None,
        output_dir: Optional[Path] = None,
        adapter: Optional[ToolMazeAdapter] = None,
    ):
        """Initialize the real pilot runner.

        Parameters
        ----------
        model_driver : ModelDriver, optional
            DEPRECATED - use model_driver_factory instead.  If provided,
            it is wrapped in a lambda for backward compatibility.
        model_driver_factory : callable, optional
            A zero-argument callable that returns a fresh ModelDriver
            instance.  Called once per trial to prevent state leakage.
            Required unless model_driver is provided.
        manifest_path : Path, optional
            Path to the frozen pilot manifest JSON.  If None, uses
            the default location.
        output_dir : Path, optional
            Output directory for trial artifacts.  Defaults to
            ``experiments/phase5/runs/{experiment_id}``.
        adapter : ToolMazeAdapter, optional
            Pre-configured adapter.  If None, creates a new one.
        """
        # Resolve the factory - prefer model_driver_factory
        if model_driver_factory is not None:
            self._model_driver_factory = model_driver_factory
        elif model_driver is not None:
            # Backward compatibility: wrap a single driver in a lambda
            # that calls reset() each time (best-effort isolation)
            _driver_ref = model_driver
            def _factory() -> Any:
                _driver_ref.reset()
                return _driver_ref
            self._model_driver_factory = _factory
        else:
            raise ValueError(
                "Either model_driver_factory or model_driver must be provided"
            )

        self._adapter = adapter or ToolMazeAdapter()

        # Tool schema derivation is owned exclusively by TrialExecutor's
        # official ExecutionEngine.
        self._tool_loader = None

        # Load or create manifest - handle both schema variants
        self._manifest_path = (
            manifest_path
            or Path("experiments/phase5/manifests/phase5-real-pilot-002.json")
        )
        self._manifest = self._load_manifest()

        self._experiment_id = self._manifest["experiment_id"]

        # The manifest's task objects are authoritative execution units. The
        # legacy selected_task_ids list is retained only for compatibility and
        # is never used to resolve a raw task.
        raw_tasks = self._manifest.get("tasks")
        if not raw_tasks:
            raise ValueError(
                "Manifest missing authoritative 'tasks' execution units"
            )
        if isinstance(raw_tasks[0], dict):
            self._manifest_tasks = [dict(t) for t in raw_tasks]
        else:
            self._manifest_tasks = [{"task_id": t} for t in raw_tasks]
        self._task_ids = [t["task_id"] for t in self._manifest_tasks]

        # FIX: accept both 'arm_definitions' and 'arms' keys
        if "arm_definitions" in self._manifest:
            self._arm_definitions = self._manifest["arm_definitions"]
        elif "arms" in self._manifest:
            # Legacy schema: 'arms' is a list of arm names
            self._arm_definitions = {
                arm_name: {} for arm_name in self._manifest["arms"]
            }
        else:
            raise ValueError(
                "Manifest missing both 'arm_definitions' and 'arms' keys"
            )

        self._gen_config = self._parse_gen_config(
            self._manifest["generation_config"]
        )
        lock_path = Path("experiments/phase5/manifests/provider-lock.json")
        self._provider_lock = json.loads(lock_path.read_text(encoding="utf-8")) if lock_path.exists() else {}
        self._provider_config_hash = provider_config_hash(self._provider_lock)

        # FIX: accept both 'budgets' and 'root_budget' keys
        if "budgets" in self._manifest:
            self._budgets = dict(self._manifest["budgets"])
        elif "root_budget" in self._manifest:
            self._budgets = self._manifest["root_budget"]
        else:
            self._budgets = {
                "max_turns": 30,
                "max_model_calls": 50,
            }

        self._budgets["token_budget"] = self._budgets.get(
            "token_budget", self._budgets.get("max_tokens")
        )
        self._seed = self._manifest.get("seed", self._manifest.get("selection_seed", 42))

        # Experiment B (B0/B1) detection — explicit schema discrimination,
        # never implicit field guessing.  Legacy pilot manifests keep the
        # original behaviour unchanged.
        self._route_arms: Optional[list[dict[str, Any]]] = None
        self._repetitions = int(self._manifest.get("repetitions", 1))
        if self._repetitions < 1:
            raise ValueError("repetitions must be >= 1")
        if self._manifest.get("schema_version") == B0B1_MANIFEST_SCHEMA_VERSION:
            self._route_arms = self._resolve_route_arms()
            if "budgets" not in self._manifest and "root_budget" not in self._manifest:
                raise ValueError(
                    "B0B1_MANIFEST_BUDGET_MISSING: the formal experiment "
                    "requires a frozen root budget (budgets/root_budget)"
                )
        elif "route_arms" in self._manifest:
            raise ValueError(
                "manifest carries 'route_arms' but schema_version is "
                f"{self._manifest.get('schema_version')!r}, expected "
                f"{B0B1_MANIFEST_SCHEMA_VERSION!r} — refusing to guess"
            )

        self._preflight = self._resolve_manifest_units()

        # Output directory
        self._output_dir = (
            output_dir
            or Path(f"experiments/phase5/runs/{self._experiment_id}")
        )
        self._output_dir.mkdir(parents=True, exist_ok=True)

        # Artifact writer and firewall
        self._artifacts = ArtifactWriter(self._output_dir)
        self._pair_validator = ExperimentPairValidator()

    def _load_manifest(self) -> dict[str, Any]:
        """Load the frozen pilot manifest."""
        if self._manifest_path.exists():
            manifest = json.loads(
                self._manifest_path.read_text(encoding="utf-8")
            )
            stored_hash = manifest.get("manifest_hash")
            if stored_hash:
                unsigned = {k: v for k, v in manifest.items() if k != "manifest_hash"}
                actual_hash = hashlib.sha256(_canonical_json(unsigned)).hexdigest()
                if stored_hash != actual_hash:
                    raise ValueError("pilot manifest hash mismatch")
            return manifest
        raise FileNotFoundError(
            f"Pilot manifest not found: {self._manifest_path}. "
            f"Run generate_pilot_manifest() first."
        )

    def _parse_gen_config(self, raw: dict[str, Any]) -> GenerationConfig:
        """Parse generation config from manifest dict."""
        return GenerationConfig(
            model_id=raw.get("model_id", "unknown"),
            provider=raw.get("provider", "unknown"),
            temperature=raw.get("temperature", 0.0),
            top_p=raw.get("top_p", 1.0),
            max_output_tokens=raw.get("max_output_tokens"),
            seed=raw.get("seed"),
        )

    def _get_raw_task(self, task_id: str) -> Optional[dict[str, Any]]:
        """Get the full raw task JSON (with hidden fields) for a task_id."""
        # The adapter's _task_index has original_task_id keys
        # We need to find by composite key (original_id_mode)
        task = self._adapter._find_task(task_id)
        return task

    def _resolve_manifest_units(self) -> dict[str, Any]:
        """Resolve every manifest execution unit exactly once, provider-free."""
        descriptors = {
            d.task_id: d for d in self._adapter.enumerate_tasks()
        }
        resolved = []
        mismatches = []
        for entry in self._manifest_tasks:
            task_id = entry["task_id"]
            desc = descriptors.get(task_id)
            raw = self._adapter._find_task(task_id)
            if desc is None or raw is None:
                mismatches.append({"task_id": task_id, "reason": "not_found"})
                continue
            expected_mode = str(entry.get("perturbation_mode", ""))
            expected_complexity = str(entry.get("complexity", ""))
            actual_file = str(raw.get("_source_file", "")).replace("/", "\\")
            expected_file = str(entry.get("file_path", "")).replace("/", "\\")
            if expected_mode and expected_mode != desc.perturbation_mode.value:
                mismatches.append({"task_id": task_id, "reason": "mode_mismatch"})
            if expected_complexity and expected_complexity != (desc.complexity or ""):
                mismatches.append({"task_id": task_id, "reason": "complexity_mismatch"})
            if expected_file and expected_file.lower() != actual_file.lower():
                mismatches.append({"task_id": task_id, "reason": "file_mismatch", "expected": expected_file, "actual": actual_file})
            resolved.append({"entry": entry, "descriptor": desc, "raw": raw})
        report = {
            "expected_execution_units": len(self._manifest_tasks),
            "resolved_execution_units": len(resolved),
            "mode_mismatches": sum(1 for m in mismatches if m["reason"] == "mode_mismatch"),
            "file_mismatches": sum(1 for m in mismatches if m["reason"] == "file_mismatch"),
            "mismatches": mismatches,
            "PILOT_TASK_IDENTITY": (
                "PROVEN"
                if len(resolved) == len(self._manifest_tasks) and not mismatches
                else "FAILED"
            ),
            "P0_P4_VARIANT_OVERWRITE": "IMPOSSIBLE",
        }
        if mismatches or len(resolved) != len(self._manifest_tasks):
            raise ValueError(f"pilot execution-unit preflight failed: {report}")
        return {"report": report, "units": resolved}

    def _assert_provider_config(self, driver: Any) -> None:
        """Require the actual LiveModelDriver to match provider-lock."""
        if self._gen_config.provider == "none":
            return
        actual = getattr(driver, "resolved_config", None)
        if not isinstance(actual, dict):
            raise ValueError("live driver must expose resolved_config for provider-lock parity")
        validate_resolved_config(actual, self._provider_lock)

    def _resolve_route_arms(self) -> list[dict[str, Any]]:
        """Validate and resolve the formal B0/B1 comparison arm definitions.

        The formal experiment only allows B0_BASELINE_ROUTE and
        B1_ROUTE_V1, both on A3_ODYS_FULL, with route_mode as the unique
        pre-registered difference.  Any other combination fails closed.
        """
        raw = self._manifest.get("route_arms")
        if not raw:
            raise ValueError("B0/B1 manifest missing 'route_arms'")
        seen: set[str] = set()
        resolved: list[dict[str, Any]] = []
        for entry in raw:
            arm_id = str(entry.get("arm_id", ""))
            route_mode = str(entry.get("route_mode", ""))
            strategy_arm = str(entry.get("recovery_strategy_arm", ""))
            try:
                comparison = ComparisonArm(arm_id)
            except ValueError as exc:
                raise ValueError(f"unknown comparison arm: {arm_id!r}") from exc
            if comparison.route_mode != route_mode:
                raise ValueError(
                    f"{arm_id} requires route_mode={comparison.route_mode!r}, "
                    f"got {route_mode!r}"
                )
            if strategy_arm != ControlArm.A3_ODYS_FULL.value:
                raise ValueError(
                    "the formal B0/B1 experiment requires both arms to run "
                    f"A3_ODYS_FULL, got {strategy_arm!r}"
                )
            if arm_id in seen:
                raise ValueError(f"duplicate comparison arm: {arm_id}")
            seen.add(arm_id)
            resolved.append({
                "arm_id": arm_id,
                "route_mode": route_mode,
                "recovery_strategy_arm": strategy_arm,
            })
        expected = {a.value for a in ComparisonArm}
        if seen != expected:
            raise ValueError(
                f"B0/B1 manifest must define exactly {sorted(expected)}, "
                f"got {sorted(seen)}"
            )
        return resolved

    def _check_b0b1_authorization(
        self,
        authorization: Optional[dict[str, Any]],
        offline_validation: bool,
    ) -> dict[str, Any]:
        """Fail-closed authorization gate for the formal experiment.

        - ``offline_validation=True`` is the independent provider-free
          entry used by tests and preflights.  It never fabricates a
          human approval status; its results are permanently marked as
          offline validation and can never enter confirmatory analysis.
          Fail-closed against mislabeling: refused when a real API
          credential is present in the environment (offline runs must
          not be able to emit real traffic that would be recorded as
          ``real_provider_executed=NO``).
        - Real execution requires an explicit human approval record bound
          to this exact manifest hash.  Anything else raises.
        """
        import os

        if offline_validation:
            if os.environ.get("ODYS_AGENT_API_KEY", "").strip():
                raise PermissionError(
                    "B0B1_OFFLINE_WITH_REAL_CREDENTIALS: offline validation "
                    "refuses to run while a real API credential is present — "
                    "real traffic must never be recorded as fake"
                )
            return {
                "execution_mode": "OFFLINE_VALIDATION",
                "included_in_confirmatory_analysis": False,
                "real_provider_executed": "NO",
            }
        requires = bool(self._manifest.get("execution_requires_human_approval", True))
        if requires:
            if not isinstance(authorization, dict):
                raise PermissionError(
                    "B0B1_APPROVAL_REQUIRED: real execution is fail-closed "
                    "until a human approval record bound to this manifest "
                    "is provided"
                )
            if authorization.get("status") != "HUMAN_APPROVED":
                raise PermissionError(
                    "B0B1_APPROVAL_INVALID: authorization.status must be "
                    "'HUMAN_APPROVED'"
                )
            approved_by = authorization.get("approved_by")
            if not approved_by or not isinstance(approved_by, str):
                raise PermissionError(
                    "B0B1_APPROVAL_INVALID: authorization.approved_by is "
                    "required"
                )
            if authorization.get("manifest_hash") != self._manifest.get("manifest_hash"):
                raise PermissionError(
                    "B0B1_APPROVAL_MANIFEST_MISMATCH: approval is bound to a "
                    "different manifest hash"
                )
            return {
                "execution_mode": "HUMAN_APPROVED_EXECUTION",
                "included_in_confirmatory_analysis": True,
                "real_provider_executed": "YES",
                "approved_by": approved_by,
                "approved_at": authorization.get("approved_at"),
            }
        return {
            "execution_mode": "UNRESTRICTED_MANIFEST",
            "included_in_confirmatory_analysis": True,
            "real_provider_executed": "YES",
        }

    def _build_trial_invariants(
        self,
        *,
        desc: TaskDescriptor,
        raw_task: Optional[dict[str, Any]],
        trial_result: TrialResult,
        trial_id: str,
        arm: ControlArm,
    ) -> dict[str, Any]:
        """Build pairing invariants from a canonical TrialResult.

        Single source of truth shared by the legacy A0–A5 pilot path and
        the formal B0/B1 experiment path.
        """
        runtime_task = self._adapter.build_runtime_task(desc)
        # Test doubles and legacy adapters may return a mock or a
        # partial runtime object.  Pairing hashes must remain
        # structured JSON; never let a mock leak into provenance.
        if isinstance(runtime_task, RuntimeTask):
            environment_snapshot = runtime_task.environment_snapshot
            if not isinstance(environment_snapshot, dict):
                environment_snapshot = {}
            runtime_task = runtime_task.model_copy(
                update={
                    "visible_tools": trial_result.runtime_tool_schema,
                    "environment_snapshot": environment_snapshot,
                }
            )
        else:
            user_input = raw_task.get("user_input", {})
            if not isinstance(user_input, dict):
                user_input = {}
            runtime_task = RuntimeTask(
                task_id=desc.task_id,
                objective=str(raw_task.get("task_description", "")),
                visible_tools=trial_result.runtime_tool_schema,
                prompt=str(
                    user_input.get(
                        "query",
                        raw_task.get("task_description", ""),
                    )
                ),
                constraints=[],
                acceptance_criteria=["complete the task as described"],
                environment_snapshot=user_input,
                budget=BudgetConfig(
                    max_turns=int(self._budgets.get("max_turns", 30)),
                    max_model_calls=int(
                        self._budgets.get("max_model_calls", 50)
                    ),
                    token_budget=self._budgets.get("token_budget"),
                    deadline_seconds=self._budgets.get(
                        "deadline_seconds"
                    ),
                ),
            )
        invariants = ExperimentPairValidator.compute_trial_invariants(
            experiment_id=self._experiment_id,
            trial_id=trial_id,
            adapter=self._adapter,
            task=runtime_task,
            generation_config=self._gen_config,
            arm=arm,
            perturbation_mode=desc.perturbation_mode.value,
            fault_source="BENCHMARK_NATIVE",
            validator_identity=(
                f"{VALIDATOR_ID}+{PublicEvidenceCompletionValidator().config_hash()}"
            ),
            offline_grader_identity="toolmaze-judge+metrics-v1+412f9c3615bfd997af903f6514f716be94a774140975b82c8ff2fd16b6547833",
        )
        invariants["seed"] = self._seed
        invariants["provider_config_hash"] = self._provider_config_hash
        invariants["environment_snapshot"] = runtime_task.environment_snapshot
        invariants["strategy_config"] = trial_result.strategy_config
        invariants["budget_accounting"] = trial_result.provider_usage
        return invariants

    # -- Main execution ------------------------------------------------

    def run_pilot(
        self,
        *,
        arms: Optional[list[ControlArm]] = None,
        max_rounds: Optional[int] = None,
        authorization: Optional[dict[str, Any]] = None,
        offline_validation: bool = False,
    ) -> dict[str, Any]:
        """Run the full pilot experiment.

        For each task x arm: calls ``execute_trial()`` - the single
        canonical execution path shared with ``CanaryRunner``.
        Writes all artifacts per trial.

        A fresh ModelDriver is created per trial via the injected
        ``model_driver_factory`` to prevent state leakage.

        Parameters
        ----------
        arms : list[ControlArm], optional
            Arms to run.  Defaults to all six arms.  For formal B0/B1
            manifests the comparison arms are fixed by the manifest and
            only ``[A3_ODYS_FULL]`` (or None) is accepted.
        max_rounds : int
            Maximum reasoning rounds per trial.
        authorization : dict, optional
            Human approval record for the formal B0/B1 experiment
            (``status="HUMAN_APPROVED"``, ``approved_by``, and the
            approved ``manifest_hash``).  Fail-closed when absent.
        offline_validation : bool
            Independent provider-free entry for tests and preflights.
            Never fabricates an approval status; results are permanently
            marked OFFLINE_VALIDATION and excluded from confirmatory
            analysis.

        Returns
        -------
        dict
            Experiment summary with per-task results, pairing
            validation, and artifact paths.
        """
        if self._route_arms is not None:
            return self._run_b0b1_experiment(
                arms=arms,
                max_rounds=max_rounds,
                authorization=authorization,
                offline_validation=offline_validation,
            )
        if authorization is not None or offline_validation:
            raise ValueError(
                "authorization/offline_validation apply only to formal "
                "B0/B1 experiment manifests"
            )
        if arms is None:
            arms = list(ControlArm)

        if max_rounds is not None and max_rounds != int(self._budgets.get("max_turns", 15)):
            raise ValueError("run_pilot(max_rounds) must equal frozen max_turns")

        # Persist the deterministic interleaved schedule before any trial.
        schedule = {}
        for index, task_id in enumerate(self._task_ids):
            task_arms = list(arms)
            rotation = (self._seed + index) % len(task_arms)
            schedule[task_id] = [a.value for a in task_arms[rotation:] + task_arms[:rotation]]
        schedule_path = self._output_dir / "arm_schedule.json"
        if not schedule_path.exists():
            _write_json_once(schedule_path, {"seed": self._seed, "schedule": schedule})

        results: list[dict[str, Any]] = []
        all_trial_manifests: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        for unit in self._preflight["units"]:
            entry, desc, raw_task = unit["entry"], unit["descriptor"], unit["raw"]
            task_id = entry["task_id"]

            # Run all arms for this task
            arm_results: dict[str, dict[str, Any]] = {}
            trial_manifests: list[dict[str, Any]] = []

            task_order = [ControlArm(a) for a in schedule[task_id]]
            for arm in task_order:
                trial_id = f"{self._experiment_id}_{task_id}_{arm.value}"

                # Build budget from manifest
                budget = BudgetConfig(
                    max_turns=self._budgets.get("max_turns", 30),
                    max_model_calls=self._budgets.get("max_model_calls", 50),
                    token_budget=self._budgets.get("token_budget"),
                    deadline_seconds=self._budgets.get("deadline_seconds"),
                )

                # Fresh model driver per trial
                model_driver = self._model_driver_factory()
                self._assert_provider_config(model_driver)

                # -- Canonical execution via TrialExecutor ----------
                try:
                    trial_result: TrialResult = execute_trial(
                        arm=arm,
                        task_json=raw_task,
                        tool_definitions=None,
                        model_driver=model_driver,
                        budget=budget,
                        experiment_id=self._experiment_id,
                        task_id=task_id,
                        max_rounds=max_rounds,
                    )

                    # Build pairing invariants from TrialResult
                    invariants = self._build_trial_invariants(
                        desc=desc,
                        raw_task=raw_task,
                        trial_result=trial_result,
                        trial_id=trial_id,
                        arm=arm,
                    )
                    trial_manifests.append(invariants)

                    arm_results[arm.value] = trial_result.to_dict()

                    # Write canonical artifacts
                    self._write_trial_artifacts(
                        trial_id, desc, invariants, trial_result,
                        raw_task=raw_task,
                    )

                except Exception as e:
                    logger.exception(
                        "Phase5 pilot trial failed: trial_id=%s arm=%s",
                        trial_id,
                        arm.value,
                    )
                    arm_results[arm.value] = {"error": str(e), "arm": arm.value}
                    errors.append({"trial_id": trial_id, "error": str(e)})

            # Validate pairing
            if len(trial_manifests) >= 2:
                pairing = self._pair_validator.validate(trial_manifests)
                paired_manifest = self._pair_validator.generate_paired_manifest(
                    self._experiment_id, task_id, trial_manifests,
                )
                all_trial_manifests.append(paired_manifest)
            else:
                pairing = {"valid": False, "violations": ["< 2 trials"]}

            results.append({
                "task_id": task_id,
                "topology": desc.topology.value,
                "perturbation_mode": desc.perturbation_mode.value,
                "arm_results": arm_results,
                "pairing_valid": pairing.get("valid", False),
                "pairing_violations": pairing.get("violations", []),
            })

        # Firewall lifecycle and per-trial reports are owned by TrialExecutor.
        firewall_report = {
            "owner": "TRIAL_EXECUTOR",
            "canary_active": True,
            "pilot_active": True,
            "offline_grader_during_runtime": "IMPOSSIBLE",
        }

        # Write experiment manifest
        experiment_manifest = {
            "schema_version": PILOT_MANIFEST_SCHEMA_VERSION,
            "experiment_role": "PIPELINE_PILOT",
            "experiment_id": self._experiment_id,
            "benchmark": self._manifest.get("benchmark", {}),
            "generation_config": self._manifest.get("generation_config", {}),
            "arm_definitions": arm_definitions_snapshot(),
            "task_count": len(self._task_ids),
            "arm_count": len(arms),
            "total_trials": len(self._task_ids) * len(arms),
            "firewall_audit": firewall_report,
            "results_summary": {
                "all_pairings_valid": all(
                    r.get("pairing_valid", False) for r in results
                ),
                "total_violations": sum(
                    len(r.get("pairing_violations", [])) for r in results
                ),
                "invariant_fields_count": len(
                    ExperimentPairValidator.INVARIANT_FIELDS
                ),
                "errors": len(errors),
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "execution_unit_preflight": self._preflight["report"],
            "arm_schedule": schedule,
            "ARM_ORDER_PRECOMMITTED": "YES",
        }

        manifest_path = self._output_dir / "experiment_manifest.json"
        if not manifest_path.exists():
            self._artifacts._create_json(manifest_path, experiment_manifest)

        # Write paired manifests
        paired_dir = self._output_dir / "paired_manifests"
        paired_dir.mkdir(exist_ok=True)
        for pm in all_trial_manifests:
            path = paired_dir / f"{pm['task_id']}_paired.json"
            if not path.exists():
                self._artifacts._create_json(path, pm)

        # Write error log if any
        if errors:
            errors_path = self._output_dir / "errors.json"
            if not errors_path.exists():
                self._artifacts._create_json(errors_path, errors)

        return {
            "experiment_id": self._experiment_id,
            "task_count": len(self._task_ids),
            "arm_count": len(arms),
            "total_trials": len(self._task_ids) * len(arms),
            "all_pairings_valid": experiment_manifest["results_summary"][
                "all_pairings_valid"
            ],
            "total_violations": experiment_manifest["results_summary"][
                "total_violations"
            ],
            "errors": len(errors),
            "output_dir": str(self._output_dir),
        }

    # -- Formal B0/B1 experiment (Experiment B) ------------------------

    def _run_b0b1_experiment(
        self,
        *,
        arms: Optional[list[ControlArm]],
        max_rounds: Optional[int],
        authorization: Optional[dict[str, Any]],
        offline_validation: bool,
    ) -> dict[str, Any]:
        """Run the formal B0/B1 paired recovery-route experiment.

        Data flow (single canonical execution path — no private runner):
        frozen manifest → identity/preflight → deterministic paired
        schedule → per-trial fresh ModelDriver → canonical
        ``execute_trial()`` with explicit ``route_mode`` → shared
        ExecutionAuthorizationGate → shared Judge/validity rules →
        create-only per-trial artifacts → paired aggregation.

        Both comparison arms run A3_ODYS_FULL with identical budgets and
        environments; ``route_mode`` is the unique pre-registered policy
        difference.
        """
        if arms is not None and arms != [ControlArm.A3_ODYS_FULL]:
            raise ValueError(
                "B0/B1 comparison arms are fixed by the manifest; ablation "
                "arms must not be passed"
            )
        if max_rounds is not None and max_rounds != int(self._budgets.get("max_turns", 15)):
            raise ValueError("run_pilot(max_rounds) must equal frozen max_turns")
        execution_mode = self._check_b0b1_authorization(authorization, offline_validation)

        # Deterministic paired schedule + identity uniqueness proof,
        # computed BEFORE any execution.
        plan: list[dict[str, Any]] = []
        seen_trial_ids: set[str] = set()
        for index, unit in enumerate(self._preflight["units"]):
            entry = unit["entry"]
            task_id = entry["task_id"]
            for rep in range(1, self._repetitions + 1):
                rotation = (self._seed + index + rep) % len(self._route_arms)
                ordered = self._route_arms[rotation:] + self._route_arms[:rotation]
                identities: list[TrialIdentity] = []
                for arm_def in ordered:
                    identity = TrialIdentity(
                        experiment_id=self._experiment_id,
                        task_id=task_id,
                        recovery_strategy_arm=ControlArm(
                            arm_def["recovery_strategy_arm"]
                        ),
                        comparison_arm_id=arm_def["arm_id"],
                        route_mode=arm_def["route_mode"],
                        repetition_index=rep,
                    )
                    if identity.trial_id in seen_trial_ids:
                        raise ValueError(
                            f"trial identity collision: {identity.trial_id}"
                        )
                    seen_trial_ids.add(identity.trial_id)
                    identities.append(identity)
                plan.append({
                    "unit": unit,
                    "repetition_index": rep,
                    "identities": identities,
                })

        schedule_path = self._output_dir / "experiment_schedule.json"
        if not schedule_path.exists():
            _write_json_once(schedule_path, {
                "seed": self._seed,
                "repetitions": self._repetitions,
                "execution_mode": execution_mode,
                "schedule": [
                    {
                        "task_id": item["unit"]["entry"]["task_id"],
                        "repetition_index": item["repetition_index"],
                        "trial_ids": [i.trial_id for i in item["identities"]],
                    }
                    for item in plan
                ],
            })

        # Experiment-level budget guard (fail-closed stop rule).
        budget_guard = dict(self._manifest.get("experiment_budget") or {})
        max_total_trials = budget_guard.get("max_total_trials")
        planned_trials = len(plan) * len(self._route_arms)
        if max_total_trials is not None and planned_trials > int(max_total_trials):
            raise ValueError(
                f"EXPERIMENT_BUDGET_EXCEEDED: planned trials {planned_trials} "
                f"> max_total_trials {max_total_trials}"
            )
        max_total_model_calls = budget_guard.get("max_total_model_calls")

        pair_records: list[dict[str, Any]] = []
        executed: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        cumulative_model_calls = 0
        stopped_reason = None

        for item in plan:
            unit = item["unit"]
            entry, desc, raw_task = unit["entry"], unit["descriptor"], unit["raw"]
            task_id = entry["task_id"]
            rep = item["repetition_index"]
            pair_invariants: list[dict[str, Any]] = []
            pair_trials: list[dict[str, Any]] = []

            for identity in item["identities"]:
                if stopped_reason is not None:
                    break
                trial_dir = self._output_dir / identity.trial_id
                if (trial_dir / "COMPLETE").exists():
                    executed.append({
                        "trial_id": identity.trial_id,
                        "comparison_arm_id": identity.comparison_arm_id,
                        "route_mode": identity.route_mode,
                        "status": "ALREADY_COMPLETE_SKIPPED",
                    })
                    continue

                budget = BudgetConfig(
                    max_turns=self._budgets.get("max_turns", 30),
                    max_model_calls=self._budgets.get("max_model_calls", 50),
                    token_budget=self._budgets.get("token_budget"),
                    deadline_seconds=self._budgets.get("deadline_seconds"),
                )
                model_driver = self._model_driver_factory()
                self._assert_provider_config(model_driver)
                try:
                    trial_result: TrialResult = execute_trial(
                        arm=ControlArm(identity.recovery_strategy_arm.value),
                        task_json=raw_task,
                        tool_definitions=None,
                        model_driver=model_driver,
                        budget=budget,
                        experiment_id=self._experiment_id,
                        task_id=task_id,
                        max_rounds=max_rounds,
                        route_mode=identity.route_mode,
                        identity=identity,
                    )
                    invariants = self._build_trial_invariants(
                        desc=desc,
                        raw_task=raw_task,
                        trial_result=trial_result,
                        trial_id=identity.trial_id,
                        arm=ControlArm(identity.recovery_strategy_arm.value),
                    )
                    invariants["comparison_arm_id"] = identity.comparison_arm_id
                    invariants["route_mode"] = identity.route_mode
                    invariants["repetition_index"] = identity.repetition_index
                    invariants["execution_mode"] = execution_mode["execution_mode"]
                    pair_invariants.append(invariants)

                    self._write_trial_artifacts(
                        identity.trial_id, desc, invariants, trial_result,
                        raw_task=raw_task,
                    )

                    judgement = trial_result.grader_result.get("judgement")
                    metrics_summary = trial_result.grader_result.get("metrics_summary") or {}
                    pair_trials.append({
                        "trial_id": identity.trial_id,
                        "comparison_arm_id": identity.comparison_arm_id,
                        "route_mode": identity.route_mode,
                        "repetition_index": identity.repetition_index,
                        "validity": trial_result.validity,
                        "termination_reason": trial_result.termination_reason,
                        "grader_pass": (judgement or {}).get("pass") if judgement is not None else None,
                        "tsr": metrics_summary.get("tsr"),
                        "recovery_routes": [
                            d.get("route") if isinstance(d, dict) else getattr(d, "route", None)
                            for d in trial_result.recovery_decisions
                        ],
                        "provider_usage": trial_result.provider_usage,
                    })
                    calls_used = int(trial_result.provider_usage.get("model_calls_used", 0) or 0)
                    cumulative_model_calls += calls_used
                    executed.append({
                        "trial_id": identity.trial_id,
                        "comparison_arm_id": identity.comparison_arm_id,
                        "route_mode": identity.route_mode,
                        "status": "EXECUTED",
                        "validity": trial_result.validity,
                    })
                    if (
                        max_total_model_calls is not None
                        and cumulative_model_calls >= int(max_total_model_calls)
                    ):
                        stopped_reason = "EXPERIMENT_BUDGET_EXHAUSTED"
                except Exception as e:
                    logger.exception(
                        "B0/B1 trial failed: trial_id=%s", identity.trial_id
                    )
                    errors.append({"trial_id": identity.trial_id, "error": str(e)})
                    pair_trials.append({
                        "trial_id": identity.trial_id,
                        "comparison_arm_id": identity.comparison_arm_id,
                        "route_mode": identity.route_mode,
                        "error": str(e),
                    })

            # Paired validation for this (task, repetition) pair.
            if len(pair_invariants) >= 2:
                pairing = self._pair_validator.validate(pair_invariants)
                paired_manifest = self._pair_validator.generate_paired_manifest(
                    self._experiment_id, task_id, pair_invariants,
                )
                paired_dir = self._output_dir / "paired_manifests"
                paired_dir.mkdir(exist_ok=True)
                paired_path = paired_dir / f"{task_id}_r{rep:02d}_paired.json"
                if not paired_path.exists():
                    self._artifacts._create_json(paired_path, paired_manifest)
            else:
                pairing = {"valid": False, "violations": ["< 2 trials"]}

            pair_records.append({
                "task_id": task_id,
                "condition_id": entry.get("condition_id", task_id),
                "cluster_id": entry.get("cluster_id", task_id.rsplit("_", 1)[0]),
                "repetition_index": rep,
                "trials": pair_trials,
                "pairing_valid": pairing.get("valid", False),
                "pairing_violations": pairing.get("violations", []),
            })
            if stopped_reason is not None:
                break

        summary = {
            "schema_version": B0B1_MANIFEST_SCHEMA_VERSION,
            "experiment_id": self._experiment_id,
            "experiment_role": "B0B1_EXPERIMENT",
            "execution_mode": execution_mode,
            "manifest_hash": self._manifest.get("manifest_hash"),
            "route_arms": self._route_arms,
            "strategy_arm": "A3_ODYS_FULL",
            "unique_difference_surface": "route_mode only (Q3a/Q3b/Q3d rule set)",
            "repetitions": self._repetitions,
            "task_count": len(self._task_ids),
            "planned_trials": planned_trials,
            "executed_trials": sum(1 for e in executed if e.get("status") == "EXECUTED"),
            "skipped_complete": sum(
                1 for e in executed if e.get("status") == "ALREADY_COMPLETE_SKIPPED"
            ),
            "errors": len(errors),
            "stopped_reason": stopped_reason,
            "cumulative_model_calls": cumulative_model_calls,
            "pairing_summary": {
                "pairs": len(pair_records),
                "all_pairings_valid": all(r["pairing_valid"] for r in pair_records),
                "total_violations": sum(len(r["pairing_violations"]) for r in pair_records),
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        manifest_path = self._output_dir / "experiment_manifest.json"
        if not manifest_path.exists():
            self._artifacts._create_json(manifest_path, summary)
        records_path = self._output_dir / "pair_records.json"
        if not records_path.exists():
            self._artifacts._create_json(records_path, pair_records)
        if errors:
            errors_path = self._output_dir / "errors.json"
            if not errors_path.exists():
                self._artifacts._create_json(errors_path, errors)

        return {
            "experiment_id": self._experiment_id,
            "execution_mode": execution_mode,
            "task_count": len(self._task_ids),
            "repetitions": self._repetitions,
            "planned_trials": planned_trials,
            "executed_trials": summary["executed_trials"],
            "skipped_complete": summary["skipped_complete"],
            "errors": len(errors),
            "stopped_reason": stopped_reason,
            "all_pairings_valid": summary["pairing_summary"]["all_pairings_valid"],
            "total_violations": summary["pairing_summary"]["total_violations"],
            "output_dir": str(self._output_dir),
            "pair_records": pair_records,
        }

    # -- Artifact writing ----------------------------------------------

    def _write_trial_artifacts(
        self,
        trial_id: str,
        desc: TaskDescriptor,
        manifest: dict[str, Any],
        trial_result: TrialResult,
        *,
        raw_task: Optional[dict[str, Any]] = None,
    ) -> None:
        """Write all trial artifacts from canonical TrialResult."""
        base_trial_dir = self._output_dir / trial_id
        if (base_trial_dir / "COMPLETE").exists():
            return
        attempt_id = "attempt-0"
        trial_dir = base_trial_dir
        artifact_trial_id = trial_id
        if base_trial_dir.exists():
            attempt_id = f"attempt-{uuid.uuid4().hex[:12]}"
            trial_dir = self._output_dir / f"{trial_id}__{attempt_id}"
            artifact_trial_id = trial_dir.name
        trial_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            **manifest,
            "trial_id": trial_id,
            "attempt_id": attempt_id,
            "rerun_reason": None if attempt_id == "attempt-0" else "incomplete_prior_attempt",
            "parent_attempt_id": None if attempt_id == "attempt-0" else "attempt-0",
        }

        # Every file is create-only. A rerun gets a new attempt directory.
        _write_json_once(trial_dir / "official_trace.json", trial_result.official_trace)
        _write_json_once(trial_dir / "derived_runtime_view.json", trial_result.derived_view)
        _write_json_once(trial_dir / "recovery_decisions.json", trial_result.recovery_decisions)
        _write_json_once(trial_dir / "progress_shadow.json", trial_result.shadow_records)
        _write_text_once(
            trial_dir / "evidence.jsonl",
            "\n".join(json.dumps(ArtifactWriter._structured(e), ensure_ascii=False)
                      for e in trial_result.evidence_events),
        )
        _write_json_once(trial_dir / "recovery_budget_ledger.json", trial_result.recovery_budget_ledger)
        _write_json_once(trial_dir / "validator_events.json", trial_result.validator_events)
        _write_json_once(trial_dir / "control_events.json", trial_result.control_events)
        _write_json_once(trial_dir / "public_tool_observations.json", trial_result.public_tool_observations)
        _write_json_once(trial_dir / "budget_ledger.json", trial_result.provider_usage)
        _write_json_once(trial_dir / "provider_usage.json", trial_result.provider_usage)
        _write_json_once(trial_dir / "native_judgement.json", trial_result.grader_result.get("judgement") or {})
        _write_json_once(
            trial_dir / "native_metrics.json",
            trial_result.grader_result.get("metrics_report") or
            trial_result.grader_result.get("metrics_summary") or {},
        )
        _write_json_once(
            trial_dir / "validity.json",
            {
                "validity": trial_result.validity,
                "termination_reason": trial_result.termination_reason,
                "grader_error": trial_result.grader_result.get("error"),
                "error_diagnostics": trial_result.error_diagnostics,
            },
        )
        _write_json_once(trial_dir / "trial_manifest.json", manifest, allow_legacy_objects=True)

        # Native result for backward compat with ArtifactWriter
        tsr_value = trial_result.grader_result.get("metrics_summary", {}).get("tsr")
        if not isinstance(tsr_value, (int, float)) or isinstance(tsr_value, bool):
            # Per-perturbation breakdown dicts belong in native_metrics;
            # the scalar slot only accepts a number (or None).
            tsr_value = None
        native = NativeResult(
            tsr=tsr_value,
            raw_score=None,
            native_metrics=trial_result.grader_result.get("metrics_report") or {},
        )
        self._artifacts.write_benchmark_result(artifact_trial_id, native)

        # Classification
        status = TrialStatus.VALID if trial_result.validity == "VALID" else TrialStatus.INVALID_INFRA
        self._artifacts.classify_trial(artifact_trial_id, status)
        marker = trial_dir / "COMPLETE"
        with marker.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps({"trial_id": trial_id, "attempt_id": attempt_id}))


# -- Pilot manifest generator -----------------------------------------

def generate_pilot_manifest(
    *,
    experiment_id: str = "phase5-real-pilot-001",
    seed: int = 42,
    model_id: str = "mimo-v2.5",
    provider: str = "mimo",
    temperature: float = 0.0,
    max_turns: int = 15,
    max_model_calls: int = 50,
    output_dir: Optional[Path] = None,
    adapter: Optional[ToolMazeAdapter] = None,
) -> dict[str, Any]:
    """Generate a 20-task pilot manifest with stratified sampling.

    Selects one task from each C1-C4 x P0-P4 cell using a fixed seed
    for determinism.  Persists the manifest to the standard location.

    Parameters
    ----------
    experiment_id : str
        Experiment identifier.
    seed : int
        Fixed seed for deterministic sampling.
    model_id : str
        Model identifier.
    provider : str
        Provider name.
    temperature : float
        Model temperature.
    max_turns : int
        Maximum turns per trial.
    max_model_calls : int
        Maximum model calls per trial.
    output_dir : Path, optional
        Output directory for the manifest.  Defaults to
        ``experiments/phase5/manifests/``.
    adapter : ToolMazeAdapter, optional
        Pre-configured adapter.  If None, creates a new one.

    Returns
    -------
    dict
        The generated pilot manifest.
    """
    # Initialize adapter to load tasks
    if adapter is None:
        adapter = ToolMazeAdapter()

    # Enumerate all tasks
    all_tasks = adapter.enumerate_tasks()

    # Stratified sampling: one from each CxP cell
    selected = select_20_tasks(all_tasks, seed=seed)
    task_ids = [t.task_id for t in selected]
    task_entries = []
    for t in selected:
        raw = adapter._find_task(t.task_id)
        task_entries.append({
            "task_id": t.task_id,
            "complexity": t.complexity,
            "perturbation_mode": t.perturbation_mode.value,
            "file_path": str(raw.get("_source_file", "")) if raw else "",
        })

    # Generation config
    gen_config = GenerationConfig(
        model_id=model_id,
        provider=provider,
        temperature=temperature,
        seed=seed,
    )

    # Budget config
    budgets = {
        "max_turns": max_turns,
        "max_model_calls": max_model_calls,
        "token_budget": 100000,
        "deadline_seconds": None,
    }

    # Arm definitions
    arm_defs = arm_definitions_snapshot()

    # Benchmark identity
    identity = adapter.benchmark_identity
    benchmark_identity = {
        "name": identity.benchmark_name.value,
        "revision": identity.benchmark_revision,
        "repository_url": identity.repository_url,
        "commit_sha": identity.commit_sha,
        "dataset_digest": identity.dataset_digest,
        "evaluator_digest": identity.evaluator_digest,
    }

    # Build manifest
    manifest = build_pilot_manifest(
        experiment_id=experiment_id,
        task_ids=task_ids,
        arm_definitions=arm_defs,
        model_config=gen_config,
        budgets=budgets,
        benchmark_identity=benchmark_identity,
        seed=seed,
        task_entries=task_entries,
    )

    # Persist to file
    out_dir = output_dir or Path("experiments/phase5/manifests")
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / f"{experiment_id}.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return manifest
