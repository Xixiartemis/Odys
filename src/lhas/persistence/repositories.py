"""Thin CRUD repositories mapping ORM rows <-> domain models (docs/02, docs/07)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

from lhas.domain.enums import TaskStatus
from lhas.domain.models import Attempt, Event, Project, Run, Task, json_dumps, json_loads
from lhas.persistence.database import Database
from lhas.persistence.orm import AttemptRow, EventRow, ProjectRow, RunRow, TaskRow, WorkspaceSessionRow


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ProjectRepository:
    def __init__(self, db: Database):
        self._db = db

    def create(self, project: Project) -> Project:
        with self._db.session() as session:
            row = ProjectRow(
                id=project.id, name=project.name, type=project.type,
                root_path=project.root_path, created_at=project.created_at,
            )
            session.add(row)
        return project

    def get_by_name(self, name: str) -> Optional[Project]:
        with self._db.session() as session:
            row = session.execute(select(ProjectRow).where(ProjectRow.name == name)).scalar_one_or_none()
            if row is None:
                return None
            return Project(id=row.id, name=row.name, type=row.type, root_path=row.root_path, created_at=row.created_at)

    def get(self, project_id: str) -> Optional[Project]:
        with self._db.session() as session:
            row = session.get(ProjectRow, project_id)
            if row is None:
                return None
            return Project(id=row.id, name=row.name, type=row.type, root_path=row.root_path, created_at=row.created_at)

    def list(self) -> list[Project]:
        with self._db.session() as session:
            rows = session.execute(select(ProjectRow).order_by(ProjectRow.created_at)).scalars().all()
            return [Project(id=r.id, name=r.name, type=r.type, root_path=r.root_path, created_at=r.created_at) for r in rows]


class TaskRepository:
    def __init__(self, db: Database):
        self._db = db

    def create(self, task: Task) -> Task:
        with self._db.session() as session:
            session.add(self._to_row(task))
        return task

    def get(self, task_id: str) -> Optional[Task]:
        with self._db.session() as session:
            row = session.get(TaskRow, task_id)
            return self._from_row(row) if row else None

    def list(self, project_id: Optional[str] = None) -> list[Task]:
        with self._db.session() as session:
            q = select(TaskRow).order_by(TaskRow.created_at)
            if project_id:
                q = q.where(TaskRow.project_id == project_id)
            rows = session.execute(q).scalars().all()
            return [self._from_row(r) for r in rows]

    def update(self, task: Task) -> Task:
        task.updated_at = _now()
        with self._db.session() as session:
            row = session.get(TaskRow, task.id)
            if row is None:
                raise KeyError(f"Task {task.id} not found")
            row.title = task.title
            row.objective = task.objective
            row.constraints = json_dumps(task.constraints)
            row.acceptance_criteria = json_dumps(task.acceptance_criteria)
            row.status = task.status.value
            row.max_attempts = task.max_attempts
            row.timeout_seconds = task.timeout_seconds
            row.updated_at = task.updated_at
        return task

    def _to_row(self, t: Task) -> TaskRow:
        return TaskRow(
            id=t.id, project_id=t.project_id, title=t.title, objective=t.objective,
            constraints=json_dumps(t.constraints), acceptance_criteria=json_dumps(t.acceptance_criteria),
            status=t.status.value, max_attempts=t.max_attempts, timeout_seconds=t.timeout_seconds,
            created_at=t.created_at, updated_at=t.updated_at,
        )

    def _from_row(self, r: TaskRow) -> Task:
        return Task(
            id=r.id, project_id=r.project_id, title=r.title, objective=r.objective,
            constraints=json_loads(r.constraints) or [], acceptance_criteria=json_loads(r.acceptance_criteria) or [],
            status=TaskStatus(r.status), max_attempts=r.max_attempts, timeout_seconds=r.timeout_seconds,
            created_at=r.created_at, updated_at=r.updated_at,
        )


class RunRepository:
    def __init__(self, db: Database):
        self._db = db

    def create(self, run: Run) -> Run:
        with self._db.session() as session:
            session.add(self._to_row(run))
        return run

    def get(self, run_id: str) -> Optional[Run]:
        with self._db.session() as session:
            row = session.get(RunRow, run_id)
            return self._from_row(row) if row else None

    def list_for_task(self, task_id: str) -> list[Run]:
        with self._db.session() as session:
            rows = session.execute(
                select(RunRow).where(RunRow.task_id == task_id).order_by(RunRow.created_at)
            ).scalars().all()
            return [self._from_row(r) for r in rows]

    def count_for_task(self, task_id: str) -> int:
        with self._db.session() as session:
            return len(session.execute(select(RunRow).where(RunRow.task_id == task_id)).scalars().all())

    def update(self, run: Run) -> Run:
        with self._db.session() as session:
            row = session.get(RunRow, run.id)
            if row is None:
                raise KeyError(f"Run {run.id} not found")
            row.experiment_id = run.experiment_id
            row.executor_type = run.executor_type
            row.provider = run.provider
            row.model = run.model
            row.harness_version = run.harness_version
            row.context_policy_version = run.context_policy_version
            row.dataset_version = run.dataset_version
            row.status = run.status.value
            row.result = run.result
            row.started_at = run.started_at
            row.finished_at = run.finished_at
        return run

    def _to_row(self, r: Run) -> RunRow:
        return RunRow(
            id=r.id, task_id=r.task_id, experiment_id=r.experiment_id,
            executor_type=r.executor_type, provider=r.provider, model=r.model,
            harness_version=r.harness_version, context_policy_version=r.context_policy_version,
            dataset_version=r.dataset_version, status=r.status.value, result=r.result,
            started_at=r.started_at, finished_at=r.finished_at, created_at=r.created_at,
        )

    def _from_row(self, r: RunRow) -> Run:
        from lhas.domain.enums import RunStatus
        return Run(
            id=r.id, task_id=r.task_id, experiment_id=r.experiment_id,
            executor_type=r.executor_type, provider=r.provider, model=r.model,
            harness_version=r.harness_version, context_policy_version=r.context_policy_version,
            dataset_version=r.dataset_version, status=RunStatus(r.status), result=r.result,
            started_at=r.started_at, finished_at=r.finished_at, created_at=r.created_at,
        )


class AttemptRepository:
    def __init__(self, db: Database):
        self._db = db

    def create(self, attempt: Attempt) -> Attempt:
        with self._db.session() as session:
            session.add(self._to_row(attempt))
        return attempt

    def get(self, attempt_id: str) -> Optional[Attempt]:
        with self._db.session() as session:
            row = session.get(AttemptRow, attempt_id)
            return self._from_row(row) if row else None

    def list_for_run(self, run_id: str) -> list[Attempt]:
        with self._db.session() as session:
            rows = session.execute(
                select(AttemptRow).where(AttemptRow.run_id == run_id).order_by(AttemptRow.attempt_number)
            ).scalars().all()
            return [self._from_row(r) for r in rows]

    def update(self, attempt: Attempt) -> Attempt:
        attempt.updated_at = _now()
        with self._db.session() as session:
            row = session.get(AttemptRow, attempt.id)
            if row is None:
                raise KeyError(f"Attempt {attempt.id} not found")
            row.status = attempt.status.value
            row.context_snapshot_id = attempt.context_snapshot_id
            row.started_at = attempt.started_at
            row.finished_at = attempt.finished_at
            row.executor_result = attempt.executor_result
            row.usage = json_dumps(attempt.usage)
            row.failure_type = attempt.failure_type
            row.error_type = attempt.error_type
            row.error_message = attempt.error_message
            row.output = attempt.output
            row.duration_ms = attempt.duration_ms
            row.updated_at = attempt.updated_at
        return attempt

    def _to_row(self, a: Attempt) -> AttemptRow:
        return AttemptRow(
            id=a.id, run_id=a.run_id, attempt_number=a.attempt_number, status=a.status.value,
            context_snapshot_id=a.context_snapshot_id, started_at=a.started_at, finished_at=a.finished_at,
            executor_result=a.executor_result, usage=json_dumps(a.usage), failure_type=a.failure_type,
            error_type=a.error_type, error_message=a.error_message, output=a.output,
            duration_ms=a.duration_ms, created_at=a.created_at, updated_at=a.updated_at,
        )

    def _from_row(self, r: AttemptRow) -> Attempt:
        from lhas.domain.enums import AttemptStatus
        return Attempt(
            id=r.id, run_id=r.run_id, attempt_number=r.attempt_number, status=AttemptStatus(r.status),
            context_snapshot_id=r.context_snapshot_id, started_at=r.started_at, finished_at=r.finished_at,
            executor_result=r.executor_result, usage=json_loads(r.usage) or {}, failure_type=r.failure_type,
            error_type=r.error_type, error_message=r.error_message, output=r.output,
            duration_ms=r.duration_ms, created_at=r.created_at, updated_at=r.updated_at,
        )


class WorkspaceSessionBindingRepository:
    """Minimal registry; file contents and workspace output stay on disk."""

    def __init__(self, db: Database):
        self._db = db

    def create(self, binding):
        with self._db.session() as session:
            session.add(WorkspaceSessionRow(
                session_id=binding.session_id, run_id=binding.run_id,
                task_id=binding.task_id, session_root=binding.session_root,
                state=binding.state, created_at=binding.created_at,
                updated_at=binding.updated_at,
            ))
        return binding

    def get_by_run(self, run_id: str):
        with self._db.session() as session:
            row = session.execute(select(WorkspaceSessionRow).where(WorkspaceSessionRow.run_id == run_id)).scalar_one_or_none()
            return self._from_row(row) if row else None

    def get(self, session_id: str):
        with self._db.session() as session:
            row = session.get(WorkspaceSessionRow, session_id)
            return self._from_row(row) if row else None

    def update_state(self, session_id: str, state: str):
        with self._db.session() as session:
            row = session.get(WorkspaceSessionRow, session_id)
            if row is None:
                raise KeyError(f"Workspace session {session_id} not found")
            row.state = state
            row.updated_at = _now()
            return self._from_row(row)

    @staticmethod
    def _from_row(row):
        from lhas.workspace.registry import WorkspaceSessionBinding
        return WorkspaceSessionBinding(
            session_id=row.session_id, run_id=row.run_id, task_id=row.task_id,
            session_root=row.session_root, state=row.state,
            created_at=row.created_at, updated_at=row.updated_at,
        )
