"""Odys-owned recovery control-plane primitives.

This module deliberately sits below benchmark adapters and above the native
turn loop.  It observes bounded execution evidence, emits durable escalation
signals, and delegates provider-call accounting to one caller-supplied root
budget authority.  It does not validate completion and it does not contain
task- or benchmark-specific truth rules.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Any


TYPED_ESCALATION_REASONS = frozenset(
    {
        "REPAIR_NO_PROGRESS",
        "REPAIR_REPEATED_ACTION",
        "REPAIR_STATE_OSCILLATION",
        "LOCAL_REPAIR_BUDGET_EXHAUSTED",
        "REPEATED_VALIDATOR_REJECTION",
    }
)


def _digest(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    except (TypeError, ValueError):
        encoded = repr(value).encode("utf-8", "replace")
    return hashlib.sha256(encoded[:16_384]).hexdigest()


def _matches(expected: Any, observed: Any) -> bool:
    if isinstance(expected, Mapping):
        return isinstance(observed, Mapping) and all(
            key in observed and _matches(value, observed[key])
            for key, value in expected.items()
        )
    if isinstance(expected, str):
        normalized = expected.casefold()
        if normalized in {"present", "pass", "passed", "successful", "true"}:
            return observed is True or str(observed).casefold() in {
                normalized,
                "true",
                "pass",
                "passed",
                "present",
                "successful",
            }
        if normalized in {"not present", "false", "failed"}:
            return observed is False or str(observed).casefold() in {
                "false",
                "failed",
                "not present",
            }
    return expected == observed


class ProgressStatus(str, Enum):
    SATISFIED = "SATISFIED"
    IMPROVED = "IMPROVED"
    CHANGED_UNKNOWN = "CHANGED_UNKNOWN"
    NO_PROGRESS = "NO_PROGRESS"
    REPEATED = "REPEATED"
    OSCILLATING = "OSCILLATING"
    REGRESSED = "REGRESSED"


class RecoveryDecision(str, Enum):
    CONTINUE_LOCAL_REPAIR = "CONTINUE_LOCAL_REPAIR"
    VALIDATE_CANDIDATE = "VALIDATE_CANDIDATE"
    STOP_NO_PROGRESS = "STOP_NO_PROGRESS"
    STOP_REPEATED_ACTION = "STOP_REPEATED_ACTION"
    STOP_OSCILLATION = "STOP_OSCILLATION"
    ESCALATE_MACRO_REPLAN = "ESCALATE_MACRO_REPLAN"
    TERMINAL_BUDGET_FAILURE = "TERMINAL_BUDGET_FAILURE"


@dataclass(frozen=True)
class EffectProgress:
    status: ProgressStatus
    state_digest: str
    action_fingerprint: str
    matched_effect_keys: tuple[str, ...] = ()
    evidence: dict[str, Any] | None = None

    @property
    def candidate_for_validation(self) -> bool:
        return self.status is ProgressStatus.SATISFIED


class EffectProgressEvaluator:
    """Compare bounded observable state without becoming a validator.

    A full expected-effect match only produces a validation *candidate*.
    Acceptance and VERIFIED remain exclusively owned by the authoritative
    validator.  When no effect predicate is available, this evaluator falls
    back to state/action/repetition observations and never invents a distance
    metric.
    """

    def __init__(self, expected_effects: Mapping[str, Any] | None = None):
        self.expected_effects = dict(expected_effects or {})
        self.seen_state_digests: list[str] = []
        self.seen_action_fingerprints: set[str] = set()
        self.last_state_digest: str | None = None
        self.last_action_fingerprint: str | None = None
        self.last_state_projection: Any = None
        self.observation_count = 0

    def observe(
        self,
        *,
        before_state: Any,
        after_state: Any,
        action: Mapping[str, Any] | None = None,
        observation: Mapping[str, Any] | None = None,
    ) -> EffectProgress:
        observation = dict(observation or {})
        if before_state is None:
            before_state = self.last_state_projection
        after = after_state if after_state is not None else observation
        state_digest = _digest(after)
        before_digest = _digest(before_state) if before_state is not None else None
        action_fingerprint = _digest(dict(action or {}))
        matched = tuple(
            key
            for key, expected in self.expected_effects.items()
            if _matches(expected, self._lookup(observation, after, key))
        )
        repeated_state = state_digest in self.seen_state_digests
        repeated_action = action_fingerprint in self.seen_action_fingerprints
        oscillating = (
            len(self.seen_state_digests) >= 2
            and state_digest == self.seen_state_digests[-2]
            and state_digest != self.seen_state_digests[-1]
        )
        unchanged = before_digest is not None and before_digest == state_digest

        if self.expected_effects and len(matched) == len(self.expected_effects):
            status = ProgressStatus.SATISFIED
        elif oscillating:
            status = ProgressStatus.OSCILLATING
        elif repeated_action or repeated_state or unchanged:
            status = ProgressStatus.REPEATED if repeated_action else ProgressStatus.NO_PROGRESS
        elif matched:
            status = ProgressStatus.IMPROVED
        else:
            status = ProgressStatus.CHANGED_UNKNOWN

        self.observation_count += 1
        self.seen_state_digests.append(state_digest)
        self.seen_state_digests = self.seen_state_digests[-64:]
        self.seen_action_fingerprints.add(action_fingerprint)
        self.last_state_digest = state_digest
        self.last_action_fingerprint = action_fingerprint
        self.last_state_projection = after
        return EffectProgress(
            status=status,
            state_digest=state_digest,
            action_fingerprint=action_fingerprint,
            matched_effect_keys=matched,
            evidence={
                "before_state_digest": before_digest,
                "after_state_digest": state_digest,
                "repeated_state": repeated_state,
                "repeated_action": repeated_action,
                "oscillating": oscillating,
                "matched_effect_keys": list(matched),
            },
        )

    @staticmethod
    def _lookup(observation: Mapping[str, Any], after: Any, key: str) -> Any:
        for container in (observation, after):
            if isinstance(container, Mapping) and key in container:
                return container[key]
            if isinstance(container, Mapping):
                for nested_key in ("bounded_output", "safe_summary", "result_summary"):
                    nested = container.get(nested_key)
                    if isinstance(nested, Mapping) and key in nested:
                        return nested[key]
        return None


class BudgetReservationError(RuntimeError):
    """Raised when a root-authority-backed recovery lease cannot be granted."""


class RecoveryBudgetManager:
    """Lease view over a single caller-owned root budget authority.

    The manager stores only protected capacity.  Actual provider-call
    consumption is delegated to ``root_authority.reserve``; it never keeps a
    second provider-call counter.  Callers must acquire all calls through this
    object once reservations are installed.
    """

    PHASES = ("local_repair", "macro_replan", "post_replan", "validation")

    def __init__(self, root_authority: Any):
        if not callable(getattr(root_authority, "reserve", None)):
            raise TypeError("root budget authority must expose reserve(phase)")
        if not isinstance(getattr(root_authority, "remaining_provider_calls", None), (int, float)):
            raise TypeError("root budget authority must expose remaining_provider_calls")
        self.root_authority = root_authority
        self._reserved = {phase: 0 for phase in self.PHASES}

    def reserve_capacity(self, phase: str, capacity: int) -> int:
        phase = self._normalize_phase(phase)
        capacity = int(capacity)
        if capacity < 0:
            raise ValueError("reservation capacity must be non-negative")
        available = int(self.root_authority.remaining_provider_calls)
        protected_elsewhere = sum(self._reserved.values())
        if capacity > available - protected_elsewhere:
            raise BudgetReservationError("BUDGET_RESERVATION_UNAVAILABLE")
        self._reserved[phase] += capacity
        return self._reserved[phase]

    def acquire(self, phase: str) -> None:
        phase = self._normalize_phase(phase)
        if self._reserved[phase] <= 0:
            raise BudgetReservationError(f"{phase.upper()}_RESERVE_EXHAUSTED")
        try:
            self.root_authority.reserve(phase)
        except Exception as exc:
            raise BudgetReservationError("ROOT_BUDGET_AUTHORITY_REJECTED") from exc
        self._reserved[phase] -= 1

    def has_capacity(self, phase: str) -> bool:
        return self._reserved[self._normalize_phase(phase)] > 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "root_remaining_provider_calls": int(self.root_authority.remaining_provider_calls),
            "reserved": dict(self._reserved),
            "root_budget_single_authority": True,
        }

    def _normalize_phase(self, phase: str) -> str:
        normalized = str(phase).casefold().replace("-", "_")
        aliases = {"repair": "local_repair", "replan": "macro_replan"}
        normalized = aliases.get(normalized, normalized)
        if normalized not in self.PHASES:
            raise ValueError(f"unsupported recovery budget phase: {phase}")
        return normalized


class RecoveryContextProjector:
    """Project compact repair context while durable history remains complete."""

    def project(
        self,
        *,
        goal: str,
        acceptance_contract: Any,
        current_state: Any,
        failure_provenance: Any,
        progress: Mapping[str, Any] | None = None,
        attempted_actions: list[Any] | None = None,
        last_useful_observation: Any = None,
        current_mismatch: Any = None,
        budget: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        state_digest = _digest(current_state)
        return {
            "current_goal": str(goal)[:4_000],
            "acceptance_contract": self._bounded(acceptance_contract, 4_000),
            "current_observable_state": {
                "digest": state_digest,
                "projection": self._bounded(current_state, 4_000),
            },
            "failure_provenance": self._bounded(failure_provenance, 4_000),
            "progress_summary": self._bounded(progress or {}, 2_000),
            "attempted_action_fingerprints": self._bounded(attempted_actions or [], 2_000),
            "last_useful_observation": self._bounded(last_useful_observation, 2_000),
            "current_mismatch": self._bounded(current_mismatch, 2_000),
            "remaining_budget_reservations": self._bounded(budget or {}, 2_000),
        }

    @staticmethod
    def _bounded(value: Any, limit: int) -> Any:
        if value is None:
            return None
        try:
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            encoded = str(value)
        if len(encoded.encode("utf-8")) <= limit:
            return value
        return encoded.encode("utf-8")[:limit].decode("utf-8", "ignore")


class RecoveryController:
    """Turn progress observations into durable control-plane decisions."""

    def __init__(
        self,
        *,
        db: Any = None,
        task_id: str,
        run_id: str,
        attempt_id: str,
        step_id: str | None = None,
        expected_effects: Mapping[str, Any] | None = None,
        budget_manager: RecoveryBudgetManager | None = None,
        signal_sink: Callable[[str, dict[str, Any]], Any] | None = None,
        max_no_progress: int = 3,
        max_repeated_action: int = 2,
        max_repeated_state: int = 2,
    ):
        self.db = db
        self.task_id = str(task_id)
        self.run_id = str(run_id)
        self.attempt_id = str(attempt_id)
        self.step_id = str(step_id) if step_id is not None else None
        self.evaluator = EffectProgressEvaluator(expected_effects)
        self.budget_manager = budget_manager
        self.signal_sink = signal_sink
        self.max_no_progress = max(1, min(int(max_no_progress), 16))
        self.max_repeated_action = max(1, min(int(max_repeated_action), 16))
        self.max_repeated_state = max(1, min(int(max_repeated_state), 16))
        self.no_progress_count = 0
        self.repeated_action_count = 0
        self.repeated_state_count = 0
        self.validator_rejection_count = 0
        self.signals: list[dict[str, Any]] = []
        self.projector = RecoveryContextProjector()

    def observe(
        self,
        *,
        before_state: Any,
        after_state: Any,
        action: Mapping[str, Any] | None = None,
        observation: Mapping[str, Any] | None = None,
    ) -> tuple[RecoveryDecision, EffectProgress]:
        progress = self.evaluator.observe(
            before_state=before_state,
            after_state=after_state,
            action=action,
            observation=observation,
        )
        if progress.status is ProgressStatus.SATISFIED:
            return RecoveryDecision.VALIDATE_CANDIDATE, progress
        if progress.status is ProgressStatus.OSCILLATING:
            self.emit_signal("REPAIR_STATE_OSCILLATION", progress)
            return RecoveryDecision.ESCALATE_MACRO_REPLAN, progress
        evidence = progress.evidence or {}
        if evidence.get("repeated_action"):
            self.repeated_action_count += 1
        if evidence.get("repeated_state"):
            self.repeated_state_count += 1
        if progress.status in {ProgressStatus.NO_PROGRESS, ProgressStatus.CHANGED_UNKNOWN}:
            self.no_progress_count += 1
        else:
            self.no_progress_count = 0
        if self.repeated_action_count >= self.max_repeated_action:
            self.emit_signal("REPAIR_REPEATED_ACTION", progress)
            return RecoveryDecision.ESCALATE_MACRO_REPLAN, progress
        if self.repeated_state_count >= self.max_repeated_state:
            self.emit_signal("REPAIR_NO_PROGRESS", progress)
            return RecoveryDecision.ESCALATE_MACRO_REPLAN, progress
        if self.no_progress_count >= self.max_no_progress:
            self.emit_signal("REPAIR_NO_PROGRESS", progress)
            return RecoveryDecision.ESCALATE_MACRO_REPLAN, progress
        return RecoveryDecision.CONTINUE_LOCAL_REPAIR, progress

    def validator_rejected(self) -> RecoveryDecision:
        """Feed an authoritative rejection back into bounded recovery.

        The rejection remains validator-owned truth.  This method only counts
        repeated rejected candidates and, at the configured bound, emits a
        typed control-plane signal for replan policy to consume.
        """
        progress = EffectProgress(
            status=ProgressStatus.NO_PROGRESS,
            state_digest=self.evaluator.last_state_digest or _digest({}),
            action_fingerprint=self.evaluator.last_action_fingerprint or _digest({}),
            evidence={
                "source": "AUTHORITATIVE_VALIDATOR",
                "validator_rejection_count": self.validator_rejection_count + 1,
            },
        )
        self.validator_rejection_count += 1
        self.no_progress_count += 1
        if self.validator_rejection_count >= self.max_no_progress:
            self.emit_signal("REPEATED_VALIDATOR_REJECTION", progress)
            return RecoveryDecision.ESCALATE_MACRO_REPLAN
        return RecoveryDecision.CONTINUE_LOCAL_REPAIR

    def acquire_local_turn(self) -> None:
        if self.budget_manager is None:
            return
        try:
            self.budget_manager.acquire("local_repair")
        except BudgetReservationError:
            raise

    def budget_failure_progress(self) -> EffectProgress:
        state_digest = self.evaluator.last_state_digest or _digest({})
        action_fingerprint = self.evaluator.last_action_fingerprint or _digest({})
        return EffectProgress(
            status=ProgressStatus.NO_PROGRESS,
            state_digest=state_digest,
            action_fingerprint=action_fingerprint,
            evidence={"source": "RecoveryBudgetManager"},
        )

    def emit_signal(self, reason: str, progress: EffectProgress, *, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if reason not in TYPED_ESCALATION_REASONS:
            raise ValueError(f"unsupported typed escalation reason: {reason}")
        evidence = {
            "source": "RecoveryController",
            "progress_status": progress.status.value,
            "state_digest": progress.state_digest,
            "action_fingerprint": progress.action_fingerprint,
            "matched_effect_keys": list(progress.matched_effect_keys),
            **dict(progress.evidence or {}),
            **dict(extra or {}),
        }
        signal = {"reason": reason, "task_id": self.task_id, "run_id": self.run_id, "attempt_id": self.attempt_id, "step_id": self.step_id, "evidence": evidence}
        self.signals.append(signal)
        if self.signal_sink is not None:
            self.signal_sink(reason, evidence)
        if self.db is not None:
            from lhas.domain.enums import EventType
            from lhas.native.models import ReplanSignal
            from lhas.native.persistence import ReplanSignalRepository
            from lhas.persistence.event_store import EventStore

            durable = ReplanSignal(
                task_id=self.task_id,
                run_id=self.run_id,
                attempt_id=self.attempt_id,
                reason=reason,
                scope="TASKGRAPH_NODE",
                failed_node_id=self.step_id,
                evidence=evidence,
            )
            ReplanSignalRepository(self.db).create(durable)
            EventStore(self.db).append(
                EventType.REPLAN_SIGNAL_CREATED,
                task_id=self.task_id,
                run_id=self.run_id,
                attempt_id=self.attempt_id,
                payload={
                    "signal_id": durable.id,
                    "reason": reason,
                    "scope": durable.scope,
                    "failed_node_id": durable.failed_node_id,
                    "evidence": evidence,
                },
            )
            signal["signal_id"] = durable.id
        return signal


__all__ = [
    "BudgetReservationError",
    "EffectProgress",
    "EffectProgressEvaluator",
    "ProgressStatus",
    "RecoveryBudgetManager",
    "RecoveryContextProjector",
    "RecoveryController",
    "RecoveryDecision",
    "TYPED_ESCALATION_REASONS",
]
