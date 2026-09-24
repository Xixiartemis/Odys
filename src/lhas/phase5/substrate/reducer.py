"""Task State Reducer and Committer.

StateCommitter is the sole authority that promotes candidate evidence
into VerifiedTaskState.  Only validator SUCCESS + ACCEPT may advance
verified progress.

Idempotency: same feedback_id cannot create duplicate state transitions.
Candidate-level idempotency: same candidate_id cannot be committed twice.
Event-sourcing-lite: rebuild digest must equal materialized digest.
Rebuild source: durable commit history only (MATERIALIZED_STATE_INDEPENDENT).

Invariant enforcement:
  - UNAUTHORIZED_EVIDENCE_PROMOTION=BLOCKED (evidence must be in feedback.evidence_refs)
  - UNAUTHORIZED_ARTIFACT_PROMOTION=BLOCKED (artifacts must exist + pass integrity)
  - DUPLICATE_CANDIDATE_PROMOTION=BLOCKED (candidate_id tracked)
  - REBUILD_SOURCE=MATERIALIZED_STATE_INDEPENDENT (rebuild reads commit history only)
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
        self._initial_task_id = initial_state.task_id
        self._initial_run_id = initial_state.run_id
        self._initial_goal = initial_state.goal
        self._state = initial_state
        self._ledger = ledger
        self._artifact_store = artifact_store
        self._commits: list[StateCommit] = []
        self._committed_feedback_ids: set[str] = set()
        self._committed_candidate_ids: set[str] = set()

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
        - feedback_id already committed (feedback-level idempotency)
        - candidate_id already committed (candidate-level idempotency)
        - evidence contains IDs not in feedback.evidence_refs (lineage check)
        - artifacts don't exist or fail integrity check (artifact guard)
        """
        # ── Feedback-level idempotency ──────────────────────────────
        if feedback.feedback_id in self._committed_feedback_ids:
            raise CommitRejected(
                f"Duplicate commit for feedback_id={feedback.feedback_id}"
            )

        # ── Candidate-level idempotency ─────────────────────────────
        if feedback.candidate_id in self._committed_candidate_ids:
            raise CommitRejected(
                f"Duplicate commit for candidate_id={feedback.candidate_id}"
            )

        # ── Only SUCCESS + ACCEPT may advance ───────────────────────
        if not feedback.is_actionable():
            raise CommitRejected(
                f"Not actionable: status={feedback.execution_status.value}, "
                f"decision={feedback.decision.value}"
            )

        # ── Evidence lineage check ──────────────────────────────────
        # Every promoted evidence_id must be declared in feedback.evidence_refs
        feedback_evidence_set = set(feedback.evidence_refs)
        evidence_ids: list[str] = []
        if evidence:
            for ev in evidence:
                if ev.evidence_id not in feedback_evidence_set:
                    raise CommitRejected(
                        f"Evidence {ev.evidence_id} not in feedback.evidence_refs "
                        f"(UNAUTHORIZED_EVIDENCE_PROMOTION=BLOCKED)"
                    )
                evidence_ids.append(ev.evidence_id)

        # ── Artifact existence + integrity check ────────────────────
        artifact_ids: list[str] = []
        if artifacts:
            for art in artifacts:
                if not self._artifact_store.exists(art.artifact_id):
                    raise CommitRejected(
                        f"Artifact {art.artifact_id} not found in store "
                        f"(UNAUTHORIZED_ARTIFACT_PROMOTION=BLOCKED)"
                    )
                if not self._artifact_store.verify_integrity(art.artifact_id):
                    raise CommitRejected(
                        f"Artifact {art.artifact_id} failed integrity check"
                    )
                artifact_ids.append(art.artifact_id)

        # ── Build verified facts from accepted evidence ─────────────
        new_facts: list[VerifiedFact] = []
        if evidence:
            for ev in evidence:
                for key, value in ev.payload.items():
                    if isinstance(value, (str, int, float, bool)):
                        fact = VerifiedFact(
                            key=f"{ev.event_type.value}.{key}",
                            value=value,
                            evidence_id=ev.evidence_id,
                            commit_id="",  # filled below
                        )
                        new_facts.append(fact)

        # ── Create the commit (draft, to get commit_id) ─────────────
        commit = StateCommit(
            feedback_id=feedback.feedback_id,
            previous_state_version=self._state.state_version,
            next_state_version=self._state.state_version + 1,
            accepted_evidence_ids=tuple(evidence_ids),
            accepted_artifact_ids=tuple(artifact_ids),
        )

        # Update commit_id in facts
        for fact in new_facts:
            object.__setattr__(fact, "commit_id", commit.commit_id)

        # ── Apply to state ──────────────────────────────────────────
        self._state = self._state.with_commit(
            commit_id=commit.commit_id,
            feedback_id=feedback.feedback_id,
            new_facts=new_facts,
            new_artifact_ids=artifact_ids,
        )

        # ── Record commit with accepted_facts + updated digest ──────
        commit_with_digest = StateCommit(
            commit_id=commit.commit_id,
            feedback_id=feedback.feedback_id,
            previous_state_version=commit.previous_state_version,
            next_state_version=commit.next_state_version,
            accepted_evidence_ids=commit.accepted_evidence_ids,
            accepted_artifact_ids=commit.accepted_artifact_ids,
            accepted_facts=tuple(new_facts),
            resulting_state_digest=self._state.state_digest,
        )

        self._commits.append(commit_with_digest)
        self._committed_feedback_ids.add(feedback.feedback_id)
        self._committed_candidate_ids.add(feedback.candidate_id)

        # ── Append event to ledger ──────────────────────────────────
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
        """Rebuild state from durable commit history only.

        REBUILD_SOURCE=MATERIALIZED_STATE_INDEPENDENT:
        Does NOT read self._state.verified_facts.  Reconstructs entirely
        from StateCommit records which store accepted_facts.
        """
        state = VerifiedTaskState(
            task_id=self._initial_task_id,
            run_id=self._initial_run_id,
            goal=self._initial_goal,
        )
        for commit in self._commits:
            state = state.with_commit(
                commit_id=commit.commit_id,
                feedback_id=commit.feedback_id,
                new_facts=list(commit.accepted_facts),
                new_artifact_ids=list(commit.accepted_artifact_ids),
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
            EffectStatus.NOT_OBSERVED: EvidenceEventType.SIDE_EFFECT_NOT_OBSERVED,
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
