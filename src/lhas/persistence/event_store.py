"""EventStore — append-only event persistence (docs/10_LOGGING_SPEC.md)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select

from lhas.domain.enums import EventType
from lhas.domain.models import Event, json_dumps, json_loads
from lhas.persistence.database import Database
from lhas.persistence.orm import EventRow


class EventStore:
    """Every state transition is appended here before the next transition runs.

    Events are append-only: there is intentionally no update/delete path.
    """

    def __init__(self, db: Database):
        self._db = db

    def append(
        self,
        event_type: EventType,
        *,
        task_id: Optional[str] = None,
        run_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
        timestamp: Optional[datetime] = None,
    ) -> Event:
        ts = timestamp or datetime.now(timezone.utc)
        with self._db.session() as session:
            row = EventRow(
                task_id=task_id, run_id=run_id, attempt_id=attempt_id,
                event_type=event_type.value, payload=json_dumps(payload or {}),
                created_at=ts,
            )
            session.add(row)
            session.flush()  # obtain the autoincrement id (== sequence)
            return Event(
                id=row.id, task_id=row.task_id, run_id=row.run_id, attempt_id=row.attempt_id,
                event_type=event_type, timestamp=row.created_at,
                payload=json_loads(row.payload) or {},
            )

    def list_all(self, limit: Optional[int] = None) -> list[Event]:
        with self._db.session() as session:
            q = select(EventRow).order_by(EventRow.id)
            if limit is not None:
                q = q.limit(limit)
            rows = session.execute(q).scalars().all()
            return [self._from_row(r) for r in rows]

    def list_for_task(self, task_id: str) -> list[Event]:
        with self._db.session() as session:
            rows = session.execute(
                select(EventRow).where(EventRow.task_id == task_id).order_by(EventRow.id)
            ).scalars().all()
            return [self._from_row(r) for r in rows]

    def list_for_run(self, run_id: str) -> list[Event]:
        with self._db.session() as session:
            rows = session.execute(
                select(EventRow).where(EventRow.run_id == run_id).order_by(EventRow.id)
            ).scalars().all()
            return [self._from_row(r) for r in rows]

    def list_for_attempt(self, attempt_id: str) -> list[Event]:
        with self._db.session() as session:
            rows = session.execute(
                select(EventRow).where(EventRow.attempt_id == attempt_id).order_by(EventRow.id)
            ).scalars().all()
            return [self._from_row(r) for r in rows]

    def count(self) -> int:
        with self._db.session() as session:
            return session.execute(select(func.count(EventRow.id))).scalar_one()

    def latest_sequence(self, run_id: str) -> int:
        with self._db.session() as session:
            value = session.execute(select(func.max(EventRow.id)).where(EventRow.run_id == run_id)).scalar_one()
            return int(value or 0)

    def list_for_run_after(self, run_id: str, after_event_id: int) -> list[Event]:
        with self._db.session() as session:
            rows = session.execute(select(EventRow).where(EventRow.run_id == run_id, EventRow.id > after_event_id).order_by(EventRow.id.asc())).scalars().all()
            return [self._from_row(r) for r in rows]

    def _from_row(self, r: EventRow) -> Event:
        return Event(
            id=r.id, task_id=r.task_id, run_id=r.run_id, attempt_id=r.attempt_id,
            event_type=EventType(r.event_type), timestamp=r.created_at,
            payload=json_loads(r.payload) or {},
        )
