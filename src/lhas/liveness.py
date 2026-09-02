"""Event-derived execution health and liveness projection.

RunStatus answers lifecycle questions.  ExecutionHealth answers whether a
running process has produced durable business progress recently.  The latter
is intentionally derived from committed events so it survives process
restarts and cannot be kept green by a polling heartbeat.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from lhas.domain.enums import EventType, ExecutionHealth, RunStatus


PROGRESS_EVENT_TYPES = frozenset(
    {
        EventType.ATTEMPT_STARTED,
        EventType.CONTEXT_BUILT,
        EventType.EXECUTOR_STARTED,
        EventType.MODEL_CALL_STARTED,
        EventType.MODEL_RESPONSE_RECEIVED,
        EventType.NATIVE_MODEL_TURN_STARTED,
        EventType.NATIVE_MODEL_TURN_COMPLETED,
        EventType.NATIVE_TOOL_REQUESTED,
        EventType.NATIVE_TOOL_STARTED,
        EventType.NATIVE_TOOL_OBSERVED,
        EventType.NATIVE_EXECUTION_SNAPSHOT,
        EventType.VALIDATION_PASSED,
        EventType.VALIDATION_FAILED,
        EventType.CHECKPOINT_CREATED,
        EventType.RUNTIME_TARGET_SWITCH_COMMITTED,
        EventType.REPLAN_SIGNAL_CREATED,
    }
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _event_type(event: Any) -> EventType | None:
    value = getattr(event, "event_type", event.get("event_type") if isinstance(event, dict) else None)
    try:
        return value if isinstance(value, EventType) else EventType(str(value))
    except (TypeError, ValueError):
        return None


def _event_timestamp(event: Any) -> datetime | None:
    value = getattr(event, "timestamp", None)
    if value is None and isinstance(event, dict):
        value = event.get("timestamp")
    return _utc(value) if isinstance(value, datetime) else None


def _event_id(event: Any) -> int:
    value = getattr(event, "id", None)
    if value is None and isinstance(event, dict):
        value = event.get("event_id") or event.get("id")
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def project_liveness(
    events: Iterable[Any],
    *,
    run_status: RunStatus | str,
    now: datetime | None = None,
    stall_threshold_seconds: float = 600.0,
) -> dict[str, Any]:
    """Project safe, deterministic health metadata from durable events."""
    ordered = list(events)
    reference = _utc(now or datetime.now(timezone.utc))
    threshold_ms = max(0, int(float(stall_threshold_seconds) * 1000))
    last_event = ordered[-1] if ordered else None
    progress = [
        event
        for event in ordered
        if _event_type(event) in PROGRESS_EVENT_TYPES and _event_timestamp(event) is not None
    ]
    last_progress = progress[-1] if progress else None
    last_progress_at = _event_timestamp(last_progress) if last_progress else None
    no_progress_ms = (
        max(0, int((reference - last_progress_at).total_seconds() * 1000))
        if last_progress_at is not None
        else None
    )

    operation: str | None = None
    operation_started_at: datetime | None = None
    for event in ordered:
        event_type = _event_type(event)
        timestamp = _event_timestamp(event)
        if timestamp is None:
            continue
        if event_type is EventType.MODEL_CALL_STARTED:
            operation, operation_started_at = "MODEL_CALL", timestamp
        elif event_type is EventType.MODEL_RESPONSE_RECEIVED:
            operation, operation_started_at = "PARSER", timestamp
        elif event_type is EventType.MODEL_RESPONSE_PARSED:
            operation, operation_started_at = None, None
        elif event_type is EventType.MODEL_RESPONSE_REJECTED:
            operation, operation_started_at = None, None
        elif event_type in {EventType.NATIVE_TOOL_REQUESTED, EventType.NATIVE_TOOL_STARTED}:
            operation, operation_started_at = "TOOL", timestamp
        elif event_type is EventType.NATIVE_TOOL_OBSERVED:
            operation, operation_started_at = None, None
        elif event_type is EventType.VALIDATION_STARTED:
            operation, operation_started_at = "VALIDATOR", timestamp
        elif event_type in {EventType.VALIDATION_PASSED, EventType.VALIDATION_FAILED}:
            operation, operation_started_at = None, None
        elif event_type is EventType.RECOVERY_STARTED:
            operation, operation_started_at = "RECOVERY", timestamp
        elif event_type in {
            EventType.ATTEMPT_COMPLETED,
            EventType.ATTEMPT_FAILED,
            EventType.ATTEMPT_TIMED_OUT,
            EventType.ATTEMPT_CRASHED,
        }:
            operation, operation_started_at = None, None

    try:
        lifecycle = run_status if isinstance(run_status, RunStatus) else RunStatus(str(run_status))
    except (TypeError, ValueError):
        lifecycle = None
    if lifecycle is not RunStatus.RUNNING:
        health = ExecutionHealth.IDLE
    elif last_progress_at is None:
        health = ExecutionHealth.UNKNOWN
    elif no_progress_ms is not None and no_progress_ms > threshold_ms:
        health = ExecutionHealth.STALLED
    else:
        health = ExecutionHealth.ACTIVE

    operation_age_ms = (
        max(0, int((reference - operation_started_at).total_seconds() * 1000))
        if operation_started_at is not None
        else None
    )
    return {
        "execution_health": health.value,
        "last_progress_at": last_progress_at.isoformat() if last_progress_at else None,
        "last_progress_event_cursor": _event_id(last_progress) if last_progress else None,
        "last_progress_event_type": _event_type(last_progress).value if last_progress else None,
        "last_event_cursor": _event_id(last_event) if last_event else 0,
        "last_event_type": _event_type(last_event).value if last_event else None,
        "current_operation": operation or "IDLE",
        "operation_started_at": operation_started_at.isoformat() if operation_started_at else None,
        "operation_age_ms": operation_age_ms,
        "heartbeat_at": None,
        "no_progress_duration_ms": no_progress_ms,
        "stall_threshold_ms": threshold_ms,
    }
