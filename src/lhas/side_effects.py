"""Runtime-level side-effect receipts and conservative reconciliation.

This module records facts about an effect boundary.  It is deliberately
independent of benchmark runners and does not decide whether a workflow
should retry, repair, or replan.  Those decisions consume the receipt facts.

The receipt is not an exactly-once guarantee for arbitrary external systems.
It makes the commit/observation gap durable and gives callers a fail-closed
answer when that gap cannot be reconciled.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import Enum
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from lhas.domain.enums import EventType
from lhas.domain.models import json_dumps, json_loads, new_id, utcnow
from lhas.persistence.event_store import EventStore
from lhas.persistence.orm import SideEffectReceiptRow


class EffectClass(str, Enum):
    """Declared effect semantics for one capability invocation."""

    NONE = "NONE"
    LOCAL_REVERSIBLE = "LOCAL_REVERSIBLE"
    LOCAL_DURABLE = "LOCAL_DURABLE"
    EXTERNAL_IDEMPOTENT = "EXTERNAL_IDEMPOTENT"
    EXTERNAL_RECEIPT = "EXTERNAL_RECEIPT"
    EXTERNAL_UNVERIFIABLE = "EXTERNAL_UNVERIFIABLE"

    # Compact names useful at adapter boundaries.  They are aliases, not
    # extra semantics, so the persisted contract remains the six values above.
    RECONCILABLE = "LOCAL_DURABLE"
    IDEMPOTENT = "EXTERNAL_IDEMPOTENT"
    RECEIPT_BACKED = "EXTERNAL_RECEIPT"
    UNVERIFIABLE = "EXTERNAL_UNVERIFIABLE"


class ReceiptStatus(str, Enum):
    REQUEST_CREATED = "REQUEST_CREATED"
    DISPATCH_STARTED = "DISPATCH_STARTED"
    COMMITTED = "COMMITTED"
    OBSERVED = "OBSERVED"
    RECONCILED = "RECONCILED"
    FAILED = "FAILED"
    COMMIT_STATE_UNKNOWN = "COMMIT_STATE_UNKNOWN"


class ReconciliationStrategy(str, Enum):
    NONE = "NONE"
    WORKSPACE_DIGEST = "WORKSPACE_DIGEST"
    IDEMPOTENCY_LOOKUP = "IDEMPOTENCY_LOOKUP"
    RECEIPT_LOOKUP = "RECEIPT_LOOKUP"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"


class ReplayDecision(str, Enum):
    REUSE_COMMITTED = "REUSE_COMMITTED"
    SAFE_TO_RETRY = "SAFE_TO_RETRY"
    RECONCILE_FIRST = "RECONCILE_FIRST"
    REQUIRE_HUMAN = "REQUIRE_HUMAN"
    NO_AUTOMATIC_RETRY = "NO_AUTOMATIC_RETRY"


class SideEffectReceipt(BaseModel):
    """Durable identity and state for one runtime side-effect boundary."""

    model_config = ConfigDict(extra="forbid")

    receipt_id: str = Field(default_factory=new_id, min_length=1, max_length=128)
    operation_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=256)
    task_id: str | None = Field(default=None, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)
    step_id: str = Field(min_length=1, max_length=128)
    tool_call_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(min_length=1, max_length=128)
    effect_class: EffectClass
    target_fingerprint: str = Field(min_length=64, max_length=64)
    request_hash: str = Field(min_length=64, max_length=64)
    dispatch_started_at: datetime | None = None
    commit_observed_at: datetime | None = None
    observation_received_at: datetime | None = None
    status: ReceiptStatus = ReceiptStatus.REQUEST_CREATED
    result_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)
    external_resource_id: str | None = Field(default=None, max_length=256)
    reconciliation_strategy: ReconciliationStrategy = ReconciliationStrategy.NONE
    replay_safe: bool = False
    workspace_before_digest: str | None = Field(default=None, min_length=64, max_length=64)
    workspace_after_digest: str | None = Field(default=None, min_length=64, max_length=64)
    error_class: str | None = Field(default=None, max_length=128)
    sanitized_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @field_validator("sanitized_metadata")
    @classmethod
    def bound_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        # Metadata is forensic context, never an argument/result transport.
        return {
            str(key)[:128]: ("[REDACTED]" if _SECRET_KEY.search(str(key)) else _safe_value(item))
            for key, item in list(value.items())[:64]
        }


def _safe_value(value: Any, limit: int = 1024) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, list):
        return [_safe_value(item, max(64, limit // 8)) for item in value[:32]]
    if isinstance(value, dict):
        return {
            str(key)[:128]: (
                "[REDACTED]"
                if _SECRET_KEY.search(str(key))
                else _safe_value(item, max(64, limit // 8))
            )
            for key, item in list(value.items())[:32]
        }
    return str(value)[:limit]


_SECRET_KEY = re.compile(r"(?i)(api[_-]?key|authorization|token|secret|password|credential)")


def _safe_metadata(value: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key)[:128]: ("[REDACTED]" if _SECRET_KEY.search(str(key)) else _safe_value(item))
        for key, item in list(value.items())[:64]
    }


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _strategy(effect_class: EffectClass) -> ReconciliationStrategy:
    if effect_class in {EffectClass.LOCAL_REVERSIBLE, EffectClass.LOCAL_DURABLE}:
        return ReconciliationStrategy.WORKSPACE_DIGEST
    if effect_class is EffectClass.EXTERNAL_IDEMPOTENT:
        return ReconciliationStrategy.IDEMPOTENCY_LOOKUP
    if effect_class is EffectClass.EXTERNAL_RECEIPT:
        return ReconciliationStrategy.RECEIPT_LOOKUP
    if effect_class is EffectClass.EXTERNAL_UNVERIFIABLE:
        return ReconciliationStrategy.HUMAN_REQUIRED
    return ReconciliationStrategy.NONE


def _initial_replay_safe(effect_class: EffectClass, idempotency_key: str | None) -> bool:
    return effect_class is EffectClass.NONE or (
        effect_class is EffectClass.EXTERNAL_IDEMPOTENT and bool(idempotency_key)
    )


class SideEffectReceiptRepository:
    """SQL projection for receipts; lifecycle events remain append-only."""

    def __init__(self, db):
        self.db = db

    def create(self, receipt: SideEffectReceipt) -> SideEffectReceipt:
        with self.db.session() as session:
            session.add(SideEffectReceiptRow(**_row_values(receipt)))
        return receipt

    def update(self, receipt: SideEffectReceipt) -> SideEffectReceipt:
        receipt.updated_at = utcnow()
        with self.db.session() as session:
            row = session.get(SideEffectReceiptRow, receipt.receipt_id)
            if row is None:
                raise KeyError(f"side-effect receipt not found: {receipt.receipt_id}")
            values = _row_values(receipt)
            for key, value in values.items():
                if key != "receipt_id":
                    setattr(row, key, value)
        return receipt

    def get(self, receipt_id: str) -> SideEffectReceipt | None:
        with self.db.session() as session:
            row = session.get(SideEffectReceiptRow, receipt_id)
            return _from_row(row) if row else None

    def get_by_operation(self, operation_id: str) -> SideEffectReceipt | None:
        with self.db.session() as session:
            row = session.execute(
                select(SideEffectReceiptRow)
                .where(SideEffectReceiptRow.operation_id == operation_id)
                .order_by(SideEffectReceiptRow.created_at.desc())
            ).scalars().first()
            return _from_row(row) if row else None

    def list_for_attempt(self, attempt_id: str) -> list[SideEffectReceipt]:
        with self.db.session() as session:
            rows = session.execute(
                select(SideEffectReceiptRow)
                .where(SideEffectReceiptRow.attempt_id == attempt_id)
                .order_by(SideEffectReceiptRow.created_at, SideEffectReceiptRow.receipt_id)
            ).scalars().all()
            return [_from_row(row) for row in rows]


def _row_values(receipt: SideEffectReceipt) -> dict[str, Any]:
    value = receipt.model_dump()
    value["effect_class"] = receipt.effect_class.value
    value["status"] = receipt.status.value
    value["reconciliation_strategy"] = receipt.reconciliation_strategy.value
    value["sanitized_metadata_json"] = json_dumps(receipt.sanitized_metadata)
    value.pop("sanitized_metadata")
    return value


def _from_row(row: SideEffectReceiptRow) -> SideEffectReceipt:
    values = {
        "receipt_id": row.receipt_id,
        "operation_id": row.operation_id,
        "idempotency_key": row.idempotency_key,
        "task_id": row.task_id,
        "run_id": row.run_id,
        "attempt_id": row.attempt_id,
        "step_id": row.step_id,
        "tool_call_id": row.tool_call_id,
        "tool_name": row.tool_name,
        "effect_class": EffectClass(row.effect_class),
        "target_fingerprint": row.target_fingerprint,
        "request_hash": row.request_hash,
        "dispatch_started_at": row.dispatch_started_at,
        "commit_observed_at": row.commit_observed_at,
        "observation_received_at": row.observation_received_at,
        "status": ReceiptStatus(row.status),
        "result_fingerprint": row.result_fingerprint,
        "external_resource_id": row.external_resource_id,
        "reconciliation_strategy": ReconciliationStrategy(row.reconciliation_strategy),
        "replay_safe": bool(row.replay_safe),
        "workspace_before_digest": row.workspace_before_digest,
        "workspace_after_digest": row.workspace_after_digest,
        "error_class": row.error_class,
        "sanitized_metadata": json_loads(row.sanitized_metadata_json) or {},
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    return SideEffectReceipt(**values)


class SideEffectReceiptManager:
    """Record receipt facts and expose conservative replay facts."""

    def __init__(self, db, *, event_store: EventStore | None = None):
        self.db = db
        self.receipts = SideEffectReceiptRepository(db)
        self.events = event_store or EventStore(db)

    def _event(self, event_type: EventType, receipt: SideEffectReceipt, **extra: Any) -> None:
        self.events.append(
            event_type,
            task_id=receipt.task_id,
            run_id=receipt.run_id,
            attempt_id=receipt.attempt_id,
            payload={
                "receipt_id": receipt.receipt_id,
                "operation_id": receipt.operation_id,
                "task_id": receipt.task_id,
                "step_id": receipt.step_id,
                "tool_call_id": receipt.tool_call_id,
                "tool_name": receipt.tool_name,
                "effect_class": receipt.effect_class.value,
                "status": receipt.status.value,
                "target_fingerprint": receipt.target_fingerprint,
                "request_hash": receipt.request_hash,
                **extra,
            },
        )

    def begin(
        self,
        *,
        operation_id: str,
        task_id: str | None = None,
        run_id: str,
        attempt_id: str,
        step_id: str,
        tool_call_id: str,
        tool_name: str,
        effect_class: EffectClass,
        target: Any,
        request: Any,
        idempotency_key: str | None = None,
        workspace_before_digest: str | None = None,
        sanitized_metadata: dict[str, Any] | None = None,
    ) -> SideEffectReceipt:
        receipt = SideEffectReceipt(
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            task_id=task_id,
            run_id=run_id,
            attempt_id=attempt_id,
            step_id=step_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            effect_class=effect_class,
            target_fingerprint=stable_hash(target),
            request_hash=stable_hash(request),
            reconciliation_strategy=_strategy(effect_class),
            replay_safe=_initial_replay_safe(effect_class, idempotency_key),
            workspace_before_digest=workspace_before_digest,
            sanitized_metadata=_safe_metadata(sanitized_metadata or {}),
        )
        self.receipts.create(receipt)
        self._event(EventType.SIDE_EFFECT_REQUEST_CREATED, receipt)
        return receipt

    def _get(self, receipt_id: str) -> SideEffectReceipt:
        receipt = self.receipts.get(receipt_id)
        if receipt is None:
            raise KeyError(f"side-effect receipt not found: {receipt_id}")
        return receipt

    def mark_dispatch_started(self, receipt_id: str) -> SideEffectReceipt:
        receipt = self._get(receipt_id)
        if receipt.status is ReceiptStatus.REQUEST_CREATED:
            receipt.status = ReceiptStatus.DISPATCH_STARTED
            receipt.dispatch_started_at = utcnow()
            self.receipts.update(receipt)
            self._event(EventType.SIDE_EFFECT_DISPATCH_STARTED, receipt)
        return receipt

    def mark_committed(
        self,
        receipt_id: str,
        *,
        result: Any = None,
        workspace_before_digest: str | None = None,
        workspace_after_digest: str | None = None,
        external_resource_id: str | None = None,
    ) -> SideEffectReceipt:
        receipt = self._get(receipt_id)
        if receipt.status in {ReceiptStatus.COMMITTED, ReceiptStatus.OBSERVED}:
            return receipt
        receipt.status = ReceiptStatus.COMMITTED
        receipt.commit_observed_at = receipt.commit_observed_at or utcnow()
        receipt.workspace_before_digest = workspace_before_digest or receipt.workspace_before_digest
        receipt.workspace_after_digest = workspace_after_digest or receipt.workspace_after_digest
        receipt.external_resource_id = external_resource_id or receipt.external_resource_id
        receipt.result_fingerprint = stable_hash(result) if result is not None else receipt.result_fingerprint
        receipt.replay_safe = True
        self.receipts.update(receipt)
        self._event(EventType.SIDE_EFFECT_COMMITTED, receipt)
        return receipt

    def mark_observed(self, receipt_id: str, *, result: Any = None) -> SideEffectReceipt:
        receipt = self._get(receipt_id)
        if receipt.status is ReceiptStatus.OBSERVED:
            return receipt
        if receipt.status is not ReceiptStatus.COMMITTED:
            raise ValueError("observation cannot precede durable commit receipt")
        receipt.status = ReceiptStatus.OBSERVED
        receipt.observation_received_at = utcnow()
        receipt.result_fingerprint = stable_hash(result) if result is not None else receipt.result_fingerprint
        self.receipts.update(receipt)
        self._event(EventType.SIDE_EFFECT_OBSERVATION_RECEIVED, receipt)
        return receipt

    def mark_failed(self, receipt_id: str, *, error_class: str) -> SideEffectReceipt:
        receipt = self._get(receipt_id)
        if receipt.status in {ReceiptStatus.COMMITTED, ReceiptStatus.OBSERVED}:
            return receipt
        receipt.status = ReceiptStatus.FAILED
        receipt.error_class = error_class[:128]
        self.receipts.update(receipt)
        self._event(EventType.SIDE_EFFECT_FAILED, receipt, error_class=receipt.error_class)
        return receipt

    def mark_unknown(self, receipt_id: str, *, error_class: str = "COMMIT_STATE_UNKNOWN") -> SideEffectReceipt:
        receipt = self._get(receipt_id)
        if receipt.status in {ReceiptStatus.COMMITTED, ReceiptStatus.OBSERVED}:
            return receipt
        receipt.status = ReceiptStatus.COMMIT_STATE_UNKNOWN
        receipt.error_class = error_class[:128]
        receipt.replay_safe = False
        receipt.reconciliation_strategy = ReconciliationStrategy.HUMAN_REQUIRED
        self.receipts.update(receipt)
        self._event(EventType.SIDE_EFFECT_COMMIT_UNKNOWN, receipt, error_class=receipt.error_class)
        return receipt

    def reconcile(
        self,
        receipt_id: str,
        *,
        observed_digest: str | None = None,
        effect_present: bool | None = None,
        lookup: Callable[[SideEffectReceipt], Any] | None = None,
    ) -> SideEffectReceipt:
        receipt = self._get(receipt_id)
        if receipt.status in {ReceiptStatus.COMMITTED, ReceiptStatus.OBSERVED, ReceiptStatus.RECONCILED}:
            return receipt
        if receipt.effect_class in {EffectClass.LOCAL_REVERSIBLE, EffectClass.LOCAL_DURABLE}:
            if observed_digest is not None and receipt.workspace_before_digest is not None:
                if observed_digest != receipt.workspace_before_digest:
                    return self.mark_committed(receipt_id, workspace_after_digest=observed_digest)
                receipt.status = ReceiptStatus.RECONCILED
                receipt.replay_safe = True
                self.receipts.update(receipt)
                self._event(EventType.SIDE_EFFECT_RECONCILED, receipt, effect_present=False)
                return receipt
            if effect_present is True:
                return self.mark_committed(receipt_id)
            if effect_present is False:
                receipt.status = ReceiptStatus.RECONCILED
                receipt.replay_safe = True
                self.receipts.update(receipt)
                self._event(EventType.SIDE_EFFECT_RECONCILED, receipt, effect_present=False)
                return receipt
        elif receipt.effect_class is EffectClass.EXTERNAL_IDEMPOTENT and lookup is not None:
            found = lookup(receipt)
            if found:
                return self.mark_committed(
                    receipt_id,
                    result=found,
                    external_resource_id=(found.get("resource_id") or found.get("operation_id"))
                    if isinstance(found, dict) else None,
                )
        elif receipt.effect_class is EffectClass.EXTERNAL_RECEIPT and lookup is not None:
            found = lookup(receipt)
            if found:
                return self.mark_committed(
                    receipt_id,
                    result=found,
                    external_resource_id=(found.get("resource_id") or found.get("operation_id"))
                    if isinstance(found, dict) else None,
                )
        return self.mark_unknown(receipt_id)

    def replay_decision(self, receipt_id: str) -> ReplayDecision:
        receipt = self._get(receipt_id)
        if receipt.status in {ReceiptStatus.COMMITTED, ReceiptStatus.OBSERVED}:
            return ReplayDecision.REUSE_COMMITTED
        if receipt.status is ReceiptStatus.RECONCILED and receipt.replay_safe:
            return ReplayDecision.SAFE_TO_RETRY
        if receipt.status is ReceiptStatus.COMMIT_STATE_UNKNOWN:
            return ReplayDecision.REQUIRE_HUMAN
        if receipt.effect_class is EffectClass.EXTERNAL_IDEMPOTENT and receipt.idempotency_key:
            return ReplayDecision.SAFE_TO_RETRY
        if receipt.effect_class is EffectClass.EXTERNAL_UNVERIFIABLE:
            return ReplayDecision.REQUIRE_HUMAN
        return ReplayDecision.RECONCILE_FIRST


class DeterministicIdempotentExternalFake:
    """Small deterministic fake for receipt integration tests and examples."""

    def __init__(self):
        self.request_count = 0
        self.effect_count = 0
        self._effects: dict[str, dict[str, Any]] = {}

    def apply(self, *, idempotency_key: str, target: str, payload: Any) -> dict[str, Any]:
        self.request_count += 1
        if idempotency_key not in self._effects:
            self.effect_count += 1
            self._effects[idempotency_key] = {
                "operation_id": stable_hash(["operation", idempotency_key, target])[:32],
                "resource_id": stable_hash([idempotency_key, target])[:24],
                "target": target,
                "payload_fingerprint": stable_hash(payload),
            }
        return dict(self._effects[idempotency_key])

    def lookup(self, idempotency_key: str) -> dict[str, Any] | None:
        value = self._effects.get(idempotency_key)
        return dict(value) if value else None


__all__ = [
    "DeterministicIdempotentExternalFake",
    "EffectClass",
    "ReceiptStatus",
    "ReconciliationStrategy",
    "ReplayDecision",
    "SideEffectReceipt",
    "SideEffectReceiptManager",
    "SideEffectReceiptRepository",
    "stable_hash",
]
