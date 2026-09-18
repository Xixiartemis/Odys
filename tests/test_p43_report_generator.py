"""Tests for the benchmark report generator (P43-REPORT-PIPELINE-01)."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from evals.reliability.report_generator import (
    NOT_MEASURED,
    build_summary,
    compute_metrics,
    generate_markdown,
    load_aggregation_input,
    write_reports,
)

# Tolerance for metrics rounded to 6 decimal places
TOL = 1e-5


@pytest.fixture()
def tmp_dir():
    """Create a temporary directory (avoids pytest-asyncio tmp_path issue on Windows)."""
    d = Path(tempfile.mkdtemp(prefix="p43_test_"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Fixtures — synthetic aggregation-input data
# ---------------------------------------------------------------------------

def _make_run(
    *,
    task_id: str = "T1",
    family: str = "famA",
    configuration: str = "minimal",
    validity: str = "VALIDATED_PASS",
    verified_completion: bool = True,
    false_completion: bool = False,
    recovery_required: bool = False,
    recovery_attempted: bool = False,
    recovery_success: bool = False,
    duplicate_side_effect_count: int = 0,
    lost_work_units: int | str = 0,
    model_cost: float | str = NOT_MEASURED,
    **extra,
) -> dict:
    """Build a single synthetic aggregation record."""
    record = {
        "benchmark_version": "phase4-v1",
        "benchmark_run_id": f"{task_id}::{configuration}::repeat-1",
        "task_id": task_id,
        "family": family,
        "repeat_index": 1,
        "configuration": configuration,
        "repo_sha": "abc123",
        "fixture_hash": "fx",
        "manifest_hash": "mh",
        "protocol_hash": "ph",
        "model": "test-model",
        "provider": "test-provider",
        "runtime_environment": {},
        "validator_id": "external-observable-v1",
        "fault_id": "FAULT",
        "fault_type": "test",
        "claimed_complete": verified_completion,
        "verified_completion": verified_completion,
        "false_completion": false_completion,
        "failure_type": None,
        "recovery_required": recovery_required,
        "recovery_attempted": recovery_attempted,
        "recovery_success": recovery_success,
        "repair_scope": None,
        "repair_attempts": 0,
        "replan_count": 0,
        "lost_work_units": lost_work_units,
        "duplicate_side_effect_count": duplicate_side_effect_count,
        "tool_calls": 5,
        "model_calls": 3,
        "attempt_count": 1,
        "tokens_input": 1000,
        "tokens_output": 500,
        "total_tokens": 1500,
        "model_cost": model_cost,
        "tool_cost": 0.0,
        "wall_time_seconds": 10.0,
        "human_intervention": False,
        "validity": validity,
        "invalid_reason": None,
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:01:00Z",
    }
    record.update(extra)
    return record


def _typical_dataset() -> list[dict]:
    """A dataset with known, hand-computable metrics.

    Layout:
      - 6 valid runs (4 VALIDATED_PASS, 2 VALIDATED_FAIL)
      - 1 INVALID_RUN
      - 2 runs with recovery_required=True, 1 recovery_success
      - 1 false_completion
      - 2 runs with duplicate_side_effect_count > 0
      - 1 recovery run with lost_work_units=3
      - 5 runs with numeric model_cost, 1 with NOT_MEASURED

    Expected valid count = 6
    Expected recovery_eligible = 2

    verified_completion_rate = 4/6
    false_completion_rate    = 1/6
    recovery_success_rate    = 1/2
    duplicate_side_effect_rate = 2/6
    lost_work_rate           = 1/2

    cost: sum of numeric costs among valid runs = 0.10+0.20+0.30+0.40+0.50 = 1.50
    verified = 4
    cost_per_verified_completion = 1.50 / 4 = 0.375
    """
    return [
        # Valid, verified, no recovery, cost=0.10
        _make_run(task_id="T1", family="alpha", configuration="minimal",
                  verified_completion=True, model_cost=0.10),
        # Valid, verified, no recovery, cost=0.20
        _make_run(task_id="T2", family="alpha", configuration="minimal",
                  verified_completion=True, model_cost=0.20),
        # Valid, verified, recovery success, cost=0.30, dup side effects=2
        _make_run(task_id="T3", family="beta", configuration="minimal",
                  verified_completion=True, recovery_required=True,
                  recovery_attempted=True, recovery_success=True,
                  duplicate_side_effect_count=2, model_cost=0.30),
        # Valid, verified, recovery fail, cost=0.40, dup side effects=1, lost work=3
        _make_run(task_id="T4", family="beta", configuration="odys_p3",
                  verified_completion=True, recovery_required=True,
                  recovery_attempted=True, recovery_success=False,
                  duplicate_side_effect_count=1, lost_work_units=3,
                  model_cost=0.40),
        # Valid, NOT verified, false_completion, cost=0.50
        _make_run(task_id="T5", family="alpha", configuration="odys_p3",
                  verified_completion=False, false_completion=True,
                  model_cost=0.50),
        # Valid, NOT verified, NOT false_completion, cost=NOT_MEASURED
        _make_run(task_id="T6", family="gamma", configuration="odys_p3",
                  verified_completion=False, model_cost=NOT_MEASURED),
        # Invalid
        _make_run(task_id="T7", family="alpha", configuration="minimal",
                  validity="INVALID_RUN", verified_completion=False,
                  model_cost=NOT_MEASURED, lost_work_units=NOT_MEASURED),
    ]


# ---------------------------------------------------------------------------
# Unit tests: compute_metrics
# ---------------------------------------------------------------------------

class TestComputeMetrics:
    def test_typical_dataset(self):
        runs = _typical_dataset()
        m = compute_metrics(runs)

        assert m["verified_completion_rate"] == pytest.approx(4 / 6, abs=TOL)
        assert m["false_completion_rate"] == pytest.approx(1 / 6, abs=TOL)
        assert m["recovery_success_rate"] == pytest.approx(0.5, abs=TOL)
        assert m["cost_per_verified_completion"] == pytest.approx(0.375, abs=TOL)
        assert m["duplicate_side_effect_rate"] == pytest.approx(2 / 6, abs=TOL)
        assert m["lost_work_rate"] == pytest.approx(0.5, abs=TOL)

    def test_all_verified(self):
        runs = [
            _make_run(task_id=f"T{i}", verified_completion=True, model_cost=float(i))
            for i in range(5)
        ]
        m = compute_metrics(runs)
        assert m["verified_completion_rate"] == 1.0
        assert m["false_completion_rate"] == 0.0
        assert m["cost_per_verified_completion"] == pytest.approx(2.0, abs=TOL)  # (0+1+2+3+4)/5

    def test_no_verified_completions_cost_not_measured(self):
        runs = [
            _make_run(task_id=f"T{i}", verified_completion=False, model_cost=float(i))
            for i in range(3)
        ]
        m = compute_metrics(runs)
        assert m["verified_completion_rate"] == 0.0
        assert m["cost_per_verified_completion"] == NOT_MEASURED

    def test_all_costs_not_measured(self):
        runs = [
            _make_run(task_id=f"T{i}", verified_completion=True, model_cost=NOT_MEASURED)
            for i in range(3)
        ]
        m = compute_metrics(runs)
        assert m["cost_per_verified_completion"] == NOT_MEASURED

    def test_empty_input_returns_defaults(self):
        m = compute_metrics([])
        assert m["verified_completion_rate"] == 0.0
        assert m["false_completion_rate"] == 0.0
        assert m["recovery_success_rate"] == NOT_MEASURED
        assert m["cost_per_verified_completion"] == NOT_MEASURED
        assert m["duplicate_side_effect_rate"] == 0.0
        assert m["lost_work_rate"] == NOT_MEASURED

    def test_all_invalid_runs(self):
        runs = [
            _make_run(task_id=f"T{i}", validity="INVALID_RUN", verified_completion=False)
            for i in range(3)
        ]
        m = compute_metrics(runs)
        assert m["verified_completion_rate"] == 0.0
        assert m["recovery_success_rate"] == NOT_MEASURED  # no recovery-eligible

    def test_no_recovery_eligible_runs(self):
        runs = [
            _make_run(task_id=f"T{i}", recovery_required=False)
            for i in range(3)
        ]
        m = compute_metrics(runs)
        assert m["recovery_success_rate"] == NOT_MEASURED
        assert m["lost_work_rate"] == NOT_MEASURED

    def test_lost_work_not_measured_string_ignored(self):
        """NOT_MEASURED string in lost_work_units should not count as lost work."""
        runs = [
            _make_run(task_id="T1", recovery_required=True, recovery_attempted=True,
                      recovery_success=True, lost_work_units=NOT_MEASURED),
        ]
        m = compute_metrics(runs)
        assert m["lost_work_rate"] == 0.0  # no numeric lost work

    def test_lost_work_zero_not_counted(self):
        runs = [
            _make_run(task_id="T1", recovery_required=True, lost_work_units=0),
        ]
        m = compute_metrics(runs)
        assert m["lost_work_rate"] == 0.0


# ---------------------------------------------------------------------------
# Unit tests: build_summary structure
# ---------------------------------------------------------------------------

class TestBuildSummary:
    def test_top_level_keys(self):
        summary = build_summary(_typical_dataset(), benchmark_version="phase4-v1", protocol_hash="abc")
        assert "verified_completion_rate" in summary
        assert "false_completion_rate" in summary
        assert "recovery_success_rate" in summary
        assert "cost_per_verified_completion" in summary
        assert "duplicate_side_effect_rate" in summary
        assert "lost_work_rate" in summary
        assert "per_family" in summary
        assert "per_config" in summary
        assert "run_counts" in summary
        assert "metadata" in summary

    def test_run_counts(self):
        runs = _typical_dataset()
        summary = build_summary(runs)
        rc = summary["run_counts"]
        assert rc["total"] == 7
        assert rc["valid"] == 6
        assert rc["invalid"] == 1

    def test_per_family_has_expected_keys(self):
        summary = build_summary(_typical_dataset())
        assert set(summary["per_family"].keys()) == {"alpha", "beta", "gamma"}

    def test_per_config_has_expected_keys(self):
        summary = build_summary(_typical_dataset())
        assert set(summary["per_config"].keys()) == {"minimal", "odys_p3"}

    def test_metadata_fields(self):
        summary = build_summary(
            _typical_dataset(),
            benchmark_version="phase4-v1",
            protocol_hash="deadbeef",
        )
        meta = summary["metadata"]
        assert meta["benchmark_version"] == "phase4-v1"
        assert meta["protocol_hash"] == "deadbeef"
        assert "generated_at" in meta


# ---------------------------------------------------------------------------
# Unit tests: generate_markdown
# ---------------------------------------------------------------------------

class TestGenerateMarkdown:
    def test_contains_title(self):
        summary = build_summary(_typical_dataset(), protocol_hash="abc123")
        md = generate_markdown(summary)
        assert "# ODYS Phase 4 Benchmark Report" in md

    def test_contains_protocol_hash(self):
        summary = build_summary(_typical_dataset(), protocol_hash="abc123")
        md = generate_markdown(summary)
        assert "`abc123`" in md

    def test_contains_all_metrics(self):
        summary = build_summary(_typical_dataset())
        md = generate_markdown(summary)
        for label in [
            "Verified Completion Rate",
            "False Completion Rate",
            "Recovery Success Rate",
            "Cost per Verified Completion",
            "Duplicate Side-Effect Rate",
            "Lost Work Rate",
        ]:
            assert label in md, f"missing metric: {label}"

    def test_contains_family_names(self):
        summary = build_summary(_typical_dataset())
        md = generate_markdown(summary)
        assert "alpha" in md
        assert "beta" in md
        assert "gamma" in md

    def test_contains_config_names(self):
        summary = build_summary(_typical_dataset())
        md = generate_markdown(summary)
        assert "minimal" in md
        assert "odys_p3" in md

    def test_run_counts_table(self):
        summary = build_summary(_typical_dataset())
        md = generate_markdown(summary)
        assert "| Total runs | 7 |" in md
        assert "| Valid runs | 6 |" in md
        assert "| Invalid runs | 1 |" in md

    def test_not_measured_displayed(self):
        summary = build_summary([
            _make_run(verified_completion=False, model_cost=NOT_MEASURED),
        ])
        md = generate_markdown(summary)
        assert "NOT_MEASURED" in md

    def test_percentage_formatting(self):
        summary = build_summary([
            _make_run(verified_completion=True, model_cost=1.0),
            _make_run(verified_completion=True, model_cost=1.0),
            _make_run(verified_completion=False, model_cost=1.0),
        ])
        md = generate_markdown(summary)
        # 2/3 = 66.7%
        assert "66.7%" in md


# ---------------------------------------------------------------------------
# Integration tests: write_reports (file I/O)
# ---------------------------------------------------------------------------

class TestWriteReports:
    def test_creates_both_files(self, tmp_dir: Path):
        input_file = tmp_dir / "aggregation-input.jsonl"
        lines = [json.dumps(r, sort_keys=True) for r in _typical_dataset()]
        input_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        output_dir = tmp_dir / "reports"
        paths = write_reports(input_file, output_dir, protocol_hash="testhash")

        assert paths["md"].exists()
        assert paths["json"].exists()
        assert paths["md"].name == "benchmark-report.md"
        assert paths["json"].name == "benchmark-summary.json"

    def test_json_is_valid_json(self, tmp_dir: Path):
        input_file = tmp_dir / "aggregation-input.jsonl"
        lines = [json.dumps(r, sort_keys=True) for r in _typical_dataset()]
        input_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        output_dir = tmp_dir / "reports"
        paths = write_reports(input_file, output_dir)
        data = json.loads(paths["json"].read_text(encoding="utf-8"))
        assert "verified_completion_rate" in data
        assert data["run_counts"]["total"] == 7

    def test_empty_input(self, tmp_dir: Path):
        input_file = tmp_dir / "aggregation-input.jsonl"
        input_file.write_text("", encoding="utf-8")

        output_dir = tmp_dir / "reports"
        paths = write_reports(input_file, output_dir)
        data = json.loads(paths["json"].read_text(encoding="utf-8"))
        assert data["run_counts"]["total"] == 0
        assert data["verified_completion_rate"] == 0.0

    def test_all_invalid(self, tmp_dir: Path):
        input_file = tmp_dir / "aggregation-input.jsonl"
        runs = [_make_run(task_id=f"T{i}", validity="INVALID_RUN", verified_completion=False)
                for i in range(5)]
        lines = [json.dumps(r, sort_keys=True) for r in runs]
        input_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        output_dir = tmp_dir / "reports"
        paths = write_reports(input_file, output_dir)
        data = json.loads(paths["json"].read_text(encoding="utf-8"))
        assert data["run_counts"]["invalid"] == 5
        assert data["run_counts"]["valid"] == 0
        assert data["verified_completion_rate"] == 0.0

    def test_protocol_hash_embedded(self, tmp_dir: Path):
        input_file = tmp_dir / "aggregation-input.jsonl"
        input_file.write_text(
            json.dumps(_typical_dataset()[0], sort_keys=True) + "\n",
            encoding="utf-8",
        )
        output_dir = tmp_dir / "reports"
        paths = write_reports(input_file, output_dir, protocol_hash="DEADBEEF")
        data = json.loads(paths["json"].read_text(encoding="utf-8"))
        assert data["metadata"]["protocol_hash"] == "DEADBEEF"
        md = paths["md"].read_text(encoding="utf-8")
        assert "DEADBEEF" in md


# ---------------------------------------------------------------------------
# Unit tests: load_aggregation_input
# ---------------------------------------------------------------------------

class TestLoadAggregationInput:
    def test_parses_jsonl(self, tmp_dir: Path):
        input_file = tmp_dir / "input.jsonl"
        r1 = _make_run(task_id="A")
        r2 = _make_run(task_id="B")
        input_file.write_text(
            json.dumps(r1, sort_keys=True) + "\n" + json.dumps(r2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        records = load_aggregation_input(input_file)
        assert len(records) == 2
        assert records[0]["task_id"] == "A"
        assert records[1]["task_id"] == "B"

    def test_skips_blank_lines(self, tmp_dir: Path):
        input_file = tmp_dir / "input.jsonl"
        input_file.write_text(
            json.dumps(_make_run(task_id="X"), sort_keys=True) + "\n\n\n",
            encoding="utf-8",
        )
        records = load_aggregation_input(input_file)
        assert len(records) == 1

    def test_empty_file(self, tmp_dir: Path):
        input_file = tmp_dir / "input.jsonl"
        input_file.write_text("", encoding="utf-8")
        records = load_aggregation_input(input_file)
        assert records == []


# ---------------------------------------------------------------------------
# Edge case: mixed config dataset (minimal vs odys_p3)
# ---------------------------------------------------------------------------

class TestPerConfigComparison:
    def test_minimal_vs_odys_p3_metrics_differ(self):
        """Ensure per-config breakdown produces distinct values."""
        runs = [
            # minimal: 2 verified, 1 false, cost 0.10+0.20
            _make_run(task_id="T1", configuration="minimal", verified_completion=True, model_cost=0.10),
            _make_run(task_id="T2", configuration="minimal", verified_completion=True, model_cost=0.20),
            _make_run(task_id="T3", configuration="minimal", verified_completion=False,
                      false_completion=True, model_cost=0.0),
            # odys_p3: 1 verified, 0 false, cost 0.50
            _make_run(task_id="T4", configuration="odys_p3", verified_completion=True, model_cost=0.50),
            _make_run(task_id="T5", configuration="odys_p3", verified_completion=False, model_cost=0.0),
        ]
        summary = build_summary(runs)
        min_m = summary["per_config"]["minimal"]
        odys_m = summary["per_config"]["odys_p3"]

        assert min_m["verified_completion_rate"] == pytest.approx(2 / 3, abs=TOL)
        assert odys_m["verified_completion_rate"] == pytest.approx(0.5, abs=TOL)
        assert min_m["false_completion_rate"] == pytest.approx(1 / 3, abs=TOL)
        assert odys_m["false_completion_rate"] == pytest.approx(0.0, abs=TOL)
        assert min_m["cost_per_verified_completion"] == pytest.approx(0.15, abs=TOL)
        assert odys_m["cost_per_verified_completion"] == pytest.approx(0.5, abs=TOL)
