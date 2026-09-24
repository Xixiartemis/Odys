"""Real Pilot Runner — executes real model trials through the unified backend.

``RealPilotRunner`` is the **single execution path** for Phase 5 real
pilot experiments.  It uses ``ToolMazeRuntimeBackend.execute()`` as
the sole execution point — never ``AgentExecutionHarness`` or synthetic
execution.

Design contract
───────────────
* Loads a frozen pilot manifest with experiment_id, task_ids, arm
  definitions, model config, and budgets.
* For each task × arm: runs through the official ExecutionEngine via
  ToolMazeRuntimeBackend.execute() with the appropriate PolicyStrategy.
* Writes all artifacts per trial (raw, benchmark, derived, classification).
* Stratified sampling: ``select_20_tasks()`` picks one task from each
  C1-C4 × P0-P4 cell for the 20-task pilot.
* Pilot manifest has ``experiment_role=PIPELINE_PILOT``.
* A fresh ModelDriver is created per trial via ``model_driver_factory``
  to prevent state leakage between trials.
* Budget exhaustion is classified as ``VALID_TASK_OUTCOME`` (the task
  ran and exhausted its budget — that is a valid experimental outcome),
  not ``INVALID_INFRA``.

This runner does NOT call AgentExecutionHarness or any synthetic
execution path.  It is the real-model execution counterpart to the
dry-run PilotRunner in pilot_runner.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .types import (
    BenchmarkName,
    BudgetConfig,
    ControlArm,
    GenerationConfig,
    NativeResult,
    PerturbationMode,
    RuntimeTask,
    TaskDescriptor,
    TopologyClass,
    TrialStatus,
)
from .toolmaze_adapter import ToolMazeAdapter
from .runtime_backend import ToolMazeRuntimeBackend
from .model_driver import BudgetExhausted
from .artifacts import ArtifactWriter
from .firewall import OfflineGraderFirewall
from .provenance import ProvenanceFreeze, arm_definitions_snapshot
from .control_arms import (
    _STRATEGY_MAP,
    ExperimentPairValidator,
)
from .trial_executor import execute_trial, TrialResult


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")


# ── Stratified sampling ──────────────────────────────────────────────

def select_20_tasks(
    tasks: Sequence[TaskDescriptor],
    *,
    seed: int = 42,
) -> list[TaskDescriptor]:
    """Select 20 tasks: one from each C1-C4 × P0-P4 cell.

    Stratified sampling ensures full coverage of the 20-cell grid
    (4 topology classes × 5 perturbation modes).  Fixed seed ensures
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
                # Log empty cell but don't fail — some cells may be
                # legitimately empty in smaller datasets
                import sys
                print(
                    f"Warning: empty cell ({topo_cls.value}, {perturb_mode.value})",
                    file=sys.stderr,
                )

    return selected

# ── Pilot manifest schema ────────────────────────────────────────────

PILOT_MANIFEST_SCHEMA_VERSION = "phase5-real-pilot-001"


