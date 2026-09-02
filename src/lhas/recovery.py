"""Recovery policy (docs/08_RECOVERY_POLICY.md).

Recovery's goal is not blind retry: add the necessary information or change
execution conditions based on the failure reason, and decide whether it is
still worth continuing.

RecoveryPolicy only DECIDES — it never executes external actions, never
touches the web or models, never modifies acceptance criteria.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field

from lhas.domain.enums import FailureType, RecoveryActionType
from lhas.domain.models import Attempt, Task, new_id
from lhas.failure import FailureReport


class RecoveryAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    attempt_id: str
    action_type: RecoveryActionType
    reason: str
    context_policy: str = "CP-2"
    attempt_from: int
    attempt_to: Optional[int] = None
    added_context: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RecoveryPolicy(Protocol):
    async def decide(
        self,
        *,
        task: Task,
        attempt: Attempt,
        failure_report: FailureReport,
        attempt_number: int,
        max_attempts: int,
        history: list[RecoveryAction],
    ) -> RecoveryAction:
        ...


class DefaultRecoveryPolicy:
    """V0 default policy (docs/08):

    - attempt 1 fails -> RETRY_WITH_FAILURE_CONTEXT
    - attempt 2 fails -> RETRY_WITH_EXPANDED_CONTEXT
    - attempt 3 fails -> ESCALATE
    - TIMEOUT         -> controlled retry
    - MISSING_CONTEXT -> supply the missing info, then retry
    - STALE_CONTEXT   -> rebuild context (new snapshot), then retry
    - CONTEXT_CONFLICT-> ESCALATE or human clarification
    - APPROVAL_REQUIRED -> HUMAN_APPROVAL gate (Phase B: recorded, then escalate)
    """

    def __init__(self, context_policy: str = "CP-2"):
        self.context_policy = context_policy

    async def decide(
        self,
        *,
        task: Task,
        attempt: Attempt,
        failure_report: FailureReport,
        attempt_number: int,
        max_attempts: int,
        history: list[RecoveryAction],
    ) -> RecoveryAction:
        ft = failure_report.failure_type

        if ft == FailureType.APPROVAL_REQUIRED:
            return self._action(attempt, RecoveryActionType.HUMAN_APPROVAL,
                                "approval required before any further action", attempt_number, None)
        if ft == FailureType.CONTEXT_CONFLICT:
            return self._action(attempt, RecoveryActionType.ESCALATE,
                                "context conflict needs human clarification", attempt_number, None)
        if ft in {FailureType.QUOTA_EXHAUSTED, FailureType.BILLING_OR_CREDIT_EXHAUSTED, FailureType.AUTH_INVALID}:
            return self._action(attempt, RecoveryActionType.BLOCK_PROVIDER,
                                "provider route is blocked; migrate explicitly before resuming", attempt_number, None)
        if attempt_number >= max_attempts:
            return self._action(attempt, RecoveryActionType.ESCALATE,
                                "max attempts reached", attempt_number, None)

        if ft == FailureType.MISSING_CONTEXT:
            return self._action(
                attempt, RecoveryActionType.RETRY_WITH_FAILURE_CONTEXT,
                "missing context detected; supply the missing information",
                attempt_number, attempt_number + 1,
                added_context={"missing_context": failure_report.summary},
            )
        if ft == FailureType.STALE_CONTEXT:
            return self._action(
                attempt, RecoveryActionType.RETRY_WITH_EXPANDED_CONTEXT,
                "stale context; rebuild from scratch",
                attempt_number, attempt_number + 1,
                added_context={"rebuild_context": True},
            )
        if ft == FailureType.TIMEOUT:
            return self._action(
                attempt, RecoveryActionType.RETRY_WITH_FAILURE_CONTEXT,
                "controlled retry after timeout (budget permitting)",
                attempt_number, attempt_number + 1,
                added_context={"failure_evidence": failure_report.evidence},
            )
        if ft == FailureType.BUDGET_EXHAUSTED:
            return self._action(
                attempt, RecoveryActionType.RETRY_WITH_FAILURE_CONTEXT,
                "native execution budget exhausted; preserve the causal failure evidence",
                attempt_number, attempt_number + 1,
                added_context={
                    "failure_evidence": failure_report.evidence,
                    "failure_type": failure_report.failure_type.value,
                    "suggested_recovery": failure_report.suggested_recovery,
                },
            )

        if attempt_number == 1:
            return self._action(
                attempt, RecoveryActionType.RETRY_WITH_FAILURE_CONTEXT,
                "first attempt failed; retry with failure context",
                attempt_number, attempt_number + 1,
                added_context={
                    "failure_evidence": failure_report.evidence,
                    "failure_type": failure_report.failure_type.value,
                    "suggested_recovery": failure_report.suggested_recovery,
                },
            )
        return self._action(
            attempt, RecoveryActionType.RETRY_WITH_EXPANDED_CONTEXT,
            "second attempt failed; retry with expanded context",
            attempt_number, attempt_number + 1,
            added_context={
                "failure_evidence": failure_report.evidence,
                "failure_type": failure_report.failure_type.value,
                "suggested_recovery": failure_report.suggested_recovery,
                "relevant_history": f"{len(history)} prior recovery action(s)",
            },
        )

    def _action(self, attempt, action_type, reason, attempt_from, attempt_to, added_context=None) -> RecoveryAction:
        return RecoveryAction(
            attempt_id=attempt.id,
            action_type=action_type,
            reason=reason,
            context_policy=self.context_policy,
            attempt_from=attempt_from,
            attempt_to=attempt_to,
            added_context=added_context or {},
        )
