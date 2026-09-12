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
    uv run python -m evals.reliability.p46_launcher warmup --config-profile cheap_model
    uv run python -m evals.reliability.p46_launcher status
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

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
PROFILE_PHASE4 = "phase4-v1"
PROFILE_CHEAP = "cheap_model"
CHEAP_CONFIG_HASH = "318a4fdf7d87b446780b5ca79381df77bf9dea5619598cd9917cee51de38f2fd"
CHEAP_CONFIG_DIR = Path("results/official_phase4/cheap_model")


@dataclass(frozen=True)
class BenchmarkConfigProfile:
    """Identity and provider-selection contract for one benchmark profile."""

    name: str
    benchmark_version: str
    model: str
    provider: str
    credential_env: str
    config_hash: str | None
    config_dir: Path | None
    endpoint_hash: str | None = None


def _canonical_config_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_benchmark_profile(
    profile_name: str,
    *,
    repo_root: Path | None = None,
) -> BenchmarkConfigProfile:
    """Load a provider profile without changing frozen Phase 4 inputs."""
    aliases = {
        "phase4": PROFILE_PHASE4,
        "phase4-v1": PROFILE_PHASE4,
        "cheap_model": PROFILE_CHEAP,
        "phase4-v1-cheap-model": PROFILE_CHEAP,
    }
    try:
        canonical_name = aliases[profile_name]
    except KeyError as exc:
        raise RuntimeError(f"UNKNOWN_CONFIG_PROFILE:{profile_name}") from exc

    if canonical_name == PROFILE_PHASE4:
        from evals.reliability.p46_provider import FROZEN_MODEL, FROZEN_PROVIDER

        return BenchmarkConfigProfile(
            name=PROFILE_PHASE4,
            benchmark_version="phase4-v1",
            model=FROZEN_MODEL,
            provider=FROZEN_PROVIDER,
            credential_env="ODYS_BENCHMARK_API_KEY",
            config_hash=None,
            config_dir=None,
        )

    root = Path(repo_root or Path(__file__).resolve().parents[2])
    config_dir = root / CHEAP_CONFIG_DIR
    config_path = config_dir / "benchmark_config.json"
    identity_path = config_dir / "benchmark_identity.json"
    provider_path = config_dir / "provider_identity.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        provider_identity = json.loads(provider_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("CHEAP_PROFILE_ARTIFACT_INVALID") from exc

    computed_hash = _canonical_config_hash(config)
    if computed_hash != CHEAP_CONFIG_HASH:
        raise RuntimeError("CHEAP_PROFILE_CONFIG_HASH_MISMATCH")
    if identity.get("benchmark_config_hash") != CHEAP_CONFIG_HASH:
        raise RuntimeError("CHEAP_PROFILE_IDENTITY_HASH_MISMATCH")
    if config.get("benchmark_version") != "phase4-v1-cheap-model":
        raise RuntimeError("CHEAP_PROFILE_VERSION_MISMATCH")
    if config.get("model_identity") != "mimo-v2.5":
        raise RuntimeError("CHEAP_PROFILE_MODEL_MISMATCH")
    if config.get("provider_identity") != "xiaomimimo-openai-compatible":
        raise RuntimeError("CHEAP_PROFILE_PROVIDER_MISMATCH")
    if provider_identity.get("model") != config["model_identity"]:
        raise RuntimeError("CHEAP_PROFILE_PROVIDER_ARTIFACT_MISMATCH")
    if provider_identity.get("provider") != config["provider_identity"]:
        raise RuntimeError("CHEAP_PROFILE_PROVIDER_ARTIFACT_MISMATCH")

    return BenchmarkConfigProfile(
        name=PROFILE_CHEAP,
        benchmark_version=config["benchmark_version"],
        model=config["model_identity"],
        provider=config["provider_identity"],
        credential_env="ODYS_CHEAP_BENCHMARK_API_KEY",
        config_hash=CHEAP_CONFIG_HASH,
        config_dir=CHEAP_CONFIG_DIR,
        endpoint_hash=config.get("endpoint_hash"),
    )


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
    execution_attempts: int = 0
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
            self.execution_attempts = int(
                data.get("execution_attempts", self.completed)
            )
            self.completed = min(max(self.completed, 0), self.total)
            self.execution_attempts = max(self.execution_attempts, self.completed)
            self.started_at = data.get("started_at", self.started_at)
        except (json.JSONDecodeError, KeyError, ValueError):
            pass  # start fresh

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.completed)

    def snapshot(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "planned_runs": self.total,
            "completed": self.completed,
            "valid": self.valid,
            "invalid": self.invalid,
            "execution_attempts": self.execution_attempts,
            "remaining": self.remaining,
            "started_at": self.started_at,
            "last_updated": _utc_now_iso(),
        }

    def record_result(self, is_valid: bool, *, counts_as_completion: bool = True) -> None:
        self.execution_attempts += 1
        if counts_as_completion:
            self.completed = min(self.total, self.completed + 1)
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

