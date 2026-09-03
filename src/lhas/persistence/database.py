"""Persistence layer: engine/session/ORM mapping (docs/07 + docs/03)."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def create_db_engine(db_path: str | Path) -> Engine:
    """Create a SQLite engine.

    ``:memory:`` uses a StaticPool so all sessions share one connection
    (tests). File paths use a normal engine with check_same_thread disabled
    (the async orchestrator may touch the DB from worker threads).
    """
    path = str(db_path)
    if path == ":memory:":
        return create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    return create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
    )


class Database:
    """Owns the engine + session factory; exposes init and a session scope."""

    def __init__(self, db_path: str | Path = ":memory:"):
        self.engine = create_db_engine(db_path)
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def init_db(self) -> None:
        # Import ORM classes so metadata is populated before create_all.
        from lhas.persistence import orm  # noqa: F401

        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def close(self) -> None:
        self.engine.dispose()
