"""Durable projections for the native loop and delegation delivery."""

from __future__ import annotations

from sqlalchemy import select

from lhas.domain.models import json_dumps, json_loads, utcnow
from lhas.native.models import (
    CandidateStatus,
    CompletionCandidate,
    ExecutionSnapshot,
    InvocationState,
    NativePhase,
    ReconciliationDecision,
    ReplanSignal,
    SideEffectClass,
    ToolInvocation,
    ValidationFailure,
)
from lhas.persistence.orm import (
    CompletionCandidateRow,
    NativeExecutionSnapshotRow,
    NativeToolInvocationRow,
    NativeValidationFailureRow,
    ReplanSignalRow,
)


class ExecutionSnapshotRepository:
    def __init__(self, db):
        self.db = db

    def get_for_attempt(self, attempt_id: str) -> ExecutionSnapshot | None:
        with self.db.session() as session:
            row = session.execute(
                select(NativeExecutionSnapshotRow).where(
                    NativeExecutionSnapshotRow.attempt_id == attempt_id
                )
            ).scalar_one_or_none()
            return self._from_row(row) if row else None

    def save(self, snapshot: ExecutionSnapshot) -> ExecutionSnapshot:
        snapshot.updated_at = utcnow()
        with self.db.session() as session:
            row = session.execute(
                select(NativeExecutionSnapshotRow).where(
                    NativeExecutionSnapshotRow.attempt_id == snapshot.attempt_id
                )
            ).scalar_one_or_none()
            payload = snapshot.model_dump(mode="json", exclude={"id", "task_id", "run_id", "attempt_id", "version", "phase", "updated_at"})
            if row is None:
                row = NativeExecutionSnapshotRow(
                    id=snapshot.id,
                    task_id=snapshot.task_id,
                    run_id=snapshot.run_id,
                    attempt_id=snapshot.attempt_id,
                    version=snapshot.version,
                    phase=snapshot.phase.value,
                    payload_json=json_dumps(payload),
                    created_at=snapshot.updated_at,
                    updated_at=snapshot.updated_at,
                )
                session.add(row)
            else:
                snapshot.id = row.id
                snapshot.version = max(snapshot.version, row.version + 1)
                row.version = snapshot.version
                row.phase = snapshot.phase.value
                row.payload_json = json_dumps(payload)
                row.updated_at = snapshot.updated_at
        return snapshot

    @staticmethod
    def _from_row(row) -> ExecutionSnapshot:
        payload = json_loads(row.payload_json) or {}
        return ExecutionSnapshot(
            id=row.id,
            task_id=row.task_id,
            run_id=row.run_id,
            attempt_id=row.attempt_id,
            version=row.version,
            phase=NativePhase(row.phase),
            updated_at=row.updated_at,
            **payload,
        )


