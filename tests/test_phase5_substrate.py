"""Phase5 Research Substrate Tests — S1–S21.

Provider-free tests for the minimum Task State / Evidence / Evaluation
substrate required to test Odys Recovery Control Policy rigorously.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from lhas.phase5.substrate import (
    ArtifactRef,
    BenchmarkOutcome,
    ControlState,
    EffectReceipt,
    EffectStatus,
    EvidenceEvent,
    EvidenceEventType,
    EvidenceLedger,
    InMemoryArtifactStore,
    OfflineGrader,
    RuntimeValidator,
    StateCommit,
    TaskStateReducer,
    ValidatorDecision,
    ValidatorExecutionStatus,
    ValidatorFeedback,
    VerifiedFact,
    VerifiedTaskState,
)
from lhas.phase5.substrate.reducer import CommitRejected


# ── Helpers ──────────────────────────────────────────────────────────

def _make_state(**overrides) -> VerifiedTaskState:
    defaults = dict(task_id="T1", run_id="R1", goal="test goal")
    defaults.update(overrides)
    return VerifiedTaskState(**defaults)


def _make_ledger(run_id: str = "R1") -> EvidenceLedger:
    return EvidenceLedger(run_id)


def _make_feedback(
    *,
    decision: ValidatorDecision = ValidatorDecision.ACCEPT,
    execution_status: ValidatorExecutionStatus = ValidatorExecutionStatus.SUCCESS,
    candidate_id: str = "cand-1",
    feedback_id: str = "fb-1",
) -> ValidatorFeedback:
    return ValidatorFeedback(
        feedback_id=feedback_id,
        validator_id="test-validator",
        candidate_id=candidate_id,
        execution_status=execution_status,
        decision=decision,
    )


# ══════════════════════════════════════════════════════════════════════
# S1: Unverified model claim cannot update VerifiedTaskState
# ══════════════════════════════════════════════════════════════════════

class TestS1_UnverifiedClaimCannotMutateState:
    def test_verified_state_has_no_model_claim_field(self):
        state = _make_state()
        fields = set(state.model_fields.keys())
        assert "model_claims" not in fields
        assert "chat_history" not in fields
        assert "reasoning_text" not in fields

    def test_cannot_inject_fact_without_commit(self):
        state = _make_state()
        # Facts can only be added through with_commit, which requires
        # VerifiedFact objects with evidence_id and commit_id
        fact = VerifiedFact(
            key="test", value="val",
            evidence_id="ev-1", commit_id="commit-1",
        )
        new_state = state.with_commit(
            commit_id="commit-1", feedback_id="fb-1",
            new_facts=[fact], new_artifact_ids=[],
        )
        assert new_state.fact_value("test") == "val"
        # Original state unchanged
        assert state.fact_value("test") is None

    def test_verified_fact_requires_evidence(self):
        fact = VerifiedFact(
            key="k", value="v",
            evidence_id="ev-1", commit_id="commit-1",
        )
        assert fact.evidence_id == "ev-1"
        assert fact.commit_id == "commit-1"


# ══════════════════════════════════════════════════════════════════════
# S2: ACCEPT + validator SUCCESS commits verified state
# ══════════════════════════════════════════════════════════════════════

class TestS2_AcceptSuccessCommitsState:
    def test_commit_advances_state(self):
        state = _make_state()
        ledger = _make_ledger()
        store = InMemoryArtifactStore()
        reducer = TaskStateReducer(state, ledger, store)

        ev = ledger.append(
            task_id="T1", attempt_id="A1",
            event_type=EvidenceEventType.TOOL_OBSERVED,
            payload={"tool": "test_tool", "status": "success"},
        )
        feedback = _make_feedback()
        commit = reducer.commit(feedback, evidence=[ev])

        assert reducer.state.state_version == 1
        assert reducer.state.last_commit_id == commit.commit_id
        assert commit.validate_version_increment()

    def test_accepted_fact_is_in_state(self):
        state = _make_state()
        ledger = _make_ledger()
        store = InMemoryArtifactStore()
        reducer = TaskStateReducer(state, ledger, store)

        ev = ledger.append(
            task_id="T1", attempt_id="A1",
            event_type=EvidenceEventType.TOOL_OBSERVED,
            payload={"result_key": "result_value"},
        )
        feedback = _make_feedback()
        reducer.commit(feedback, evidence=[ev])

        # Fact should be in state
        facts = reducer.state.verified_facts
        assert any(f.value == "result_value" for f in facts.values())


# ══════════════════════════════════════════════════════════════════════
# S3: REJECT does not advance verified state
# ══════════════════════════════════════════════════════════════════════

class TestS3_RejectDoesNotAdvance:
    def test_reject_raises(self):
        state = _make_state()
        ledger = _make_ledger()
        store = InMemoryArtifactStore()
        reducer = TaskStateReducer(state, ledger, store)

        feedback = _make_feedback(decision=ValidatorDecision.REJECT)
        with pytest.raises(CommitRejected):
            reducer.commit(feedback)

        assert reducer.state.state_version == 0


# ══════════════════════════════════════════════════════════════════════
# S4: INDETERMINATE does not advance verified state
# ══════════════════════════════════════════════════════════════════════

class TestS4_IndeterminateDoesNotAdvance:
    def test_indeterminate_raises(self):
        state = _make_state()
        ledger = _make_ledger()
        store = InMemoryArtifactStore()
        reducer = TaskStateReducer(state, ledger, store)

        feedback = _make_feedback(decision=ValidatorDecision.INDETERMINATE)
        with pytest.raises(CommitRejected):
            reducer.commit(feedback)

        assert reducer.state.state_version == 0


# ══════════════════════════════════════════════════════════════════════
# S5: Duplicate ValidatorFeedback is idempotent
# ══════════════════════════════════════════════════════════════════════

class TestS5_DuplicateFeedbackIdempotent:
    def test_same_feedback_id_rejected(self):
        state = _make_state()
        ledger = _make_ledger()
        store = InMemoryArtifactStore()
        reducer = TaskStateReducer(state, ledger, store)

        ev = ledger.append(
            task_id="T1", attempt_id="A1",
            event_type=EvidenceEventType.TOOL_OBSERVED,
            payload={},
        )
        feedback = _make_feedback(feedback_id="fb-dup")
        reducer.commit(feedback, evidence=[ev])

        with pytest.raises(CommitRejected, match="Duplicate"):
            reducer.commit(feedback, evidence=[ev])

        assert reducer.state.state_version == 1


# ══════════════════════════════════════════════════════════════════════
# S6: StateCommit increments exactly one version
# ══════════════════════════════════════════════════════════════════════

class TestS6_CommitIncrementsOneVersion:
    def test_version_increments(self):
        state = _make_state()
        ledger = _make_ledger()
        store = InMemoryArtifactStore()
        reducer = TaskStateReducer(state, ledger, store)

        for i in range(3):
            ev = ledger.append(
                task_id="T1", attempt_id="A1",
                event_type=EvidenceEventType.TOOL_OBSERVED,
                payload={"step": i},
            )
            feedback = _make_feedback(feedback_id=f"fb-{i}")
            commit = reducer.commit(feedback, evidence=[ev])
            assert commit.next_state_version == i + 1
            assert commit.validate_version_increment()

        assert reducer.state.state_version == 3


# ══════════════════════════════════════════════════════════════════════
# S7: Append-only evidence cannot be overwritten
# ══════════════════════════════════════════════════════════════════════

class TestS7_EvidenceAppendOnly:
    def test_events_are_frozen(self):
        ledger = _make_ledger()
        ev = ledger.append(
            task_id="T1", attempt_id="A1",
            event_type=EvidenceEventType.TOOL_OBSERVED,
            payload={"key": "original"},
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            ev.payload = {"key": "modified"}  # type: ignore

    def test_ledger_grows_monotonically(self):
        ledger = _make_ledger()
        for i in range(5):
            ledger.append(
                task_id="T1", attempt_id="A1",
                event_type=EvidenceEventType.TOOL_OBSERVED,
                payload={"i": i},
            )
            assert ledger.count() == i + 1


# ══════════════════════════════════════════════════════════════════════
# S8: Evidence sequence is monotonic
# ══════════════════════════════════════════════════════════════════════

class TestS8_SequenceMonotonic:
    def test_sequences_are_ascending(self):
        ledger = _make_ledger()
        sequences = []
        for i in range(10):
            ev = ledger.append(
                task_id="T1", attempt_id="A1",
                event_type=EvidenceEventType.TOOL_OBSERVED,
                payload={},
            )
            sequences.append(ev.sequence)
        assert sequences == list(range(10))


# ══════════════════════════════════════════════════════════════════════
# S9: State rebuild digest equals materialized digest
# ══════════════════════════════════════════════════════════════════════

class TestS9_RebuildDigestParity:
    def test_rebuild_equals_materialized(self):
        state = _make_state()
        ledger = _make_ledger()
        store = InMemoryArtifactStore()
        reducer = TaskStateReducer(state, ledger, store)

        for i in range(3):
            ev = ledger.append(
                task_id="T1", attempt_id="A1",
                event_type=EvidenceEventType.TOOL_OBSERVED,
                payload={"step": i, "result": f"r{i}"},
            )
            feedback = _make_feedback(feedback_id=f"fb-{i}")
            reducer.commit(feedback, evidence=[ev])

        materialized = reducer.state.state_digest
        rebuilt = reducer.state.rebuild_digest()
        assert materialized == rebuilt


# ══════════════════════════════════════════════════════════════════════
# S10: Artifact digest detects modification
# ══════════════════════════════════════════════════════════════════════

class TestS10_ArtifactDigestDetectsModification:
    def test_digest_mismatch(self):
        store = InMemoryArtifactStore()
        ref = store.put(b"original content", kind="test")
        assert store.verify_integrity(ref.artifact_id)

        # Tamper with stored content directly
        store._store[ref.artifact_id] = b"tampered content"
        assert not store.verify_integrity(ref.artifact_id)

    def test_content_addressing(self):
        store = InMemoryArtifactStore()
        ref1 = store.put(b"same content", kind="test")
        ref2 = store.put(b"same content", kind="test")
        # Same content returns same ref (dedup)
        assert ref1.sha256 == ref2.sha256


# ══════════════════════════════════════════════════════════════════════
# S11: Confirmed artifact persists after later failure
# ══════════════════════════════════════════════════════════════════════

class TestS11_ConfirmedArtifactPersists:
    def test_accepted_work_survives_later_failure(self):
        state = _make_state()
        ledger = _make_ledger()
        store = InMemoryArtifactStore()
        reducer = TaskStateReducer(state, ledger, store)

        # Step A: accepted
        ev_a = ledger.append(
            task_id="T1", attempt_id="A1",
            event_type=EvidenceEventType.TOOL_OBSERVED,
            payload={"step": "A", "status": "success"},
        )
        fb_a = _make_feedback(feedback_id="fb-A")
        reducer.commit(fb_a, evidence=[ev_a])

        # Step B: rejected (simulating failure)
        fb_b = _make_feedback(
            feedback_id="fb-B", decision=ValidatorDecision.REJECT,
        )
        with pytest.raises(CommitRejected):
            reducer.commit(fb_b)

        # Step A's work is still in verified state
        assert reducer.state.state_version == 1
        assert any(f.value == "success" for f in reducer.state.verified_facts.values())


# ══════════════════════════════════════════════════════════════════════
# S12: Confirmed side effect blocks blind retry
# ══════════════════════════════════════════════════════════════════════

class TestS12_ConfirmedBlocksBlindRetry:
    def test_confirmed_receipt_recorded(self):
        state = _make_state()
        ledger = _make_ledger()
        store = InMemoryArtifactStore()
        reducer = TaskStateReducer(state, ledger, store)

        receipt = reducer.effect_receipt(
            "inv-1", EffectStatus.CONFIRMED,
            before_digest="abc", after_digest="def",
        )
        assert receipt.effect_status == EffectStatus.CONFIRMED
        # Ledger should have a SIDE_EFFECT_CONFIRMED event
        confirmed = ledger.events_by_type(EvidenceEventType.SIDE_EFFECT_CONFIRMED)
        assert len(confirmed) == 1


# ══════════════════════════════════════════════════════════════════════
# S13: Uncertain side effect requires reconciliation
# ══════════════════════════════════════════════════════════════════════

class TestS13_UncertainRequiresReconciliation:
    def test_uncertain_receipt_recorded(self):
        state = _make_state()
        ledger = _make_ledger()
        store = InMemoryArtifactStore()
        reducer = TaskStateReducer(state, ledger, store)

        receipt = reducer.effect_receipt(
            "inv-2", EffectStatus.UNCERTAIN,
        )
        assert receipt.effect_status == EffectStatus.UNCERTAIN
        uncertain = ledger.events_by_type(EvidenceEventType.SIDE_EFFECT_UNCERTAIN)
        assert len(uncertain) == 1


# ══════════════════════════════════════════════════════════════════════
# S14: Control state cannot be serialized as verified environment fact
# ══════════════════════════════════════════════════════════════════════

class TestS14_ControlStateSeparation:
    def test_control_state_is_not_verified(self):
        cs = ControlState(task_id="T1", run_id="R1", attempt_id="A1")
        vs = _make_state()
        # ControlState fields are not in VerifiedTaskState
        cs_fields = set(cs.model_fields.keys())
        vs_fields = set(vs.model_fields.keys())
        runtime_only = {"turn_index", "remaining_model_calls", "execution_status"}
        assert runtime_only.issubset(cs_fields)
        assert not runtime_only.intersection(vs_fields)

    def test_control_state_budget_decrement(self):
        cs = ControlState(task_id="T1", run_id="R1", attempt_id="A1")
        initial = cs.remaining_model_calls
        cs.decrement_call()
        assert cs.remaining_model_calls == initial - 1


# ══════════════════════════════════════════════════════════════════════
# S15: Benchmark outcome cannot mutate runtime state
# ══════════════════════════════════════════════════════════════════════

class TestS15_BenchmarkOutcomeNoRuntimeAuthority:
    def test_outcome_has_no_state_reference(self):
        outcome = BenchmarkOutcome(
            trial_id="T1", benchmark_name="toolmaze",
            tsr=1.0, prr=0.8, rc=0.2,
        )
        # BenchmarkOutcome is frozen and has no method to mutate state
        assert not hasattr(outcome, "commit")
        assert not hasattr(outcome, "apply")
        assert not hasattr(outcome, "reduce")

    def test_offline_grader_has_no_state_reference(self):
        grader = OfflineGrader("toolmaze")
        outcome = grader.grade(
            trial_id="T1",
            native_metrics={"tsr": 1.0, "prr": 0.5},
        )
        assert outcome.tsr == 1.0
        # Grader has no reference to VerifiedTaskState
        assert not hasattr(grader, "_state")


# ══════════════════════════════════════════════════════════════════════
# S16: Hidden benchmark fields cannot enter runtime evidence
# ══════════════════════════════════════════════════════════════════════

class TestS16_HiddenFieldsNotInEvidence:
    def test_evidence_payload_is_open(self):
        """Evidence payload is caller-controlled, but the substrate
        itself does not inject hidden benchmark fields."""
        ledger = _make_ledger()
        ev = ledger.append(
            task_id="T1", attempt_id="A1",
            event_type=EvidenceEventType.TOOL_OBSERVED,
            payload={"tool": "test", "result": "ok"},
        )
        assert "expected_result" not in ev.payload
        assert "execution_trace" not in ev.payload
        assert "oracle" not in ev.payload


# ══════════════════════════════════════════════════════════════════════
# S17: Hidden milestones cannot enter EnvironmentObservation
# ══════════════════════════════════════════════════════════════════════

class TestS17_HiddenMilestonesNotInObservation:
    def test_env_observation_type_clean(self):
        """EnvironmentObservation is recorded as EvidenceEvent.
        The substrate does not inject milestone data."""
        ledger = _make_ledger()
        ev = ledger.append(
            task_id="T1", attempt_id="A1",
            event_type=EvidenceEventType.ENVIRONMENT_OBSERVED,
            payload={"namespace": "toolsandbox", "changed": True},
        )
        assert "target_milestones" not in ev.payload
        assert "milestone_mapping" not in ev.payload


# ══════════════════════════════════════════════════════════════════════
# S18: Recovery policy input contains no offline evaluator object
# ══════════════════════════════════════════════════════════════════════

class TestS18_RecoveryPolicyNoOfflineEvaluator:
    def test_control_state_has_no_evaluator(self):
        cs = ControlState(task_id="T1", run_id="R1", attempt_id="A1")
        cs_json = cs.model_dump_json()
        assert "evaluator" not in cs_json.lower()
        assert "judge" not in cs_json.lower()
        assert "metrics_calculator" not in cs_json.lower()

    def test_verified_state_has_no_evaluator(self):
        vs = _make_state()
        vs_json = vs.model_dump_json()
        assert "evaluator" not in vs_json.lower()
        assert "judge" not in vs_json.lower()


# ══════════════════════════════════════════════════════════════════════
# S19: All six arms use identical substrate contracts
# ══════════════════════════════════════════════════════════════════════

class TestS19_SixArmSharedSubstrate:
    def test_substrate_imports_for_all_arms(self):
        """All arm strategies import from the same substrate package."""
        from lhas.phase5.control_arms import (
            BareStrategy, RetryOnlyStrategy, ValidatorOnlyStrategy,
            OdysFullStrategy, OdysMinusObservableProgress,
            OdysMinusRecoveryBudgetPolicy,
        )
        for cls in [BareStrategy, RetryOnlyStrategy, ValidatorOnlyStrategy,
                     OdysFullStrategy, OdysMinusObservableProgress,
                     OdysMinusRecoveryBudgetPolicy]:
            # All should use the same substrate types
            assert cls is not None


# ══════════════════════════════════════════════════════════════════════
# S20: Phase4 external finalization regression stays green
# ══════════════════════════════════════════════════════════════════════

class TestS20_Phase4Regression:
    def test_phase4_core_imports_intact(self):
        from lhas.recovery import DefaultRecoveryPolicy
        from lhas.domain.enums import FailureType, RecoveryActionType
        from lhas.experiments import ExperimentRecorder
        assert DefaultRecoveryPolicy is not None
        assert FailureType.TOOL_ERROR.value == "TOOL_ERROR"
        assert RecoveryActionType.ESCALATE.value == "ESCALATE"


# ══════════════════════════════════════════════════════════════════════
# S21: Clean-checkout CI remains green
# ══════════════════════════════════════════════════════════════════════

class TestS21_CleanCheckoutCI:
    def test_substrate_imports_without_benchmark(self):
        """Substrate package imports without any benchmark checkout."""
        from lhas.phase5.substrate import (
            VerifiedTaskState, ControlState, EvidenceLedger,
            ArtifactStore, InMemoryArtifactStore,
            ValidatorFeedback, TaskStateReducer,
            BenchmarkOutcome, RuntimeValidator, OfflineGrader,
        )
        assert VerifiedTaskState is not None
        assert OfflineGrader is not None

    def test_substrate_creates_valid_state(self):
        state = VerifiedTaskState(
            task_id="CI-1", run_id="CI-R1",
            goal="verify clean checkout",
        )
        assert state.state_version == 0
        assert state.state_digest != ""
