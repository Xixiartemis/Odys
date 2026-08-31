"""Durable parent/child ownership and idempotent delivery state."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from lhas.domain.enums import EventType
from lhas.domain.models import json_dumps, json_loads, utcnow
from lhas.persistence.event_store import EventStore
from lhas.persistence.orm import AttemptRow, DelegationLifecycleRow, DelegationRow, RunRow, TaskRow


class DispatchState(str, Enum):
    CREATED = "CREATED"
    DISPATCHED = "DISPATCHED"


class ChildExecutionState(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CRASHED = "CRASHED"
    VALIDATION_REJECTED = "VALIDATION_REJECTED"


class DeliveryState(str, Enum):
    NOT_READY = "NOT_READY"
    DELIVERY_PENDING = "DELIVERY_PENDING"
    DELIVERED = "DELIVERED"
    CONSUMED = "CONSUMED"


class ChildOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ChildExecutionState
    failure_type: str | None = Field(default=None, max_length=128)
    artifact_refs: list[str] = Field(default_factory=list)
    workspace_mutation_present: bool = False
    verification: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False
    child_run_id: str
    summary: str = Field(default="", max_length=8_000)


class DelegationLifecycle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delegation_id: str
    parent_attempt_id: str
    execution_owner: str
    conversation_owner: str
    delivery_owner: str
    dispatch_state: DispatchState = DispatchState.CREATED
    execution_state: ChildExecutionState = ChildExecutionState.CREATED
    delivery_state: DeliveryState = DeliveryState.NOT_READY
    delivery_token: str
    outcome: dict[str, Any] = Field(default_factory=dict)
    artifact_ref: str | None = None
    validator_result: bool | None = None
    retry_of_delegation_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    delivered_at: datetime | None = None
    consumed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class DelegationLifecycleRepository:
    def __init__(self, db):
        self.db = db

    def create(
        self,
        *,
        delegation_id: str,
        parent_attempt_id: str,
        execution_owner: str,
        conversation_owner: str,
        delivery_owner: str,
        retry_of_delegation_id: str | None = None,
    ) -> DelegationLifecycle:
        token = hashlib.sha256(f"delegation-delivery:{delegation_id}".encode()).hexdigest()
        item = DelegationLifecycle(
            delegation_id=delegation_id,
            parent_attempt_id=parent_attempt_id,
            execution_owner=execution_owner,
            conversation_owner=conversation_owner,
            delivery_owner=delivery_owner,
            delivery_token=token,
            retry_of_delegation_id=retry_of_delegation_id,
        )
        with self.db.session() as session:
            if session.get(DelegationLifecycleRow, delegation_id) is not None:
                raise ValueError("DELEGATION_LIFECYCLE_ALREADY_EXISTS")
            session.add(self._to_row(item))
        return item

    def get(self, delegation_id: str) -> DelegationLifecycle | None:
        with self.db.session() as session:
            row = session.get(DelegationLifecycleRow, delegation_id)
            return self._from_row(row) if row else None

    def list_for_parent_attempt(self, attempt_id: str) -> list[DelegationLifecycle]:
        with self.db.session() as session:
            rows = session.execute(
                select(DelegationLifecycleRow)
                .where(DelegationLifecycleRow.parent_attempt_id == attempt_id)
                .order_by(DelegationLifecycleRow.created_at, DelegationLifecycleRow.delegation_id)
            ).scalars().all()
            return [self._from_row(row) for row in rows]

    def list_delivery_pending(self) -> list[DelegationLifecycle]:
        with self.db.session() as session:
            rows = session.execute(
                select(DelegationLifecycleRow)
                .where(DelegationLifecycleRow.delivery_state == DeliveryState.DELIVERY_PENDING.value)
                .order_by(DelegationLifecycleRow.created_at)
            ).scalars().all()
            return [self._from_row(row) for row in rows]

    def update(self, item: DelegationLifecycle) -> DelegationLifecycle:
        item.updated_at = utcnow()
        with self.db.session() as session:
            row = session.get(DelegationLifecycleRow, item.delegation_id)
            if row is None:
                raise KeyError(f"delegation lifecycle not found: {item.delegation_id}")
            row.dispatch_state = item.dispatch_state.value
            row.execution_state = item.execution_state.value
            row.delivery_state = item.delivery_state.value
            row.outcome_json = json_dumps(item.outcome)
            row.artifact_ref = item.artifact_ref
            row.validator_result = None if item.validator_result is None else int(item.validator_result)
            row.started_at = item.started_at
            row.finished_at = item.finished_at
            row.delivered_at = item.delivered_at
            row.consumed_at = item.consumed_at
            row.updated_at = item.updated_at
        return item

    @staticmethod
    def _to_row(item: DelegationLifecycle):
        return DelegationLifecycleRow(
            delegation_id=item.delegation_id,
            parent_attempt_id=item.parent_attempt_id,
            execution_owner=item.execution_owner,
            conversation_owner=item.conversation_owner,
            delivery_owner=item.delivery_owner,
            dispatch_state=item.dispatch_state.value,
            execution_state=item.execution_state.value,
            delivery_state=item.delivery_state.value,
            delivery_token=item.delivery_token,
            outcome_json=json_dumps(item.outcome),
            artifact_ref=item.artifact_ref,
            validator_result=None,
            retry_of_delegation_id=item.retry_of_delegation_id,
            started_at=item.started_at,
            finished_at=item.finished_at,
            delivered_at=item.delivered_at,
            consumed_at=item.consumed_at,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )

    @staticmethod
    def _from_row(row):
        return DelegationLifecycle(
            delegation_id=row.delegation_id,
            parent_attempt_id=row.parent_attempt_id,
            execution_owner=row.execution_owner,
            conversation_owner=row.conversation_owner,
            delivery_owner=row.delivery_owner,
            dispatch_state=DispatchState(row.dispatch_state),
            execution_state=ChildExecutionState(row.execution_state),
            delivery_state=DeliveryState(row.delivery_state),
            delivery_token=row.delivery_token,
            outcome=json_loads(row.outcome_json) or {},
            artifact_ref=row.artifact_ref,
            validator_result=None if row.validator_result is None else bool(row.validator_result),
            retry_of_delegation_id=row.retry_of_delegation_id,
            started_at=row.started_at,
            finished_at=row.finished_at,
            delivered_at=row.delivered_at,
            consumed_at=row.consumed_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class DurableDeliveryService:
    """At-least-once durable attempt with idempotent logical consume."""

    def __init__(self, db):
        self.db = db
        self.repo = DelegationLifecycleRepository(db)
        self.events = EventStore(db)

    def record_started(self, delegation_id: str) -> DelegationLifecycle:
        item = self._require(delegation_id)
        item.dispatch_state = DispatchState.DISPATCHED
        item.execution_state = ChildExecutionState.RUNNING
        item.started_at = item.started_at or utcnow()
        return self.repo.update(item)

    def record_outcome(self, delegation_id: str, outcome: ChildOutcome, *, validator_result: bool | None) -> DelegationLifecycle:
        item = self._require(delegation_id)
        if item.delivery_state is not DeliveryState.NOT_READY:
            existing_status = item.outcome.get("status")
            existing_run = item.outcome.get("child_run_id")
            if existing_status == outcome.status.value and existing_run == outcome.child_run_id:
                return item
            raise ValueError("CONFLICTING_CHILD_OUTCOME")
        item.execution_state = outcome.status
        item.delivery_state = DeliveryState.DELIVERY_PENDING
        item.outcome = outcome.model_dump(mode="json")
        item.artifact_ref = outcome.artifact_refs[0][:2_000] if outcome.artifact_refs else None
        item.validator_result = validator_result
        item.finished_at = utcnow()
        self.repo.update(item)
        delegation = self._delegation(delegation_id)
        self.events.append(EventType.CHILD_OUTCOME_RECORDED, task_id=delegation.child_task_id, run_id=delegation.child_run_id, payload={"delegation_id": delegation_id, "status": outcome.status.value, "delivery_token": item.delivery_token, "validator_result": validator_result, "workspace_mutation_present": outcome.workspace_mutation_present})
        self.events.append(EventType.DELEGATION_DELIVERY_PENDING, task_id=delegation.parent_task_id, run_id=delegation.parent_run_id, attempt_id=item.parent_attempt_id, payload={"delegation_id": delegation_id, "delivery_token": item.delivery_token})
        return item

    def deliver(self, delegation_id: str) -> DelegationLifecycle:
        item = self._require(delegation_id)
        if item.delivery_state in {DeliveryState.DELIVERED, DeliveryState.CONSUMED}:
            return item
        if item.delivery_state is not DeliveryState.DELIVERY_PENDING:
            raise ValueError("DELEGATION_RESULT_NOT_READY")
        item.delivery_state = DeliveryState.DELIVERED
        item.delivered_at = utcnow()
        self.repo.update(item)
        delegation = self._delegation(delegation_id)
        self.events.append(EventType.DELEGATION_DELIVERED, task_id=delegation.parent_task_id, run_id=delegation.parent_run_id, attempt_id=item.parent_attempt_id, payload={"delegation_id": delegation_id, "delivery_token": item.delivery_token})
        return item

    def resume_pending_deliveries(self) -> list[DelegationLifecycle]:
        return [self.deliver(item.delegation_id) for item in self.repo.list_delivery_pending()]

    def consume_for_parent_attempt(self, attempt_id: str, consumed_tokens: set[str] | None = None) -> list[dict[str, Any]]:
        consumed_tokens = set(consumed_tokens or set())
        outcomes = []
        for item in self.repo.list_for_parent_attempt(attempt_id):
            if item.delivery_state not in {DeliveryState.DELIVERED, DeliveryState.CONSUMED}:
                continue
            if item.delivery_token in consumed_tokens or item.delivery_state is DeliveryState.CONSUMED:
                continue
            outcomes.append({"delivery_token": item.delivery_token, "delegation_id": item.delegation_id, "outcome": item.outcome})
            item.delivery_state = DeliveryState.CONSUMED
            item.consumed_at = utcnow()
            self.repo.update(item)
            delegation = self._delegation(item.delegation_id)
            self.events.append(EventType.DELEGATION_DELIVERY_CONSUMED, task_id=delegation.parent_task_id, run_id=delegation.parent_run_id, attempt_id=attempt_id, payload={"delegation_id": item.delegation_id, "delivery_token": item.delivery_token})
        return outcomes

    def validate_lineage(self, delegation_id: str) -> None:
        delegation = self._delegation(delegation_id)
        lifecycle = self._require(delegation_id)
        with self.db.session() as session:
            parent_task = session.get(TaskRow, delegation.parent_task_id)
            child_task = session.get(TaskRow, delegation.child_task_id)
            parent_run = session.get(RunRow, delegation.parent_run_id)
            parent_attempt = session.get(AttemptRow, lifecycle.parent_attempt_id)
        if parent_task is None or child_task is None or parent_run is None or parent_attempt is None:
            raise ValueError("MISSING_DELEGATION_OWNER")
        if parent_run.task_id != delegation.parent_task_id or parent_attempt.run_id != delegation.parent_run_id:
            raise ValueError("MALFORMED_DELEGATION_OWNER")
        if delegation.parent_task_id == delegation.child_task_id:
            raise ValueError("CYCLIC_DELEGATION_LINEAGE")
        visited = {delegation.child_task_id}
        current_parent = delegation.parent_task_id
        while current_parent:
            if current_parent in visited:
                raise ValueError("CYCLIC_DELEGATION_LINEAGE")
            visited.add(current_parent)
            with self.db.session() as session:
                parent = session.execute(select(DelegationRow).where(DelegationRow.child_task_id == current_parent)).scalar_one_or_none()
            current_parent = parent.parent_task_id if parent else ""

    def _require(self, delegation_id: str) -> DelegationLifecycle:
        item = self.repo.get(delegation_id)
        if item is None:
            raise KeyError(f"delegation lifecycle not found: {delegation_id}")
        return item

    def _delegation(self, delegation_id: str):
        with self.db.session() as session:
            row = session.get(DelegationRow, delegation_id)
            if row is None:
                raise KeyError(f"delegation not found: {delegation_id}")
            return row
