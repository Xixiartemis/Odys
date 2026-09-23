"""Validator Feedback.

The validator itself must not directly write VerifiedTaskState.
Only StateCommitter can promote evidence into verified state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


def _new_id() -> str:
    import uuid
    return uuid.uuid4().hex


class ValidatorDecision(str, Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    INDETERMINATE = "INDETERMINATE"


class ValidatorExecutionStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    TIMEOUT = "TIMEOUT"
    SKIPPED = "SKIPPED"


class ValidatorFeedback(BaseModel):
    """Immutable feedback from a runtime validator.

    The validator does NOT write VerifiedTaskState directly.
    Only StateCommitter may promote accepted feedback into verified state.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    feedback_id: str = Field(default_factory=_new_id)
    validator_id: str
    validator_version: str = "1.0"
    candidate_id: str  # the evidence/artifact being validated
    execution_status: ValidatorExecutionStatus
    decision: ValidatorDecision
    evidence_refs: list[str] = Field(default_factory=list)
    observed_state_digest: str = ""
    failure_type: Optional[str] = None
    detail: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def is_actionable(self) -> bool:
        """Only SUCCESS + ACCEPT may advance verified state."""
        return (
            self.execution_status == ValidatorExecutionStatus.SUCCESS
            and self.decision == ValidatorDecision.ACCEPT
        )
