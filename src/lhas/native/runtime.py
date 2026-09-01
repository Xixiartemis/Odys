"""Durable runtime-target identity, provider classification, and switching."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable

from lhas.domain.enums import EventType
from lhas.domain.models import json_dumps, json_loads, new_id, utcnow
from lhas.native.models import (
    ProviderFailureCategory,
    ProviderHealthState,
    RuntimeTarget,
    TargetSwitch,
    TargetSwitchState,
)
from lhas.persistence.event_store import EventStore
from lhas.persistence.orm import NativeRuntimeTargetRow, ProviderHealthRow


class RuntimeTargetError(RuntimeError):
    def __init__(self, code: str, message: str, *, candidates: list[dict[str, str]] | None = None):
        super().__init__(message)
        self.code = code
        self.candidates = candidates or []


class RuntimeTargetResolver:
    """Resolve only explicit or durably unambiguous provider identities."""

    @staticmethod
    def resolve(model_id: str, configured: Iterable[RuntimeTarget], *, provider_id: str | None = None,
                credential_route_id: str | None = None) -> RuntimeTarget:
        candidates = [item for item in configured if item.model_id == model_id]
        if provider_id is not None:
            candidates = [item for item in candidates if item.provider_id == provider_id]
        if credential_route_id is not None:
            candidates = [item for item in candidates if item.credential_route_id == credential_route_id]
        safe = [item.safe_projection() for item in candidates]
        if not candidates:
            raise RuntimeTargetError("RUNTIME_TARGET_NOT_CONFIGURED", f"no configured target for model {model_id}")
        if len(candidates) != 1:
            raise RuntimeTargetError(
                "AMBIGUOUS_RUNTIME_TARGET",
                f"model {model_id} maps to multiple configured runtime targets",
                candidates=safe,
            )
        return candidates[0]


class ProviderFailureClassifier:
    """Conservative provider error classifier; unknown errors stay bounded."""

    @staticmethod
    def classify(error: Any) -> ProviderFailureCategory:
        if isinstance(error, ProviderFailureCategory):
            return error
        status = getattr(error, "status_code", None)
        response = getattr(error, "response", None)
        status = status or getattr(response, "status_code", None)
        try:
            status = int(status) if status is not None else None
        except (TypeError, ValueError):
            status = None
        text = " ".join(str(getattr(error, key, "")) for key in ("code", "type", "message"))
        text += " " + str(error)
        for key in ("body", "data", "details", "metadata"):
            value = getattr(error, key, None)
            if value:
                text += " " + str(value)
        upper = text[:4000].upper()
        if "PROVIDER_RESPONSE_NOT_NORMALIZABLE" in upper:
            return ProviderFailureCategory.MALFORMED_PROVIDER_RESPONSE
        quota_evidence = (
            re.search(r"\b(?:MONTHLY|WEEKLY|DAILY|ANNUAL)\s+(?:USAGE|QUOTA|LIMIT)\b", upper)
            or re.search(r"\b(?:BILLING\s+PERIOD|SUBSCRIPTION|PLAN)\b.{0,80}\b(?:EXHAUST|LIMIT|QUOTA)\b", upper)
            or re.search(r"\bUSAGE\s+LIMIT\b.{0,100}\bRESET\s+IN\s+(?:\d+\s+)?(?:DAY|DAYS|WEEK|WEEKS|MONTH|MONTHS)\b", upper)
            or "PROVIDER QUOTA" in upper
            or (status == 429 and "QUOTA" in upper)
        )
        transient_429_evidence = any(token in upper for token in (
            "RATE LIMIT", "REQUESTS PER MINUTE", "TOKENS PER MINUTE", "RETRY-AFTER",
            "BURST LIMIT", "TOO MANY REQUESTS",
        ))
        if quota_evidence:
            return ProviderFailureCategory.QUOTA_EXHAUSTED
        if any(token in upper for token in ("BILLING", "CREDIT", "INSUFFICIENT FUNDS", "PAYMENT REQUIRED")) or status == 402:
            return ProviderFailureCategory.BILLING_OR_CREDIT_EXHAUSTED
        if status in {401, 403} or any(token in upper for token in ("INVALID API KEY", "AUTHENTICATION", "UNAUTHORIZED", "FORBIDDEN")):
            return ProviderFailureCategory.AUTH_INVALID
        if status == 429:
            return ProviderFailureCategory.TRANSIENT_RATE_LIMIT if transient_429_evidence else ProviderFailureCategory.UNKNOWN_PROVIDER_FAILURE
        if isinstance(error, TimeoutError) or "TIMEOUT" in upper:
            return ProviderFailureCategory.PROVIDER_TIMEOUT
        if status in {408, 425, 500, 502, 503, 504} or any(token in upper for token in ("UNAVAILABLE", "CONNECTION RESET", "SERVICE DOWN")):
            return ProviderFailureCategory.PROVIDER_UNAVAILABLE
        return ProviderFailureCategory.UNKNOWN_PROVIDER_FAILURE

    @staticmethod
    def taxonomy_code(category: ProviderFailureCategory | str) -> str:
        """Stable, secret-free lower-case taxonomy for user-facing evidence."""
        category = ProviderFailureCategory(category)
        return {
            ProviderFailureCategory.PROVIDER_TIMEOUT: "provider_timeout",
            ProviderFailureCategory.QUOTA_EXHAUSTED: "quota_exhausted",
            ProviderFailureCategory.TRANSIENT_RATE_LIMIT: "rate_limit",
            ProviderFailureCategory.PROVIDER_UNAVAILABLE: "connection_error",
            ProviderFailureCategory.MALFORMED_PROVIDER_RESPONSE: "invalid_response",
            ProviderFailureCategory.UNKNOWN_PROVIDER_FAILURE: "unknown_provider_failure",
            ProviderFailureCategory.BILLING_OR_CREDIT_EXHAUSTED: "billing_or_credit_exhausted",
            ProviderFailureCategory.AUTH_INVALID: "auth_invalid",
        }[category]

    @classmethod
    def report(cls, error: Any) -> dict[str, Any]:
        category = cls.classify(error)
        return {
            "category": category.value,
            "same_target_retryable": category in {ProviderFailureCategory.TRANSIENT_RATE_LIMIT, ProviderFailureCategory.PROVIDER_UNAVAILABLE, ProviderFailureCategory.PROVIDER_TIMEOUT},
            "safe_error": str(error)[:512],
        }

    @classmethod
    def classify_response(cls, response: Any) -> ProviderFailureCategory:
        """Classify a provider payload before it reaches the response parser."""
        if response is None:
            return ProviderFailureCategory.MALFORMED_PROVIDER_RESPONSE
        if isinstance(response, dict):
            if "choices" not in response and not any(key in response for key in ("content", "tool_calls", "completion_claim")):
                return ProviderFailureCategory.MALFORMED_PROVIDER_RESPONSE
            return ProviderFailureCategory.UNKNOWN_PROVIDER_FAILURE
        if not hasattr(response, "choices") and not hasattr(response, "content") and not hasattr(response, "tool_calls"):
            return ProviderFailureCategory.MALFORMED_PROVIDER_RESPONSE
        return ProviderFailureCategory.UNKNOWN_PROVIDER_FAILURE


class ProviderHealthRepository:
    def __init__(self, db):
        self.db = db

    def get(self, target: RuntimeTarget) -> dict[str, Any] | None:
        with self.db.session() as session:
            row = session.get(ProviderHealthRow, target.composite_id)
            if row is None:
                return None
            if row.blocked_until and row.blocked_until <= utcnow() and row.state == ProviderHealthState.TRANSIENTLY_UNAVAILABLE.value:
                return None
            return {"target": RuntimeTarget.model_validate(json_loads(row.target_json)), "state": row.state,
                    "failure_category": row.failure_category, "reason": row.reason,
                    "blocked_until": row.blocked_until, "updated_at": row.updated_at}

    def record(self, target: RuntimeTarget, state: ProviderHealthState, *, category: ProviderFailureCategory | None = None,
               reason: str = "", blocked_until: datetime | None = None) -> dict[str, Any]:
        now = utcnow()
        with self.db.session() as session:
            row = session.get(ProviderHealthRow, target.composite_id)
            if row is None:
                row = ProviderHealthRow(target_id=target.composite_id, target_json=json_dumps(target.safe_projection()), state=state.value,
                                        failure_category=category.value if category else None, reason=reason[:512], blocked_until=blocked_until, updated_at=now)
                session.add(row)
            else:
                row.state = state.value; row.failure_category = category.value if category else row.failure_category
                row.reason = reason[:512]; row.blocked_until = blocked_until; row.updated_at = now
        return {"target": target.safe_projection(), "state": state.value, "failure_category": category.value if category else None,
                "reason": reason[:512], "blocked_until": blocked_until.isoformat() if blocked_until else None}


class RuntimeTargetController:
    """CAS-guarded switch/fallback controller scoped to one run/session identity."""

    def __init__(self, db):
        self.db = db
        self.events = EventStore(db)

    def bind(self, execution_id: str, configured_target: RuntimeTarget, *, run_id: str | None = None,
             session_id: str | None = None) -> dict[str, Any]:
        with self.db.session() as session:
            row = session.get(NativeRuntimeTargetRow, execution_id)
            if row is None:
                row = NativeRuntimeTargetRow(execution_id=execution_id, owner_run_id=run_id, owner_session_id=session_id,
                    state=TargetSwitchState.COMMITTED.value, configured_json=json_dumps(configured_target.safe_projection()),
                    effective_json=json_dumps(configured_target.safe_projection()), pending_json=None, fallback_reason=None,
                    switch_id=None, version=1, updated_at=utcnow())
                session.add(row)
            elif row.owner_run_id not in {None, run_id} or row.owner_session_id not in {None, session_id}:
                raise RuntimeTargetError("OWNERSHIP_CONFLICT", "runtime target binding belongs to another run/session")
        return self.current(execution_id)

    def current(self, execution_id: str) -> dict[str, Any]:
        with self.db.session() as session:
            row = session.get(NativeRuntimeTargetRow, execution_id)
            if row is None:
                raise RuntimeTargetError("RUNTIME_TARGET_NOT_BOUND", "execution has no durable runtime target")
            return {"execution_id": execution_id, "state": row.state,
                    "configured_target": RuntimeTarget.model_validate(json_loads(row.configured_json)),
                    "effective_target": RuntimeTarget.model_validate(json_loads(row.effective_json)),
                    "pending_target": RuntimeTarget.model_validate(json_loads(row.pending_json)) if row.pending_json else None,
                    "fallback_reason": row.fallback_reason, "version": row.version,
                    "owner_run_id": row.owner_run_id, "owner_session_id": row.owner_session_id, "switch_id": row.switch_id}

    def request_switch(self, execution_id: str, requested_target: RuntimeTarget, *, expected_current: RuntimeTarget,
                       confirm: Any = True, runtime_id: str | None = None, reason: str = "provider migration",
                       prepare: Any = None, install: Any = None, rollback: Any = None) -> TargetSwitch:
        current = self.current(execution_id)
        if runtime_id is not None and runtime_id != execution_id:
            raise RuntimeTargetError("OWNERSHIP_CONFLICT", "stale runtime identity cannot switch this execution")
        if current["effective_target"] != expected_current:
            raise RuntimeTargetError("OWNERSHIP_CONFLICT", "expected-current-target guard rejected switch")
        switch = TargetSwitch(execution_id=execution_id, state=TargetSwitchState.REQUESTED,
            previous_target=current["effective_target"], requested_target=requested_target, effective_target=current["effective_target"])
        self.events.append(EventType.RUNTIME_TARGET_SWITCH_REQUESTED, payload={"execution_id": execution_id, "switch_id": switch.id,
            "previous_target": switch.previous_target.safe_projection(), "requested_target": requested_target.safe_projection()})
        with self.db.session() as session:
            row = session.get(NativeRuntimeTargetRow, execution_id)
            if row is None or RuntimeTarget.model_validate(json_loads(row.effective_json)) != expected_current:
                raise RuntimeTargetError("OWNERSHIP_CONFLICT", "target changed concurrently")
            row.state = TargetSwitchState.PENDING.value; row.pending_json = json_dumps(requested_target.safe_projection()); row.switch_id = switch.id; row.version += 1; row.updated_at = utcnow()
        switch.state = TargetSwitchState.PENDING
        self.events.append(EventType.RUNTIME_TARGET_SWITCH_PENDING, payload={"execution_id": execution_id, "switch_id": switch.id})
        candidate = None
        try:
            candidate = prepare() if callable(prepare) else None
            if hasattr(candidate, "__await__"):
                raise RuntimeTargetError("ASYNC_PROVIDER_FACTORY_UNSUPPORTED", "provider factory must construct synchronously")
            if candidate is not None:
                actual = getattr(candidate, "runtime_target", None)
                if actual != requested_target:
                    raise RuntimeTargetError("RUNTIME_TARGET_PROVIDER_MISMATCH", "candidate provider target does not match requested target")
            confirmed = confirm() if callable(confirm) else bool(confirm)
            if hasattr(confirmed, "__await__"):
                raise RuntimeTargetError("ASYNC_CONFIRMATION_UNSUPPORTED", "switch confirmation must be completed before commit")
            if not confirmed:
                raise RuntimeTargetError("RUNTIME_CONFIRMATION_REJECTED", "runtime confirmation rejected")
            install_result = None
            if callable(install):
                install_result = install(candidate)
            if hasattr(install_result, "__await__"):
                close = getattr(install_result, "close", None)
                if callable(close):
                    close()
                raise RuntimeTargetError("ASYNC_PROVIDER_INSTALL_UNSUPPORTED", "provider install must complete synchronously")
        except Exception as exc:
            if callable(rollback) and candidate is not None:
                try:
                    rollback(candidate)
                except Exception:
                    pass
            switch.state = TargetSwitchState.FAILED
            code = f"{exc.code}: " if isinstance(exc, RuntimeTargetError) else ""
            switch.failure_reason = (code + str(exc))[:512]
            self._fail(execution_id, switch)
            return switch
        try:
            with self.db.session() as session:
                row = session.get(NativeRuntimeTargetRow, execution_id)
                if row is None or row.switch_id != switch.id:
                    raise RuntimeTargetError("OWNERSHIP_CONFLICT", "switch ownership changed before commit")
                row.state = TargetSwitchState.COMMITTED.value; row.effective_json = json_dumps(requested_target.safe_projection()); row.pending_json = None; row.version += 1; row.updated_at = utcnow()
        except Exception as exc:
            if callable(rollback) and candidate is not None:
                try:
                    rollback(candidate)
                except Exception:
                    pass
            switch.state = TargetSwitchState.FAILED
            code = f"{exc.code}: " if isinstance(exc, RuntimeTargetError) else ""
            switch.failure_reason = (code + str(exc))[:512]
            self._fail(execution_id, switch)
            return switch
        switch.state = TargetSwitchState.COMMITTED; switch.effective_target = requested_target; switch.updated_at = utcnow()
        self.events.append(EventType.RUNTIME_TARGET_SWITCH_COMMITTED, payload={"execution_id": execution_id, "switch_id": switch.id,
            "effective_target": requested_target.safe_projection()})
        return switch

    def _fail(self, execution_id: str, switch: TargetSwitch) -> None:
        with self.db.session() as session:
            row = session.get(NativeRuntimeTargetRow, execution_id)
            if row is not None:
                row.state = TargetSwitchState.FAILED.value; row.pending_json = None; row.switch_id = switch.id; row.version += 1; row.updated_at = utcnow()
        self.events.append(EventType.RUNTIME_TARGET_SWITCH_FAILED, payload={"execution_id": execution_id, "switch_id": switch.id,
            "reason": switch.failure_reason})

    def record_fallback(self, execution_id: str, effective_target: RuntimeTarget, *, reason: str) -> dict[str, Any]:
        current = self.current(execution_id)
        if current["configured_target"] == effective_target:
            raise RuntimeTargetError("FALLBACK_NOT_A_CHANGE", "fallback target must differ from configured target")
        fallback_id = new_id()
        with self.db.session() as session:
            row = session.get(NativeRuntimeTargetRow, execution_id)
            row.state = "FALLBACK"; row.effective_json = json_dumps(effective_target.safe_projection()); row.fallback_reason = reason[:512]; row.switch_id = fallback_id; row.version += 1; row.updated_at = utcnow()
        self.events.append(EventType.RUNTIME_TARGET_SWITCH_COMMITTED, payload={"execution_id": execution_id, "state": "FALLBACK",
            "event_identity": fallback_id, "configured_target": current["configured_target"].safe_projection(), "effective_target": effective_target.safe_projection(), "fallback_reason": reason[:512]})
        return self.current(execution_id)
