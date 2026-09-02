"""Phase B persistence: context snapshots, validation results, failure
reports, recovery actions (docs/02 objects, docs/05/06/07/08)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

from lhas.context_builder import ContextSnapshot
from lhas.domain.models import json_dumps, json_loads
from lhas.failure import FailureReport
from lhas.persistence.database import Database
from lhas.persistence.orm import (
    AttemptRow,
    ContextSnapshotRow,
    FailureReportRow,
    RecoveryActionRow,
    ValidationResultRow,
)
from lhas.recovery import RecoveryAction
from lhas.validation import ValidationResult


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ContextSnapshotRepository:
    def __init__(self, db: Database):
        self._db = db

    def create(self, snapshot: ContextSnapshot) -> ContextSnapshot:
        with self._db.session() as session:
            session.add(
                ContextSnapshotRow(
                    id=snapshot.id, task_id=snapshot.task_id, run_id=snapshot.run_id,
                    attempt_id=snapshot.attempt_id, attempt_number=snapshot.attempt_number,
                    policy=snapshot.policy, sections=json_dumps(snapshot.sections),
                    raw_text=snapshot.raw_text, created_at=snapshot.created_at, metrics=json_dumps(snapshot.metrics), context_sha256=snapshot.context_sha256,
                )
            )
        return snapshot

    def get(self, snapshot_id: str) -> Optional[ContextSnapshot]:
        with self._db.session() as session:
            row = session.get(ContextSnapshotRow, snapshot_id)
            if row is None:
                return None
            return ContextSnapshot(
                id=row.id, task_id=row.task_id, run_id=row.run_id, attempt_id=row.attempt_id,
                attempt_number=row.attempt_number, policy=row.policy,
                sections=json_loads(row.sections) or {}, raw_text=row.raw_text or "",
                created_at=row.created_at, metrics=json_loads(row.metrics) or {}, context_sha256=row.context_sha256 or "",
            )

    def list_for_attempt(self, attempt_id: str) -> list[ContextSnapshot]:
        with self._db.session() as session:
            rows = session.execute(
                select(ContextSnapshotRow).where(ContextSnapshotRow.attempt_id == attempt_id).order_by(ContextSnapshotRow.created_at, ContextSnapshotRow.id)
            ).scalars().all()
            return [self._from_row(r) for r in rows]

    def latest_for_attempt(self, attempt_id: str) -> Optional[ContextSnapshot]:
        values = self.list_for_attempt(attempt_id)
        return values[-1] if values else None

    def _from_row(self, r: ContextSnapshotRow) -> ContextSnapshot:
        return ContextSnapshot(
            id=r.id, task_id=r.task_id, run_id=r.run_id, attempt_id=r.attempt_id,
            attempt_number=r.attempt_number, policy=r.policy,
            sections=json_loads(r.sections) or {}, raw_text=r.raw_text or "", created_at=r.created_at,
            metrics=json_loads(r.metrics) or {}, context_sha256=r.context_sha256 or "",
        )


class ValidationResultRepository:
    def __init__(self, db: Database):
        self._db = db

    def create(self, v: ValidationResult) -> ValidationResult:
        with self._db.session() as session:
            session.add(
                ValidationResultRow(
                    id=v.id, attempt_id=v.attempt_id, passed=bool(v.passed), level=v.level,
                    checks=json_dumps([c.model_dump(mode="json") for c in v.checks]),
                    evidence=v.evidence, stdout=v.stdout, stderr=v.stderr,
                    exit_code=v.exit_code,
                    duration_ms=v.duration_ms, created_at=_now(),
                )
            )
        return v

    def update(self, v: ValidationResult) -> ValidationResult:
        with self._db.session() as session:
            row = session.get(ValidationResultRow, v.id)
            if row is None:
                raise KeyError(f"validation result not found: {v.id}")
            row.passed = bool(v.passed)
            row.level = v.level
            row.checks = json_dumps([c.model_dump(mode="json") for c in v.checks])
            row.evidence = v.evidence
            row.stdout = v.stdout
            row.stderr = v.stderr
            row.exit_code = v.exit_code
            row.duration_ms = v.duration_ms
        return v

    def list_for_attempt(self, attempt_id: str) -> list[ValidationResult]:
        with self._db.session() as session:
            rows = session.execute(
                select(ValidationResultRow).where(ValidationResultRow.attempt_id == attempt_id).order_by(ValidationResultRow.created_at, ValidationResultRow.id)
            ).scalars().all()
            return [self._from_row(r) for r in rows]

    def get_for_attempt(self, attempt_id: str) -> Optional[ValidationResult]:
        values = self.list_for_attempt(attempt_id)
        return values[-1] if values else None

    def _from_row(self, r: ValidationResultRow) -> ValidationResult:
        from lhas.validation import ValidationCheck
        return ValidationResult(
            id=r.id, attempt_id=r.attempt_id, passed=bool(r.passed), level=r.level,
            checks=[ValidationCheck(**c) for c in (json_loads(r.checks) or [])],
            evidence=r.evidence, stdout=r.stdout, stderr=r.stderr, exit_code=r.exit_code,
            duration_ms=r.duration_ms,
        )


class FailureReportRepository:
    def __init__(self, db: Database):
        self._db = db

    def create(self, report: FailureReport) -> FailureReport:
        with self._db.session() as session:
            session.add(
                FailureReportRow(
                    id=report.id, attempt_id=report.attempt_id,
                    failure_type=report.failure_type.value, failure_class=report.failure_class.value,
                    evidence=report.evidence, summary=report.summary,
                    confidence=report.confidence, suggested_recovery=report.suggested_recovery,
                    created_at=_now(),
                )
            )
        return report

    def list_for_attempt(self, attempt_id: str) -> list[FailureReport]:
        with self._db.session() as session:
            rows = session.execute(
                select(FailureReportRow).where(FailureReportRow.attempt_id == attempt_id).order_by(FailureReportRow.created_at, FailureReportRow.id)
            ).scalars().all()
            return [self._from_row(r) for r in rows]

    def get_for_attempt(self, attempt_id: str) -> Optional[FailureReport]:
        values = self.list_for_attempt(attempt_id)
        return values[-1] if values else None

    def _from_row(self, r: FailureReportRow) -> FailureReport:
        from lhas.domain.enums import FailureClass, FailureType
        return FailureReport(
            id=r.id, attempt_id=r.attempt_id, failure_type=FailureType(r.failure_type),
            failure_class=FailureClass(r.failure_class), evidence=r.evidence or "",
            summary=r.summary or "", confidence=r.confidence, suggested_recovery=r.suggested_recovery or "",
        )


class RecoveryActionRepository:
    def __init__(self, db: Database):
        self._db = db

    def create(self, action: RecoveryAction) -> RecoveryAction:
        with self._db.session() as session:
            session.add(
                RecoveryActionRow(
                    id=action.id, attempt_id=action.attempt_id,
                    action_type=action.action_type.value, reason=action.reason,
                    context_policy=action.context_policy, attempt_from=action.attempt_from,
                    attempt_to=action.attempt_to, added_context=json_dumps(action.added_context),
                    created_at=action.created_at,
                )
            )
        return action

    def list_for_attempt(self, attempt_id: str) -> list[RecoveryAction]:
        with self._db.session() as session:
            rows = session.execute(
                select(RecoveryActionRow).where(RecoveryActionRow.attempt_id == attempt_id).order_by(RecoveryActionRow.created_at, RecoveryActionRow.id)
            ).scalars().all()
            return [self._from_row(r) for r in rows]

    def get_for_attempt(self, attempt_id: str) -> Optional[RecoveryAction]:
        values = self.list_for_attempt(attempt_id)
        return values[-1] if values else None

    def list_for_run(self, run_id: str) -> list[RecoveryAction]:
        with self._db.session() as session:
            rows = session.execute(
                select(RecoveryActionRow)
                .join(AttemptRow, AttemptRow.id == RecoveryActionRow.attempt_id)
                .where(AttemptRow.run_id == run_id)
                .order_by(RecoveryActionRow.created_at, RecoveryActionRow.id)
            ).scalars().all()
            return [self._from_row(r) for r in rows]

    def _from_row(self, r: RecoveryActionRow) -> RecoveryAction:
        from lhas.domain.enums import RecoveryActionType
        return RecoveryAction(
            id=r.id, attempt_id=r.attempt_id, action_type=RecoveryActionType(r.action_type),
            reason=r.reason or "", context_policy=r.context_policy, attempt_from=r.attempt_from,
            attempt_to=r.attempt_to, added_context=json_loads(r.added_context) or {},
            created_at=r.created_at,
        )
