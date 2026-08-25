"""Deterministic E6-B manual process-resume scenarios."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from lhas.domain.enums import AttemptStatus, EventType, ExecutionStatus, RunStatus, TaskStatus
from lhas.domain.models import Project, Task
from lhas import HARNESS_VERSION
from lhas.executors.protocol import ExecutionRequest, ExecutionResult
from lhas.orchestrator_v2 import RecoveringOrchestrator
from lhas.persistence.database import Database
from lhas.persistence.event_store import EventStore
from lhas.persistence.repositories import (
    AttemptRepository,
    ProjectRepository,
    RunRepository,
    TaskRepository,
    WorkspaceSessionBindingRepository,
)
from lhas.recovery import DefaultRecoveryPolicy
from lhas.task_service import create_task
from lhas.validation import ValidationCheck, ValidationResult
from lhas.workspace import RunWorkspaceManager, WorkspaceSessionBindingMismatch


class ProcessDeath(BaseException):
    """Simulates a process disappearing; Orchestrator intentionally catches Exception only."""


class DyingExecutor:
    name = "DyingExecutor"

    def __init__(self, workspace, calls: list[int]):
        self.workspace = workspace
        self.calls = calls

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls.append(request.attempt_number)
        await self.workspace.edit_file("src/calculator.py", "return a - b", "return a + b")
        raise ProcessDeath("simulated process death")


class RepairExecutor:
    name = "RepairExecutor"

    def __init__(self, workspace, calls: list[int]):
        self.workspace = workspace
        self.calls = calls

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls.append(request.attempt_number)
        content = (self.workspace.root / "src" / "calculator.py").read_text(encoding="utf-8")
        if "return a + b" not in content:
            await self.workspace.edit_file("src/calculator.py", "return a - b", "return a + b")
        return ExecutionResult(status=ExecutionStatus.SUCCESS, output="repaired")


class WorkspaceStateValidator:
    def __init__(self, session_root: Path, *, fail_recovery_once: bool):
        self.session_root = session_root
        self.fail_recovery_once = fail_recovery_once
        self.calls: list[int] = []

    async def validate(self, *, task, attempt, result):
        self.calls.append(attempt.attempt_number)
        text = (self.session_root / "work" / "src" / "calculator.py").read_text(encoding="utf-8")
        good = "return a + b" in text
        if self.fail_recovery_once and attempt.status is AttemptStatus.CRASHED:
            good = False
        return ValidationResult(
            attempt_id=attempt.id,
            passed=good,
            checks=[ValidationCheck(name="workspace_repaired", passed=good)],
            evidence="workspace contains repaired implementation" if good else "workspace still needs retry",
        )


def _source(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "src" / "calculator.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    return root


def _create_persisted_run(tmp_path: Path, *, max_attempts: int = 2):
    db_path = tmp_path / "resume.sqlite"
    source = _source(tmp_path / "source")
    db = Database(db_path)
    db.init_db()
    project = ProjectRepository(db).create(Project(name="resume-project", root_path=str(source)))
    task = create_task(
        db,
        project_id=project.id,
        title="resume calculator",
        objective="repair calculator",
        max_attempts=max_attempts,
        timeout_seconds=5,
    )
    calls: list[int] = []
    manager = RunWorkspaceManager(db, tmp_path / "sessions")
    orch = RecoveringOrchestrator(
        db,
        workspace_executor_factory=lambda workspace: DyingExecutor(workspace, calls),
        workspace_manager=manager,
        validator=WorkspaceStateValidator(tmp_path / "never", fail_recovery_once=False),
        recovery_policy=DefaultRecoveryPolicy(context_policy="CP-2"),
        harness_version=HARNESS_VERSION,
    )
    with pytest.raises(ProcessDeath):
        asyncio.run(orch.execute_task(task.id))
    run = RunRepository(db).list_for_task(task.id)[0]
    assert run.status is RunStatus.RUNNING
    assert AttemptRepository(db).list_for_run(run.id)[0].status is AttemptStatus.RUNNING
    assert calls == [1]
    db.close()
    return db_path, source, run.id


def _resume(tmp_path: Path, db_path: Path, run_id: str, *, retry: bool):
    db = Database(db_path)
    db.init_db()
    manager = RunWorkspaceManager(db, tmp_path / "sessions")
    validator = WorkspaceStateValidator(tmp_path / "sessions" / run_id, fail_recovery_once=retry)
    calls: list[int] = []
    orch = RecoveringOrchestrator(
        db,
        workspace_executor_factory=lambda workspace: RepairExecutor(workspace, calls),
        workspace_manager=manager,
        validator=validator,
        recovery_policy=DefaultRecoveryPolicy(context_policy="CP-2"),
        harness_version=HARNESS_VERSION,
    )
    run = asyncio.run(orch.resume_run(run_id))
    return db, manager, orch, validator, calls, run


def test_manual_resume_validates_durable_workspace_without_retry(tmp_path):
    db_path, _source_root, run_id = _create_persisted_run(tmp_path)
    db, manager, orch, validator, calls, run = _resume(tmp_path, db_path, run_id, retry=False)
    try:
        assert run.status is RunStatus.COMPLETED
        attempts = AttemptRepository(db).list_for_run(run_id)
        assert [a.status for a in attempts] == [AttemptStatus.CRASHED]
        assert calls == []
        assert validator.calls == [1]
        assert TaskRepository(db).get(run.task_id).status is TaskStatus.COMPLETED
        assert WorkspaceSessionBindingRepository(db).get_by_run(run_id).state == "COMPLETED"
        event_types = [e.event_type for e in EventStore(db).list_for_run(run_id)]
        assert EventType.RUN_RESUME_STARTED in event_types
        assert EventType.RUN_RESUME_COMPLETED in event_types
        recovery = next(e for e in EventStore(db).list_for_run(run_id) if e.event_type is EventType.WORKSPACE_RECOVERY_STATE)
        assert "diff" not in recovery.payload
        assert "patch_sha256" in recovery.payload
        # Replaying a terminal run is a no-op: no new attempt or executor call.
        again = asyncio.run(orch.resume_run(run_id))
        assert again.id == run_id and len(AttemptRepository(db).list_for_run(run_id)) == 1
        assert calls == []
    finally:
        db.close()


def test_manual_resume_recovery_creates_cp3_retry_in_same_workspace(tmp_path):
    db_path, _source_root, run_id = _create_persisted_run(tmp_path)
    db, manager, _orch, validator, calls, run = _resume(tmp_path, db_path, run_id, retry=True)
    try:
        assert run.status is RunStatus.COMPLETED
        attempts = AttemptRepository(db).list_for_run(run_id)
        assert [a.status for a in attempts] == [AttemptStatus.CRASHED, AttemptStatus.COMPLETED]
        assert calls == [2]
        assert validator.calls == [1, 2]
        binding = WorkspaceSessionBindingRepository(db).get_by_run(run_id)
        assert binding.state == "COMPLETED"
        assert Path(binding.session_root) == (tmp_path / "sessions" / run_id).resolve()
        assert (Path(binding.session_root) / "work" / "src" / "calculator.py").read_text(encoding="utf-8").find("return a + b") >= 0
        checkpoint = __import__("lhas.checkpoint", fromlist=["CheckpointRepository"]).CheckpointRepository(db).latest_for_run(run_id)
        assert checkpoint is not None
        assert checkpoint.working_state.candidate_patch_summary["files_changed"] == 1
        second = attempts[1]
        snapshot = __import__("lhas.persistence.phaseb_repos", fromlist=["ContextSnapshotRepository"]).ContextSnapshotRepository(db).get(second.context_snapshot_id)
        assert snapshot.policy == "CP-3"
        events = EventStore(db).list_for_run(run_id)
        assert any(e.event_type is EventType.RUN_RESUME_COMPLETED for e in events)
        assert any(e.event_type is EventType.RECOVERY_STARTED for e in events)
    finally:
        db.close()


def test_resume_rejects_binding_manifest_identity_mismatch(tmp_path):
    db_path, _source_root, run_id = _create_persisted_run(tmp_path)
    db = Database(db_path)
    db.init_db()
    binding = WorkspaceSessionBindingRepository(db).get_by_run(run_id)
    manifest_path = Path(binding.session_root) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["run_id"] = "other-run"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manager = RunWorkspaceManager(db, tmp_path / "sessions")
    task = TaskRepository(db).get(RunRepository(db).get(run_id).task_id)
    with pytest.raises(WorkspaceSessionBindingMismatch, match="WORKSPACE_SESSION_BINDING_MISMATCH"):
        manager.reopen_for_run(task, RunRepository(db).get(run_id))
    db.close()
