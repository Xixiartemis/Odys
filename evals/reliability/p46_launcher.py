"""P4.6 Official Benchmark Launcher.

Orchestrates the full frozen-phase4-v1 benchmark run:
  - headline:  60 tasks × 3 repeats × 2 configs = 360 runs
  - ablation:  12 tasks × 3 repeats × 4 configs = 144 runs
  - smoke:      1 task  × 1 repeat  × 2 configs =   2 runs
  - warmup:     6 frozen tasks × 1 repeat × 2 configs =  12 runs

Writes results to ``results/official_phase4/`` with progress tracking,
resume support, and summary computation of all 6 primary metrics.

Usage::

    uv run python -m evals.reliability.p46_launcher headline
    uv run python -m evals.reliability.p46_launcher ablation
    uv run python -m evals.reliability.p46_launcher smoke
    uv run python -m evals.reliability.p46_launcher warmup
    uv run python -m evals.reliability.p46_launcher status
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.reliability.run_phase4 import (
    BenchmarkExecutor,
    NOT_MEASURED,
    Phase4Runner,
    ProtocolSnapshot,
    RunSpec,
    select_runs,
    load_executor,
)

# ─── Constants ────────────────────────────────────────────────────────

DEFAULT_OUTPUT_DIR = Path("results/official_phase4")
PROGRESS_FILENAME = "progress.json"
SUMMARY_FILENAME = "summary.json"
PROGRESS_PRINT_INTERVAL = 10


# ─── Data helpers ─────────────────────────────────────────────────────

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class ProgressTracker:
    """Tracks benchmark run progress and persists to progress.json."""

    total: int
    output_dir: Path
    started_at: str = field(default_factory=_utc_now_iso)
    completed: int = 0
    valid: int = 0
    invalid: int = 0
    last_updated: str = field(default_factory=_utc_now_iso)

    def __post_init__(self) -> None:
        self._progress_path = self.output_dir / PROGRESS_FILENAME
        self._load_existing()

    def _load_existing(self) -> None:
        """Load counters from an existing progress.json (for resume)."""
        if not self._progress_path.exists():
            return
        try:
            data = json.loads(self._progress_path.read_text(encoding="utf-8"))
            self.completed = int(data.get("completed", 0))
            self.valid = int(data.get("valid", 0))
            self.invalid = int(data.get("invalid", 0))
            self.started_at = data.get("started_at", self.started_at)
        except (json.JSONDecodeError, KeyError, ValueError):
            pass  # start fresh

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.completed)

    def snapshot(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "completed": self.completed,
            "valid": self.valid,
            "invalid": self.invalid,
            "remaining": self.remaining,
            "started_at": self.started_at,
            "last_updated": _utc_now_iso(),
        }

    def record_result(self, is_valid: bool) -> None:
        self.completed += 1
        if is_valid:
            self.valid += 1
        else:
            self.invalid += 1
        self.last_updated = _utc_now_iso()

    def save(self) -> None:
        self._progress_path.write_text(
            json.dumps(self.snapshot(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def print_progress(self) -> None:
        snap = self.snapshot()
        print(
            f"[PROGRESS] completed={snap['completed']}/{snap['total']} "
            f"valid={snap['valid']} invalid={snap['invalid']} "
            f"remaining={snap['remaining']}"
        )


# ─── Summary computation ─────────────────────────────────────────────

def compute_summary(output_dir: Path) -> dict[str, Any]:
    """Read raw.jsonl and compute the 6 primary metrics.

    Returns a dict ready to be written as summary.json.
    """
    raw_path = output_dir / "raw.jsonl"
    invalid_path = output_dir / "invalid.jsonl"

    records: list[dict[str, Any]] = []
    if raw_path.exists():
        for line in raw_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))

    invalid_count = 0
    if invalid_path.exists():
        for line in invalid_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                invalid_count += 1

    valid_runs = len(records)
    total_runs = valid_runs + invalid_count

    if valid_runs == 0:
        return {
            "total_runs": total_runs,
            "valid_runs": 0,
            "invalid_runs": invalid_count,
            "verified_completion_rate": NOT_MEASURED,
            "false_completion_rate": NOT_MEASURED,
            "recovery_success_rate": NOT_MEASURED,
            "cost_per_verified_completion": NOT_MEASURED,
            "duplicate_side_effect_rate": NOT_MEASURED,
            "lost_work_rate": NOT_MEASURED,
        }

    # ── Numerators / denominators ──
    verified_completions = sum(1 for r in records if r.get("verified_completion"))
    false_completions = sum(1 for r in records if r.get("false_completion"))
    recovery_eligible = sum(1 for r in records if r.get("recovery_required"))
    recovery_successes = sum(
        1
        for r in records
        if r.get("recovery_required") and r.get("recovery_success")
    )

    # Cost: sum all measurable model_cost values
    def _is_measured(v: Any) -> bool:
        return v is not None and v != NOT_MEASURED and isinstance(v, (int, float))

    total_cost = sum(
        r["model_cost"] for r in records if _is_measured(r.get("model_cost"))
    )

    runs_with_duplicates = sum(
        1 for r in records if r.get("duplicate_side_effect_count", 0) > 0
    )
    runs_with_lost_work = sum(
        1
        for r in records
        if r.get("recovery_required")
        and r.get("lost_work_units") not in (None, NOT_MEASURED, 0)
    )

    def _safe_rate(num: int, den: int) -> float | str:
        if den == 0:
            return NOT_MEASURED
        return round(num / den, 6)

    return {
        "total_runs": total_runs,
        "valid_runs": valid_runs,
        "invalid_runs": invalid_count,
        "verified_completions": verified_completions,
        "false_completions": false_completions,
        "recovery_eligible": recovery_eligible,
        "recovery_successes": recovery_successes,
        "runs_with_duplicates": runs_with_duplicates,
        "runs_with_lost_work": runs_with_lost_work,
        "total_cost": total_cost,
        "verified_completion_rate": _safe_rate(verified_completions, valid_runs),
        "false_completion_rate": _safe_rate(false_completions, valid_runs),
        "recovery_success_rate": _safe_rate(recovery_successes, recovery_eligible),
        "cost_per_verified_completion": (
            round(total_cost / verified_completions, 6)
            if verified_completions > 0
            else NOT_MEASURED
        ),
        "duplicate_side_effect_rate": _safe_rate(runs_with_duplicates, valid_runs),
        "lost_work_rate": _safe_rate(runs_with_lost_work, recovery_eligible),
        "computed_at": _utc_now_iso(),
    }


# ─── Run-set helpers ──────────────────────────────────────────────────

def _load_executor_from_flag(executor_spec: str | None) -> BenchmarkExecutor | None:
    """Resolve --executor or fall back to P45BenchmarkExecutor."""
    if executor_spec:
        return load_executor(executor_spec)
    # Default: P45BenchmarkExecutor via its public factory
    try:
        from evals.reliability.p45_executor import create_executor

        return create_executor()
    except ImportError:
        from evals.reliability.run_phase4 import UnconfiguredExecutor

        return UnconfiguredExecutor()


def _build_smoke_runs(snapshot: ProtocolSnapshot) -> tuple[RunSpec, ...]:
    """Build 1 task × 1 repeat × 2 configs = 2 runs for smoke test."""
    from evals.reliability.run_phase4 import ConfigLoader

    task = snapshot.tasks[0]
    config_names = list(snapshot.protocol["headline"]["configs"])
    loader = ConfigLoader(snapshot)
    configs = [loader.load(name) for name in config_names]
    return tuple(
        RunSpec(task=task, config=config, repeat_index=1) for config in configs
    )


def _build_warmup_runs(snapshot: ProtocolSnapshot) -> tuple[RunSpec, ...]:
    """Build the fixed P48 warmup selection from existing frozen tasks."""
    warmup_ids = ("CI-01", "ESR-01", "CWR-01", "PTF-01", "RTP-01", "DL-01")
    tasks_by_id = {task["task_id"]: task for task in snapshot.tasks}
    missing = [task_id for task_id in warmup_ids if task_id not in tasks_by_id]
    if missing:
        raise RuntimeError(f"WARMUP_TASK_MISSING: {','.join(missing)}")
    from evals.reliability.run_phase4 import ConfigLoader

    loader = ConfigLoader(snapshot)
    configs = [loader.load(name) for name in snapshot.protocol["headline"]["configs"]]
    return tuple(
        RunSpec(task=tasks_by_id[task_id], config=config, repeat_index=1)
        for task_id in warmup_ids
        for config in configs
    )


# ─── Benchmark orchestration ─────────────────────────────────────────

async def _run_benchmark(
    *,
    runs: tuple[RunSpec, ...],
    output_dir: Path,
    snapshot: ProtocolSnapshot,
    executor: BenchmarkExecutor,
    model: str,
    provider: str,
    resume: bool,
) -> dict[str, Any]:
    """Execute a set of runs with progress tracking.

    Hooks into Phase4Runner.run() but adds progress.json updates and
    periodic progress printing.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    persist_identity = getattr(executor, "persist_provider_identity", None)
    if not callable(persist_identity):
        raise RuntimeError("PROVIDER_IDENTITY_ARTIFACT_SCOPE_UNAVAILABLE")
    persist_identity(output_dir / "provider_identity.json")

    runner = Phase4Runner(
        snapshot,
        output_dir=output_dir,
        executor=executor,
        model=model,
        provider=provider,
        trace_path=output_dir / "traces.jsonl",
        require_trace=True,
    )

    # Filter runs for resume: skip those already recorded
    if resume:
        remaining_runs = [
            spec for spec in runs if not runner.output.has_run(spec.run_id)
        ]
        already_done = len(runs) - len(remaining_runs)
        if already_done > 0:
            print(f"[RESUME] Skipping {already_done} already-completed runs")
    else:
        remaining_runs = list(runs)

    total_planned = len(runs)
    tracker = ProgressTracker(total=total_planned, output_dir=output_dir)
    # Seed from resume data
    if resume:
        tracker.completed = total_planned - len(remaining_runs)
        tracker.valid = runner.output.counts["valid"]
        tracker.invalid = runner.output.counts["invalid"]
        tracker.save()

    print(
        f"[LAUNCH] {len(remaining_runs)} runs to execute "
        f"({total_planned} total planned, output={output_dir})"
    )

    run_count_since_print = 0
    for spec in remaining_runs:
        try:
            await runner._run_one(spec)
            is_valid = runner.output.counts["valid"] > tracker.valid
            tracker.record_result(is_valid)
        except Exception as exc:
            # Unexpected error in the harness itself — record as invalid
            tracker.record_result(is_valid=False)
            print(f"[ERROR] Harness error on {spec.run_id}: {exc}")

        tracker.save()
        run_count_since_print += 1

        if run_count_since_print >= PROGRESS_PRINT_INTERVAL:
            tracker.print_progress()
            run_count_since_print = 0

    # Final aggregation rewrite
    runner.output._rewrite_aggregation()

    # Final progress + summary
    tracker.save()
    tracker.print_progress()

    summary = compute_summary(output_dir)
    summary_path = output_dir / SUMMARY_FILENAME
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return summary


