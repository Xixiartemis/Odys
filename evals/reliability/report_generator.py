"""Benchmark report generator for phase4-v1 aggregation data.

Reads ``aggregation-input.jsonl`` (produced by :class:`Phase4Runner`'s
:class:`ResultWriter`) and produces two outputs:

* ``benchmark-report.md`` — human-readable markdown report
* ``benchmark-summary.json`` — machine-readable JSON summary

Usage::

    uv run python -m evals.reliability.report_generator \
        --input artifacts/phase4/phase4-v1/aggregation-input.jsonl \
        --output-dir artifacts/phase4/phase4-v1/reports
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

NOT_MEASURED = "NOT_MEASURED"

# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------

def _safe_divide(numerator: int, denominator: int, *, not_measured: bool = False) -> float | str:
    """Return numerator/denominator, or NOT_MEASURED / 0.0 when denominator is zero."""
    if denominator == 0:
        return NOT_MEASURED if not_measured else 0.0
    return round(numerator / denominator, 6)


def compute_metrics(runs: list[dict[str, Any]]) -> dict[str, float | str]:
    """Compute the six primary benchmark metrics from a list of result records.

    Parameters
    ----------
    runs:
        Parsed JSON objects from aggregation-input.jsonl.

    Returns
    -------
    dict with the six historical metrics plus the P410 semantic metrics.
    """
    valid_runs = [r for r in runs if r.get("validity") in ("VALIDATED_PASS", "VALIDATED_FAIL")]

    def recovery_required_after_validation(record: dict[str, Any]) -> bool:
        environment = record.get("runtime_environment")
        if isinstance(environment, dict):
            recovery = environment.get("recovery")
            if isinstance(recovery, dict) and "recovery_required_after_validation" in recovery:
                return recovery.get("recovery_required_after_validation") is True
            validation = environment.get("validation")
            if (
                isinstance(validation, dict)
                and validation.get("acceptance_status") == "ACCEPTED"
                and record.get("recovery_attempted") is not True
            ):
                return False
        # Backwards-compatible interpretation for records created before the
        # validation-boundary field existed.
        return record.get("recovery_required") is True

    recovery_eligible = [r for r in valid_runs if recovery_required_after_validation(r)]
    verified = [r for r in valid_runs if r.get("verified_completion") is True]
    def validation_identity(record: dict[str, Any]) -> dict[str, Any]:
        environment = record.get("runtime_environment")
        value = environment.get("validation") if isinstance(environment, dict) else None
        return value if isinstance(value, dict) else {}

    def false_completion_detected(record: dict[str, Any]) -> bool:
        return bool(
            record.get("false_completion_detected") is True
            or validation_identity(record).get("false_completion_detected") is True
            or record.get("false_completion") is True
        )

    false_positive = [r for r in valid_runs if false_completion_detected(r)]
    recovery_attempted = [r for r in valid_runs if r.get("recovery_attempted") is True]
    recovery_successes = [r for r in recovery_attempted if r.get("recovery_success") is True]
    dup_side_effects = [r for r in valid_runs if (r.get("duplicate_side_effect_count") or 0) > 0]

    # Lost work: recovery-eligible runs with lost_work_units > 0 (numeric)
    lost_work_runs = []
    for r in recovery_eligible:
        lwu = r.get("lost_work_units")
        if isinstance(lwu, (int, float)) and lwu > 0:
            lost_work_runs.append(r)

    # Cost aggregation
    total_cost: float | None = None
    for r in valid_runs:
        mc = r.get("model_cost")
        if isinstance(mc, (int, float)):
            total_cost = (total_cost or 0.0) + float(mc)

    if total_cost is not None and len(verified) > 0:
        cost_per_verified = round(total_cost / len(verified), 6)
    else:
        cost_per_verified = NOT_MEASURED

    return {
        "verified_completion_rate": _safe_divide(len(verified), len(valid_runs)),
        "false_completion_rate": _safe_divide(len(false_positive), len(valid_runs)),
        "false_completion_detected_rate": _safe_divide(len(false_positive), len(valid_runs)),
        "recovery_execution_rate": _safe_divide(len(recovery_attempted), len(recovery_eligible), not_measured=True),
        "recovery_success_rate": _safe_divide(len(recovery_successes), len(recovery_attempted), not_measured=True),
        "cost_per_verified_completion": cost_per_verified,
        "duplicate_side_effect_rate": _safe_divide(len(dup_side_effects), len(valid_runs)),
        "lost_work_rate": _safe_divide(len(lost_work_runs), len(recovery_eligible), not_measured=True),
    }


def _metric_subset(runs: list[dict[str, Any]]) -> dict[str, float | str]:
    """Compute metrics for a subset of runs (family or config)."""
    return compute_metrics(runs)


def build_summary(
    runs: list[dict[str, Any]],
    *,
    benchmark_version: str = "phase4-v1",
    protocol_hash: str = "",
) -> dict[str, Any]:
    """Build the full machine-readable summary dict."""
    all_metrics = compute_metrics(runs)

    # Per-family breakdown
    families: dict[str, list[dict[str, Any]]] = {}
    for r in runs:
        families.setdefault(r.get("family", "unknown"), []).append(r)
    per_family = {name: _metric_subset(subset) for name, subset in sorted(families.items())}

    # Per-configuration breakdown
    configs: dict[str, list[dict[str, Any]]] = {}
    for r in runs:
        configs.setdefault(r.get("configuration", "unknown"), []).append(r)
    per_config = {name: _metric_subset(subset) for name, subset in sorted(configs.items())}

    # Run counts
    total = len(runs)
    valid = sum(1 for r in runs if r.get("validity") in ("VALIDATED_PASS", "VALIDATED_FAIL"))
    invalid = sum(1 for r in runs if r.get("validity") == "INVALID_RUN")

    return {
        **all_metrics,
        "per_family": per_family,
        "per_config": per_config,
        "run_counts": {"total": total, "valid": valid, "invalid": invalid},
        "metadata": {
            "benchmark_version": benchmark_version,
            "protocol_hash": protocol_hash,
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    }


# ---------------------------------------------------------------------------
# Markdown report generation
# ---------------------------------------------------------------------------

def _fmt_metric(value: float | str) -> str:
    """Format a metric for display in markdown."""
    if value == NOT_MEASURED:
        return "NOT_MEASURED"
    if isinstance(value, float):
        if 0.0 <= value <= 1.0:
            return f"{value:.1%}"
        return f"{value:.4f}"
    return str(value)


def _fmt_metric_json(value: float | str) -> float | str:
    """Return JSON-serializable metric value."""
    return value


METRIC_LABELS = {
    "verified_completion_rate": "Verified Completion Rate",
    "false_completion_rate": "False Completion Rate",
    "false_completion_detected_rate": "False Completion Detected Rate",
    "recovery_execution_rate": "Recovery Execution Rate",
    "recovery_success_rate": "Recovery Success Rate",
    "cost_per_verified_completion": "Cost per Verified Completion",
    "duplicate_side_effect_rate": "Duplicate Side-Effect Rate",
    "lost_work_rate": "Lost Work Rate",
}


def generate_markdown(summary: dict[str, Any]) -> str:
    """Render the benchmark summary as a markdown report."""
    lines: list[str] = []
    meta = summary["metadata"]

    # Header
    lines.append("# ODYS Phase 4 Benchmark Report")
    lines.append("")
    lines.append(f"- **Benchmark version:** {meta['benchmark_version']}")
    lines.append(f"- **Protocol hash:** `{meta['protocol_hash']}`")
    lines.append(f"- **Generated at:** {meta['generated_at']}")
    lines.append("")

    # Run counts
    rc = summary["run_counts"]
    lines.append("## Run Counts")
    lines.append("")
    lines.append(f"| Metric | Count |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Total runs | {rc['total']} |")
    lines.append(f"| Valid runs | {rc['valid']} |")
    lines.append(f"| Invalid runs | {rc['invalid']} |")
    lines.append("")

    # Executive summary — primary metrics
    lines.append("## Executive Summary — Primary Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    for key, label in METRIC_LABELS.items():
        lines.append(f"| {label} | {_fmt_metric(summary[key])} |")
    lines.append("")

    # Per-family breakdown
    per_family = summary.get("per_family", {})
    if per_family:
        lines.append("## Per-Family Breakdown")
        lines.append("")
        header = "| Family | " + " | ".join(METRIC_LABELS.values()) + " |"
        sep = "|--------|" + "|".join(["--------"] * len(METRIC_LABELS)) + "|"
        lines.append(header)
        lines.append(sep)
        for family_name, metrics in sorted(per_family.items()):
            vals = " | ".join(_fmt_metric(metrics.get(k, NOT_MEASURED)) for k in METRIC_LABELS)
            lines.append(f"| {family_name} | {vals} |")
        lines.append("")

    # Per-configuration comparison
    per_config = summary.get("per_config", {})
    if per_config:
        lines.append("## Per-Configuration Comparison")
        lines.append("")
        header = "| Configuration | " + " | ".join(METRIC_LABELS.values()) + " |"
        sep = "|---------------|" + "|".join(["--------"] * len(METRIC_LABELS)) + "|"
        lines.append(header)
        lines.append(sep)
        for config_name, metrics in sorted(per_config.items()):
            vals = " | ".join(_fmt_metric(metrics.get(k, NOT_MEASURED)) for k in METRIC_LABELS)
            lines.append(f"| {config_name} | {vals} |")
        lines.append("")

    # Invalid runs summary
    invalid_count = rc["invalid"]
    lines.append("## Invalid Runs Summary")
    lines.append("")
    lines.append(f"Total invalid runs: **{invalid_count}**")
    if invalid_count == 0:
        lines.append("")
        lines.append("No invalid runs detected.")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def load_aggregation_input(path: Path) -> list[dict[str, Any]]:
    """Parse aggregation-input.jsonl into a list of dicts."""
    records: list[dict[str, Any]] = []
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def write_reports(
    input_path: Path,
    output_dir: Path,
    *,
    benchmark_version: str = "phase4-v1",
    protocol_hash: str = "",
) -> dict[str, Path]:
    """Read aggregation-input.jsonl and write both report files.

    Returns dict with keys ``md`` and ``json`` pointing to the output paths.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    runs = load_aggregation_input(input_path)
    summary = build_summary(
        runs,
        benchmark_version=benchmark_version,
        protocol_hash=protocol_hash,
    )

    # Write JSON summary
    json_path = output_dir / "benchmark-summary.json"
    json_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
        encoding="utf-8",
    )

    # Write markdown report
    md_path = output_dir / "benchmark-report.md"
    md_path.write_text(generate_markdown(summary), encoding="utf-8")

    return {"md": md_path, "json": json_path}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate benchmark reports from aggregation-input.jsonl"
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Path to aggregation-input.jsonl",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory for benchmark-report.md and benchmark-summary.json",
    )
    parser.add_argument(
        "--benchmark-version",
        default="phase4-v1",
        help="Benchmark version string for the report header",
    )
    parser.add_argument(
        "--protocol-hash",
        default="",
        help="Protocol hash to embed in the report",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    paths = write_reports(
        input_path=args.input,
        output_dir=args.output_dir,
        benchmark_version=args.benchmark_version,
        protocol_hash=args.protocol_hash,
    )
    print(json.dumps({"md": str(paths["md"]), "json": str(paths["json"])}, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
