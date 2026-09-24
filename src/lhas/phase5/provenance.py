"""Benchmark Provenance Freeze.

Before running any model, records exact:
  - repository URL
  - commit SHA / release
  - dataset digest
  - evaluator digest
  - selected task IDs
  - model exact ID
  - generation configuration
  - arm definitions
  - budgets

Writes an immutable manifest hash.  If benchmark source changes,
a new experiment revision must be created (never silently update).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .types import (
    BenchmarkIdentity,
    BudgetConfig,
    ControlArm,
    GenerationConfig,
)


def canonical_json(value: Any) -> bytes:
    """Deterministic JSON serialization for hashing."""
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def compute_manifest_hash(data: dict[str, Any]) -> str:
    """Compute SHA-256 hash of canonical JSON."""
    return hashlib.sha256(canonical_json(data)).hexdigest()


class ProvenanceFreeze:
    """Records and verifies benchmark provenance before execution.

    The frozen manifest is immutable — if any input changes, a new
    experiment revision must be created.
    """

    def __init__(self, storage_path: str | Path):
        self._storage = Path(storage_path)
        self._storage.mkdir(parents=True, exist_ok=True)

    def freeze(
        self,
        *,
        experiment_id: str,
        benchmark_identity: BenchmarkIdentity,
        selected_task_ids: list[str],
        generation_config: GenerationConfig,
        arm_definitions: dict[str, dict[str, Any]],
        budgets: dict[str, Any],
        extra_metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Create an immutable provenance manifest.

        Returns the manifest dict with its hash.
        """
        manifest = {
            "experiment_id": experiment_id,
            "frozen_at": datetime.now(timezone.utc).isoformat(),
            "benchmark": {
                "name": benchmark_identity.benchmark_name.value,
                "revision": benchmark_identity.benchmark_revision,
                "repository_url": benchmark_identity.repository_url,
                "commit_sha": benchmark_identity.commit_sha,
                "dataset_digest": benchmark_identity.dataset_digest,
                "evaluator_digest": benchmark_identity.evaluator_digest,
            },
            "selected_task_ids": sorted(selected_task_ids),
            "generation_config": generation_config.model_dump(mode="json"),
            "arm_definitions": arm_definitions,
            "budgets": budgets,
            "extra_metadata": extra_metadata or {},
        }
        manifest["manifest_hash"] = compute_manifest_hash(manifest)

        # Write immutable file
        path = self._storage / f"provenance_{experiment_id}.json"
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("manifest_hash") != manifest["manifest_hash"]:
                raise ValueError(
                    f"Provenance drift detected for {experiment_id}. "
                    f"Existing hash: {existing.get('manifest_hash')}, "
                    f"New hash: {manifest['manifest_hash']}. "
                    f"Create a new experiment revision instead."
                )
            return existing

        path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return manifest

    def verify(self, experiment_id: str) -> dict[str, Any]:
        """Verify that a frozen manifest has not been tampered with.

        Returns the manifest if valid, raises if corrupted.
        """
        path = self._storage / f"provenance_{experiment_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"No provenance manifest for {experiment_id}")

        manifest = json.loads(path.read_text(encoding="utf-8"))
        stored_hash = manifest.pop("manifest_hash")
        computed_hash = compute_manifest_hash(manifest)
        manifest["manifest_hash"] = stored_hash

        if stored_hash != computed_hash:
            raise ValueError(
                f"Provenance integrity violation for {experiment_id}. "
                f"Stored: {stored_hash}, Computed: {computed_hash}"
            )
        return manifest

    def list_frozen(self) -> list[str]:
        """List all frozen experiment IDs."""
        return sorted(
            p.stem.replace("provenance_", "")
            for p in self._storage.glob("provenance_*.json")
        )


def arm_definitions_snapshot() -> dict[str, dict[str, Any]]:
    """Standard arm definitions for the 6-arm study."""
    return {
        arm.value: {
            "arm": arm.value,
            "description": _ARM_DESCRIPTIONS[arm],
            "recovery_enabled": arm not in {ControlArm.A0_BARE, ControlArm.A1_RETRY_ONLY, ControlArm.A2_VALIDATOR_ONLY},
            "validator_enabled": arm not in {ControlArm.A0_BARE, ControlArm.A1_RETRY_ONLY},
            "observable_progress_enabled": arm not in {
                ControlArm.A0_BARE, ControlArm.A1_RETRY_ONLY,
                ControlArm.A2_VALIDATOR_ONLY, ControlArm.A4_ODYS_MINUS_OBSERVABLE_PROGRESS,
            },
            "recovery_budget_policy_enabled": arm not in {
                ControlArm.A0_BARE, ControlArm.A1_RETRY_ONLY,
                ControlArm.A2_VALIDATOR_ONLY, ControlArm.A5_ODYS_MINUS_RECOVERY_BUDGET_POLICY,
            },
        }
        for arm in ControlArm
    }


_ARM_DESCRIPTIONS: dict[ControlArm, str] = {
    ControlArm.A0_BARE: "No recovery, no validation. Pure single-pass execution.",
    ControlArm.A1_RETRY_ONLY: "Retry on failure, no validator, no observable progress.",
    ControlArm.A2_VALIDATOR_ONLY: "Validator present, no recovery policy.",
    ControlArm.A3_ODYS_FULL: "Full Odys recovery + validation + observable progress.",
    ControlArm.A4_ODYS_MINUS_OBSERVABLE_PROGRESS: "Odys Full minus Observable Progress authority.",
    ControlArm.A5_ODYS_MINUS_RECOVERY_BUDGET_POLICY: "Odys Full minus Recovery Budget Policy.",
}