# ─── Status subcommand ────────────────────────────────────────────────

def _show_status(output_dir: Path) -> None:
    """Print progress.json contents."""
    progress_path = output_dir / PROGRESS_FILENAME
    if not progress_path.exists():
        print(f"No progress file found at {progress_path}")
        print("No benchmark run has been started yet.")
        return

    data = json.loads(progress_path.read_text(encoding="utf-8"))
    print("=== P4.6 Benchmark Progress ===")
    print(f"  Total:      {data['total']}")
    print(f"  Completed:  {data['completed']}")
    print(f"  Valid:       {data['valid']}")
    print(f"  Invalid:     {data['invalid']}")
    print(f"  Remaining:   {data['remaining']}")
    print(f"  Started:     {data['started_at']}")
    print(f"  Last update: {data['last_updated']}")
    pct = (data["completed"] / data["total"] * 100) if data["total"] > 0 else 0
    print(f"  Progress:    {pct:.1f}%")

    # Show summary if available
    summary_path = output_dir / SUMMARY_FILENAME
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        print("\n=== Summary (6 Primary Metrics) ===")
        for key in (
            "verified_completion_rate",
            "false_completion_rate",
            "recovery_success_rate",
            "cost_per_verified_completion",
            "duplicate_side_effect_rate",
            "lost_work_rate",
        ):
            print(f"  {key}: {summary.get(key, 'N/A')}")


