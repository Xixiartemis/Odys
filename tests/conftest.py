"""Shared fixtures: hermetic tmp-file SQLite per test (never touches data/)."""

from __future__ import annotations

import pytest

from lhas.domain.models import Project
from lhas.executors.mock import MockConfig, MockExecutor, MockScenario
from lhas.orchestrator import Orchestrator
from lhas.persistence.database import Database
from lhas.persistence.event_store import EventStore
from lhas.persistence.repositories import ProjectRepository
from lhas.task_service import create_task


@pytest.fixture(autouse=True)
def isolated_agent_environment(monkeypatch):
    """Keep provider selection tests independent of the invoking shell."""
    for name in (
        "ODYS_AGENT_API_KEY",
        "ODYS_AGENT_API_MODE",
        "ODYS_AGENT_BASE_URL",
        "ODYS_AGENT_MODEL",
        "ODYS_AGENT_PROVIDER_PROFILE",
        "ODYS_AGENT_SDK_TRACING",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    database.init_db()
    yield database
    database.close()


@pytest.fixture()
def project(db):
    return ProjectRepository(db).create(Project(name="test-project", type="test"))


@pytest.fixture()
def make_task(db, project):
    def _make(title="t", objective="do the thing", max_attempts=3, timeout_seconds=5.0, **kw):
        return create_task(
            db,
            project_id=project.id,
            title=title,
            objective=objective,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
            **kw,
        )
    return _make


@pytest.fixture()
def make_orchestrator(db):
    def _make(scenario=MockScenario.SUCCESS, **kw):
        return Orchestrator(
            db,
            executor_factory=lambda: MockExecutor(MockConfig(scenario=scenario)),
            **kw,
        )
    return _make


@pytest.fixture()
def event_store(db):
    return EventStore(db)


def chain_of(db: Database, task_id: str) -> list[str]:
    """Event type chain for a task, in sequence order."""
    return [e.event_type.value for e in EventStore(db).list_for_task(task_id)]
