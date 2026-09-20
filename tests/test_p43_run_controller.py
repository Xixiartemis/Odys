"""Tests for the official benchmark run controller (P43)."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

import pytest

from evals.reliability.run_controller import (
    ControllerError,
    RunController,
    _atomic_write_json,
    _environment_hash,
    _read_json,
)


RELIABILITY = Path(__file__).resolve().parent.parent / "evals" / "reliability"


@pytest.fixture()
def snapshot():
    """Load the frozen protocol snapshot."""
    from evals.reliability.run_phase4 import ProtocolSnapshot
    return ProtocolSnapshot.load(RELIABILITY / "phase4_v1")


@pytest.fixture()
def env_hash():
    """Compute environment hash."""
    return _environment_hash(RELIABILITY / "environment.json")


@pytest.fixture()
def work_dir(tmp_path_factory):
    """Provide a temporary work directory with cleanup."""
    d = tmp_path_factory.mktemp("run_ctrl")
    yield d
    # Cleanup happens automatically for tmp_path_factory


class TestManifestGeneration:
    """Test run_manifest.json generation."""

    def test_manifest_written(self, snapshot, work_dir):
        """Manifest is written to output dir."""
        from evals.reliability.run_phase4 import select_runs

        output = work_dir / "manifest_test"
        output.mkdir()
        runs = select_runs(snapshot, task_id="CI-01", config_name="minimal", repeat_index=1)
        controller = RunController(output_dir=output)
        manifest = controller.generate_manifest(snapshot, runs, selection="custom")

        assert output.joinpath("run_manifest.json").exists()
        assert manifest["task_ids"] == ["CI-01"]
        assert manifest["configs"] == ["minimal"]
        assert manifest["repeats"] == 1
        assert manifest["total_runs"] == 1
        assert manifest["selection"] == "custom"

    def test_manifest_contains_hashes(self, snapshot, work_dir, env_hash):
        """Manifest contains protocol_hash and environment_hash."""
        from evals.reliability.run_phase4 import select_runs

        output = work_dir / "hash_test"
        output.mkdir()
        runs = select_runs(snapshot, task_id="CI-01", config_name="minimal", repeat_index=1)
        controller = RunController(output_dir=output)
        manifest = controller.generate_manifest(snapshot, runs)

        assert manifest["protocol_hash"] == snapshot.protocol_hash
        assert manifest["environment_hash"] == env_hash

    def test_manifest_headline(self, snapshot, work_dir):
        """Headline selection generates correct manifest."""
        from evals.reliability.run_phase4 import select_runs

        output = work_dir / "headline_test"
        output.mkdir()
        runs = select_runs(snapshot, headline=True)
        controller = RunController(output_dir=output)
        manifest = controller.generate_manifest(snapshot, runs, selection="headline")

        assert manifest["selection"] == "headline"
        assert manifest["total_runs"] == len(runs)
        assert len(manifest["task_ids"]) > 0
        assert manifest["repeats"] > 0

    def test_manifest_runner_git_sha(self, snapshot, work_dir):
        """Manifest contains runner_git_sha."""
        from evals.reliability.run_phase4 import select_runs

        output = work_dir / "sha_test"
        output.mkdir()
        runs = select_runs(snapshot, task_id="CI-01", config_name="minimal", repeat_index=1)
        controller = RunController(output_dir=output)
        manifest = controller.generate_manifest(snapshot, runs)

        assert "runner_git_sha" in manifest
        assert len(manifest["runner_git_sha"]) == 40 or manifest["runner_git_sha"] == "UNKNOWN"

    def test_manifest_started_at_iso(self, snapshot, work_dir):
        """started_at is an ISO timestamp."""
        from datetime import datetime
        from evals.reliability.run_phase4 import select_runs

        output = work_dir / "iso_test"
        output.mkdir()
        runs = select_runs(snapshot, task_id="CI-01", config_name="minimal", repeat_index=1)
        controller = RunController(output_dir=output)
        manifest = controller.generate_manifest(snapshot, runs)

        assert manifest["started_at"].endswith("Z")
        dt = datetime.fromisoformat(manifest["started_at"].replace("Z", "+00:00"))
        assert dt.year == 2026


class TestCheckpoint:
    """Test checkpoint.json write/read."""

    def test_checkpoint_written(self, snapshot, work_dir):
        """Checkpoint is written after _write_checkpoint."""
        output = work_dir / "ckpt_test"
        output.mkdir()
        controller = RunController(output_dir=output)
        controller._write_checkpoint(
            "CI-01::minimal::repeat-1",
            {"valid": 5, "invalid": 1},
            snapshot=snapshot,
        )

        assert output.joinpath("checkpoint.json").exists()
        data = json.loads(output.joinpath("checkpoint.json").read_text())
        assert data["last_completed_run_id"] == "CI-01::minimal::repeat-1"
        assert data["completed_count"] == 5
        assert data["failed_count"] == 1
        assert data["invalid_count"] == 1

    def test_checkpoint_contains_hashes(self, snapshot, work_dir, env_hash):
        """Checkpoint contains protocol and environment hashes."""
        output = work_dir / "ckpt_hash"
        output.mkdir()
        controller = RunController(output_dir=output)
        controller._write_checkpoint(
            "test-run", {"valid": 1, "invalid": 0}, snapshot=snapshot
        )

        data = json.loads(output.joinpath("checkpoint.json").read_text())
        assert data["protocol_hash"] == snapshot.protocol_hash
        assert data["environment_hash"] == env_hash

    def test_checkpoint_updated_at(self, snapshot, work_dir):
        """Checkpoint contains updated_at timestamp."""
        output = work_dir / "ckpt_time"
        output.mkdir()
        controller = RunController(output_dir=output)
        controller._write_checkpoint(
            "test-run", {"valid": 0, "invalid": 0}, snapshot=snapshot
        )

        data = json.loads(output.joinpath("checkpoint.json").read_text())
        assert data["updated_at"].endswith("Z")

    def test_checkpoint_load_missing(self, work_dir):
        """_load_checkpoint returns None when file doesn't exist."""
        output = work_dir / "ckpt_missing"
        output.mkdir()
        controller = RunController(output_dir=output)
        assert controller._load_checkpoint() is None

    def test_checkpoint_load_existing(self, snapshot, work_dir):
        """_load_checkpoint reads existing checkpoint."""
        output = work_dir / "ckpt_load"
        output.mkdir()
        controller = RunController(output_dir=output)
        controller._write_checkpoint(
            "test-run", {"valid": 3, "invalid": 0}, snapshot=snapshot
        )

        loaded = controller._load_checkpoint()
        assert loaded is not None
        assert loaded["completed_count"] == 3


