"""Deterministic E6-D post-non-success workspace outcome arbitration."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from lhas import HARNESS_VERSION
from lhas.checkpoint import CheckpointRepository
from lhas.domain.enums import (
    AttemptStatus,
    EventType,
    ExecutionStatus,
    FailureClass,
    FailureType,
    RunStatus,
    TaskStatus,
)
from lhas.domain.models import Project
from lhas.executors.protocol import ExecutionRequest, ExecutionResult
from lhas.failure import FailureReport
from lhas.orchestrator_v2 import RecoveringOrchestrator
from lhas.persistence.database import Database
from lhas.persistence.event_store import EventStore
from lhas.persistence.phaseb_repos import (
    FailureReportRepository,
    RecoveryActionRepository,
    ValidationResultRepository,
)
from lhas.persistence.repositories import (
    AttemptRepository,
    ProjectRepository,
    RunRepository,
    TaskRepository,
)
from lhas.resume import CrashPoint
from lhas.task_service import create_task
from lhas.validation import ValidationCheck, ValidationResult
from lhas.workspace import RunWorkspaceManager


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


class WorkspaceOutcomeExecutor:
    name = "WorkspaceOutcomeExecutor"

    def __init__(self, workspace, calls: list[int], *, failures=(), mutations=(), timeouts=()):
        self.workspace = workspace
        self.calls = calls
        self.failures = set(failures)
        self.mutations = set(mutations)
        self.timeouts = set(timeouts)

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        number = request.attempt_number
        self.calls.append(number)
        if number in self.mutations:
            path = Path(self.workspace.root) / "src" / "fixture.py"
            current = path.read_text(encoding="utf-8")
            if "value = 1" not in current:
                await self.workspace.edit_file("src/fixture.py", "value = 0", "value = 1")
        if number in self.timeouts:
            await asyncio.sleep(1.0)
        if number in self.failures:
            return ExecutionResult(
                status=ExecutionStatus.FAILURE,
                output="turn budget exhausted after durable edits",
                error_type="AGENT_TURN_LIMIT",
                error_message="deterministic turn limit",
            )
        return ExecutionResult(status=ExecutionStatus.SUCCESS, output="completed")


class NonWorkspaceFailureExecutor:
    name = "NonWorkspaceFailureExecutor"

    def __init__(self, calls: list[int]):
        self.calls = calls

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls.append(request.attempt_number)
        return ExecutionResult(
            status=ExecutionStatus.FAILURE,
            error_type="AGENT_TURN_LIMIT",
            error_message="deterministic turn limit",
        )


class WorkspaceOutcomeValidator:
    def __init__(self, sessions_root: Path, outcomes: dict[int, bool] | None = None):
        self.sessions_root = Path(sessions_root)
        self.outcomes = outcomes or {}
        self.calls: list[int] = []
        self.result_statuses: list[ExecutionStatus] = []

    async def validate(self, *, task, attempt, result):
        self.calls.append(attempt.attempt_number)
        self.result_statuses.append(result.status)
        path = self.sessions_root / attempt.run_id / "work" / "src" / "fixture.py"
        workspace_passes = path.read_text(encoding="utf-8").strip() == "value = 1"
        passed = self.outcomes.get(attempt.attempt_number, workspace_passes)
        return ValidationResult(
            attempt_id=attempt.id,
            passed=passed,
            checks=[ValidationCheck(name="workspace_outcome", passed=passed)],
            evidence=f"workspace_pass={passed}",
        )


def _make_case(
    tmp_path: Path,
    *,
    max_attempts: int = 3,
    timeout_seconds: float = 1.0,
    failures=(),
    mutations=(),
    timeouts=(),
    validation_outcomes: dict[int, bool] | None = None,
    crash_point: CrashPoint | None = None,
    workspace_enabled: bool = True,
):
    db_path = tmp_path / "run.sqlite"
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src" / "fixture.py").write_text("value = 0\n", encoding="utf-8")
    db = Database(db_path)
    db.init_db()
    project = ProjectRepository(db).create(Project(name="e6d", root_path=str(source) if workspace_enabled else None))
    task = create_task(
        db,
        project_id=project.id,
        title="E6-D outcome arbitration",
        objective="repair fixture",
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
    )
    calls: list[int] = []
    sessions_root = tmp_path / "sessions"
    validator = WorkspaceOutcomeValidator(sessions_root, validation_outcomes)
    kwargs = {
        "db": db,
        "validator": validator,
        "crash_injector": CrashOnce(crash_point) if crash_point else None,
        "harness_version": HARNESS_VERSION,
    }
    if workspace_enabled:
        manager = RunWorkspaceManager(db, sessions_root)
        kwargs["workspace_manager"] = manager
        kwargs["workspace_executor_factory"] = lambda workspace: WorkspaceOutcomeExecutor(
            workspace, calls, failures=failures, mutations=mutations, timeouts=timeouts
        )
    else:
        kwargs["executor_factory"] = lambda: NonWorkspaceFailureExecutor(calls)
    orchestrator = RecoveringOrchestrator(**kwargs)
    return db, task, calls, validator, orchestrator, db_path, source, sessions_root


def _counts(db: Database, run_id: str) -> dict[str, int]:
    attempts = AttemptRepository(db).list_for_run(run_id)
    return {
        "attempts": len(attempts),
        "validations": sum(len(ValidationResultRepository(db).list_for_attempt(a.id)) for a in attempts),
        "reports": sum(len(FailureReportRepository(db).list_for_attempt(a.id)) for a in attempts),
        "actions": sum(len(RecoveryActionRepository(db).list_for_attempt(a.id)) for a in attempts),
        "checkpoints": len(CheckpointRepository(db).list_for_run(run_id)),
    }


def test_failed_turn_limit_passes_validation_without_next_attempt(tmp_path):
    db, task, calls, validator, orchestrator, *_ = _make_case(
        tmp_path,
        failures={1, 2},
        mutations={2},
        validation_outcomes={1: False, 2: True},
    )
    try:
        run = asyncio.run(orchestrator.execute_task(task.id))
        attempts = AttemptRepository(db).list_for_run(run.id)
        attempt2 = attempts[1]
        validation2 = ValidationResultRepository(db).get_for_attempt(attempt2.id)
        assert run.status is RunStatus.COMPLETED
        assert TaskRepository(db).get(task.id).status is TaskStatus.COMPLETED
        assert [attempt.status for attempt in attempts] == [AttemptStatus.FAILED, AttemptStatus.FAILED]
        assert attempt2.error_type == "AGENT_TURN_LIMIT"
        assert validation2 is not None and validation2.passed is True
        assert json.loads(run.result)["status"] == "FAILURE"
        assert json.loads(run.result)["validation"] is True
        assert calls == [1, 2]
        assert validator.calls == [1, 2]
        assert validator.result_statuses == [ExecutionStatus.FAILURE, ExecutionStatus.FAILURE]
        assert FailureReportRepository(db).get_for_attempt(attempt2.id) is None
        assert RecoveryActionRepository(db).get_for_attempt(attempt2.id) is None
        assert not [c for c in CheckpointRepository(db).list_for_run(run.id) if c.attempt_id == attempt2.id]

        events = EventStore(db).list_for_run(run.id)
        attempt2_events = [event for event in events if event.attempt_id == attempt2.id]
        event_types = [event.event_type for event in attempt2_events]
        assert event_types.index(EventType.ATTEMPT_FAILED) < event_types.index(EventType.VALIDATION_STARTED)
        assert event_types.index(EventType.VALIDATION_STARTED) < event_types.index(EventType.VALIDATION_PASSED)
        validation_started = next(event for event in attempt2_events if event.event_type is EventType.VALIDATION_STARTED)
        assert validation_started.payload == {
            "outcome_arbitration": True,
            "executor_attempt_status": "FAILED",
            "executor_error_type": "AGENT_TURN_LIMIT",
        }
    finally:
        db.close()


def test_failed_validator_preserves_classification_recovery_checkpoint_and_cp3(tmp_path):
    db, task, calls, validator, orchestrator, *_ = _make_case(
        tmp_path,
        max_attempts=2,
        failures={1, 2},
        validation_outcomes={1: False, 2: False},
    )
    try:
        run = asyncio.run(orchestrator.execute_task(task.id))
        attempts = AttemptRepository(db).list_for_run(run.id)
        assert run.status is RunStatus.ESCALATED
        assert [attempt.status for attempt in attempts] == [AttemptStatus.FAILED, AttemptStatus.FAILED]
        assert validator.calls == [1, 2]
        assert _counts(db, run.id) == {"attempts": 2, "validations": 2, "reports": 2, "actions": 2, "checkpoints": 2}
        second_snapshot = __import__("lhas.persistence.phaseb_repos", fromlist=["ContextSnapshotRepository"]).ContextSnapshotRepository(db).get(attempts[1].context_snapshot_id)
        assert second_snapshot is not None and second_snapshot.policy == "CP-3"
        assert calls == [1, 2]
    finally:
        db.close()


def test_timeout_passes_validation_without_retry_and_preserves_timeout(tmp_path):
    db, task, calls, validator, orchestrator, *_ = _make_case(
        tmp_path,
        timeout_seconds=0.01,
        timeouts={1},
        mutations={1},
        validation_outcomes={1: True},
    )
    try:
        run = asyncio.run(orchestrator.execute_task(task.id))
        attempt = AttemptRepository(db).list_for_run(run.id)[0]
        assert run.status is RunStatus.COMPLETED
        assert TaskRepository(db).get(task.id).status is TaskStatus.COMPLETED
        assert attempt.status is AttemptStatus.TIMED_OUT
        assert validator.calls == [1]
        assert validator.result_statuses == [ExecutionStatus.FAILURE]
        assert calls == [1]
        assert _counts(db, run.id) == {"attempts": 1, "validations": 1, "reports": 0, "actions": 0, "checkpoints": 0}
        assert json.loads(run.result)["status"] == "FAILURE"
    finally:
        db.close()


def test_timeout_validation_failure_uses_normal_recovery_path(tmp_path):
    db, task, calls, validator, orchestrator, *_ = _make_case(
        tmp_path,
        max_attempts=1,
        timeout_seconds=0.01,
        timeouts={1},
        validation_outcomes={1: False},
    )
    try:
        run = asyncio.run(orchestrator.execute_task(task.id))
        attempt = AttemptRepository(db).list_for_run(run.id)[0]
        assert run.status is RunStatus.ESCALATED
        assert attempt.status is AttemptStatus.TIMED_OUT
        assert validator.calls == [1]
        assert _counts(db, run.id) == {"attempts": 1, "validations": 1, "reports": 1, "actions": 1, "checkpoints": 1}
        assert calls == [1]
    finally:
        db.close()


def test_non_workspace_failure_keeps_hv12_no_validator_behavior(tmp_path):
    db, task, calls, validator, orchestrator, *_ = _make_case(tmp_path, workspace_enabled=False, max_attempts=1)
    try:
        run = asyncio.run(orchestrator.execute_task(task.id))
        assert run.status is RunStatus.ESCALATED
        assert validator.calls == []
        assert calls == [1]
        assert ValidationResultRepository(db).list_for_attempt(AttemptRepository(db).list_for_run(run.id)[0].id) == []
    finally:
        db.close()


def _crashed_case(
    tmp_path,
    crash_point: CrashPoint,
    *,
    validator_outcomes: dict[int, bool],
    failures=(),
    mutations=(),
):
    db, task, calls, validator, orchestrator, db_path, source, sessions_root = _make_case(
        tmp_path,
        validation_outcomes=validator_outcomes,
        failures=failures,
        mutations=mutations,
        crash_point=crash_point,
    )
    with pytest.raises(ProcessDeath):
        asyncio.run(orchestrator.execute_task(task.id))
    run = RunRepository(db).list_for_task(task.id)[0]
    db.close()
    return db_path, task.id, run.id, calls, validator.calls, source, sessions_root


def _resume_case(db_path: Path, task_id: str, run_id: str, sessions_root: Path, *, validator_outcomes, failures=(), mutations=()):
    db = Database(db_path)
    db.init_db()
    calls: list[int] = []
    validator = WorkspaceOutcomeValidator(sessions_root, validator_outcomes)
    orchestrator = RecoveringOrchestrator(
        db,
        workspace_executor_factory=lambda workspace: WorkspaceOutcomeExecutor(
            workspace, calls, failures=failures, mutations=mutations
        ),
        workspace_manager=RunWorkspaceManager(db, sessions_root),
        validator=validator,
        harness_version=HARNESS_VERSION,
    )
    run = asyncio.run(orchestrator.resume_run(run_id))
    return db, run, calls, validator


def test_w_a_crash_after_failed_result_resumes_and_validates_once(tmp_path):
    db_path, task_id, run_id, _calls, _validator_calls, _source, sessions_root = _crashed_case(
        tmp_path,
        CrashPoint.AFTER_EXECUTOR_PERSISTED,
        validator_outcomes={1: True},
        failures={1},
        mutations={1},
    )
    db_before = Database(db_path)
    try:
        attempt = AttemptRepository(db_before).list_for_run(run_id)[0]
        assert attempt.status is AttemptStatus.FAILED
        assert attempt.error_type == "AGENT_TURN_LIMIT"
        assert ValidationResultRepository(db_before).get_for_attempt(attempt.id) is None
    finally:
        db_before.close()
    db, run, calls, validator = _resume_case(
        db_path, task_id, run_id, sessions_root, validator_outcomes={1: True}
    )
    try:
        assert run.status is RunStatus.COMPLETED
        attempt = AttemptRepository(db).list_for_run(run_id)[0]
        assert attempt.status is AttemptStatus.FAILED
        assert attempt.error_type == "AGENT_TURN_LIMIT"
        assert calls == []
        assert validator.calls == [1]
        assert _counts(db, run_id) == {"attempts": 1, "validations": 1, "reports": 0, "actions": 0, "checkpoints": 0}
        validation_started = next(
            event
            for event in EventStore(db).list_for_run(run_id)
            if event.event_type is EventType.VALIDATION_STARTED
        )
        assert validation_started.payload["outcome_arbitration"] is True
        assert validation_started.payload["executor_attempt_status"] == "FAILED"
    finally:
        db.close()


def test_w_b_crash_after_validation_pass_resumes_without_revalidation(tmp_path):
    db_path, task_id, run_id, _calls, _validator_calls, _source, sessions_root = _crashed_case(
        tmp_path,
        CrashPoint.AFTER_VALIDATION_PERSISTED,
        validator_outcomes={1: True},
        failures={1},
        mutations={1},
    )
    db_before = Database(db_path)
    try:
        attempt = AttemptRepository(db_before).list_for_run(run_id)[0]
        validation = ValidationResultRepository(db_before).get_for_attempt(attempt.id)
        assert attempt.status is AttemptStatus.FAILED
        assert attempt.error_type == "AGENT_TURN_LIMIT"
        assert validation is not None and validation.passed is True
    finally:
        db_before.close()
    db, run, calls, validator = _resume_case(
        db_path, task_id, run_id, sessions_root, validator_outcomes={1: True}
    )
    try:
        assert run.status is RunStatus.COMPLETED
        assert AttemptRepository(db).list_for_run(run_id)[0].status is AttemptStatus.FAILED
        assert calls == []
        assert validator.calls == []
        assert _counts(db, run_id) == {"attempts": 1, "validations": 1, "reports": 0, "actions": 0, "checkpoints": 0}
    finally:
        db.close()


def test_w_c_crash_after_validation_fail_continues_recovery(tmp_path):
    db_path, task_id, run_id, _calls, _validator_calls, _source, sessions_root = _crashed_case(
        tmp_path,
        CrashPoint.AFTER_VALIDATION_PERSISTED,
        validator_outcomes={1: False},
        failures={1},
    )
    db_before = Database(db_path)
    try:
        attempt = AttemptRepository(db_before).list_for_run(run_id)[0]
        validation = ValidationResultRepository(db_before).get_for_attempt(attempt.id)
        assert attempt.status is AttemptStatus.FAILED
        assert attempt.error_type == "AGENT_TURN_LIMIT"
        assert validation is not None and validation.passed is False
    finally:
        db_before.close()
    db, run, calls, validator = _resume_case(
        db_path,
        task_id,
        run_id,
        sessions_root,
        validator_outcomes={2: True},
    )
    try:
        assert run.status is RunStatus.COMPLETED
        attempts = AttemptRepository(db).list_for_run(run_id)
        assert attempts[0].status is AttemptStatus.FAILED
        assert attempts[0].error_type == "AGENT_TURN_LIMIT"
        assert calls == [2]
        assert validator.calls == [2]
        assert _counts(db, run_id) == {"attempts": 2, "validations": 2, "reports": 1, "actions": 1, "checkpoints": 1}
        checkpoint = CheckpointRepository(db).list_for_run(run_id)[0]
        assert checkpoint.attempt_number == 1
        assert checkpoint.attempt_id == attempts[0].id
        second_snapshot = __import__("lhas.persistence.phaseb_repos", fromlist=["ContextSnapshotRepository"]).ContextSnapshotRepository(db).get(attempts[1].context_snapshot_id)
        assert second_snapshot is not None and second_snapshot.policy == "CP-3"
    finally:
        db.close()


def test_w_d_existing_failure_report_does_not_bypass_validation(tmp_path):
    db_path, task_id, run_id, _calls, _validator_calls, _source, sessions_root = _crashed_case(
        tmp_path,
        CrashPoint.AFTER_EXECUTOR_PERSISTED,
        validator_outcomes={1: True},
        failures={1},
        mutations={1},
    )
    db = Database(db_path)
    db.init_db()
    attempt = AttemptRepository(db).list_for_run(run_id)[0]
    FailureReportRepository(db).create(
        FailureReport(
            attempt_id=attempt.id,
            failure_type=FailureType.UNKNOWN,
            failure_class=FailureClass.UNKNOWN,
            evidence="preexisting safe report",
            summary="preexisting report",
            confidence=0.2,
            suggested_recovery="retry",
        )
    )
    assert attempt.status is AttemptStatus.FAILED
    assert attempt.error_type == "AGENT_TURN_LIMIT"
    assert ValidationResultRepository(db).get_for_attempt(attempt.id) is None
    assert FailureReportRepository(db).get_for_attempt(attempt.id) is not None
    db.close()

    db, run, calls, validator = _resume_case(
        db_path, task_id, run_id, sessions_root, validator_outcomes={1: True}
    )
    try:
        assert run.status is RunStatus.COMPLETED
        assert AttemptRepository(db).list_for_run(run_id)[0].status is AttemptStatus.FAILED
        assert calls == []
        assert validator.calls == [1]
        assert RecoveryActionRepository(db).get_for_attempt(attempt.id) is None
        assert CheckpointRepository(db).list_for_run(run_id) == []
        assert _counts(db, run_id) == {"attempts": 1, "validations": 1, "reports": 1, "actions": 0, "checkpoints": 0}
        validation_started = next(
            event
            for event in EventStore(db).list_for_run(run_id)
            if event.event_type is EventType.VALIDATION_STARTED
        )
        assert validation_started.payload["outcome_arbitration"] is True
    finally:
        db.close()
