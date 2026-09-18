"""Bounded, provider-neutral progress control for repair executions.

The tracker is deliberately narrower than completion or validation authority.
It only observes bounded tool projections and reports whether a repair should
continue, stop for lack of convergence, or present a plausible candidate for
the authoritative validator.  Durable attempt history remains owned by the
planning/runtime layers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from typing import Any


REPAIR_STOP_REASONS = frozenset(
    {
        "VERIFIED",
        "NO_PROGRESS",
        "REPEATED_ACTION",
        "BUDGET_EXHAUSTED",
        "MODEL_FAILURE",
        "TOOL_FAILURE",
        "REPLAN_REQUIRED",
        "CANCELLED",
    }
)


def _canonical_digest(value: Any) -> str:
    """Hash a bounded projection without returning its content."""
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
                normalized,
                "false",
                "failed",
            }
    return expected == observed


def _first_mapping_value(observation: Mapping[str, Any], key: str) -> Any:
    for container_name in ("bounded_output", "safe_summary", "result_summary"):
        container = observation.get(container_name)
        if isinstance(container, Mapping) and key in container:
            return container[key]
    return observation.get(key)


@dataclass(frozen=True)
class RepairProgressDecision:
    """The non-authoritative decision after one bounded tool observation."""

    continue_repair: bool
    candidate_for_validation: bool = False
    stop_reason: str | None = None
    metrics: dict[str, Any] | None = None


class RepairProgressTracker:
    """Track generic repair convergence using bounded execution evidence.

    ``expected_effects`` is supplied by the current PlanStep.  The tracker
    never knows a task name, target checksum, or benchmark fixture.  A match
    only marks a candidate for authoritative validation; it can never mark a
    plan or step VERIFIED.
    """

    def __init__(
        self,
        *,
        expected_effects: Mapping[str, Any] | None = None,
        initial_state_digest: str | None = None,
        max_no_progress: int = 3,
        max_repeated_state: int = 2,
        max_repeated_action: int = 2,
    ) -> None:
        self.expected_effects = dict(expected_effects or {})
        self.initial_state_digest = (
            str(initial_state_digest) if initial_state_digest else None
        )
        self.max_no_progress = max(1, min(int(max_no_progress), 16))
        self.max_repeated_state = max(1, min(int(max_repeated_state), 16))
        self.max_repeated_action = max(1, min(int(max_repeated_action), 16))
        self.repair_turns = 0
        self.unique_repair_states = 0
        self.repeated_state_count = 0
        self.no_progress_count = 0
        self.validation_candidate_count = 0
        self.repeated_action_count = 0
        self.repair_stop_reason: str | None = None
        self._state_digests: set[str] = set()
        self._action_fingerprints: set[str] = set()

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "RepairProgressTracker":
        return cls(
            expected_effects=(
                config.get("expected_effects", {})
                if isinstance(config.get("expected_effects", {}), Mapping)
                else {}
            ),
            initial_state_digest=config.get("initial_state_digest"),
            max_no_progress=int(config.get("max_no_progress", 3)),
            max_repeated_state=int(config.get("max_repeated_state", 2)),
            max_repeated_action=int(config.get("max_repeated_action", 2)),
        )

    def begin_turn(self) -> None:
        if self.repair_stop_reason is None:
            self.repair_turns += 1

    def candidate_for_validation(self) -> RepairProgressDecision:
        """Record a candidate without granting completion."""
        if self.repair_stop_reason is None:
            self.validation_candidate_count += 1
        return self._decision(candidate=True)

    def validation_rejected(self) -> RepairProgressDecision:
        """Count an authoritative rejection as non-convergent repair work."""
        if self.repair_stop_reason is not None:
            return self._decision()
        self.no_progress_count += 1
        if self.no_progress_count >= self.max_no_progress:
            return self._stop("NO_PROGRESS")
        return self._decision()

    def observe(self, observation: Mapping[str, Any]) -> RepairProgressDecision:
        """Consume one bounded tool observation and apply stop thresholds."""
        if self.repair_stop_reason is not None:
            return self._decision()

        bounded = observation.get("bounded_output", {})
        summary = observation.get("safe_summary", {})
        state_digest = self._state_digest(observation, bounded, summary)
        if state_digest:
            if state_digest in self._state_digests or state_digest == self.initial_state_digest:
                self.repeated_state_count += 1
            else:
                self._state_digests.add(state_digest)
                self.unique_repair_states += 1

        capability = str(observation.get("capability", ""))
        args_sha = str(observation.get("args_sha256", ""))
        action_fingerprint = _canonical_digest(
            {"capability": capability, "args_sha256": args_sha}
        )
        if action_fingerprint in self._action_fingerprints:
            self.repeated_action_count += 1
        else:
            self._action_fingerprints.add(action_fingerprint)

        candidate = self._effect_matches(observation, bounded, summary)
        if candidate:
            self.validation_candidate_count += 1
            self.no_progress_count = 0
        else:
            # A changing but still incorrect state is not convergence.  This
            # bounded counter prevents oscillation and endless plausible edits
            # from consuming the entire provider budget.
            self.no_progress_count += 1

        if str(observation.get("status", "")).upper() == "FAILURE":
            return self._stop("TOOL_FAILURE")
        if isinstance(summary, Mapping) and summary.get("strategy_change_required"):
            return self._stop("REPLAN_REQUIRED")

        if self.repeated_action_count >= self.max_repeated_action:
            return self._stop("REPEATED_ACTION")
        if self.repeated_state_count >= self.max_repeated_state:
            return self._stop("NO_PROGRESS")
        if self.no_progress_count >= self.max_no_progress:
            return self._stop("NO_PROGRESS")
        return self._decision(candidate=candidate)

    def stop(self, reason: str) -> None:
        normalized = str(reason).upper()
        if normalized not in REPAIR_STOP_REASONS:
            raise ValueError(f"unsupported repair stop reason: {reason}")
        self.repair_stop_reason = normalized

    def snapshot(self) -> dict[str, Any]:
        """Return a bounded, JSON-safe metric projection."""
        return {
            "repair_turns": int(self.repair_turns),
            "unique_repair_states": int(self.unique_repair_states),
            "repeated_state_count": int(self.repeated_state_count),
            "no_progress_count": int(self.no_progress_count),
            "validation_candidate_count": int(self.validation_candidate_count),
            "repeated_action_count": int(self.repeated_action_count),
            "repair_stop_reason": self.repair_stop_reason,
            "thresholds": {
                "max_no_progress": self.max_no_progress,
                "max_repeated_state": self.max_repeated_state,
                "max_repeated_action": self.max_repeated_action,
            },
        }

    def _decision(self, *, candidate: bool = False) -> RepairProgressDecision:
        return RepairProgressDecision(
            continue_repair=self.repair_stop_reason is None,
            candidate_for_validation=candidate,
            stop_reason=self.repair_stop_reason,
            metrics=self.snapshot(),
        )

    def _stop(self, reason: str) -> RepairProgressDecision:
        self.repair_stop_reason = reason
        return self._decision()

    def _effect_matches(
        self,
        observation: Mapping[str, Any],
        bounded: Any,
        summary: Any,
    ) -> bool:
        if not self.expected_effects:
            return False
        for container in (bounded, summary, observation):
            if isinstance(container, Mapping) and all(
                key in container and _matches(value, container[key])
                for key, value in self.expected_effects.items()
            ):
                return True
        return all(
            _matches(value, _first_mapping_value(observation, key))
            for key, value in self.expected_effects.items()
        )

    @staticmethod
    def _state_digest(
        observation: Mapping[str, Any],
        bounded: Any,
        summary: Any,
    ) -> str | None:
        for container in (bounded, summary, observation):
            if not isinstance(container, Mapping):
                continue
            for key in (
                "state_digest",
                "after_sha256",
                "checksum",
                "workspace_after_digest",
            ):
                value = container.get(key)
                if value:
                    return str(value)
        if isinstance(bounded, Mapping) and bounded:
            return _canonical_digest(bounded)
        if isinstance(summary, Mapping) and summary:
            return _canonical_digest(summary)
        return None


__all__ = [
    "REPAIR_STOP_REASONS",
    "RepairProgressDecision",
    "RepairProgressTracker",
]
