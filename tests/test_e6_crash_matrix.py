"""E6-C deterministic crash-window and idempotency coverage."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from lhas import HARNESS_VERSION
from lhas.checkpoint import CheckpointRepository
from lhas.domain.enums import AttemptStatus, RunStatus, TaskStatus
from lhas.domain.models import Project
from lhas.executors.protocol import ExecutionRequest, ExecutionResult
from lhas.orchestrator_v2 import RecoveringOrchestrator
from lhas.persistence.database import Database
from lhas.persistence.phaseb_repos import (
    ContextSnapshotRepository,
    FailureReportRepository,
    RecoveryActionRepository,
    ValidationResultRepository,
)
from lhas.persistence.repositories import (
    AttemptRepository,
    ProjectRepository,
    RunRepository,
    TaskRepository,
    WorkspaceSessionBindingRepository,
)
from lhas.resume import CrashPoint
from lhas.task_service import create_task
from lhas.validation import ValidationCheck, ValidationResult
from lhas.workspace import RunWorkspaceManager
from lhas.workspace import DurableWorkspaceSession, WorkspaceSessionBinding, WorkspaceSessionCorrupt


class ProcessDeath(BaseException):
    pass


class CrashOnce:
    def __init__(self, point: CrashPoint):
        self.point = point
        self.fired = False

    def hit(self, point, **context):
        if point is self.point and not self.fired:
            self.fired = True
            raise ProcessDeath(point.value)


class ScriptedExecutor:
    name = "ScriptedExecutor"

    def __init__(self, workspace, calls: list[int]):
        self.workspace = workspace
        self.calls = calls

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls.append(request.attempt_number)
        return ExecutionResult(status=__import__("lhas.domain.enums", fromlist=["ExecutionStatus"]).ExecutionStatus.SUCCESS,
                                output=f"attempt-{request.attempt_number}")


class ScriptedValidator:
    def __init__(self, outcomes: dict[int, bool]):
        self.outcomes = outcomes
        self.calls: list[int] = []

    async def validate(self, *, task, attempt, result):
        self.calls.append(attempt.attempt_number)
        passed = self.outcomes.get(attempt.attempt_number, True)
        return ValidationResult(
            attempt_id=attempt.id,
            passed=passed,
            checks=[ValidationCheck(name="scripted", passed=passed)],
            evidence=f"scripted={passed}",
        )


def _source(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "src" / "fixture.py").write_text("value = 1\n", encoding="utf-8")
    return root


def _start_crashed_run(tmp_path: Path, point: CrashPoint, outcomes: dict[int, bool]):
    db_path = tmp_path / f"{point.value}.sqlite"
    source = _source(tmp_path / point.value)
    db = Database(db_path)
    db.init_db()
    project = ProjectRepository(db).create(Project(name=point.value, root_path=str(source)))
    task = create_task(db, project_id=project.id, title=point.value, objective="resume fixture", max_attempts=3, timeout_seconds=5)
    calls: list[int] = []
    validator = ScriptedValidator(outcomes)
    manager = RunWorkspaceManager(db, tmp_path / "sessions")
    orch = RecoveringOrchestrator(
        db,
        workspace_executor_factory=lambda workspace: ScriptedExecutor(workspace, calls),
        workspace_manager=manager,
        validator=validator,
        crash_injector=CrashOnce(point),
        harness_version=HARNESS_VERSION,
    )
    with pytest.raises(ProcessDeath):
        asyncio.run(orch.execute_task(task.id))
    run = RunRepository(db).list_for_task(task.id)[0]
    binding = WorkspaceSessionBindingRepository(db).get_by_run(run.id)
    before_root = binding.session_root if binding else None
    db.close()
    return db_path, task.id, run.id, source, before_root, calls, validator.calls


def _resume(tmp_path: Path, db_path: Path, run_id: str, outcomes: dict[int, bool]):
    db = Database(db_path)
    db.init_db()
    manager = RunWorkspaceManager(db, tmp_path / "sessions")
    calls: list[int] = []
    validator = ScriptedValidator(outcomes)
    orch = RecoveringOrchestrator(
        db,
        workspace_executor_factory=lambda workspace: ScriptedExecutor(workspace, calls),
        workspace_manager=manager,
        validator=validator,
        harness_version=HARNESS_VERSION,
    )
    run = asyncio.run(orch.resume_run(run_id))
    return db, run, calls, validator.calls


def _counts(db, run_id: str):
    attempts = AttemptRepository(db).list_for_run(run_id)
    validations = sum(len(ValidationResultRepository(db).list_for_attempt(a.id)) for a in attempts)
    reports = sum(len(FailureReportRepository(db).list_for_attempt(a.id)) for a in attempts)
    actions = sum(len(RecoveryActionRepository(db).list_for_attempt(a.id)) for a in attempts)
    checkpoints = len(CheckpointRepository(db).list_for_run(run_id))
    return {
        "attempt_count": len(attempts),
        "validation_record_count": validations,
        "failure_report_count": reports,
        "recovery_action_count": actions,
        "checkpoint_count": checkpoints,
        "attempts": [a.status.value for a in attempts],
    }


@pytest.mark.parametrize("point", list(CrashPoint))
def test_crash_window_matrix_recovers_without_duplicate_durable_phases(tmp_path, point):
    # W6/W7 need a validation failure to create the persisted recovery phase;
    # all other windows use a passing deterministic validator.
    outcomes = {1: False, 2: True} if point in {CrashPoint.AFTER_VALIDATION_PERSISTED, CrashPoint.AFTER_FAILURE_CLASSIFIED, CrashPoint.AFTER_RECOVERY_DECIDED, CrashPoint.AFTER_CHECKPOINT_CREATED} else {1: True}
    db_path, task_id, run_id, source, before_root, _calls_a, _validator_a = _start_crashed_run(tmp_path, point, outcomes)
    db, run, calls_b, validator_b = _resume(tmp_path, db_path, run_id, outcomes)
    try:
        assert run.status is RunStatus.COMPLETED
        assert TaskRepository(db).get(task_id).status is TaskStatus.COMPLETED
        counts = _counts(db, run_id)
        assert counts["validation_record_count"] == (2 if outcomes[1] is False else 1)
        assert counts["failure_report_count"] == (1 if outcomes[1] is False else 0)
        assert counts["recovery_action_count"] == (1 if outcomes[1] is False else 0)
        if point in {CrashPoint.AFTER_ATTEMPT_STARTED, CrashPoint.AFTER_CONTEXT_BUILT}:
            assert counts["attempts"] == ["CRASHED"]
            assert calls_b == []
            assert validator_b == [1]
        elif point in {CrashPoint.AFTER_VALIDATION_PERSISTED, CrashPoint.AFTER_FAILURE_CLASSIFIED, CrashPoint.AFTER_RECOVERY_DECIDED, CrashPoint.AFTER_CHECKPOINT_CREATED}:
            assert counts["attempts"] == ["COMPLETED", "COMPLETED"]
            assert calls_b == [2]
            assert validator_b == [2]
        else:
            assert counts["attempts"] == ["COMPLETED"]
        binding = WorkspaceSessionBindingRepository(db).get_by_run(run_id)
        assert binding.state == "COMPLETED"
        if before_root is not None:
            assert binding.session_root == before_root
        assert source.joinpath("src", "fixture.py").read_text(encoding="utf-8") == "value = 1\n"
        # A second sequential resume is terminal and cannot add durable rows.
        before = _counts(db, run_id)
        second = asyncio.run(RecoveringOrchestrator(
            db,
            workspace_executor_factory=lambda workspace: ScriptedExecutor(workspace, []),
            workspace_manager=RunWorkspaceManager(db, tmp_path / "sessions"),
            validator=ScriptedValidator(outcomes),
            harness_version=HARNESS_VERSION,
        ).resume_run(run_id))
        assert second.status is RunStatus.COMPLETED
        assert _counts(db, run_id) == before
    finally:
        db.close()


def test_resume_continues_multiple_recovery_attempts_after_interruption(tmp_path):
    outcomes = {1: False, 2: False, 3: True}
    db_path, task_id, run_id, source, _root, _calls_a, _validator_a = _start_crashed_run(tmp_path, CrashPoint.AFTER_ATTEMPT_STARTED, outcomes)
    db, run, calls_b, validator_b = _resume(tmp_path, db_path, run_id, outcomes)
    try:
        assert run.status is RunStatus.COMPLETED
        assert TaskRepository(db).get(task_id).status is TaskStatus.COMPLETED
        attempts = AttemptRepository(db).list_for_run(run_id)
        assert [a.attempt_number for a in attempts] == [1, 2, 3]
        assert [a.status for a in attempts] == [AttemptStatus.CRASHED, AttemptStatus.COMPLETED, AttemptStatus.COMPLETED]
        assert calls_b == [2, 3]
        assert validator_b == [1, 2, 3]
        snapshots = [ContextSnapshotRepository(db).get(a.context_snapshot_id) for a in attempts[1:]]
        assert all(s is not None and s.policy == "CP-3" for s in snapshots)
        counts = _counts(db, run_id)
        assert counts["validation_record_count"] == 3
        assert counts["failure_report_count"] == 2
        assert counts["recovery_action_count"] == 2
        assert counts["checkpoint_count"] == 2
        assert source.joinpath("src", "fixture.py").read_text(encoding="utf-8") == "value = 1\n"
    finally:
        db.close()


def test_creating_workspace_binding_recovers_absent_and_valid_existing_roots(tmp_path):
    db_path, task_id, run_id, source, _root, _calls, _validator = _start_crashed_run(tmp_path, CrashPoint.AFTER_RUN_STARTED, {1: True})
    db = Database(db_path)
    db.init_db()
    task = TaskRepository(db).get(task_id)
    run = RunRepository(db).get(run_id)
    manager = RunWorkspaceManager(db, tmp_path / "sessions")
    session_id, session_root = manager._identity(task, run)
    binding_repo = WorkspaceSessionBindingRepository(db)
    binding_repo.create(WorkspaceSessionBinding(session_id=session_id, run_id=run_id, task_id=task_id, session_root=str(session_root), state="CREATING"))
    # The absent-root CREATING record is completed in place, never duplicated.
    session = manager.ensure_for_run(task, run)
    assert session.manifest.session_id == session_id
    assert binding_repo.get_by_run(run_id).state == "OPEN"
    db.close()

    # A second deterministic fixture with a valid root is promoted rather than recreated.
    db2 = Database(tmp_path / "valid.sqlite")
    db2.init_db()
    project = ProjectRepository(db2).create(Project(name="valid-existing", root_path=str(source)))
    task2 = create_task(db2, project_id=project.id, title="valid", objective="x", max_attempts=1)
    run2 = __import__("lhas.domain.models", fromlist=["Run"]).Run(task_id=task2.id, status=RunStatus.RUNNING)
    RunRepository(db2).create(run2)
    manager2 = RunWorkspaceManager(db2, tmp_path / "valid-sessions")
    sid2, root2 = manager2._identity(task2, run2)
    DurableWorkspaceSession.create(source, root2, session_id=sid2, run_id=run2.id, task_id=task2.id)
    WorkspaceSessionBindingRepository(db2).create(WorkspaceSessionBinding(session_id=sid2, run_id=run2.id, task_id=task2.id, session_root=str(root2), state="CREATING"))
    reopened = manager2.ensure_for_run(task2, run2)
    assert reopened.manifest.session_id == sid2
    assert WorkspaceSessionBindingRepository(db2).get_by_run(run2.id).state == "OPEN"
    db2.close()


def test_creating_workspace_binding_rejects_partial_root(tmp_path):
    db_path, task_id, run_id, _source, _root, _calls, _validator = _start_crashed_run(tmp_path, CrashPoint.AFTER_RUN_STARTED, {1: True})
    db = Database(db_path)
    db.init_db()
    task = TaskRepository(db).get(task_id)
    run = RunRepository(db).get(run_id)
    manager = RunWorkspaceManager(db, tmp_path / "sessions")
    session_id, session_root = manager._identity(task, run)
    session_root.mkdir(parents=True)
    (session_root / "manifest.json").write_text("{}", encoding="utf-8")
    WorkspaceSessionBindingRepository(db).create(WorkspaceSessionBinding(session_id=session_id, run_id=run_id, task_id=task_id, session_root=str(session_root), state="CREATING"))
    with pytest.raises(WorkspaceSessionCorrupt):
        manager.ensure_for_run(task, run)
    assert WorkspaceSessionBindingRepository(db).get_by_run(run_id).state == "CREATING"
    db.close()
