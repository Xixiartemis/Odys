"""Persistence layer: engine/session/ORM mapping (docs/07 + docs/03)."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import Engine, create_engine, inspect, text
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


# Columns added in Phase 3.1 for plan_steps table.
# Maps column_name -> (column_type_sql, default_value_or_None)
_P31_NEW_COLUMNS: dict[str, tuple[str, str | None]] = {
    "preconditions": ("TEXT", None),
    "expected_effects": ("TEXT", None),
    "evidence": ("TEXT", None),
    "risk_class": ("TEXT", "'LOW'"),
    "budget": ("TEXT", None),
    "checkpoint_policy": ("TEXT", "'ON_FAILURE'"),
    "recovery_policy": ("TEXT", "'RETRY_WITH_FAILURE_CONTEXT'"),
    "semantic_fingerprint": ("TEXT", None),
}


def _migrate_plan_step_columns(engine: Engine) -> None:
    """Add missing columns to plan_steps for P3.1 schema upgrade.

    SQLite ALTER TABLE ADD COLUMN is safe — it adds the column with a NULL
    default. For columns with explicit defaults, we UPDATE NULLs after.
    """
    inspector = inspect(engine)
    existing_tables = inspector.get_table_names()
    if "plan_steps" not in existing_tables:
        return  # fresh DB, create_all will handle it

    existing_cols = {col["name"] for col in inspector.get_columns("plan_steps")}

    with engine.begin() as conn:
        for col_name, (col_type, default) in _P31_NEW_COLUMNS.items():
            if col_name not in existing_cols:
                if default is not None:
                    conn.execute(text(
                        f"ALTER TABLE plan_steps ADD COLUMN {col_name} {col_type} DEFAULT {default}"
                    ))
                else:
                    conn.execute(text(
                        f"ALTER TABLE plan_steps ADD COLUMN {col_name} {col_type}"
                    ))


class Database:
    """Owns the engine + session factory; exposes init and a session scope."""

    def __init__(self, db_path: str | Path = ":memory:"):
        self.engine = create_db_engine(db_path)
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def init_db(self) -> None:
        # Import ORM classes so metadata is populated before create_all.
        from lhas.persistence import orm  # noqa: F401

        # BLOCKER F: migrate existing DBs before create_all.
        # create_all() only creates NEW tables; it does not add missing columns.
        _migrate_plan_step_columns(self.engine)

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
