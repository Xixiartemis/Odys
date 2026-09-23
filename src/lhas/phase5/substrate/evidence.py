"""Append-only Evidence Ledger.

EvidenceEvent records are immutable once appended.
Monotonic sequence per run.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


def _new_id() -> str:
    import uuid
    return uuid.uuid4().hex


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class EvidenceEventType(str, Enum):
    RUN_STARTED = "RUN_STARTED"
    MODEL_TURN_COMPLETED = "MODEL_TURN_COMPLETED"
    TOOL_REQUESTED = "TOOL_REQUESTED"
    TOOL_OBSERVED = "TOOL_OBSERVED"
    ENVIRONMENT_OBSERVED = "ENVIRONMENT_OBSERVED"
    SIDE_EFFECT_CONFIRMED = "SIDE_EFFECT_CONFIRMED"
    SIDE_EFFECT_UNCERTAIN = "SIDE_EFFECT_UNCERTAIN"
    PROGRESS_SIGNAL = "PROGRESS_SIGNAL"
    RECOVERY_DECISION = "RECOVERY_DECISION"
    VALIDATION_CANDIDATE = "VALIDATION_CANDIDATE"
    VALIDATION_RESULT = "VALIDATION_RESULT"
    STATE_COMMITTED = "STATE_COMMITTED"
    TERMINATED = "TERMINATED"


class EvidenceEvent(BaseModel):
    """Immutable append-only evidence record.

    Never mutated after creation.  Sequence is monotonic per run.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(default_factory=_new_id)
    sequence: int
    task_id: str
    run_id: str
    attempt_id: str
    strategy_epoch: int = 0
    event_type: EvidenceEventType
    parent_event_id: Optional[str] = None
    correlation_id: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_digest: str = ""
    artifact_refs: list[str] = Field(default_factory=list)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        if not self.payload_digest:
            object.__setattr__(
                self, "payload_digest",
                hashlib.sha256(_canonical_json(self.payload)).hexdigest(),
            )


class EvidenceLedger:
    """Append-only in-memory evidence ledger.

    Monotonic sequence guarantee.  No mutation of historical records.
    """

    def __init__(self, run_id: str):
        self._run_id = run_id
        self._events: list[EvidenceEvent] = []
        self._next_sequence = 0
        self._by_id: dict[str, EvidenceEvent] = {}

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def next_sequence(self) -> int:
        return self._next_sequence

    def append(
        self,
        *,
        task_id: str,
        attempt_id: str,
        event_type: EvidenceEventType,
        payload: Optional[dict[str, Any]] = None,
        parent_event_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        artifact_refs: Optional[list[str]] = None,
        strategy_epoch: int = 0,
    ) -> EvidenceEvent:
        """Append a new evidence event.  Returns the immutable record."""
        event = EvidenceEvent(
            sequence=self._next_sequence,
            task_id=task_id,
            run_id=self._run_id,
            attempt_id=attempt_id,
            strategy_epoch=strategy_epoch,
            event_type=event_type,
            parent_event_id=parent_event_id,
            correlation_id=correlation_id,
            payload=payload or {},
            artifact_refs=artifact_refs or [],
        )
        self._events.append(event)
        self._by_id[event.evidence_id] = event
        self._next_sequence += 1
        return event

    def get(self, evidence_id: str) -> Optional[EvidenceEvent]:
        return self._by_id.get(evidence_id)

    def events_since(self, sequence: int) -> list[EvidenceEvent]:
        """Return events with sequence >= given value."""
        return [e for e in self._events if e.sequence >= sequence]

    def events_by_type(self, event_type: EvidenceEventType) -> list[EvidenceEvent]:
        return [e for e in self._events if e.event_type == event_type]

    def all_events(self) -> list[EvidenceEvent]:
        return list(self._events)

    def count(self) -> int:
        return len(self._events)

    def last_event(self) -> Optional[EvidenceEvent]:
        return self._events[-1] if self._events else None
