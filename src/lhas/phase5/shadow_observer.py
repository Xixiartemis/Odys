"""Shadow Progress Observer.

Consumes ONLY information available to the running agent:
  - action/tool identity
  - public tool result
  - permitted environment observation
  - request-scoped side-effect receipts
  - bounded state projection
  - action/state repetition history

MUST NOT:
  - influence execution
  - request recovery
  - modify budget
  - see benchmark perturbation labels
  - see oracle path
  - see final judge
  - see ToolSandbox milestones
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from .types import ProgressObserver, ShadowRecord, SignalKind


class ShadowProgressObserver:
    """Non-interfering progress observer.

    Observations are persisted as progress_shadow.jsonl records.
    The observer maintains a sliding window of recent observations
    for stall/regression detection but never modifies execution state.
    """

    def __init__(self, *, window_size: int = 5):
        self._window_size = window_size
        self._history: dict[str, list[dict[str, Any]]] = {}  # task_id -> history
        self._records: list[ShadowRecord] = []

    def observe(
        self,
        *,
        task_id: str,
        step: int,
        action_identity: str,
        tool_result: dict[str, Any],
        environment_observation: Optional[dict[str, Any]] = None,
    ) -> ShadowRecord:
        """Record a shadow observation.  Returns the signal without side effects."""
        if task_id not in self._history:
            self._history[task_id] = []

        entry = {
            "step": step,
            "action": action_identity,
            "tool_result_keys": sorted(tool_result.keys()),
            "result_status": tool_result.get("status", "unknown"),
        }
        self._history[task_id].append(entry)
        # Trim window
        if len(self._history[task_id]) > self._window_size * 2:
            self._history[task_id] = self._history[task_id][-self._window_size:]

        signal, reason = self._classify(task_id, step, action_identity, tool_result)

        record = ShadowRecord(
            trial_id=task_id,
            step=step,
            observable_features={
                "action_identity": action_identity,
                "tool_result_keys": sorted(tool_result.keys()),
                "result_status": tool_result.get("status", "unknown"),
                "environment_available": environment_observation is not None,
                "history_length": len(self._history[task_id]),
            },
            signal=signal,
            signal_reason=reason,
            confidence=self._compute_confidence(task_id),
        )
        self._records.append(record)
        return record

    def _classify(
        self,
        task_id: str,
        step: int,
        action_identity: str,
        tool_result: dict[str, Any],
    ) -> tuple[SignalKind, str]:
        """Classify the observation into a signal.

        This is a deterministic, model-free classification based on
        observable features only.
        """
        history = self._history.get(task_id, [])
        result_status = tool_result.get("status", "unknown")

        # Check for tool error
        if result_status in {"error", "failure", "FAILURE"}:
            return SignalKind.ANOMALY, f"tool returned status={result_status}"

        # Check for repetition (potential stall)
        if len(history) >= 3:
            recent_actions = [h["action"] for h in history[-3:]]
            if len(set(recent_actions)) == 1:
                return SignalKind.STALLED, f"action '{action_identity}' repeated 3 times"

        # Check for regression (result status degraded)
        if len(history) >= 2:
            prev_status = history[-2].get("result_status", "unknown")
            if prev_status == "success" and result_status != "success":
                return SignalKind.REGRESSING, "result status degraded from success"

        # Default: progressing
        if result_status in {"success", "ok", "completed"}:
            return SignalKind.PROGRESSING, "tool call succeeded"

        return SignalKind.NO_OBSERVATION, "insufficient signal"

    def _compute_confidence(self, task_id: str) -> float:
        """Compute a confidence score based on history consistency."""
        history = self._history.get(task_id, [])
        if len(history) < 2:
            return 0.5
        success_count = sum(
            1 for h in history
            if h.get("result_status") in {"success", "ok", "completed"}
        )
        return min(success_count / len(history), 1.0)

    def get_records(self, task_id: Optional[str] = None) -> list[ShadowRecord]:
        """Return all recorded observations, optionally filtered by task_id."""
        if task_id is None:
            return list(self._records)
        return [r for r in self._records if r.trial_id == task_id]

    def to_jsonl(self, task_id: Optional[str] = None) -> str:
        """Serialize records to JSONL format."""
        records = self.get_records(task_id)
        lines = [r.model_dump_json() for r in records]
        return "\n".join(lines) + "\n" if lines else ""
