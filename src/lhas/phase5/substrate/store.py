"""Benchmark Outcome and Runtime/Offline Separation.

Runtime validator produces ValidatorFeedback (can trigger StateCommit).
Benchmark offline grader produces BenchmarkOutcome (CANNOT mutate runtime state).

Type/API firewall: BenchmarkOutcome cannot reach runtime state mutation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from .state import VerifiedTaskState, ControlState
from .validation import ValidatorFeedback, ValidatorDecision, ValidatorExecutionStatus


class BenchmarkOutcome(BaseModel):
    """Offline benchmark scoring result.

    MUST NOT mutate TaskState, trigger recovery, change ControlState,
    or decide runtime action.  For experiment scoring only.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    trial_id: str
    benchmark_name: str
    tsr: Optional[float] = None
    prr: Optional[float] = None
    rc: Optional[float] = None
    raw_score: Optional[float] = None
    native_metrics: dict[str, Any] = Field(default_factory=dict)
    judge_output: Optional[dict[str, Any]] = None
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # CRITICAL: No reference to VerifiedTaskState, ControlState, or
    # StateCommitter.  This type has no method to mutate runtime state.
    # It is purely a data carrier for offline scoring.


class RuntimeValidator:
    """Runtime validator that produces ValidatorFeedback.

    May trigger StateCommit through the reducer.
    """

    def __init__(self, validator_id: str, version: str = "1.0"):
        self._validator_id = validator_id
        self._version = version

    def validate(
        self,
        *,
        candidate_id: str,
        evidence_refs: list[str],
        observed_state_digest: str = "",
        accept_condition: bool = True,
        failure_type: Optional[str] = None,
    ) -> ValidatorFeedback:
        """Produce validator feedback for a candidate."""
        if accept_condition:
            return ValidatorFeedback(
                validator_id=self._validator_id,
                validator_version=self._version,
                candidate_id=candidate_id,
                execution_status=ValidatorExecutionStatus.SUCCESS,
                decision=ValidatorDecision.ACCEPT,
                evidence_refs=evidence_refs,
                observed_state_digest=observed_state_digest,
            )
        else:
            return ValidatorFeedback(
                validator_id=self._validator_id,
                validator_version=self._version,
                candidate_id=candidate_id,
                execution_status=ValidatorExecutionStatus.SUCCESS,
                decision=ValidatorDecision.REJECT,
                evidence_refs=evidence_refs,
                observed_state_digest=observed_state_digest,
                failure_type=failure_type or "validation_failed",
            )


class OfflineGrader:
    """Offline benchmark grader.

    Runs AFTER runtime termination.  Produces BenchmarkOutcome.
    CANNOT mutate VerifiedTaskState or ControlState.
    """

    def __init__(self, benchmark_name: str):
        self._benchmark_name = benchmark_name

    def grade(
        self,
        *,
        trial_id: str,
        native_metrics: dict[str, Any],
        judge_output: Optional[dict[str, Any]] = None,
    ) -> BenchmarkOutcome:
        """Produce offline benchmark outcome.

        This method has no reference to any runtime state object.
        """
        return BenchmarkOutcome(
            trial_id=trial_id,
            benchmark_name=self._benchmark_name,
            tsr=native_metrics.get("tsr"),
            prr=native_metrics.get("prr"),
            rc=native_metrics.get("rc"),
            raw_score=native_metrics.get("raw_score"),
            native_metrics=native_metrics,
            judge_output=judge_output,
        )