# ─── CLI ──────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="p46_launcher",
        description="P4.6 Official Benchmark Launcher — orchestrates the frozen phase4-v1 benchmark",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # Shared arguments applied to all run subcommands
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Output directory (default: results/official_phase4/)",
    )
    shared.add_argument(
        "--executor",
        default=None,
        help="Execution adapter as module:factory (default: evals.reliability.p45_executor:create_executor)",
    )
    shared.add_argument(
        "--model",
        default=os.environ.get("ODYS_BENCHMARK_MODEL", "FROZEN_BY_P4.2"),
        help="Model identity string",
    )
    shared.add_argument(
        "--provider",
        default=os.environ.get("ODYS_BENCHMARK_PROVIDER", "FROZEN_BY_P4.2"),
        help="Provider identity string",
    )
    shared.add_argument(
        "--resume",
        action="store_true",
        help="Skip runs already recorded in output directory",
    )
    shared.add_argument(
        "--protocol-root",
        type=Path,
        default=None,
        help="Path to frozen protocol directory (default: auto-detected)",
    )

    # headline: 60 × 3 × 2 = 360 runs
    sub.add_parser("headline", parents=[shared], help="Run headline benchmark (360 runs)")

    # ablation: 12 × 3 × 4 = 144 runs
    sub.add_parser("ablation", parents=[shared], help="Run ablation benchmark (144 runs)")

    # smoke: 1 × 1 × 2 = 2 runs
    sub.add_parser("smoke", parents=[shared], help="Run smoke test (2 runs)")

    # warmup: 6 fixed tasks × 1 repeat × 2 configs = 12 runs
    sub.add_parser("warmup", parents=[shared], help="Run P48 warmup (12 runs)")

    # status: read-only
    status_p = sub.add_parser("status", help="Show benchmark progress")
    status_p.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Output directory to check",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "status":
        _show_status(args.output)
        return 0

    # Load frozen protocol snapshot
    protocol_root = args.protocol_root
    if protocol_root is not None:
        snapshot = ProtocolSnapshot.load(protocol_root)
    else:
        snapshot = ProtocolSnapshot.load()

    # Select runs based on subcommand
    if args.command == "headline":
        runs = select_runs(snapshot, headline=True)
        print(f"[HEADLINE] {len(runs)} runs selected (60 tasks × 3 repeats × 2 configs)")
    elif args.command == "ablation":
        runs = select_runs(snapshot, ablation=True)
        print(f"[ABLATION] {len(runs)} runs selected (12 tasks × 3 repeats × 4 configs)")
    elif args.command == "smoke":
        runs = _build_smoke_runs(snapshot)
        print(f"[SMOKE] {len(runs)} runs selected (1 task × 1 repeat × 2 configs)")
    elif args.command == "warmup":
        runs = _build_warmup_runs(snapshot)
        print(f"[WARMUP] {len(runs)} runs selected (6 tasks × 1 repeat × 2 configs)")
    else:
        print(f"Unknown command: {args.command}")
        return 1

    # Build executor
    executor = _load_executor_from_flag(args.executor)

    # The official path must use the identity proved by the real provider,
    # never a placeholder label supplied by the CLI defaults.
    proved_identity = getattr(executor, "provider_identity", None)
    if not isinstance(proved_identity, dict):
        raise RuntimeError("PROVIDER_IDENTITY_UNVERIFIED")
    model = proved_identity.get("model")
    provider = proved_identity.get("provider")
    if not isinstance(model, str) or not isinstance(provider, str):
        raise RuntimeError("PROVIDER_IDENTITY_UNAVAILABLE")
    if args.model not in ("FROZEN_BY_P4.2", model):
        raise RuntimeError("MODEL_IDENTITY_MISMATCH")
    if args.provider not in ("FROZEN_BY_P4.2", provider):
        raise RuntimeError("PROVIDER_IDENTITY_MISMATCH")

    # Run the benchmark
    summary = asyncio.run(
        _run_benchmark(
            runs=runs,
            output_dir=args.output,
            snapshot=snapshot,
            executor=executor,
            model=model,
            provider=provider,
            resume=args.resume,
        )
    )

    # Print final summary
    print("\n=== Final Summary ===")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
