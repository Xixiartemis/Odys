"""Execution Control — per-call authorization and normalized tool-result semantics.

This module owns the shared base execution safety gate that is active in
EVERY experimental arm (A0..A5).  It is deliberately separate from the
non-interfering ``ShadowProgressObserver`` (which only observes) and from
the arm-specific Recovery Strategy / Recovery Budget components.

Components
──────────
- ``ToolExecStatus`` / ``ToolReadyStatus`` / ``Retryability`` — the three
  normalized result axes (Section: standardized result semantics).
- ``NormalizedToolResult`` — original result preserved verbatim plus the
  three axes, digests and evidence keys.
- ``ToolResultNormalizer`` — generic result adapter.  No hidden benchmark
  fault labels, no judge rules.
- ``ToolSemantics`` / ``ToolSemanticsRegistry`` — explicit tool metadata
  with a generic name-verb fallback.  Dependency/readiness semantics are
  expressed here, never hardcoded as canary-specific fault handling.
- ``ExecutionAuthorizationGate`` — the deterministic per-call
  authorization authority.  Records control events (proposed, authorized,
  executed, observed, blocked, recovery, escalation) and holds control
  state that persists across model turns.

Authorization rules (deterministic, in evaluation order)
────────────────────────────────────────────────────────
R1  DEPENDENCY_PRECONDITION_UNRESOLVED
    Dependent calls (commit-class tools or tools whose metadata declares
    ``requires_readiness``) are blocked while a readiness condition is
    unresolved (``*_ready=false`` → NOT_READY; declared-but-undetermined →
    UNKNOWN-affecting), or while the current release batch has just
    produced a non-success (FAILED / UNKNOWN) observation.

R2  NO_PROGRESS_RETRY
    An identical repeat call (same tool + same canonical argument digest)
    with no new evidence, no argument change and no observable state
    change, and no explicit transient retry basis, is rejected and the
    gate enters an explicit stop/escalation state.  A single bounded
    verification retry after a non-NOT_RETRYABLE outcome is allowed —
    only the observation after it can establish "identical result".

R3  SIDE_EFFECT_REPEAT_NOT_IDEMPOTENT
    A repeat of an already-successful side-effect call with identical
    arguments is blocked unless the tool is declared idempotent or the
    prior execution receipt explicitly grants an idempotent repeat.

``is_retry_justified()`` (R2) and the recovery budget gate are TWO
INDEPENDENT judgments: retry reasonableness is decided here, budget
sufficiency is decided by ``recovery_budget.RecoveryBudgetGate``.

This module imports NOTHING from the frozen ToolMaze benchmark.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple


# ── Normalized result axes ───────────────────────────────────────────

class ToolExecStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class ToolReadyStatus(str, Enum):
    READY = "READY"
    NOT_READY = "NOT_READY"
    UNKNOWN = "UNKNOWN"
    NO_EVIDENCE = "NO_EVIDENCE"


class Retryability(str, Enum):
    RETRYABLE = "RETRYABLE"
    NOT_RETRYABLE = "NOT_RETRYABLE"
    UNKNOWN = "UNKNOWN"


class ControlEventKind(str, Enum):
    PROPOSED = "PROPOSED"
    AUTHORIZED = "AUTHORIZED"
    EXECUTED = "EXECUTED"
    OBSERVED = "OBSERVED"
    BLOCKED = "BLOCKED"
    RECOVERY = "RECOVERY"
    ESCALATION = "ESCALATION"


# ── Generic status vocabularies (tool metadata may extend) ───────────

_SUCCESS_STATUS = {"success", "ok", "okay", "completed", "complete", "succeeded", "confirmed", "done"}
_FAILURE_STATUS = {"error", "failure", "failed", "fail", "exception", "rejected", "denied", "invalid"}
_TRANSIENT_STATUS = {
    "transient", "transient_error", "timeout", "timed_out", "rate_limited",
    "throttled", "busy", "unavailable", "temporarily_unavailable", "retryable",
}
_PERMANENT_ERROR_TYPES = {
    "permanent", "fatal", "invalid", "not_found", "forbidden", "unauthorized",
    "validation", "bad_request", "conflict", "not_retryable",
}
_TRANSIENT_ERROR_TYPES = {
    "transient", "timeout", "rate_limited", "throttled", "busy",
    "unavailable", "connection_error", "temporarily_unavailable",
}

# Confirmation receipts never grant readiness (e.g. status=confirmed).
_CONFIRMATION_STATUS = {"confirmed", "confirmation", "accepted_receipt"}


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:16]


def _canonical_arguments(arguments: Any) -> Dict[str, Any]:
    if isinstance(arguments, dict):
        return dict(arguments)
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            return {"_raw_arguments": arguments}
    return {"_raw_arguments": str(arguments)}


# ── Tool semantics (explicit metadata + generic verb heuristics) ─────

# Commit-class verbs: these commit side effects and are dependent on
# readiness evidence.  Generic industry vocabulary — not benchmark labels.
COMMIT_VERBS = frozenset({
    "confirm", "submit", "execute", "commit", "pay", "purchase", "order",
    "place", "book", "reserve", "checkout", "finalize", "send", "dispatch",
    "transmit", "apply", "enroll", "register", "approve", "authorize",
})

# Write-class verbs: side effects that do not commit an external order.
WRITE_VERBS = frozenset({
    "create", "hold", "update", "delete", "cancel", "add", "modify", "write",
    "insert", "remove", "set", "upload", "rename", "append", "put", "move",
    "copy", "store", "save", "release", "allocate", "assign", "clear",
})


@dataclass(frozen=True)
class ToolSemantics:
    """Declarative per-tool semantics used by the authorization gate."""

    name: str
    read_only: bool = True
    side_effect: bool = False
    commit: bool = False
    idempotent: bool = True
    provides_readiness: bool = True
    readiness_keys: Tuple[str, ...] = ()
    requires_readiness: Tuple[str, ...] = ()

    @property
    def dependent(self) -> bool:
        """A dependent call needs readiness evidence before execution."""
        return self.commit or bool(self.requires_readiness)


class ToolSemanticsRegistry:
    """Tool semantics from explicit metadata with a generic verb fallback.

    Explicit metadata sources (in priority order):
    1. ``odys_semantics`` mapping on the tool definition.
    2. ``annotations`` mapping (``readOnlyHint`` / ``idempotentHint``).

    Fallback: first-token verb heuristics over generic vocabularies.
    Unknown tools default to read-only so the gate never invents
    dependencies it has no evidence for.
    """

    def __init__(self) -> None:
        self._semantics: Dict[str, ToolSemantics] = {}

    def register(self, semantics: ToolSemantics) -> None:
        self._semantics[semantics.name] = semantics

    def register_from_definitions(self, tool_definitions: Sequence[Any]) -> None:
        for definition in tool_definitions or []:
            name = _definition_name(definition)
            if not name:
                continue
            self._semantics[name] = self._semantics.get(name) or self._infer(name, definition)

    def get(self, tool_name: str) -> ToolSemantics:
        if tool_name in self._semantics:
            return self._semantics[tool_name]
        inferred = self._infer(tool_name, None)
        self._semantics[tool_name] = inferred
        return inferred

    def _infer(self, name: str, definition: Any) -> ToolSemantics:
        explicit: Dict[str, Any] = {}
        if isinstance(definition, dict):
            raw = definition.get("odys_semantics")
            if isinstance(raw, dict):
                explicit = raw
        verb = name.split("_", 1)[0].lower()
        if explicit:
            return ToolSemantics(
                name=name,
                read_only=bool(explicit.get("read_only", verb not in WRITE_VERBS and verb not in COMMIT_VERBS)),
                side_effect=bool(explicit.get("side_effect", verb in WRITE_VERBS or verb in COMMIT_VERBS)),
                commit=bool(explicit.get("commit", verb in COMMIT_VERBS)),
                idempotent=bool(explicit.get("idempotent", verb not in WRITE_VERBS and verb not in COMMIT_VERBS)),
                provides_readiness=bool(explicit.get("provides_readiness", verb not in COMMIT_VERBS)),
                readiness_keys=tuple(explicit.get("readiness_keys", ())),
                requires_readiness=tuple(explicit.get("requires_readiness", ())),
            )
        annotations: Dict[str, Any] = {}
        if isinstance(definition, dict) and isinstance(definition.get("annotations"), dict):
            annotations = definition["annotations"]
        commit = verb in COMMIT_VERBS
        side_effect = commit or verb in WRITE_VERBS
        read_only = not side_effect
        if "readOnlyHint" in annotations:
            read_only = bool(annotations["readOnlyHint"])
            side_effect = not read_only
        idempotent = not side_effect
        if "idempotentHint" in annotations:
            idempotent = bool(annotations["idempotentHint"])
        return ToolSemantics(
            name=name,
            read_only=read_only,
            side_effect=side_effect,
            commit=commit,
            idempotent=idempotent,
            provides_readiness=not commit,
            readiness_keys=(),
            requires_readiness=(),
        )


def _definition_name(definition: Any) -> Optional[str]:
    if isinstance(definition, dict):
        name = definition.get("name")
        if isinstance(name, str) and name:
            return name
        try:
            spec = definition["paradigms"]["function_call"]["spec"]
            name = spec.get("name")
            if isinstance(name, str) and name:
                return name
        except (KeyError, TypeError):
            pass
    return getattr(definition, "name", None)


# ── Normalized result ────────────────────────────────────────────────

@dataclass(frozen=True)
class NormalizedToolResult:
    """Original tool result preserved verbatim plus normalized semantics."""

    raw: Dict[str, Any]
    exec_status: ToolExecStatus
    ready_status: ToolReadyStatus
    retryable: Retryability
    readiness_evidence: Tuple[str, ...] = ()
    readiness_values: Dict[str, Any] = field(default_factory=dict)
    confirmation_receipt: bool = False
    transient_basis: bool = False
    arguments_digest: str = ""
    result_digest: str = ""
    status_source: str = "none"

    @property
    def is_success(self) -> bool:
        return self.exec_status is ToolExecStatus.SUCCEEDED

    @property
    def affects_followups(self) -> bool:
        """True when this observation must gate dependent follow-ups.

        - READY evidence never gates (preconditions are satisfied).
        - FAILED gates.  NOT_READY / declared-but-undetermined readiness
          gates.  An UNKNOWN outcome with no readiness proof also gates —
          missing evidence is never defaulted to success.
        """
        if self.ready_status is ToolReadyStatus.READY:
            return False
        if self.exec_status is ToolExecStatus.FAILED:
            return True
        if self.ready_status in (ToolReadyStatus.NOT_READY, ToolReadyStatus.UNKNOWN):
            return True
        return self.exec_status is ToolExecStatus.UNKNOWN


class ToolResultNormalizer:
    """Generic tool-result adapter — no hidden fault labels, no judge rules.

    Axes:
    - exec: explicit error/failure markers → FAILED; explicit success
      markers (including ``status=confirmed``) → SUCCEEDED; otherwise
      UNKNOWN.  Missing evidence is NEVER defaulted to success.
    - ready: explicit readiness keys (``ready`` / ``*_ready`` or
      metadata-declared) — any False → NOT_READY; all True → READY;
      declared but undetermined → UNKNOWN (affects follow-ups); absent →
      NO_EVIDENCE.  Confirmation receipts provide no readiness evidence.
    - retryable: explicit ``retryable`` / ``transient`` flags and
      transient/permanent error types; otherwise UNKNOWN.
    """

    def __init__(self, registry: Optional[ToolSemanticsRegistry] = None):
        self._registry = registry or ToolSemanticsRegistry()

    def normalize(
        self,
        tool_name: str,
        result: Any,
        *,
        arguments: Any = None,
    ) -> NormalizedToolResult:
        semantics = self._registry.get(tool_name)
        raw: Dict[str, Any] = result if isinstance(result, dict) else {"raw": str(result)}

        exec_status, status_source = self._exec_status(raw)
        ready_status, evidence, values = self._ready_status(raw, semantics)
        retryable, transient_basis = self._retryability(raw, exec_status)

        arguments_digest = _digest(_canonical_arguments(arguments))
        return NormalizedToolResult(
            raw=raw,
            exec_status=exec_status,
            ready_status=ready_status,
            retryable=retryable,
            readiness_evidence=tuple(evidence),
            readiness_values=dict(values),
            confirmation_receipt=str(raw.get("status", "")).lower() in _CONFIRMATION_STATUS,
            transient_basis=transient_basis,
            arguments_digest=arguments_digest,
            result_digest=_digest(raw),
            status_source=status_source,
        )

    # ── axis helpers ─────────────────────────────────────────────────

    @staticmethod
    def _exec_status(raw: Dict[str, Any]) -> Tuple[ToolExecStatus, str]:
        status = str(raw.get("status", "")).strip().lower()
        if raw.get("error"):
            return ToolExecStatus.FAILED, "error_key"
        if status in _FAILURE_STATUS:
            return ToolExecStatus.FAILED, f"status:{status or 'unset'}"
        if status in _TRANSIENT_STATUS:
            return ToolExecStatus.FAILED, f"status:{status}"
        if status in _SUCCESS_STATUS:
            return ToolExecStatus.SUCCEEDED, f"status:{status}"
        if raw.get("success") is True:
            return ToolExecStatus.SUCCEEDED, "success_key"
        if raw.get("success") is False:
            return ToolExecStatus.FAILED, "success_key"
        return ToolExecStatus.UNKNOWN, "none"

    @staticmethod
    def _ready_status(
        raw: Dict[str, Any],
        semantics: ToolSemantics,
    ) -> Tuple[ToolReadyStatus, List[str], Dict[str, Any]]:
        keys: List[str] = list(semantics.readiness_keys)
        if not keys:
            keys = [
                key for key in raw
                if key == "ready" or (key.endswith("_ready") and isinstance(raw[key], (bool, type(None))))
            ]
        if not keys:
            return ToolReadyStatus.NO_EVIDENCE, [], {}
        values = {key: raw.get(key) for key in keys}
        if not semantics.provides_readiness:
            # Confirmation-class receipts cannot satisfy preconditions.
            return ToolReadyStatus.NO_EVIDENCE, keys, values
        concrete = [raw.get(key) for key in keys]
        if any(value is False for value in concrete):
            return ToolReadyStatus.NOT_READY, keys, values
        if any(value is None for value in concrete):
            return ToolReadyStatus.UNKNOWN, keys, values
        if all(value is True for value in concrete):
            return ToolReadyStatus.READY, keys, values
        return ToolReadyStatus.UNKNOWN, keys, values

    @staticmethod
    def _retryability(
        raw: Dict[str, Any],
        exec_status: ToolExecStatus,
    ) -> Tuple[Retryability, bool]:
        if exec_status is ToolExecStatus.SUCCEEDED:
            return Retryability.NOT_RETRYABLE, False
        if raw.get("retryable") is True or raw.get("transient") is True:
            return Retryability.RETRYABLE, True
        if raw.get("retryable") is False or raw.get("transient") is False:
            return Retryability.NOT_RETRYABLE, False
        status = str(raw.get("status", "")).strip().lower()
        if status in _TRANSIENT_STATUS:
            return Retryability.RETRYABLE, True
        error = raw.get("error")
        error_type = ""
        if isinstance(error, dict):
            error_type = str(error.get("type", "") or error.get("error_type", "")).strip().lower()
        elif isinstance(error, str):
            error_type = error.strip().lower()
        error_type = error_type or str(raw.get("error_type", "")).strip().lower()
        if error_type in _TRANSIENT_ERROR_TYPES:
            return Retryability.RETRYABLE, True
        if error_type in _PERMANENT_ERROR_TYPES:
            return Retryability.NOT_RETRYABLE, False
        return Retryability.UNKNOWN, False


# ── Authorization gate ───────────────────────────────────────────────

@dataclass(frozen=True)
class AuthorizationDecision:
    authorized: bool
    rule: str
    reason: str
    terminal: bool = False


@dataclass
class _AttemptRecord:
    step: int
    arguments_digest: str
    result_digest: str
    state_digest: str
    exec_status: str
    ready_status: str
    retryable: str
    receipt_grants_repeat: bool
    normalized: NormalizedToolResult


class ExecutionAuthorizationGate:
    """Deterministic per-call execution authorization authority.

    Shared base safety gate — identical in every arm.  Arm-specific
    recovery strategy and recovery budget remain independently wired.
    Control state persists across model turns for the whole trial.
    """

    def __init__(self, *, registry: Optional[ToolSemanticsRegistry] = None):
        self._registry = registry or ToolSemanticsRegistry()
        self._normalizer = ToolResultNormalizer(self._registry)
        self._events: List[Dict[str, Any]] = []
        self._attempts: Dict[str, List[_AttemptRecord]] = {}
        self._conditions: Dict[str, str] = {}
        self._batch_unsafe: bool = False
        self._terminal: bool = False
        self._terminal_reason: str = ""
        self._event_sequence: int = 0

    # ── lifecycle ────────────────────────────────────────────────────

    def reset(self) -> None:
        self._events.clear()
        self._attempts.clear()
        self._conditions.clear()
        self._batch_unsafe = False
        self._terminal = False
        self._terminal_reason = ""
        self._event_sequence = 0

    def bind_tool_definitions(self, tool_definitions: Sequence[Any]) -> None:
        self._registry.register_from_definitions(tool_definitions or [])

    def begin_batch(self) -> None:
        """Start releasing a fresh model-proposed batch."""
        self._batch_unsafe = False

    # ── observation (record first, judge after) ──────────────────────

    def observe(
        self,
        *,
        step: int,
        tool_name: str,
        arguments: Any,
        result: Any,
        tool_call_id: Optional[str] = None,
        provider_tool_call_id: Optional[str] = None,
    ) -> NormalizedToolResult:
        """Record an executed tool result and update control state."""
        normalized = self._normalizer.normalize(tool_name, result, arguments=arguments)
        self._update_conditions(normalized)
        if normalized.affects_followups:
            self._batch_unsafe = True

        key = self._attempt_key(tool_name, normalized.arguments_digest)
        prior = self._attempts.get(key, [])
        self._attempts.setdefault(key, []).append(_AttemptRecord(
            step=step,
            arguments_digest=normalized.arguments_digest,
            result_digest=normalized.result_digest,
            state_digest=self._state_digest(),
            exec_status=normalized.exec_status.value,
            ready_status=normalized.ready_status.value,
            retryable=normalized.retryable.value,
            receipt_grants_repeat=self._receipt_grants_repeat(normalized),
            normalized=normalized,
        ))
        del prior

        self.record_event(
            ControlEventKind.EXECUTED,
            step=step,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            provider_tool_call_id=provider_tool_call_id,
            arguments_digest=normalized.arguments_digest,
        )
        self.record_event(
            ControlEventKind.OBSERVED,
            step=step,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            provider_tool_call_id=provider_tool_call_id,
            exec_status=normalized.exec_status.value,
            ready_status=normalized.ready_status.value,
            retryable=normalized.retryable.value,
            result_digest=normalized.result_digest,
            arguments_digest=normalized.arguments_digest,
            state_digest=self._state_digest(),
            confirmation_receipt=normalized.confirmation_receipt,
            readiness_evidence=list(normalized.readiness_evidence),
            status_source=normalized.status_source,
        )
        return normalized

    # ── authorization (per call, before execution) ───────────────────

    def propose(
        self,
        *,
        step: int,
        tool_name: str,
        arguments: Any,
        tool_call_id: Optional[str] = None,
        provider_tool_call_id: Optional[str] = None,
        origin: str = "model_batch",
        batch_index: int = 0,
    ) -> AuthorizationDecision:
        """Decide whether a single proposed tool call may execute."""
        semantics = self._registry.get(tool_name)
        canonical_args = _canonical_arguments(arguments)
        arguments_digest = _digest(canonical_args)

        self.record_event(
            ControlEventKind.PROPOSED,
            step=step,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            provider_tool_call_id=provider_tool_call_id,
            origin=origin,
            batch_index=batch_index,
            arguments_digest=arguments_digest,
        )

        decision = self._authorize(
            step=step,
            tool_name=tool_name,
            semantics=semantics,
            arguments_digest=arguments_digest,
            origin=origin,
        )
        kind = ControlEventKind.AUTHORIZED if decision.authorized else ControlEventKind.BLOCKED
        self.record_event(
            kind,
            step=step,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            provider_tool_call_id=provider_tool_call_id,
            origin=origin,
            batch_index=batch_index,
            rule=decision.rule,
            reason=decision.reason,
            terminal=decision.terminal,
        )
        return decision

    def _authorize(
        self,
        *,
        step: int,
        tool_name: str,
        semantics: ToolSemantics,
        arguments_digest: str,
        origin: str,
    ) -> AuthorizationDecision:
        # R0 — explicit terminal control state (no-progress stop).
        if self._terminal:
            return AuthorizationDecision(
                False,
                "TERMINAL_CONTROL_STATE",
                f"control gate already stopped: {self._terminal_reason}",
                terminal=True,
            )

        # R1 — dependency preconditions unresolved.
        if semantics.dependent:
            if self._conditions:
                keys = sorted(self._conditions)
                return AuthorizationDecision(
                    False,
                    "DEPENDENCY_PRECONDITION_UNRESOLVED",
                    f"unresolved readiness conditions {keys} block dependent call",
                )
            if self._batch_unsafe and origin == "model_batch":
                return AuthorizationDecision(
                    False,
                    "DEPENDENCY_PRECONDITION_UNRESOLVED",
                    "a preceding call in this batch failed or returned not-ready/unknown",
                )

        # R2 / R3 — repeat-call analysis.
        repeat = self._evaluate_repeat(
            step=step,
            tool_name=tool_name,
            semantics=semantics,
            arguments_digest=arguments_digest,
            origin=origin,
        )
        if repeat is not None:
            return repeat

        return AuthorizationDecision(True, "ALLOW", "authorized by execution control gate")

    def _evaluate_repeat(
        self,
        *,
        step: int,
        tool_name: str,
        semantics: ToolSemantics,
        arguments_digest: str,
        origin: str,
    ) -> Optional[AuthorizationDecision]:
        key = self._attempt_key(tool_name, arguments_digest)
        attempts = self._attempts.get(key)
        if not attempts:
            return None
        last = attempts[-1]

        # R2 — no-progress retry: identical results + no state change and
        # no explicit transient basis → reject and stop/escalate.
        if last.exec_status == ToolExecStatus.FAILED.value and last.retryable == Retryability.NOT_RETRYABLE.value:
            return AuthorizationDecision(
                False,
                "NO_PROGRESS_RETRY",
                "previous identical call failed NOT_RETRYABLE — no-progress retry rejected",
                terminal=True,
            )
        if (
            len(attempts) >= 2
            and attempts[-1].result_digest == attempts[-2].result_digest
            and attempts[-1].state_digest == attempts[-2].state_digest
            and not attempts[-1].normalized.transient_basis
        ):
            return AuthorizationDecision(
                False,
                "NO_PROGRESS_RETRY",
                "identical arguments produced identical results with no state change — "
                "no-progress retry rejected",
                terminal=True,
            )

        # R3 — successful side-effect repeat without idempotency evidence.
        if (
            semantics.side_effect
            and last.exec_status == ToolExecStatus.SUCCEEDED.value
            and not semantics.idempotent
            and not last.receipt_grants_repeat
        ):
            return AuthorizationDecision(
                False,
                "SIDE_EFFECT_REPEAT_NOT_IDEMPOTENT",
                "identical side-effect call already succeeded and the tool is not "
                "declared idempotent — repeat blocked",
            )

        # Single bounded verification retry after a non-NOT_RETRYABLE outcome.
        return None

    # ── retry justification (independent of recovery budget) ─────────

    def is_retry_justified(
        self,
        *,
        step: int,
        tool_name: str,
        arguments: Any,
    ) -> AuthorizationDecision:
        """Judge whether a same-call retry is reasonable (P1).

        This is deliberately SEPARATE from recovery budget sufficiency:
        this gate answers "would a retry make progress?", the recovery
        budget gate answers "is there intervention budget left?".
        """
        semantics = self._registry.get(tool_name)
        arguments_digest = _digest(_canonical_arguments(arguments))
        if self._terminal:
            return AuthorizationDecision(
                False, "NO_PROGRESS_RETRY",
                f"control gate already stopped: {self._terminal_reason}",
                terminal=True,
            )
        key = self._attempt_key(tool_name, arguments_digest)
        attempts = self._attempts.get(key)
        if not attempts:
            return AuthorizationDecision(True, "RETRY_JUSTIFIED", "no prior identical attempt")
        last = attempts[-1]
        if last.normalized.transient_basis:
            return AuthorizationDecision(
                True, "RETRY_JUSTIFIED", "explicit transient retry basis observed",
            )
        if last.exec_status == ToolExecStatus.FAILED.value and last.retryable == Retryability.NOT_RETRYABLE.value:
            return AuthorizationDecision(
                False, "NO_PROGRESS_RETRY",
                "previous identical call failed NOT_RETRYABLE",
                terminal=True,
            )
        if (
            len(attempts) >= 2
            and attempts[-1].result_digest == attempts[-2].result_digest
            and attempts[-1].state_digest == attempts[-2].state_digest
        ):
            return AuthorizationDecision(
                False, "NO_PROGRESS_RETRY",
                "identical arguments produced identical results with no state change",
                terminal=True,
            )
        # One bounded verification retry is allowed after a non-identical
        # or single non-transient outcome.
        return AuthorizationDecision(
            True, "RETRY_JUSTIFIED", "single bounded verification retry permitted",
        )

    # ── control-event sink (recovery / escalation from the core) ─────

    def record_event(self, kind: ControlEventKind, **fields: Any) -> Dict[str, Any]:
        self._event_sequence += 1
        event: Dict[str, Any] = {
            "event_id": self._event_sequence,
            "kind": kind.value if isinstance(kind, ControlEventKind) else str(kind),
        }
        for key, value in fields.items():
            if value is not None:
                event[key] = value
        self._events.append(event)
        return event

    def mark_terminal(self, *, step: int, reason: str) -> None:
        self._terminal = True
        self._terminal_reason = reason
        self.record_event(
            ControlEventKind.ESCALATION,
            step=step,
            reason=reason,
            rule="NO_PROGRESS_STOP_OR_ESCALATE",
        )

    # ── introspection ────────────────────────────────────────────────

    @property
    def control_events(self) -> List[Dict[str, Any]]:
        return list(self._events)

    @property
    def conditions(self) -> Dict[str, str]:
        return dict(self._conditions)

    @property
    def is_terminal(self) -> bool:
        return self._terminal

    def attempt_history(self, tool_name: str, arguments: Any) -> List[Dict[str, Any]]:
        key = self._attempt_key(tool_name, _digest(_canonical_arguments(arguments)))
        return [
            {
                "step": record.step,
                "result_digest": record.result_digest,
                "state_digest": record.state_digest,
                "exec_status": record.exec_status,
                "ready_status": record.ready_status,
                "retryable": record.retryable,
            }
            for record in self._attempts.get(key, [])
        ]

    # ── internals ────────────────────────────────────────────────────

    @staticmethod
    def _attempt_key(tool_name: str, arguments_digest: str) -> str:
        return f"{tool_name}::{arguments_digest}"

    def _state_digest(self) -> str:
        return _digest({
            "conditions": sorted(self._conditions.items()),
            "batch_unsafe": self._batch_unsafe,
        })

    def _update_conditions(self, normalized: NormalizedToolResult) -> None:
        semantics = None  # readiness keys already resolved by the normalizer
        del semantics
        if not normalized.readiness_evidence:
            return
        if normalized.ready_status is ToolReadyStatus.NO_EVIDENCE:
            return
        if not normalized.readiness_values and normalized.ready_status is ToolReadyStatus.NO_EVIDENCE:
            return
        for key, value in normalized.readiness_values.items():
            if value is True:
                self._conditions.pop(key, None)
            elif value is False:
                self._conditions[key] = "NOT_READY"
            else:
                self._conditions.setdefault(key, "DECLARED_UNKNOWN")

    @staticmethod
    def _receipt_grants_repeat(normalized: NormalizedToolResult) -> bool:
        raw = normalized.raw
        if raw.get("idempotent") is True:
            return True
        if "idempotency_key" in raw:
            return True
        return False
