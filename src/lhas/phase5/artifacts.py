"""Artifact schema management for Phase 5 falsification harness.

Directory structure:
    results/phase5-falsification-01/
        manifest.json
        raw/<trial_id>/
        benchmark/<trial_id>/
        derived/<trial_id>/
        audits/
        analysis/

Raw runtime artifacts must never contain hidden ground truth.
Failed runs are never deleted — classified as VALID, INVALID_INFRA,
or EXCLUDED_<reason>.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .types import (
    AuditReport,
    BenchmarkName,
    ControlArm,
    DerivedMetrics,
    FaultSource,
    GenerationConfig,
    NativeResult,
    PerturbationMode,
    BudgetConfig,
    TrialManifest,
    TrialStatus,
)


ARTIFACT_SCHEMA_VERSION = "phase5-falsification-01"


class ArtifactWriter:
    """Writes trial artifacts to the standard directory structure.

    Raw artifacts never contain hidden ground truth.
    """

    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        for subdir in ["raw", "benchmark", "derived", "audits", "analysis"]:
            (self.base_dir / subdir).mkdir(parents=True, exist_ok=True)

    def write_manifest(self, manifest: TrialManifest) -> Path:
        """Write the top-level experiment manifest."""
        path = self.base_dir / "manifest.json"
        data = manifest.model_dump(mode="json")
        data["_schema_version"] = ARTIFACT_SCHEMA_VERSION
        data["_written_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def write_raw_artifact(
        self,
        trial_id: str,
        runtime_events: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]],
        state_observations: list[dict[str, Any]],
        progress_shadow: list[dict[str, Any]],
        budget_ledger: dict[str, Any],
    ) -> Path:
        """Write raw runtime artifacts.  NO hidden labels allowed."""
        trial_dir = self.base_dir / "raw" / trial_id
        trial_dir.mkdir(parents=True, exist_ok=True)

        artifact = {
            "trial_id": trial_id,
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "runtime_events": runtime_events,
            "tool_calls": tool_calls,
            "state_observations": state_observations,
            "progress_shadow": progress_shadow,
            "budget_ledger": budget_ledger,
        }
        path = trial_dir / "raw_artifact.json"
        path.write_text(
            json.dumps(artifact, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def write_benchmark_result(
        self,
        trial_id: str,
        native_result: NativeResult,
    ) -> Path:
        """Write native benchmark result."""
        trial_dir = self.base_dir / "benchmark" / trial_id
        trial_dir.mkdir(parents=True, exist_ok=True)

        path = trial_dir / "native_result.json"
        path.write_text(
            json.dumps(native_result.model_dump(mode="json"), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def write_derived_metrics(
        self,
        trial_id: str,
        derived: DerivedMetrics,
    ) -> Path:
        """Write Phase5-derived metrics.  Always labeled as derived."""
        trial_dir = self.base_dir / "derived" / trial_id
        trial_dir.mkdir(parents=True, exist_ok=True)

        data = derived.model_dump(mode="json")
        data["_label"] = "PHASE5_DERIVED_METRIC"
        data["_not_native_benchmark_score"] = True

        path = trial_dir / "derived_metrics.json"
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def write_audit(
        self,
        audit_name: str,
        report: AuditReport,
    ) -> Path:
        """Write an audit report."""
        path = self.base_dir / "audits" / f"{audit_name}.json"
        path.write_text(
            json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def classify_trial(
        self,
        trial_id: str,
        status: TrialStatus,
        reason: Optional[str] = None,
    ) -> Path:
        """Classify a trial as VALID, INVALID_INFRA, or EXCLUDED."""
        trial_dir = self.base_dir / "raw" / trial_id
        trial_dir.mkdir(parents=True, exist_ok=True)

        classification = {
            "trial_id": trial_id,
            "status": status.value,
            "reason": reason,
            "classified_at": datetime.now(timezone.utc).isoformat(),
        }
        path = trial_dir / "classification.json"
        path.write_text(
            json.dumps(classification, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def load_raw_artifact(self, trial_id: str) -> Optional[dict[str, Any]]:
        """Load a raw artifact for analysis."""
        path = self.base_dir / "raw" / trial_id / "raw_artifact.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_trials(self) -> list[str]:
        """List all trial IDs with raw artifacts."""
        raw_dir = self.base_dir / "raw"
        if not raw_dir.exists():
            return []
        return sorted(
            d.name for d in raw_dir.iterdir()
            if d.is_dir() and (d / "raw_artifact.json").exists()
        )