class TestProgressTracking:
    """Test progress.json tracking."""

    def test_progress_written(self, work_dir):
        """Progress file is written after _write_progress."""
        output = work_dir / "prog_test"
        output.mkdir()
        controller = RunController(output_dir=output)
        started = time.perf_counter()
        controller._write_progress(
            completed=["run-1", "run-2"],
            failed=["run-3"],
            remaining=["run-4", "run-5"],
            started=started,
            total_runs=5,
        )

        assert output.joinpath("progress.json").exists()
        data = json.loads(output.joinpath("progress.json").read_text())
        assert data["total_runs"] == 5
        assert data["completed_runs"] == ["run-1", "run-2"]
        assert data["failed_runs"] == ["run-3"]
        assert data["remaining_runs"] == ["run-4", "run-5"]

    def test_progress_percent(self, work_dir):
        """Percent complete is calculated correctly."""
        output = work_dir / "prog_pct"
        output.mkdir()
        controller = RunController(output_dir=output)
        started = time.perf_counter()
        controller._write_progress(
            completed=["run-1", "run-2"],
            failed=["run-3"],
            remaining=["run-4", "run-5"],
            started=started,
            total_runs=5,
        )

        data = json.loads(output.joinpath("progress.json").read_text())
        # 3 of 5 = 60%
        assert data["percent_complete"] == 60.0

    def test_progress_timing(self, work_dir):
        """Elapsed and estimated remaining are present."""
        output = work_dir / "prog_time"
        output.mkdir()
        controller = RunController(output_dir=output)
        started = time.perf_counter() - 10.0  # 10 seconds ago
        controller._write_progress(
            completed=["run-1"],
            failed=[],
            remaining=["run-2", "run-3"],
            started=started,
            total_runs=3,
        )

        data = json.loads(output.joinpath("progress.json").read_text())
        assert data["elapsed_seconds"] >= 9.0
        assert data["estimated_remaining_seconds"] >= 0

    def test_progress_zero_runs(self, work_dir):
        """Progress handles zero total runs."""
        output = work_dir / "prog_zero"
        output.mkdir()
        controller = RunController(output_dir=output)
        started = time.perf_counter()
        controller._write_progress(
            completed=[], failed=[], remaining=[], started=started, total_runs=0
        )

        data = json.loads(output.joinpath("progress.json").read_text())
        assert data["percent_complete"] == 0.0


