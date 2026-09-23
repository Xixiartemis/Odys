"""Task State Reducer and Committer.

StateCommitter is the sole authority that promotes candidate evidence
into VerifiedTaskState.  Only validator SUCCESS + ACCEPT may advance
verified progress.

Idempotency: same feedback_id cannot create duplicate state transitions.
Event-sourcing-lite: rebuild digest must equal materialized digest.
"""

from __future__ import annotations

from typing import Any, Optional

from .state import StateCommit, VerifiedFact, VerifiedTaskState
from .evidence import EvidenceEvent, EvidenceEventType, EvidenceLedger
from .artifacts import ArtifactRef, ArtifactStore, EffectReceipt, EffectStatus
from .validation import ValidatorDecision, ValidatorFeedback, ValidatorExecutionStatus


class CommitRejected(Exception):
    """Raised when a commit is rejected (not actionable or duplicate)."""


class TaskStateReducer:
    """Reduces evidence events into verified task state through commits.

    This is the ONLY path from evidence to verified state.
    """

    def __init__(
        self,
        initial_state: VerifiedTaskState,
        ledger: EvidenceLedger,
        artifact_store: ArtifactStore,
    ):
        self._state = initial_state
        self._ledger = ledger
        self._artifact_store = artifact_store
        self._commits: list[StateCommit] = []
        self._committed_feedback_ids: set[str] = set()

    @property
    def state(self) -> VerifiedTaskState:
        return self._state

    @property
    def commits(self) -> list[StateCommit]:
        return list(self._commits)

    def commit(
        self,
        feedback: ValidatorFeedback,
        *,
        evidence: Optional[list[EvidenceEvent]] = None,
        artifacts: Optional[list[ArtifactRef]] = None,
    ) -> StateCommit:
        """Attempt to promote evidence into verified state.

        Raises CommitRejected if:
        - feedback is not actionable (not SUCCESS + ACCEPT)
        - feedback_id already committed (idempotency guard)
        """
        # Idempotency check
        if feedback.feedback_id in self._committed_feedback_ids:
            raise CommitRejected(
                f"Duplicate commit for feedback_id={feedback.feedback_id}"
            )

        # Only SUCCESS + ACCEPT may advance
        if not feedback.is_actionable():
            raise CommitRejected(
                f"Not actionable: status={feedback.execution_status.value}, "
                f"decision={feedback.decision.value}"
            )

        # Build verified facts from accepted evidence
        new_facts: list[VerifiedFact] = []
        evidence_ids: list[str] = []
        if evidence:
            for ev in evidence:
                evidence_ids.append(ev.evidence_id)
                for key, value in ev.payload.items():
                    if isinstance(value, (str, int, float, bool)):
                        fact = VerifiedFact(
                            key=f"{ev.event_type.value}.{key}",
                            value=value,
                            evidence_id=ev.evidence_id,
                            commit_id="",  # filled below
                        )
                        new_facts.append(fact)

        # Collect artifact IDs
        artifact_ids = [a.artifact_id for a in (artifacts or [])]

        # Create the commit
        commit = StateCommit(
            feedback_id=feedback.feedback_id,
            previous_state_version=self._state.state_version,
            next_state_version=self._state.state_version + 1,
            accepted_evidence_ids=evidence_ids,
            accepted_artifact_ids=artifact_ids,
        )

        # Update commit_id in facts
        for fact in new_facts:
            object.__setattr__(fact, "commit_id", commit.commit_id)

        # Apply to state
        self._state = self._state.with_commit(
            commit_id=commit.commit_id,
            feedback_id=feedback.feedback_id,
            new_facts=new_facts,
            new_artifact_ids=artifact_ids,
        )

        # Record commit with updated digest
        commit_with_digest = StateCommit(
            commit_id=commit.commit_id,
            feedback_id=feedback.feedback_id,
            previous_state_version=commit.previous_state_version,
            next_state_version=commit.next_state_version,
            accepted_evidence_ids=commit.accepted_evidence_ids,
            accepted_artifact_ids=commit.accepted_artifact_ids,
            resulting_state_digest=self._state.state_digest,
        )

        self._commits.append(commit_with_digest)
        self._committed_feedback_ids.add(feedback.feedback_id)

        # Append event to ledger
        self._ledger.append(
            task_id=self._state.task_id,
            attempt_id="",
            event_type=EvidenceEventType.STATE_COMMITTED,
            payload={
                "commit_id": commit_with_digest.commit_id,
                "feedback_id": feedback.feedback_id,
                "state_version": self._state.state_version,
            },
        )

        return commit_with_digest

    def rebuild_state(self) -> VerifiedTaskState:
        """Rebuild state from commits — for parity testing."""
        state = VerifiedTaskState(
            task_id=self._state.task_id,
            run_id=self._state.run_id,
            goal=self._state.goal,
        )
        for commit in self._commits:
            # Replay facts from commit
            new_facts = []
            for fact in self._state.verified_facts.values():
                if fact.commit_id == commit.commit_id:
                    new_facts.append(fact)
            state = state.with_commit(
                commit_id=commit.commit_id,
                feedback_id=commit.feedback_id,
                new_facts=new_facts,
                new_artifact_ids=commit.accepted_artifact_ids,
            )
        return state

    def effect_receipt(
        self,
        invocation_id: str,
        effect_status: EffectStatus,
        *,
        before_digest: Optional[str] = None,
        after_digest: Optional[str] = None,
        artifact_refs: Optional[list[str]] = None,
    ) -> EffectReceipt:
        """Record an effect receipt in the ledger."""
        receipt = EffectReceipt(
            invocation_id=invocation_id,
            effect_status=effect_status,
            before_digest=before_digest,
            after_digest=after_digest,
            artifact_refs=artifact_refs or [],
        )
        event_type = {
            EffectStatus.CONFIRMED: EvidenceEventType.SIDE_EFFECT_CONFIRMED,
            EffectStatus.UNCERTAIN: EvidenceEventType.SIDE_EFFECT_UNCERTAIN,
            EffectStatus.NOT_OBSERVED: EvidenceEventType.TERMINATED,  # placeholder
        }[effect_status]

        self._ledger.append(
            task_id=self._state.task_id,
            attempt_id="",
            event_type=event_type,
            payload={
                "invocation_id": invocation_id,
                "effect_status": effect_status.value,
            },
        )
        return receipt