class ToolInvocationRepository:
    def __init__(self, db):
        self.db = db

    def create(self, invocation: ToolInvocation) -> ToolInvocation:
        with self.db.session() as session:
            session.add(NativeToolInvocationRow(
                id=invocation.id,
                task_id=invocation.task_id,
                run_id=invocation.run_id,
                attempt_id=invocation.attempt_id,
                ordinal=invocation.ordinal,
                capability=invocation.capability,
                args_fingerprint=invocation.args_fingerprint,
                side_effect_class=invocation.side_effect_class.value,
                state=invocation.state.value,
                observed_mutation=int(invocation.observed_mutation),
                result_status=invocation.result_status,
                error_type=invocation.error_type,
                result_summary_json=json_dumps(invocation.result_summary),
                reconciliation=invocation.reconciliation.value if invocation.reconciliation else None,
                started_at=invocation.started_at,
                finished_at=invocation.finished_at,
                duration_ms=invocation.duration_ms,
                created_at=invocation.created_at,
                updated_at=invocation.updated_at,
            ))
        return invocation

    def update(self, invocation: ToolInvocation) -> ToolInvocation:
        invocation.updated_at = utcnow()
        with self.db.session() as session:
            row = session.get(NativeToolInvocationRow, invocation.id)
            if row is None:
                raise KeyError(f"tool invocation not found: {invocation.id}")
            row.state = invocation.state.value
            row.observed_mutation = int(invocation.observed_mutation)
            row.result_status = invocation.result_status
            row.error_type = invocation.error_type
            row.result_summary_json = json_dumps(invocation.result_summary)
            row.reconciliation = invocation.reconciliation.value if invocation.reconciliation else None
            row.started_at = invocation.started_at
            row.finished_at = invocation.finished_at
            row.duration_ms = invocation.duration_ms
            row.updated_at = invocation.updated_at
        return invocation

    def get(self, invocation_id: str) -> ToolInvocation | None:
        with self.db.session() as session:
            row = session.get(NativeToolInvocationRow, invocation_id)
            return self._from_row(row) if row else None

    def list_for_attempt(self, attempt_id: str) -> list[ToolInvocation]:
        with self.db.session() as session:
            rows = session.execute(
                select(NativeToolInvocationRow)
                .where(NativeToolInvocationRow.attempt_id == attempt_id)
                .order_by(NativeToolInvocationRow.ordinal, NativeToolInvocationRow.created_at)
            ).scalars().all()
            return [self._from_row(row) for row in rows]

    def unfinished_for_attempt(self, attempt_id: str) -> list[ToolInvocation]:
        return [item for item in self.list_for_attempt(attempt_id) if item.state in {InvocationState.REQUESTED, InvocationState.STARTED}]

    @staticmethod
    def _from_row(row) -> ToolInvocation:
        return ToolInvocation(
            id=row.id,
            task_id=row.task_id,
            run_id=row.run_id,
            attempt_id=row.attempt_id,
            ordinal=row.ordinal,
            capability=row.capability,
            args_fingerprint=row.args_fingerprint,
            side_effect_class=SideEffectClass(row.side_effect_class),
            state=InvocationState(row.state),
            observed_mutation=bool(row.observed_mutation),
            result_status=row.result_status,
            error_type=row.error_type,
            result_summary=json_loads(row.result_summary_json) or {},
            reconciliation=ReconciliationDecision(row.reconciliation) if row.reconciliation else None,
            started_at=row.started_at,
            finished_at=row.finished_at,
            duration_ms=row.duration_ms,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class CompletionCandidateRepository:
    def __init__(self, db):
        self.db = db

    def create(self, candidate: CompletionCandidate) -> CompletionCandidate:
        with self.db.session() as session:
            session.add(CompletionCandidateRow(
                id=candidate.id,
                task_id=candidate.task_id,
                run_id=candidate.run_id,
                attempt_id=candidate.attempt_id,
                source=candidate.source,
                status=candidate.status.value,
                claim_sha256=candidate.claim_sha256,
                summary=candidate.summary,
                validation_json=json_dumps(candidate.validation),
                created_at=candidate.created_at,
                updated_at=candidate.updated_at,
            ))
        return candidate

    def update(self, candidate: CompletionCandidate) -> CompletionCandidate:
        candidate.updated_at = utcnow()
        with self.db.session() as session:
            row = session.get(CompletionCandidateRow, candidate.id)
            if row is None:
                raise KeyError(f"completion candidate not found: {candidate.id}")
            row.status = candidate.status.value
            row.summary = candidate.summary
            row.validation_json = json_dumps(candidate.validation)
            row.updated_at = candidate.updated_at
        return candidate

    def get(self, candidate_id: str) -> CompletionCandidate | None:
        with self.db.session() as session:
            row = session.get(CompletionCandidateRow, candidate_id)
            return self._from_row(row) if row else None

    def list_for_attempt(self, attempt_id: str) -> list[CompletionCandidate]:
        with self.db.session() as session:
            rows = session.execute(
                select(CompletionCandidateRow)
                .where(CompletionCandidateRow.attempt_id == attempt_id)
                .order_by(CompletionCandidateRow.created_at, CompletionCandidateRow.id)
            ).scalars().all()
            return [self._from_row(row) for row in rows]

    def latest_for_attempt(self, attempt_id: str) -> CompletionCandidate | None:
        values = self.list_for_attempt(attempt_id)
        return values[-1] if values else None

    @staticmethod
    def _from_row(row) -> CompletionCandidate:
        return CompletionCandidate(
            id=row.id,
            task_id=row.task_id,
            run_id=row.run_id,
            attempt_id=row.attempt_id,
            source=row.source,
            status=CandidateStatus(row.status),
            claim_sha256=row.claim_sha256,
            summary=row.summary or "",
            validation=json_loads(row.validation_json) or {},
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class ValidationFailureRepository:
    def __init__(self, db):
        self.db = db

    def create(self, failure: ValidationFailure) -> ValidationFailure:
        with self.db.session() as session:
            session.add(NativeValidationFailureRow(
                id=failure.id,
                candidate_id=failure.candidate_id,
                attempt_id=failure.attempt_id,
                category=failure.category,
                failed_criterion=failure.failed_criterion,
                safe_evidence=failure.safe_evidence,
                recommended_recovery=failure.recommended_recovery,
                created_at=failure.created_at,
            ))
        return failure

    def list_for_attempt(self, attempt_id: str) -> list[ValidationFailure]:
        with self.db.session() as session:
            rows = session.execute(
                select(NativeValidationFailureRow)
                .where(NativeValidationFailureRow.attempt_id == attempt_id)
                .order_by(NativeValidationFailureRow.created_at, NativeValidationFailureRow.id)
            ).scalars().all()
            return [ValidationFailure(
                id=row.id,
                candidate_id=row.candidate_id,
                attempt_id=row.attempt_id,
                category=row.category,
                failed_criterion=row.failed_criterion or "",
                safe_evidence=row.safe_evidence or "",
                recommended_recovery=row.recommended_recovery,
                created_at=row.created_at,
            ) for row in rows]


class ReplanSignalRepository:
    def __init__(self, db):
        self.db = db

    def create(self, signal: ReplanSignal) -> ReplanSignal:
        with self.db.session() as session:
            session.add(ReplanSignalRow(
                id=signal.id,
                task_id=signal.task_id,
                run_id=signal.run_id,
                attempt_id=signal.attempt_id,
                reason=signal.reason,
                scope=signal.scope,
                failed_node_id=signal.failed_node_id,
                evidence_json=json_dumps(signal.evidence),
                created_at=signal.created_at,
            ))
        return signal

    def list_for_attempt(self, attempt_id: str) -> list[ReplanSignal]:
        with self.db.session() as session:
            rows = session.execute(
                select(ReplanSignalRow)
                .where(ReplanSignalRow.attempt_id == attempt_id)
                .order_by(ReplanSignalRow.created_at, ReplanSignalRow.id)
            ).scalars().all()
            return [ReplanSignal(
                id=row.id,
                task_id=row.task_id,
                run_id=row.run_id,
                attempt_id=row.attempt_id,
                reason=row.reason,
                scope=row.scope,
                failed_node_id=row.failed_node_id,
                evidence=json_loads(row.evidence_json) or {},
                created_at=row.created_at,
            ) for row in rows]