def compute_summary(output_dir: Path, *, planned_runs: int | None = None) -> dict[str, Any]:
    """Read raw.jsonl and compute the primary and P410 closure metrics.

    Returns a dict ready to be written as summary.json.
    """
    raw_path = output_dir / "raw.jsonl"
    invalid_path = output_dir / "invalid.jsonl"

    records: list[dict[str, Any]] = []
    if raw_path.exists():
        for line in raw_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))

    invalid_records: list[dict[str, Any]] = []
    if invalid_path.exists():
        for line in invalid_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                invalid_records.append(json.loads(line))

    invalid_count = len(invalid_records)
    collision_count = sum(
        1
        for record in invalid_records
        if record.get("error_type") == "IMMUTABLE_RESULT_COLLISION"
    )

    raw_by_id = {
        record.get("benchmark_run_id", record.get("run_id")): record
        for record in records
    }
    invalid_by_id = {
        record.get("benchmark_run_id", record.get("run_id")): record
        for record in invalid_records
    }
    result_ids = {
        record.get("benchmark_run_id", record.get("run_id"))
        for record in (*records, *invalid_records)
    }
    result_ids.discard(None)
    if planned_runs is not None and len(result_ids) > planned_runs:
        result_ids = set(sorted(result_ids)[:planned_runs])

    # A collision is an invalid execution of an already materialized run ID;
    # it must not make the same benchmark run count as both valid and invalid.
    invalid_ids = result_ids.intersection(invalid_by_id)
    valid_ids = result_ids.intersection(raw_by_id).difference(invalid_ids)
    valid_records = [raw_by_id[run_id] for run_id in valid_ids]
    valid_runs = len(valid_records)
    invalid_count = len(invalid_ids)
    total_runs = valid_runs + invalid_count

    if valid_runs == 0:
        return {
            "planned_runs": planned_runs,
            "execution_attempts": len(records) + len(invalid_records),
            "total_runs": total_runs,
            "valid_runs": 0,
            "invalid_runs": invalid_count,
            "collision_count": collision_count,
            "verified_completion_rate": NOT_MEASURED,
            "false_completion_rate": NOT_MEASURED,
            "false_completion_detected_rate": NOT_MEASURED,
            "recovery_execution_rate": NOT_MEASURED,
            "recovery_success_rate": NOT_MEASURED,
            "cost_per_verified_completion": NOT_MEASURED,
            "duplicate_side_effect_rate": NOT_MEASURED,
            "lost_work_rate": NOT_MEASURED,
        }

    # ── Numerators / denominators ──
    verified_completions = sum(1 for r in valid_records if r.get("verified_completion"))
    def _validation_identity(record: Mapping[str, Any]) -> Mapping[str, Any]:
        environment = record.get("runtime_environment")
        if isinstance(environment, Mapping):
            value = environment.get("validation")
            if isinstance(value, Mapping):
                return value
        return {}

    def _false_completion_detected(record: Mapping[str, Any]) -> bool:
        return bool(
            record.get("false_completion_detected")
            or _validation_identity(record).get("false_completion_detected")
            or record.get("false_completion")
        )

    false_completions = sum(1 for r in valid_records if _false_completion_detected(r))
    recovery_eligible = sum(1 for r in valid_records if r.get("recovery_required"))
    recovery_attempted = sum(1 for r in valid_records if r.get("recovery_attempted"))
    recovery_successes = sum(
        1
        for r in valid_records
        if r.get("recovery_attempted") and r.get("recovery_success")
    )

    # Cost: sum all measurable model_cost values
    def _is_measured(v: Any) -> bool:
        return v is not None and v != NOT_MEASURED and isinstance(v, (int, float))

    measured_costs = [
        r["model_cost"] for r in valid_records if _is_measured(r.get("model_cost"))
    ]
    total_cost: float | str = (
        sum(measured_costs) if measured_costs else NOT_MEASURED
    )

    runs_with_duplicates = sum(
        1 for r in valid_records if r.get("duplicate_side_effect_count", 0) > 0
    )
    runs_with_lost_work = sum(
        1
        for r in valid_records
        if r.get("recovery_required")
        and r.get("lost_work_units") not in (None, NOT_MEASURED, 0)
    )

    def _safe_rate(num: int, den: int) -> float | str:
        if den == 0:
            return NOT_MEASURED
        return round(num / den, 6)

    return {
        "planned_runs": planned_runs,
        "execution_attempts": len(records) + len(invalid_records),
        "total_runs": total_runs,
        "valid_runs": valid_runs,
        "invalid_runs": invalid_count,
        "collision_count": collision_count,
        "verified_completions": verified_completions,
        "false_completions": false_completions,
        "recovery_eligible": recovery_eligible,
        "recovery_successes": recovery_successes,
        "runs_with_duplicates": runs_with_duplicates,
        "runs_with_lost_work": runs_with_lost_work,
        "total_cost": total_cost,
        "verified_completion_rate": _safe_rate(verified_completions, valid_runs),
        "false_completion_rate": _safe_rate(false_completions, valid_runs),
        "false_completion_detected_rate": _safe_rate(false_completions, valid_runs),
        "recovery_execution_rate": _safe_rate(recovery_attempted, recovery_eligible),
        "recovery_success_rate": _safe_rate(recovery_successes, recovery_attempted),
        "cost_per_verified_completion": (
            round(total_cost / verified_completions, 6)
            if verified_completions > 0 and isinstance(total_cost, (int, float))
            else NOT_MEASURED
        ),
        "duplicate_side_effect_rate": _safe_rate(runs_with_duplicates, valid_runs),
        "lost_work_rate": _safe_rate(runs_with_lost_work, recovery_eligible),
        "computed_at": _utc_now_iso(),
    }