class TestResumeValidation:
    """Test resume validation (hash matching)."""

    def test_resume_matching_hashes(self, snapshot, work_dir):
        """Resume succeeds when checkpoint hashes match current."""
        output = work_dir / "resume_match"
        output.mkdir()
        controller = RunController(output_dir=output)
        controller._write_checkpoint(
            "test-run", {"valid": 1, "invalid": 0}, snapshot=snapshot
        )

        checkpoint = controller._load_checkpoint()
        controller._validate_resume(checkpoint, snapshot)  # No exception

    def test_resume_protocol_hash_mismatch(self, snapshot, work_dir):
        """Resume raises error when protocol_hash doesn't match."""
        output = work_dir / "resume_proto"
        output.mkdir()
        controller = RunController(output_dir=output)

        checkpoint_data = {
            "last_completed_run_id": "test-run",
            "completed_count": 1,
            "failed_count": 0,
            "invalid_count": 0,
            "protocol_hash": "wrong_hash",
            "environment_hash": _environment_hash(RELIABILITY / "environment.json"),
            "updated_at": "2026-01-01T00:00:00Z",
        }
        _atomic_write_json(output / "checkpoint.json", checkpoint_data)

        checkpoint = controller._load_checkpoint()
        with pytest.raises(ControllerError, match="RESUME_PROTOCOL_HASH_MISMATCH"):
            controller._validate_resume(checkpoint, snapshot)

    def test_resume_environment_hash_mismatch(self, snapshot, work_dir):
        """Resume raises error when environment_hash doesn't match."""
        output = work_dir / "resume_env"
        output.mkdir()
        controller = RunController(output_dir=output)

        checkpoint_data = {
            "last_completed_run_id": "test-run",
            "completed_count": 1,
            "failed_count": 0,
            "invalid_count": 0,
            "protocol_hash": snapshot.protocol_hash,
            "environment_hash": "wrong_env_hash",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        _atomic_write_json(output / "checkpoint.json", checkpoint_data)

        checkpoint = controller._load_checkpoint()
        with pytest.raises(ControllerError, match="RESUME_ENVIRONMENT_HASH_MISMATCH"):
            controller._validate_resume(checkpoint, snapshot)

    def test_resume_skips_completed_runs(self, work_dir):
        """Resume detects already completed runs from raw/invalid JSONL."""
        output = work_dir / "resume_skip"
        output.mkdir()
        controller = RunController(output_dir=output)

        run_id = "CI-01::minimal::repeat-1"
        record = {"benchmark_run_id": run_id, "task_id": "CI-01"}
        raw_path = output / "raw.jsonl"
        raw_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

        completed = controller._get_completed_run_ids()
        assert run_id in completed

    def test_resume_detects_invalid_jsonl(self, work_dir):
        """Resume detects runs from invalid.jsonl too."""
        output = work_dir / "resume_invalid"
        output.mkdir()
        controller = RunController(output_dir=output)

        run_id = "CI-02::minimal::repeat-1"
        record = {"benchmark_run_id": run_id, "task_id": "CI-02"}
        invalid_path = output / "invalid.jsonl"
        invalid_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

        completed = controller._get_completed_run_ids()
        assert run_id in completed


class TestAtomicWrite:
    """Test atomic write helper."""

    def test_atomic_write_creates_file(self, work_dir):
        """_atomic_write_json creates the target file."""
        path = work_dir / "atomic_create.json"
        _atomic_write_json(path, {"key": "value"})

        assert path.exists()
        data = json.loads(path.read_text())
        assert data == {"key": "value"}

    def test_atomic_write_overwrites(self, work_dir):
        """_atomic_write_json overwrites existing file."""
        path = work_dir / "atomic_overwrite.json"
        _atomic_write_json(path, {"v": 1})
        _atomic_write_json(path, {"v": 2})

        data = json.loads(path.read_text())
        assert data == {"v": 2}

    def test_atomic_write_creates_parent_dirs(self, work_dir):
        """_atomic_write_json creates parent directories."""
        path = work_dir / "sub" / "dir" / "atomic_nested.json"
        _atomic_write_json(path, {"nested": True})

        assert path.exists()


class TestEnvironmentHash:
    """Test environment hash computation."""

    def test_env_hash_deterministic(self, env_hash):
        """Environment hash is deterministic."""
        hash2 = _environment_hash(RELIABILITY / "environment.json")
        assert env_hash == hash2

    def test_env_hash_sha256_format(self, env_hash):
        """Environment hash is a hex SHA256 string."""
        assert len(env_hash) == 64
        assert all(c in "0123456789abcdef" for c in env_hash)


class TestRunControllerInit:
    """Test RunController initialization."""

    def test_creates_output_dir(self, work_dir):
        """Controller creates output directory if needed."""
        output = work_dir / "new" / "output"
        controller = RunController(output_dir=output)
        assert output.exists()

    def test_paths_set(self, work_dir):
        """Controller sets correct paths."""
        output = work_dir / "paths"
        output.mkdir()
        controller = RunController(output_dir=output)

        assert controller.manifest_path == output / "run_manifest.json"
        assert controller.checkpoint_path == output / "checkpoint.json"
        assert controller.progress_path == output / "progress.json"
