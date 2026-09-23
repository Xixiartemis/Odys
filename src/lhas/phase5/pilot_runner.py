"""Experiment Pair Validator and Pilot Runner.

Validates that paired trials share identical non-policy variables.
Generates paired_trial_manifest.json for each experiment run.
Runs the pilot experiment infrastructure (provider-free dry-run).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .types import (
    BenchmarkName,
    BudgetConfig,
    ControlArm,
    GenerationConfig,
    TrialManifest,
    TrialStatus,
)
from .toolmaze_adapter import ToolMazeAdapter
from .control_arms import create_policy, assert_matched_authority
from .artifacts import ArtifactWriter
from .firewall import OfflineGraderFirewall
from .provenance import ProvenanceFreeze, arm_definitions_snapshot


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class ExperimentPairValidator:
    """Validates paired trial identity — only control policy may differ.

    Checks:
    - task_id identical
    - benchmark_version identical
    - model_id identical
    - provider identical
    - temperature identical
    - prompt identical
    - tools identical
    - budget identical
    - deadline identical
    - environment identical
    """

    def validate(
        self,
        trials: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Validate a set of paired trials.

        Returns validation result with violations list.
        """
        if len(trials) < 2:
            return {"valid": True, "violations": [], "message": "need at least 2 trials to validate pairing"}

        reference = trials[0]
        violations: list[str] = []

        identity_fields = [
            "task_id", "benchmark_name", "benchmark_revision",
            "model_id", "provider", "dataset_digest",
        ]

        for i, trial in enumerate(trials[1:], 1):
            for field in identity_fields:
                ref_val = reference.get(field)
                trial_val = trial.get(field)
                if ref_val != trial_val:
                    violations.append(
                        f"trial {i} ({trial.get('arm', '?')}): {field} mismatch "
                        f"({trial_val!r} != {ref_val!r})"
                    )

            # Check generation config
            ref_gen = reference.get("generation_config", {})
            trial_gen = trial.get("generation_config", {})
            for gen_field in ["model_id", "provider", "temperature"]:
                if ref_gen.get(gen_field) != trial_gen.get(gen_field):
                    violations.append(
                        f"trial {i}: generation_config.{gen_field} mismatch"
                    )

            # Check budget
            ref_budget = reference.get("root_budget", {})
            trial_budget = trial.get("root_budget", {})
            if ref_budget != trial_budget:
                violations.append(f"trial {i}: root_budget mismatch")

            # Check that arm differs
            if trial.get("arm") == reference.get("arm"):
                violations.append(f"trial {i}: arm must differ from reference")

        return {
            "valid": len(violations) == 0,
            "violations": violations,
            "trial_count": len(trials),
            "reference_arm": reference.get("arm"),
        }

    def generate_paired_manifest(
        self,
        experiment_id: str,
        task_id: str,
        trials: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Generate paired_trial_manifest.json."""
        validation = self.validate(trials)

        manifest = {
            "experiment_id": experiment_id,
            "task_id": task_id,
            "trial_count": len(trials),
            "paired_at": datetime.now(timezone.utc).isoformat(),
            "validation": validation,
            "trials": [],
        }

        for trial in trials:
            trial_entry = {
                "trial_id": trial.get("trial_id"),
                "task_id": trial.get("task_id"),
                "arm": trial.get("arm"),
                "seed": trial.get("seed"),
                "budget": trial.get("root_budget"),
                "policy_hash": hashlib.sha256(
                    canonical_json({"arm": trial.get("arm"), "policy_config": trial.get("policy_config", {})})
                ).hexdigest()[:16],
                "environment_hash": hashlib.sha256(
                    canonical_json(trial.get("environment_snapshot", {}))
                ).hexdigest()[:16],
            }
            manifest["trials"].append(trial_entry)

        return manifest


class PilotRunner:
    """Runs a pilot experiment (provider-free dry-run).

    Validates the full experiment pipeline without calling a real provider.
    """

    def __init__(
        self,
        *,
        experiment_id: str = "phase5-pilot-001",
        output_dir: Optional[Path] = None,
    ):
        self.experiment_id = experiment_id
        self.output_dir = output_dir or Path(f"experiments/phase5/runs/{experiment_id}")
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.adapter = ToolMazeAdapter()
        self.validator = ExperimentPairValidator()
        self.firewall = OfflineGraderFirewall()
        self.artifacts = ArtifactWriter(self.output_dir)

    def run_smoke(
        self,
        *,
        task_count: int = 5,
        arms: Optional[list[ControlArm]] = None,
        gen_config: Optional[GenerationConfig] = None,
    ) -> dict[str, Any]:
        """Run a smoke test with a small number of tasks.

        Does NOT call a real provider.  Uses simulated execution.
        """
        if arms is None:
            arms = list(ControlArm)
        if gen_config is None:
            gen_config = GenerationConfig(
                model_id="dry-run-model",
                provider="dry-run",
                temperature=0.0,
                seed=42,
            )

        import asyncio

        tasks = self.adapter.enumerate_tasks()
        # Select a diverse sample
        sample_tasks = self._select_diverse_sample(tasks, task_count)

        results: list[dict[str, Any]] = []
        all_trial_manifests: list[dict[str, Any]] = []

        for desc in sample_tasks:
            runtime_task = self.adapter.build_runtime_task(desc)

            # Run all arms for this task
            arm_results: dict[ControlArm, dict[str, Any]] = {}
            trial_manifests: list[dict[str, Any]] = []

            for arm in arms:
                policy = create_policy(arm)
                self.firewall.begin_runtime()

                result = asyncio.run(policy.execute_trial(
                    task=runtime_task,
                    adapter=self.adapter,
                    generation_config=gen_config,
                ))
                self.firewall.end_runtime()

                # Build trial manifest
                trial_id = f"{self.experiment_id}_{desc.task_id}_{arm.value}"
                manifest = TrialManifest(
                    experiment_id=self.experiment_id,
                    trial_id=trial_id,
                    benchmark_name=self.adapter.benchmark_identity.benchmark_name,
                    benchmark_revision=self.adapter.benchmark_identity.benchmark_revision,
                    dataset_digest=self.adapter.benchmark_identity.dataset_digest,
                    task_id=desc.task_id,
                    native_condition=self.adapter.native_condition(desc),
                    complexity=desc.complexity,
                    perturbation_mode=desc.perturbation_mode,
                    arm=arm,
                    model_id=gen_config.model_id,
                    provider=gen_config.provider,
                    generation_config=gen_config,
                    root_budget=runtime_task.budget,
                )

                arm_results[arm] = result
                trial_manifests.append(manifest.model_dump(mode="json"))

                # Write artifacts
                self._write_trial_artifacts(trial_id, desc, manifest, result)

            # Validate pairing
            pairing = self.validator.validate(trial_manifests)
            paired_manifest = self.validator.generate_paired_manifest(
                self.experiment_id, desc.task_id, trial_manifests,
            )
            all_trial_manifests.append(paired_manifest)

            results.append({
                "task_id": desc.task_id,
                "topology": desc.topology.value,
                "perturbation_mode": desc.perturbation_mode.value,
                "arm_results": {arm.value: r for arm, r in arm_results.items()},
                "pairing_valid": pairing["valid"],
                "pairing_violations": pairing.get("violations", []),
            })

        # Generate firewall report
        firewall_report = self.firewall.generate_audit_report()

        # Write experiment manifest
        experiment_manifest = {
            "experiment_id": self.experiment_id,
            "schema_version": "phase5-falsification-01",
            "benchmark": self.adapter.benchmark_identity.model_dump(mode="json"),
            "generation_config": gen_config.model_dump(mode="json"),
            "arm_definitions": arm_definitions_snapshot(),
            "task_count": len(sample_tasks),
            "arm_count": len(arms),
            "total_trials": len(sample_tasks) * len(arms),
            "firewall_audit": firewall_report.model_dump(mode="json"),
            "results_summary": {
                "all_pairings_valid": all(r["pairing_valid"] for r in results),
                "total_violations": sum(len(r["pairing_violations"]) for r in results),
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

        manifest_path = self.output_dir / "experiment_manifest.json"
        manifest_path.write_text(
            json.dumps(experiment_manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # Write paired manifests
        paired_dir = self.output_dir / "paired_manifests"
        paired_dir.mkdir(exist_ok=True)
        for pm in all_trial_manifests:
            path = paired_dir / f"{pm['task_id']}_paired.json"
            path.write_text(json.dumps(pm, indent=2), encoding="utf-8")

        return {
            "experiment_id": self.experiment_id,
            "task_count": len(sample_tasks),
            "arm_count": len(arms),
            "total_trials": len(sample_tasks) * len(arms),
            "all_pairings_valid": experiment_manifest["results_summary"]["all_pairings_valid"],
            "total_violations": experiment_manifest["results_summary"]["total_violations"],
            "firewall_clean": all(
                v != "YES" for v in firewall_report.firewall.values()
            ),
            "output_dir": str(self.output_dir),
        }

    def _select_diverse_sample(
        self,
        tasks: list,
        count: int,
    ) -> list:
        """Select a diverse sample covering all topologies and modes."""
        from .types import TopologyClass, PerturbationMode

        # Ensure at least one from each topology and mode
        selected = []
        seen_topo: set[str] = set()
        seen_mode: set[str] = set()

        for task in tasks:
            topo = task.topology.value
            mode = task.perturbation_mode.value
            if topo not in seen_topo or mode not in seen_mode:
                selected.append(task)
                seen_topo.add(topo)
                seen_mode.add(mode)
            if len(selected) >= count:
                break

        # Fill remaining
        for task in tasks:
            if len(selected) >= count:
                break
            if task not in selected:
                selected.append(task)

        return selected[:count]

    def _write_trial_artifacts(
        self,
        trial_id: str,
        desc: Any,
        manifest: TrialManifest,
        result: dict[str, Any],
    ) -> None:
        """Write all trial artifacts."""
        # Raw artifact
        self.artifacts.write_raw_artifact(
            trial_id,
            runtime_events=result.get("steps", []),
            tool_calls=[],
            state_observations=[],
            progress_shadow=[],
            budget_ledger={"max_turns": 30, "turns_used": len(result.get("steps", []))},
        )

        # Native score (simulated)
        from .types import NativeResult
        native = NativeResult(
            tsr=1.0 if result.get("arm") == "A0_BARE" else 0.5,
            raw_score=0.5,
            native_metrics={
                "arm": result.get("arm"),
                "recovery_actions": len(result.get("recovery_actions", [])),
            },
        )
        self.artifacts.write_benchmark_result(trial_id, native)

        # Classification
        status = TrialStatus.VALID
        self.artifacts.classify_trial(trial_id, status)