# ─── Run-set helpers ──────────────────────────────────────────────────

def _load_executor_from_flag(
    executor_spec: str | None,
    profile: BenchmarkConfigProfile | None = None,
) -> BenchmarkExecutor | None:
    """Resolve --executor or fall back to P45BenchmarkExecutor."""
    profile = profile or load_benchmark_profile(PROFILE_PHASE4)
    if executor_spec:
        return load_executor(executor_spec)
    # Default: P45BenchmarkExecutor via its public factory
    try:
        from evals.reliability.p45_executor import (
            create_cheap_executor,
            create_executor,
        )

        if profile.name == PROFILE_CHEAP:
            return create_cheap_executor()
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
    profile: BenchmarkConfigProfile,
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
    persisted_provider_identity = persist_identity(output_dir / "provider_identity.json")
    if not isinstance(persisted_provider_identity, dict):
        raise RuntimeError("PROVIDER_IDENTITY_UNAVAILABLE")
    if persisted_provider_identity.get("model") != profile.model:
        raise RuntimeError("MODEL_IDENTITY_MISMATCH")
    if persisted_provider_identity.get("provider") != profile.provider:
        raise RuntimeError("PROVIDER_IDENTITY_MISMATCH")
    if profile.endpoint_hash is not None and persisted_provider_identity.get("endpoint_hash") != profile.endpoint_hash:
        raise RuntimeError("PROVIDER_ENDPOINT_MISMATCH")

    benchmark_identity = {
        "base_protocol_hash": snapshot.protocol_hash,
        "benchmark_config_hash": profile.config_hash,
        "benchmark_version": profile.benchmark_version,
        "model_identity": model,
        "provider_identity": provider,
    }
    identity_path = output_dir / "benchmark_identity.json"
    if identity_path.exists():
        try:
            existing_identity = json.loads(identity_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("BENCHMARK_IDENTITY_ARTIFACT_INVALID") from exc
        if existing_identity != benchmark_identity:
            raise RuntimeError("BENCHMARK_IDENTITY_ARTIFACT_MISMATCH")
    else:
        identity_path.write_text(
            json.dumps(benchmark_identity, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

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
        tracker.completed = min(total_planned, total_planned - len(remaining_runs))
        raw_ids = {
            record["benchmark_run_id"] for record in runner.output._raw
        }
        invalid_ids = {
            record["benchmark_run_id"] for record in runner.output._invalid
        }
        tracker.valid = min(total_planned, len(raw_ids - invalid_ids))
        tracker.invalid = min(
            total_planned - tracker.valid,
            len(invalid_ids),
        )
        tracker.completed = min(total_planned, tracker.valid + tracker.invalid)
        tracker.save()

    print(
        f"[LAUNCH] {len(remaining_runs)} runs to execute "
        f"({total_planned} total planned, output={output_dir})"
    )

    run_count_since_print = 0
    for spec in remaining_runs:
        was_recorded = runner.output.has_run(spec.run_id)
        try:
            await runner._run_one(spec)
            is_valid = runner.output.counts["valid"] > tracker.valid
            result_was_added = not was_recorded and runner.output.has_run(spec.run_id)
            tracker.record_result(
                is_valid,
                counts_as_completion=result_was_added,
            )
        except Exception as exc:
            # Unexpected error in the harness itself — record as invalid
            tracker.record_result(is_valid=False, counts_as_completion=False)
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

    summary = compute_summary(
        output_dir,
        planned_runs=total_planned,
    )
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
        print("\n=== Summary (Primary + P410 Recovery Metrics) ===")
        for key in (
            "verified_completion_rate",
            "false_completion_rate",
            "false_completion_detected_rate",
            "recovery_execution_rate",
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
    shared.add_argument(
        "--config-profile",
        default=PROFILE_PHASE4,
        help=(
            "Provider/benchmark profile (default: phase4-v1; "
            "cheap_model selects the isolated mimo-v2.5 profile)"
        ),
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

    profile = load_benchmark_profile(args.config_profile)
    if snapshot.protocol_hash != "993eae04290fe683d40fcf845b9e7325b9572b227e78e7cb3090dd7ee37637c3":
        raise RuntimeError("PROTOCOL_HASH_MISMATCH")

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
    executor = _load_executor_from_flag(args.executor, profile)

    output_dir = args.output
    if output_dir == DEFAULT_OUTPUT_DIR and profile.name == PROFILE_CHEAP:
        output_dir = DEFAULT_OUTPUT_DIR / PROFILE_CHEAP

    # The official path must use the identity proved by the real provider,
    # never a placeholder label supplied by the CLI defaults.
    proved_identity = getattr(executor, "provider_identity", None)
    if not isinstance(proved_identity, dict):
        raise RuntimeError("PROVIDER_IDENTITY_UNVERIFIED")
    model = proved_identity.get("model")
    provider = proved_identity.get("provider")
    if not isinstance(model, str) or not isinstance(provider, str):
        raise RuntimeError("PROVIDER_IDENTITY_UNAVAILABLE")
    if model != profile.model or args.model not in ("FROZEN_BY_P4.2", model):
        raise RuntimeError("MODEL_IDENTITY_MISMATCH")
    if provider != profile.provider or args.provider not in ("FROZEN_BY_P4.2", provider):
        raise RuntimeError("PROVIDER_IDENTITY_MISMATCH")

    # Run the benchmark
    summary = asyncio.run(
        _run_benchmark(
            runs=runs,
            output_dir=output_dir,
            snapshot=snapshot,
            executor=executor,
            model=model,
            provider=provider,
            profile=profile,
            resume=args.resume,
        )
    )

    # Print final summary
    print("\n=== Final Summary ===")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