def build_pilot_manifest(
    *,
    experiment_id: str,
    task_ids: list[str],
    arm_definitions: dict[str, dict[str, Any]],
    model_config: GenerationConfig,
    budgets: dict[str, Any],
    benchmark_identity: dict[str, Any],
    seed: int = 42,
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
        "tasks": sorted(task_ids),
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
        "budgets": budgets,
        "root_budget": budgets,
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


# ── RealPilotRunner ──────────────────────────────────────────────────

class RealPilotRunner:
    """Runs real model pilot experiments through the unified backend.

    Uses ``ToolMazeRuntimeBackend.execute()`` as the single execution
    path.  Never calls ``AgentExecutionHarness`` or synthetic execution.

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
            DEPRECATED — use model_driver_factory instead.  If provided,
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
        # Resolve the factory — prefer model_driver_factory
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

        # Tool loader for canonical tool definitions
        _repo = Path(__file__).resolve().parents[3] / "experiments" / "phase5" / "benchmarks" / "toolmaze"
        _tools_dir = _repo / "tools" / "definitions"
        if _tools_dir.is_dir():
            import sys
            if str(_repo) not in sys.path:
                sys.path.insert(0, str(_repo))
            from tools.loader import ToolLoader
            self._tool_loader = ToolLoader(str(_tools_dir))
        else:
            self._tool_loader = None

        # Load or create manifest — handle both schema variants
        self._manifest_path = (
            manifest_path
            or Path("experiments/phase5/manifests/phase5-real-pilot-001.json")
        )
        self._manifest = self._load_manifest()

        self._experiment_id = self._manifest["experiment_id"]

        # FIX: accept both 'selected_task_ids' and 'tasks' keys
        if "selected_task_ids" in self._manifest:
            self._task_ids = self._manifest["selected_task_ids"]
        elif "tasks" in self._manifest:
            # Legacy schema: 'tasks' is a list of task objects or IDs
            raw_tasks = self._manifest["tasks"]
            if raw_tasks and isinstance(raw_tasks[0], dict):
                self._task_ids = [t["task_id"] for t in raw_tasks]
            else:
                self._task_ids = list(raw_tasks)
        else:
            raise ValueError(
                "Manifest missing both 'selected_task_ids' and 'tasks' keys"
            )

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

        # FIX: accept both 'budgets' and 'root_budget' keys
        if "budgets" in self._manifest:
            self._budgets = self._manifest["budgets"]
        elif "root_budget" in self._manifest:
            self._budgets = self._manifest["root_budget"]
        else:
            self._budgets = {
                "max_turns": 30,
                "max_model_calls": 50,
            }

        self._seed = self._manifest.get("seed", 42)

        # Output directory
        self._output_dir = (
            output_dir
            or Path(f"experiments/phase5/runs/{self._experiment_id}")
        )
        self._output_dir.mkdir(parents=True, exist_ok=True)

        # Artifact writer and firewall
        self._artifacts = ArtifactWriter(self._output_dir)
        self._firewall = OfflineGraderFirewall()
        self._pair_validator = ExperimentPairValidator()

    def _load_manifest(self) -> dict[str, Any]:
        """Load the frozen pilot manifest."""
        if self._manifest_path.exists():
            return json.loads(
                self._manifest_path.read_text(encoding="utf-8")
            )
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

    # ── Main execution ────────────────────────────────────────────────

    def run_pilot(
        self,
        *,
        arms: Optional[list[ControlArm]] = None,
        max_rounds: int = 15,
    ) -> dict[str, Any]:
        """Run the full pilot experiment.

        For each task × arm: runs through the official ExecutionEngine
        via ``ToolMazeRuntimeBackend.execute()`` with the appropriate
        ``PolicyStrategy``.  Writes all artifacts per trial.

        A fresh ModelDriver is created per trial via the injected
        ``model_driver_factory`` to prevent state leakage.

        Parameters
        ----------
        arms : list[ControlArm], optional
            Arms to run.  Defaults to all six arms.
        max_rounds : int
            Maximum reasoning rounds per trial.

        Returns
        -------
        dict
            Experiment summary with per-task results, pairing
            validation, and artifact paths.
        """
        if arms is None:
            arms = list(ControlArm)

        # Resolve task descriptors
        all_descriptors = self._adapter.enumerate_tasks()
        task_desc_map = {d.task_id: d for d in all_descriptors}

        results: list[dict[str, Any]] = []
        all_trial_manifests: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        for task_id in self._task_ids:
            desc = task_desc_map.get(task_id)
            if desc is None:
                errors.append({
                    "task_id": task_id,
                    "error": "Task descriptor not found",
                })
                continue

            raw_task = self._get_raw_task(task_id)
            if raw_task is None:
                errors.append({
                    "task_id": task_id,
                    "error": "Raw task JSON not found",
                })
                continue

            # Run all arms for this task
            arm_results: dict[str, dict[str, Any]] = {}
            trial_manifests: list[dict[str, Any]] = []

            for arm in arms:
                trial_id = f"{self._experiment_id}_{task_id}_{arm.value}"

                # Build tool definitions from raw task (canonical)
                tool_names = set()
                for step in raw_task.get("execution_trace", []):
                    if "tool_name" in step:
                        tool_names.add(step["tool_name"])
                tool_definitions = []
                for tn in sorted(tool_names):
                    tool = self._tool_loader.get_tool_by_name(tn)
                    if tool:
                        tool_definitions.append(tool)

                # Build budget from manifest
                budget = BudgetConfig(
                    max_turns=self._budgets.get("max_turns", 30),
                    max_model_calls=self._budgets.get("max_model_calls", 50),
                    token_budget=self._budgets.get("token_budget"),
                    deadline_seconds=self._budgets.get("deadline_seconds"),
                )

                # Fresh model driver per trial
                model_driver = self._model_driver_factory()

                # ── Canonical execution via TrialExecutor ──────────
                try:
                    self._firewall.begin_runtime()

                    trial_result: TrialResult = execute_trial(
                        arm=arm,
                        task_json=raw_task,
                        tool_definitions=tool_definitions,
                        model_driver=model_driver,
                        budget=budget,
                        experiment_id=self._experiment_id,
                        task_id=task_id,
                        max_rounds=max_rounds,
                    )

                    self._firewall.end_runtime()

                    # Build pairing invariants from TrialResult
                    runtime_task = self._adapter.build_runtime_task(desc)
                    invariants = ExperimentPairValidator.compute_trial_invariants(
                        experiment_id=self._experiment_id,
                        trial_id=trial_id,
                        adapter=self._adapter,
                        task=runtime_task,
                        generation_config=self._gen_config,
                        arm=arm,
                        perturbation_mode=desc.perturbation_mode.value,
                        fault_source="BENCHMARK_NATIVE",
                        validator_identity="phase5-real-pilot",
                        offline_grader_identity="toolmaze-judge",
                    )
                    invariants["seed"] = self._seed
                    invariants["environment_snapshot"] = runtime_task.environment_snapshot
                    invariants["strategy_config"] = trial_result.strategy_config
                    invariants["budget_accounting"] = trial_result.provider_usage
                    trial_manifests.append(invariants)

                    arm_results[arm.value] = trial_result.to_dict()

                    # Write canonical artifacts
                    self._write_trial_artifacts(
                        trial_id, desc, invariants, trial_result,
                        raw_task=raw_task,
                    )

                except Exception as e:
                    self._firewall.end_runtime()
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

        # Generate firewall report
        firewall_report = self._firewall.generate_audit_report()

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
            "firewall_audit": (
                firewall_report.model_dump(mode="json")
                if hasattr(firewall_report, "model_dump")
                else firewall_report
            ),
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
        }

        manifest_path = self._output_dir / "experiment_manifest.json"
        manifest_path.write_text(
            json.dumps(experiment_manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # Write paired manifests
        paired_dir = self._output_dir / "paired_manifests"
        paired_dir.mkdir(exist_ok=True)
        for pm in all_trial_manifests:
            path = paired_dir / f"{pm['task_id']}_paired.json"
            path.write_text(
                json.dumps(pm, indent=2), encoding="utf-8",
            )

        # Write error log if any
        if errors:
            errors_path = self._output_dir / "errors.json"
            errors_path.write_text(
                json.dumps(errors, indent=2), encoding="utf-8",
            )

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

    # ── Artifact writing ──────────────────────────────────────────────

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
        trial_dir = self._output_dir / trial_id
        trial_dir.mkdir(parents=True, exist_ok=True)

        # Official trace — verbatim
        (trial_dir / "official_trace.json").write_text(
            json.dumps(trial_result.official_trace, indent=2, default=str, ensure_ascii=False))

        # Derived view — separate
        (trial_dir / "derived_runtime_view.json").write_text(
            json.dumps(trial_result.derived_view, indent=2, default=str, ensure_ascii=False))

        # Recovery decisions
        (trial_dir / "recovery_decisions.json").write_text(
            json.dumps(trial_result.recovery_decisions, indent=2, default=str, ensure_ascii=False))

        # Actual shadow records (not placeholder)
        (trial_dir / "progress_shadow.json").write_text(
            json.dumps(trial_result.shadow_records, indent=2, default=str, ensure_ascii=False))

        # Actual evidence events
        (trial_dir / "evidence.jsonl").write_text(
            "\n".join(json.dumps(e, default=str, ensure_ascii=False)
                       for e in trial_result.evidence_events) if trial_result.evidence_events else "")

        # Recovery budget ledger
        (trial_dir / "recovery_budget_ledger.json").write_text(
            json.dumps(trial_result.recovery_budget_ledger, indent=2, default=str, ensure_ascii=False))

        # Budget/provider usage
        (trial_dir / "budget_ledger.json").write_text(
            json.dumps(trial_result.provider_usage, indent=2, default=str))
        (trial_dir / "provider_usage.json").write_text(
            json.dumps(trial_result.provider_usage, indent=2, default=str))

        # Native judgement + metrics from official grader (verbatim)
        (trial_dir / "native_judgement.json").write_text(
            json.dumps(trial_result.grader_result.get("judgement") or {}, indent=2, default=str, ensure_ascii=False))
        (trial_dir / "native_metrics.json").write_text(
            json.dumps(trial_result.grader_result.get("metrics_report") or
                        trial_result.grader_result.get("metrics_summary") or {},
                        indent=2, default=str, ensure_ascii=False))

        # Validity
        (trial_dir / "validity.json").write_text(
            json.dumps({"validity": trial_result.validity,
                         "termination_reason": trial_result.termination_reason,
                         "grader_error": trial_result.grader_result.get("error"),
                         "error_diagnostics": trial_result.error_diagnostics},
                        indent=2, default=str, ensure_ascii=False))

        # Trial manifest
        (trial_dir / "trial_manifest.json").write_text(
            json.dumps(manifest, indent=2, default=str, ensure_ascii=False))

        # Native result for backward compat with ArtifactWriter
        native = NativeResult(
            tsr=trial_result.grader_result.get("metrics_summary", {}).get("tsr"),
            raw_score=None,
            native_metrics=trial_result.grader_result.get("metrics_report") or {},
        )
        self._artifacts.write_benchmark_result(trial_id, native)

        # Classification
        status = TrialStatus.VALID if trial_result.validity == "VALID" else TrialStatus.INVALID_INFRA
        self._artifacts.classify_trial(trial_id, status)


# ── Pilot manifest generator ─────────────────────────────────────────

def generate_pilot_manifest(
    *,
    experiment_id: str = "phase5-real-pilot-001",
    seed: int = 42,
    model_id: str = "gpt-4o",
    provider: str = "openai",
    temperature: float = 0.0,
    max_turns: int = 30,
    max_model_calls: int = 50,
    output_dir: Optional[Path] = None,
    adapter: Optional[ToolMazeAdapter] = None,
) -> dict[str, Any]:
    """Generate a 20-task pilot manifest with stratified sampling.

    Selects one task from each C1-C4 × P0-P4 cell using a fixed seed
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

    # Stratified sampling: one from each C×P cell
    selected = select_20_tasks(all_tasks, seed=seed)
    task_ids = [t.task_id for t in selected]

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
        "token_budget": None,
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
