"""Run-to-durable-workspace bindings and orchestration-facing manager."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from lhas.persistence.repositories import ProjectRepository, WorkspaceSessionBindingRepository

from .errors import WorkspaceSessionBindingMismatch, WorkspaceSessionError
from .session import DurableWorkspaceSession


class WorkspaceSessionBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    run_id: str
    task_id: str
    session_root: str
    state: str = "OPEN"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RunWorkspaceManager:
    """Locate sessions only through the DB binding, never filesystem scans."""

    def __init__(self, db, sessions_root: str | Path, source_root: str | Path | None = None, limits=None):
        self.db = db
        self.sessions_root = Path(sessions_root).resolve()
        self.source_root = Path(source_root).resolve() if source_root is not None else None
        self.limits = limits
        self.bindings = WorkspaceSessionBindingRepository(db)

    def _resolve_source(self, task):
        project = ProjectRepository(self.db).get(task.project_id)
        configured = project.root_path if project is not None else None
        source = Path(configured).resolve() if configured else self.source_root
        if source is None or not source.is_dir():
            raise WorkspaceSessionError("WORKSPACE_SOURCE_NOT_CONFIGURED")
        return source

    def create_for_run(self, task, run):
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        session_root = self.sessions_root / run.id
        session = DurableWorkspaceSession.create(
            self._resolve_source(task), session_root, limits=self.limits,
            run_id=run.id, task_id=task.id,
        )
        self.bindings.create(WorkspaceSessionBinding(
            session_id=session.manifest.session_id, run_id=run.id,
            task_id=task.id, session_root=str(session.root), state="OPEN",
        ))
        return session

    def reopen_for_run(self, task, run):
        binding = self.bindings.get_by_run(run.id)
        if binding is None or binding.task_id != task.id or binding.run_id != run.id:
            raise WorkspaceSessionBindingMismatch("WORKSPACE_SESSION_BINDING_MISMATCH")
        session = DurableWorkspaceSession.reopen(binding.session_root)
        manifest = session.manifest
        if (
            manifest.session_id != binding.session_id
            or manifest.run_id != binding.run_id
            or manifest.task_id != binding.task_id
            or Path(binding.session_root).resolve() != session.root
        ):
            raise WorkspaceSessionBindingMismatch("WORKSPACE_SESSION_BINDING_MISMATCH")
        return session

    def mark_completed(self, run_id: str):
        binding = self.bindings.get_by_run(run_id)
        if binding is not None:
            self.bindings.update_state(binding.session_id, "COMPLETED")
