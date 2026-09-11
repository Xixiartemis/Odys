"""Official benchmark run controller with checkpoint, resume, and progress tracking.

Wraps :class:`Phase4Runner` and adds:
- Run manifest generation (``run_manifest.json``)
- Checkpoint/resume support (``checkpoint.json``)
- Failure recovery
- Progress tracking (``progress.json``)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.reliability.run_phase4 import (
    DEFAULT_PROTOCOL_ROOT,
    Phase4Runner,
    ProtocolSnapshot,
    ResultWriter,
    RunSpec,
    load_executor,
    select_runs,
)


class ControllerError(RuntimeError):
    """Raised when the run controller detects an invalid state."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _environment_hash(env_path: Path) -> str:
    """Compute SHA256 of the environment.json file contents."""
    raw = env_path.read_bytes()
    return hashlib.sha256(raw).hexdigest()


def _repo_sha(repo_root: Path) -> str:
    """Get the current git SHA of the repository."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "UNKNOWN"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON to a file atomically using temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read JSON file, returning None if it doesn't exist."""
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


class RunController:
    """Official benchmark run controller with checkpoint, resume, and progress.

    Wraps ``Phase4Runner`` to add run manifest generation, checkpoint/resume
    support, failure recovery, and progress tracking.
    """

    def __init__(
        self,
        output_dir: Path,
        executor_factory: str | None = None,
        model: str = "FROZEN_BY_P4.2",
        provider: str = "FROZEN_BY_P4.2",
        repo_root: Path | None = None,
        protocol_root: Path = DEFAULT_PROTOCOL_ROOT,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.executor_factory = executor_factory
        self.model = model
        self.provider = provider
        self.repo_root = Path(repo_root or Path.cwd()).resolve()
        self.protocol_root = Path(protocol_root)

        self.manifest_path = self.output_dir / "run_manifest.json"
        self.checkpoint_path = self.output_dir / "checkpoint.json"
        self.progress_path = self.output_dir / "progress.json"
        self.environment_path = (
            Path(__file__).parent / "environment.json"
        )

    def generate_manifest(
        self,
        snapshot: ProtocolSnapshot,
        runs: tuple[RunSpec, ...],
        *,
        selection: str = "custom",
    ) -> dict[str, Any]:
        """Generate and write run_manifest.json before execution starts."""
        task_ids = sorted(set(spec.task["task_id"] for spec in runs))
        config_names = sorted(set(spec.config["config_id"] for spec in runs))
        repeats = max(spec.repeat_index for spec in runs) if runs else 0

        env_hash = _environment_hash(self.environment_path)

        manifest = {
            "task_ids": task_ids,
            "configs": config_names,
            "repeats": repeats,
            "runner_git_sha": _repo_sha(self.repo_root),
            "protocol_hash": snapshot.protocol_hash,
            "environment_hash": env_hash,
            "total_runs": len(runs),
            "started_at": _timestamp(_utc_now()),
            "selection": selection,
        }

        _atomic_write_json(self.manifest_path, manifest)
        return manifest

    def _load_checkpoint(self) -> dict[str, Any] | None:
        """Read checkpoint.json if it exists."""
        return _read_json(self.checkpoint_path)

    def _validate_resume(
        self,
        checkpoint: dict[str, Any],
        snapshot: ProtocolSnapshot,
    ) -> None:
        """Validate that checkpoint hashes match the current experiment.

        Raises ``ControllerError`` if the hashes don't match.
        """
        env_hash = _environment_hash(self.environment_path)

        if checkpoint.get("protocol_hash") != snapshot.protocol_hash:
            raise ControllerError(
                f"RESUME_PROTOCOL_HASH_MISMATCH: "
                f"checkpoint={checkpoint.get('protocol_hash')}, "
                f"current={snapshot.protocol_hash}"
            )

        if checkpoint.get("environment_hash") != env_hash:
            raise ControllerError(
                f"RESUME_ENVIRONMENT_HASH_MISMATCH: "
                f"checkpoint={checkpoint.get('environment_hash')}, "
                f"current={env_hash}"
            )

    def _write_checkpoint(
        self,
        run_id: str,
        counts: dict[str, int],
        *,
        snapshot: ProtocolSnapshot,
    ) -> None:
        """Atomically write checkpoint.json after a run completes."""
        env_hash = _environment_hash(self.environment_path)
        data = {
            "last_completed_run_id": run_id,
            "completed_count": counts.get("valid", 0),
            "failed_count": counts.get("invalid", 0),
            "invalid_count": counts.get("invalid", 0),
            "protocol_hash": snapshot.protocol_hash,
            "environment_hash": env_hash,
            "updated_at": _timestamp(_utc_now()),
        }
        _atomic_write_json(self.checkpoint_path, data)

    def _write_progress(
        self,
        completed: list[str],
        failed: list[str],
        remaining: list[str],
        started: float,
        total_runs: int,
    ) -> None:
        """Atomically write progress.json after each run."""
        elapsed = time.perf_counter() - started
        done = len(completed) + len(failed)
        percent = (done / total_runs * 100) if total_runs > 0 else 0.0
        avg_time = elapsed / done if done > 0 else 0.0
        est_remaining = avg_time * len(remaining)

        data = {
            "total_runs": total_runs,
            "completed_runs": completed,
            "failed_runs": failed,
            "remaining_runs": remaining,
            "percent_complete": round(percent, 2),
            "elapsed_seconds": round(elapsed, 2),
            "estimated_remaining_seconds": round(est_remaining, 2),
        }
        _atomic_write_json(self.progress_path, data)

    def _get_completed_run_ids(self) -> set[str]:
        """Get set of run IDs that are already completed (from raw.jsonl / invalid.jsonl)."""
        completed: set[str] = set()
        for filename in ("raw.jsonl", "invalid.jsonl"):
            path = self.output_dir / filename
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        try:
                            record = json.loads(line)
                            completed.add(record["benchmark_run_id"])
                        except (json.JSONDecodeError, KeyError):
                            continue
        return completed

    def _categorize_existing(
        self,
        all_done: list[str],
        failed: list[str],
        writer: ResultWriter,
    ) -> None:
        """Categorize already-completed run IDs into valid/invalid.

        Moves IDs from *all_done* to *failed* if they are in invalid.jsonl.
        """
        invalid_ids = set()
        invalid_path = self.output_dir / "invalid.jsonl"
        if invalid_path.exists():
            for line in invalid_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        record = json.loads(line)
                        invalid_ids.add(record["benchmark_run_id"])
                    except (json.JSONDecodeError, KeyError):
                        continue
        # Move invalid IDs from completed to failed
        to_move = [rid for rid in all_done if rid in invalid_ids]
        for rid in to_move:
            all_done.remove(rid)
            failed.append(rid)

    @staticmethod
    def _is_valid_run(writer: ResultWriter, run_id: str) -> bool:
        """Check whether a run_id is in raw (valid) vs invalid."""
        # If it's in the writer's _raw set, it's valid
        return run_id in {r["benchmark_run_id"] for r in writer._raw}

    def run(
        self,
        snapshot: ProtocolSnapshot,
        runs: tuple[RunSpec, ...],
        *,
        selection: str = "custom",
    ) -> dict[str, int]:
        """Execute all runs with checkpointing and progress tracking.

        Returns counts dict with 'valid' and 'invalid' keys.
        """
        # Generate manifest
        self.generate_manifest(snapshot, runs, selection=selection)

        # Check for resume
        checkpoint = self._load_checkpoint()
        if checkpoint is not None:
            self._validate_resume(checkpoint, snapshot)
            print(f"Resuming from checkpoint (last: {checkpoint.get('last_completed_run_id')})")

        # Get already completed runs
        already_done = self._get_completed_run_ids()

        # Filter remaining runs
        remaining_runs = [spec for spec in runs if spec.run_id not in already_done]
        all_run_ids = [spec.run_id for spec in runs]

        if not remaining_runs:
            print(f"All {len(runs)} runs already completed.")
            writer = ResultWriter(self.output_dir)
            return writer.counts

        print(f"Executing {len(remaining_runs)} of {len(runs)} runs "
              f"({len(already_done)} already completed)")

        # Initialize runner
        executor = load_executor(self.executor_factory)
        runner = Phase4Runner(
            snapshot,
            output_dir=self.output_dir,
            executor=executor,
            model=self.model,
            provider=self.provider,
            repo_root=self.repo_root,
        )

        # Track progress
        started_clock = time.perf_counter()
        completed_ids = sorted(already_done)
        failed_ids: list[str] = []

        # Categorize already-done runs as valid or invalid
        self._categorize_existing(completed_ids, failed_ids, runner.output)

        # Initialize progress file
        remaining_ids = [spec.run_id for spec in remaining_runs]
        self._write_progress(
            completed_ids, failed_ids, remaining_ids, started_clock, len(runs)
        )

        # Execute each run
        for spec in remaining_runs:
            try:
                self._execute_one(runner, spec, snapshot)
            except Exception as exc:
                print(f"  UNEXPECTED ERROR: {spec.run_id}: {exc}")

            # Check whether the run was written to raw or invalid
            if runner.output.has_run(spec.run_id):
                # Determine if it's valid or invalid based on actual files
                if self._is_valid_run(runner.output, spec.run_id):
                    completed_ids.append(spec.run_id)
                else:
                    failed_ids.append(spec.run_id)
            else:
                # Shouldn't happen but be safe
                failed_ids.append(spec.run_id)
                print(f"  WARNING: {spec.run_id} not found in any output")

            # Update progress
            remaining_ids = [
                rid for rid in all_run_ids
                if rid not in completed_ids and rid not in failed_ids
            ]
            self._write_progress(
                completed_ids, failed_ids, remaining_ids, started_clock, len(runs)
            )

            # Write checkpoint
            self._write_checkpoint(
                spec.run_id,
                {"valid": len(completed_ids), "invalid": len(failed_ids)},
                snapshot=snapshot,
            )

        # Final aggregation
        runner.output._rewrite_aggregation()

        counts = {"valid": len(completed_ids), "invalid": len(failed_ids)}
        print(f"Run complete: {json.dumps(counts, sort_keys=True)}")
        return counts

    def _execute_one(
        self,
        runner: Phase4Runner,
        spec: RunSpec,
        snapshot: ProtocolSnapshot,
    ) -> None:
        """Execute a single run spec.

        Delegates to Phase4Runner._run_one, which handles writing to raw.jsonl
        or invalid.jsonl. The runner is already idempotent (ResultWriter deduplicates).
        """
        # Run the single spec via the runner
        asyncio.run(runner.run([spec]))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Official benchmark run controller with checkpoint/resume"
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--headline", action="store_true")
    selection.add_argument("--ablation", action="store_true")
    selection.add_argument("--task", help="Run a single task by ID")

    parser.add_argument("--output", type=Path, required=True, help="Output directory")
    parser.add_argument("--executor", help="module:factory execution adapter")
    parser.add_argument("--model", default=os.environ.get("ODYS_BENCHMARK_MODEL", "FROZEN_BY_P4.2"))
    parser.add_argument("--provider", default=os.environ.get("ODYS_BENCHMARK_PROVIDER", "FROZEN_BY_P4.2"))
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--root", type=Path, default=DEFAULT_PROTOCOL_ROOT, help="Protocol root directory")
    parser.add_argument("--repeat", type=int, help="Repeat index (for --task)")
    parser.add_argument("--config", help="Config name (for --task)")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from checkpoint if available",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the run controller."""
    args = _build_parser().parse_args(argv)

    # Load snapshot
    snapshot = ProtocolSnapshot.load(args.root)

    # Select runs
    runs = select_runs(
        snapshot,
        headline=args.headline,
        ablation=args.ablation,
        task_id=args.task,
        config_name=args.config,
        repeat_index=args.repeat,
    )

    # Determine selection label
    if args.headline:
        selection = "headline"
    elif args.ablation:
        selection = "ablation"
    else:
        selection = f"task:{args.task}"

    # Create controller
    controller = RunController(
        output_dir=args.output,
        executor_factory=args.executor,
        model=args.model,
        provider=args.provider,
        repo_root=args.repo_root,
        protocol_root=args.root,
    )

    # Check for resume
    if args.resume:
        checkpoint = controller._load_checkpoint()
        if checkpoint is not None:
            controller._validate_resume(checkpoint, snapshot)
            print("Resume validated, continuing from checkpoint.")
        else:
            print("No checkpoint found, starting fresh.")

    # Execute
    counts = controller.run(snapshot, runs, selection=selection)
    print(json.dumps({"selection": selection, "planned": len(runs), **counts}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
