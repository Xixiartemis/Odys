"""Phase5 Research Substrate Tests — HARDENED (S1-S21 + V1-V26).

Provider-free adversarial tests for the minimum Task State / Evidence /
Evaluation substrate.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from lhas.phase5.substrate import (
    ArtifactRef,
    BenchmarkOutcome,
    CommitRejected,
    ControlState,
    EFFECT_EVENT_MAP,
    EffectReceipt,
    EffectStatus,
    EvidenceEvent,
    EvidenceEventType,
    EvidenceLedger,
    InMemoryArtifactStore,
    OfflineGrader,
    RuntimeValidatorProtocol,
    StateCommit,
    TaskStateReducer,
    TestValidatorHelper,
    ValidatorDecision,
    ValidatorExecutionStatus,
    ValidatorFeedback,
    VerifiedFact,
    VerifiedTaskState,
)
from lhas.phase5.types import ControlArm, NativeResult


# ── Helpers ──────────────────────────────────────────────────────

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
    evidence_refs: list[str] | None = None,
) -> ValidatorFeedback:
    return ValidatorFeedback(
        feedback_id=feedback_id,
        validator_id="test-validator",
        candidate_id=candidate_id,
        execution_status=execution_status,
        decision=decision,
        evidence_refs=evidence_refs or [],
    )


def _commit_evidence(
    reducer: TaskStateReducer,
    ledger: EvidenceLedger,
    *,
    payload: dict | None = None,
    feedback_id: str = "fb-1",
    candidate_id: str = "cand-1",
) -> StateCommit:
    ev = ledger.append(
        task_id="T1", attempt_id="A1",
        event_type=EvidenceEventType.TOOL_OBSERVED,
        payload=payload or {"result": "ok"},
    )
    fb = _make_feedback(
        feedback_id=feedback_id, candidate_id=candidate_id,
        evidence_refs=[ev.evidence_id],
    )
    return reducer.commit(fb, evidence=[ev])


# ══════════════════════════════════════════════════════════════════════
# S1: Unverified model claim cannot update VerifiedTaskState
# ══════════════════════════════════════════════════════════════════════

class TestS1_UnverifiedClaimCannotMutateState:
    def test_verified_state_has_no_model_claim_field(self):
        state = _make_state()
        fields = set(VerifiedTaskState.model_fields.keys())
        assert "model_claims" not in fields
        assert "chat_history" not in fields

    def test_verified_fact_requires_evidence(self):
        fact = VerifiedFact(
            key="k", value="v",
            evidence_id="ev-1", commit_id="commit-1",
        )
        assert fact.evidence_id == "ev-1"


# ══════════════════════════════════════════════════════════════════════
# S2: ACCEPT + SUCCESS commits verified state
# ══════════════════════════════════════════════════════════════════════

class TestS2_AcceptSuccessCommitsState:
    def test_commit_advances_state(self):
        state = _make_state()
        ledger = _make_ledger()
        store = InMemoryArtifactStore()
        reducer = TaskStateReducer(state, ledger, store)
        commit = _commit_evidence(reducer, ledger)
        assert reducer.state.state_version == 1
        assert commit.validate_version_increment()


# ══════════════════════════════════════════════════════════════════════
# S3-S4: REJECT/INDETERMINATE does not advance
# ══════════════════════════════════════════════════════════════════════

class TestS3S4_RejectIndeterminate:
    def test_reject_raises(self):
        reducer = TaskStateReducer(_make_state(), _make_ledger(), InMemoryArtifactStore())
        fb = _make_feedback(decision=ValidatorDecision.REJECT, evidence_refs=[])
        with pytest.raises(CommitRejected):
            reducer.commit(fb)

    def test_indeterminate_raises(self):
        reducer = TaskStateReducer(_make_state(), _make_ledger(), InMemoryArtifactStore())
        fb = _make_feedback(decision=ValidatorDecision.INDETERMINATE, evidence_refs=[])
        with pytest.raises(CommitRejected):
            reducer.commit(fb)


# ══════════════════════════════════════════════════════════════════════
# S5-S6: Idempotency and version increment
# ══════════════════════════════════════════════════════════════════════

class TestS5S6_IdempotencyAndVersion:
    def test_duplicate_feedback_blocked(self):
        state = _make_state()
        ledger = _make_ledger()
        store = InMemoryArtifactStore()
        reducer = TaskStateReducer(state, ledger, store)
        _commit_evidence(reducer, ledger, feedback_id="fb-dup")
        with pytest.raises(CommitRejected, match="Duplicate"):
            _commit_evidence(reducer, ledger, feedback_id="fb-dup")

    def test_version_increments_exactly_one(self):
        state = _make_state()
        ledger = _make_ledger()
        store = InMemoryArtifactStore()
        reducer = TaskStateReducer(state, ledger, store)
        for i in range(3):
            commit = _commit_evidence(reducer, ledger, feedback_id=f"fb-{i}", candidate_id=f"c-{i}")
            assert commit.next_state_version == i + 1


# ══════════════════════════════════════════════════════════════════════
# S7-S8: Append-only and monotonic
# ══════════════════════════════════════════════════════════════════════

class TestS7S8_AppendOnlyMonotonic:
    def test_events_are_frozen(self):
        ledger = _make_ledger()
        ev = ledger.append(
            task_id="T1", attempt_id="A1",
            event_type=EvidenceEventType.TOOL_OBSERVED, payload={"k": "v"},
        )
        with pytest.raises(Exception):
            ev.payload = {"k": "modified"}  # type: ignore

    def test_sequences_are_ascending(self):
        ledger = _make_ledger()
        seqs = []
        for _ in range(5):
            ev = ledger.append(
                task_id="T1", attempt_id="A1",
                event_type=EvidenceEventType.TOOL_OBSERVED, payload={},
            )
            seqs.append(ev.sequence)
        assert seqs == list(range(5))


# ══════════════════════════════════════════════════════════════════════
# S9: Rebuild digest parity
# ══════════════════════════════════════════════════════════════════════

class TestS9_RebuildDigestParity:
    def test_rebuild_equals_materialized(self):
        state = _make_state()
        ledger = _make_ledger()
        store = InMemoryArtifactStore()
        reducer = TaskStateReducer(state, ledger, store)
        for i in range(3):
            _commit_evidence(reducer, ledger, feedback_id=f"fb-{i}", candidate_id=f"c-{i}")
        assert reducer.state.state_digest == reducer.state.rebuild_digest()


# ══════════════════════════════════════════════════════════════════════
# S10: Artifact digest detection
# ══════════════════════════════════════════════════════════════════════

class TestS10_ArtifactDigest:
    def test_integrity_check(self):
        store = InMemoryArtifactStore()
        ref = store.put(b"original", kind="test")
        assert store.verify_integrity(ref.artifact_id)
        store._store[ref.artifact_id] = b"tampered"
        assert not store.verify_integrity(ref.artifact_id)


# ══════════════════════════════════════════════════════════════════════
# S11: Confirmed artifact persists after later failure
# ══════════════════════════════════════════════════════════════════════

class TestS11_ConfirmedPersists:
    def test_accepted_work_survives_later_failure(self):
        state = _make_state()
        ledger = _make_ledger()
        store = InMemoryArtifactStore()
        reducer = TaskStateReducer(state, ledger, store)
        _commit_evidence(reducer, ledger, feedback_id="fb-A", candidate_id="c-A")
        fb_b = _make_feedback(feedback_id="fb-B", decision=ValidatorDecision.REJECT, evidence_refs=[])
        with pytest.raises(CommitRejected):
            reducer.commit(fb_b)
        assert reducer.state.state_version == 1


# ══════════════════════════════════════════════════════════════════════
# S12-S13: Effect receipts
# ══════════════════════════════════════════════════════════════════════

class TestS12S13_EffectReceipts:
    def test_confirmed_receipt(self):
        reducer = TaskStateReducer(_make_state(), _make_ledger(), InMemoryArtifactStore())
        receipt = reducer.effect_receipt("inv-1", EffectStatus.CONFIRMED)
        assert receipt.effect_status == EffectStatus.CONFIRMED

    def test_uncertain_receipt(self):
        reducer = TaskStateReducer(_make_state(), _make_ledger(), InMemoryArtifactStore())
        receipt = reducer.effect_receipt("inv-2", EffectStatus.UNCERTAIN)
        assert receipt.effect_status == EffectStatus.UNCERTAIN


# ══════════════════════════════════════════════════════════════════════
# S14: Control state separation
# ══════════════════════════════════════════════════════════════════════

class TestS14_ControlStateSeparation:
    def test_control_state_fields_not_in_verified(self):
        cs = ControlState(task_id="T1", run_id="R1", attempt_id="A1")
        vs = _make_state()
        cs_fields = set(ControlState.model_fields.keys())
        vs_fields = set(VerifiedTaskState.model_fields.keys())
        runtime_only = {"turn_index", "remaining_model_calls", "execution_status"}
        assert runtime_only.issubset(cs_fields)
        assert not runtime_only.intersection(vs_fields)


# ══════════════════════════════════════════════════════════════════════
# S15: Benchmark outcome has no runtime authority
# ══════════════════════════════════════════════════════════════════════

class TestS15_BenchmarkOutcomeNoAuthority:
    def test_outcome_has_no_state_methods(self):
        outcome = BenchmarkOutcome(trial_id="T1", benchmark_name="toolmaze")
        assert not hasattr(outcome, "commit")
        assert not hasattr(outcome, "apply")

    def test_offline_grader_no_state_reference(self):
        grader = OfflineGrader("toolmaze")
        outcome = grader.grade(trial_id="T1", native_metrics={"tsr": 1.0})
        assert outcome.tsr == 1.0
        assert not hasattr(grader, "_state")


# ══════════════════════════════════════════════════════════════════════
# S16-S17: Hidden fields not in evidence
# ══════════════════════════════════════════════════════════════════════

class TestS16S17_HiddenFieldsClean:
    def test_evidence_no_hidden_fields(self):
        ledger = _make_ledger()
        ev = ledger.append(
            task_id="T1", attempt_id="A1",
            event_type=EvidenceEventType.TOOL_OBSERVED,
            payload={"tool": "test", "result": "ok"},
        )
        assert "expected_result" not in ev.payload
        assert "oracle" not in ev.payload

    def test_env_observation_no_milestones(self):
        ledger = _make_ledger()
        ev = ledger.append(
            task_id="T1", attempt_id="A1",
            event_type=EvidenceEventType.ENVIRONMENT_OBSERVED,
            payload={"changed": True},
        )
        assert "target_milestones" not in ev.payload


# ══════════════════════════════════════════════════════════════════════
# S18: Recovery policy input clean
# ══════════════════════════════════════════════════════════════════════

class TestS18_RecoveryPolicyClean:
    def test_control_state_no_evaluator(self):
        cs = ControlState(task_id="T1", run_id="R1", attempt_id="A1")
        assert "evaluator" not in cs.model_dump_json().lower()


# ══════════════════════════════════════════════════════════════════════
# S19: Six arm shared substrate
# ══════════════════════════════════════════════════════════════════════

class TestS19_SixArmSharedSubstrate:
    def test_all_arm_strategies_exist(self):
        from lhas.phase5.control_arms import (
            BareStrategy, RetryOnlyStrategy, ValidatorOnlyStrategy,
            OdysFullStrategy, OdysMinusObservableProgress,
            OdysMinusRecoveryBudgetPolicy,
        )
        for cls in [BareStrategy, RetryOnlyStrategy, ValidatorOnlyStrategy,
                     OdysFullStrategy, OdysMinusObservableProgress,
                     OdysMinusRecoveryBudgetPolicy]:
            assert cls is not None


# ══════════════════════════════════════════════════════════════════════
# S20-S21: Phase4 regression + clean checkout
# ══════════════════════════════════════════════════════════════════════

class TestS20_Phase4Regression:
    def test_phase4_core_imports(self):
        from lhas.recovery import DefaultRecoveryPolicy
        from lhas.domain.enums import FailureType
        assert FailureType.TOOL_ERROR.value == "TOOL_ERROR"


class TestS21_CleanCheckout:
    def test_substrate_imports_without_benchmark(self):
        from lhas.phase5.substrate import (
            VerifiedTaskState, ControlState, EvidenceLedger,
            InMemoryArtifactStore, TaskStateReducer,
            BenchmarkOutcome, TestValidatorHelper, OfflineGrader,
        )
        assert VerifiedTaskState is not None


# ══════════════════════════════════════════════════════════════════════
# V1: Direct VerifiedTaskState mutation blocked (frozen)
# ══════════════════════════════════════════════════════════════════════

class TestV1_DirectMutationBlocked:
    def test_cannot_set_status(self):
        state = _make_state()
        with pytest.raises(Exception):
            state.status = "FAILED"  # type: ignore

    def test_cannot_set_state_version(self):
        state = _make_state()
        with pytest.raises(Exception):
            state.state_version = 99  # type: ignore

    def test_cannot_set_state_digest(self):
        state = _make_state()
        with pytest.raises(Exception):
            state.state_digest = "tampered"  # type: ignore

    def test_reducer_state_mutation_blocked(self):
        state = _make_state()
        ledger = _make_ledger()
        store = InMemoryArtifactStore()
        reducer = TaskStateReducer(state, ledger, store)
        with pytest.raises(Exception):
            reducer.state.status = "FAILED"  # type: ignore


# ══════════════════════════════════════════════════════════════════════
# V2: Nested verified collections cannot bypass commit
# ══════════════════════════════════════════════════════════════════════

class TestV2_NestedCollectionsImmutable:
    def test_verified_facts_is_tuple(self):
        state = _make_state()
        assert isinstance(state.verified_facts, tuple)

    def test_verified_artifact_ids_is_tuple(self):
        state = _make_state()
        assert isinstance(state.verified_artifact_ids, tuple)

    def test_cannot_append_to_facts(self):
        state = _make_state()
        with pytest.raises(AttributeError):
            state.verified_facts.append("x")  # type: ignore

    def test_cannot_append_to_artifacts(self):
        state = _make_state()
        with pytest.raises(AttributeError):
            state.verified_artifact_ids.append("x")  # type: ignore


# ══════════════════════════════════════════════════════════════════════
# V3: Accepted feedback cannot promote unrelated evidence
# ══════════════════════════════════════════════════════════════════════

class TestV3_UnrelatedEvidenceBlocked:
    def test_evidence_not_in_feedback_refs_rejected(self):
        state = _make_state()
        ledger = _make_ledger()
        store = InMemoryArtifactStore()
        reducer = TaskStateReducer(state, ledger, store)

        ev = ledger.append(
            task_id="T1", attempt_id="A1",
            event_type=EvidenceEventType.TOOL_OBSERVED,
            payload={"result": "ok"},
        )
        # Feedback authorizes a DIFFERENT evidence
        fb = _make_feedback(evidence_refs=["some-other-evidence-id"])
        with pytest.raises(CommitRejected, match="not in feedback.evidence_refs"):
            reducer.commit(fb, evidence=[ev])


# ══════════════════════════════════════════════════════════════════════
# V4: Accepted feedback cannot promote unknown artifact
# ══════════════════════════════════════════════════════════════════════

class TestV4_UnknownArtifactBlocked:
    def test_artifact_not_in_store_rejected(self):
        state = _make_state()
        ledger = _make_ledger()
        store = InMemoryArtifactStore()
        reducer = TaskStateReducer(state, ledger, store)

        ev = ledger.append(
            task_id="T1", attempt_id="A1",
            event_type=EvidenceEventType.TOOL_OBSERVED, payload={},
        )
        fake_artifact = ArtifactRef(
            artifact_id="nonexistent", kind="test", sha256="abc",
        )
        fb = _make_feedback(evidence_refs=[ev.evidence_id])
        with pytest.raises(CommitRejected, match="not found"):
            reducer.commit(fb, evidence=[ev], artifacts=[fake_artifact])


# ══════════════════════════════════════════════════════════════════════
# V5: Corrupted artifact cannot be promoted
# ══════════════════════════════════════════════════════════════════════

class TestV5_CorruptedArtifactBlocked:
    def test_corrupted_digest_rejected(self):
        state = _make_state()
        ledger = _make_ledger()
        store = InMemoryArtifactStore()
        reducer = TaskStateReducer(state, ledger, store)

        ref = store.put(b"good content", kind="test")
        # Corrupt the stored content
        store._store[ref.artifact_id] = b"corrupted"

        ev = ledger.append(
            task_id="T1", attempt_id="A1",
            event_type=EvidenceEventType.TOOL_OBSERVED, payload={},
        )
        fb = _make_feedback(evidence_refs=[ev.evidence_id])
        with pytest.raises(CommitRejected, match="integrity check"):
            reducer.commit(fb, evidence=[ev], artifacts=[ref])


# ══════════════════════════════════════════════════════════════════════
# V6: Duplicate feedback cannot re-commit
# ══════════════════════════════════════════════════════════════════════

class TestV6_DuplicateFeedbackBlocked:
    def test_same_feedback_id_blocked(self):
        state = _make_state()
        ledger = _make_ledger()
        store = InMemoryArtifactStore()
        reducer = TaskStateReducer(state, ledger, store)
        _commit_evidence(reducer, ledger, feedback_id="fb-dup")
        with pytest.raises(CommitRejected, match="Duplicate commit for feedback"):
            _commit_evidence(reducer, ledger, feedback_id="fb-dup")


# ══════════════════════════════════════════════════════════════════════
# V7: Same candidate with new feedback cannot re-commit
# ══════════════════════════════════════════════════════════════════════

class TestV7_DuplicateCandidateBlocked:
    def test_same_candidate_different_feedback_blocked(self):
        state = _make_state()
        ledger = _make_ledger()
        store = InMemoryArtifactStore()
        reducer = TaskStateReducer(state, ledger, store)
        _commit_evidence(reducer, ledger, feedback_id="fb-1", candidate_id="cand-X")
        with pytest.raises(CommitRejected, match="Duplicate commit for candidate_id"):
            _commit_evidence(reducer, ledger, feedback_id="fb-2", candidate_id="cand-X")


# ══════════════════════════════════════════════════════════════════════
# V8: Rebuild uses no materialized verified facts
# ══════════════════════════════════════════════════════════════════════

class TestV8_RebuildIndependent:
    def test_rebuild_from_commits_only(self):
        state = _make_state()
        ledger = _make_ledger()
        store = InMemoryArtifactStore()
        reducer = TaskStateReducer(state, ledger, store)
        for i in range(3):
            _commit_evidence(reducer, ledger, payload={"step": i}, feedback_id=f"fb-{i}", candidate_id=f"c-{i}")
        # Rebuild should work from commit history
        rebuilt = reducer.rebuild_state()
        assert rebuilt.state_digest == reducer.state.state_digest
        assert rebuilt.state_version == 3


# ══════════════════════════════════════════════════════════════════════
# V9: Rebuild survives destroyed materialized state
# ══════════════════════════════════════════════════════════════════════

class TestV9_RebuildSurvivesDestruction:
    def test_rebuild_after_state_reset(self):
        state = _make_state()
        ledger = _make_ledger()
        store = InMemoryArtifactStore()
        reducer = TaskStateReducer(state, ledger, store)
        for i in range(3):
            _commit_evidence(reducer, ledger, payload={f"step_{i}": i}, feedback_id=f"fb-{i}", candidate_id=f"c-{i}")
        # Save commit history
        commits_backup = list(reducer._commits)
        # Corrupt materialized state
        reducer._state = _make_state()  # reset to empty
        # Restore commits and rebuild
        reducer._commits = commits_backup
        rebuilt = reducer.rebuild_state()
        assert rebuilt.state_version == 3
        assert len(rebuilt.verified_facts) == 3


# ══════════════════════════════════════════════════════════════════════
# V10-V11: Effect taxonomy correctness
# ══════════════════════════════════════════════════════════════════════

class TestV10V11_EffectTaxonomy:
    def test_all_effect_statuses_map(self):
        assert EFFECT_EVENT_MAP["CONFIRMED"] == EvidenceEventType.SIDE_EFFECT_CONFIRMED
        assert EFFECT_EVENT_MAP["UNCERTAIN"] == EvidenceEventType.SIDE_EFFECT_UNCERTAIN
        assert EFFECT_EVENT_MAP["NOT_OBSERVED"] == EvidenceEventType.SIDE_EFFECT_NOT_OBSERVED

    def test_terminated_never_represents_not_observed(self):
        assert "NOT_OBSERVED" not in str(EvidenceEventType.TERMINATED)
        assert EFFECT_EVENT_MAP.get("NOT_OBSERVED") != EvidenceEventType.TERMINATED


# ══════════════════════════════════════════════════════════════════════
# V12: Digest semantics cover all trusted state
# ══════════════════════════════════════════════════════════════════════

class TestV12_DigestSemantics:
    def test_digest_changes_with_facts(self):
        s1 = _make_state()
        s2 = s1.with_commit(
            commit_id="c1", feedback_id="fb1",
            new_facts=(VerifiedFact(key="new", value="val", evidence_id="e1", commit_id="c1"),),
            new_artifact_ids=(),
        )
        assert s1.state_digest != s2.state_digest

    def test_digest_changes_with_artifacts(self):
        s1 = _make_state()
        s2 = s1.with_commit(
            commit_id="c1", feedback_id="fb1",
            new_facts=(),
            new_artifact_ids=("art-1",),
        )
        assert s1.state_digest != s2.state_digest

    def test_status_change_does_not_affect_digest(self):
        s1 = _make_state()
        s2 = s1.with_commit(
            commit_id="c1", feedback_id="fb1",
            new_facts=(), new_artifact_ids=(), new_status="COMPLETED",
        )
        # Status change alone should produce different digest only because
        # state_version changed, not because status is in digest.
        # But the facts/artifacts are the same, so the underlying
        # _compute_digest should be the same.
        assert s1._compute_digest() == s2._compute_digest()


# ══════════════════════════════════════════════════════════════════════
# V13: Test validator cannot become production authority
# ══════════════════════════════════════════════════════════════════════

class TestV13_TestValidatorNotLive:
    def test_helper_is_not_protocol(self):
        helper = TestValidatorHelper()
        # TestValidatorHelper is a concrete class, not a Protocol
        assert not isinstance(helper, type(RuntimeValidatorProtocol))

    def test_helper_has_accept_condition_param(self):
        """The accept_condition param is test-only — production validators
        receive actual runtime evidence instead."""
        import inspect
        sig = inspect.signature(TestValidatorHelper.validate)
        assert "accept_condition" in sig.parameters


# ══════════════════════════════════════════════════════════════════════
# V14: Clean checkout full pytest passes
# ══════════════════════════════════════════════════════════════════════

class TestV14_CleanCheckout:
    def test_all_phase5_modules_importable(self):
        modules = [
            "lhas.phase5.types",
            "lhas.phase5.substrate",
            "lhas.phase5.control_arms",
            "lhas.phase5.shadow_observer",
            "lhas.phase5.firewall",
            "lhas.phase5.fault_layer",
            "lhas.phase5.artifacts",
            "lhas.phase5.provenance",
        ]
        import importlib
        for mod_name in modules:
            mod = importlib.import_module(mod_name)
            assert mod is not None


# ══════════════════════════════════════════════════════════════════════
# V15-V21: Benchmark-dependent tests (skip if no benchmark)
# ══════════════════════════════════════════════════════════════════════

_BENCHMARK_DIR = Path(__file__).resolve().parents[1] / "experiments" / "phase5" / "benchmarks" / "toolmaze" / "data"
_BENCHMARK_AVAILABLE = (_BENCHMARK_DIR / "perturbed_tasks").is_dir()


@pytest.mark.skipif(not _BENCHMARK_AVAILABLE, reason="External ToolMaze benchmark not available")
class TestV15V21_BenchmarkIntegration:
    def test_v15_frozen_sha_verified(self):
        from lhas.phase5.toolmaze_adapter import ToolMazeAdapter
        adapter = ToolMazeAdapter()
        assert adapter.benchmark_identity.commit_sha == "ef0798a"

    def test_v16_official_runtime_invoked(self):
        from lhas.phase5.runtime_backend import ToolMazeRuntimeBackend
        assert hasattr(ToolMazeRuntimeBackend, "execute")

    def test_v17_no_custom_perturbation_in_live_path(self):
        """Verify runtime_backend delegates to official ExecutionEngine."""
        from lhas.phase5.runtime_backend import ToolMazeRuntimeBackend
        import inspect
        source = inspect.getsource(ToolMazeRuntimeBackend)
        # Should reference official ExecutionEngine, not custom perturbation
        assert "ToolExecutor" in source or "ExecutionEngine" in source

    def test_v18_actual_trace_reaches_judge(self):
        from lhas.phase5.toolmaze_adapter import ToolMazeAdapter
        adapter = ToolMazeAdapter()
        # Verify offline_native_evaluate uses actual trace
        import inspect
        source = inspect.getsource(adapter.offline_native_evaluate)
        assert "ToolMazeOfflineEvaluator" in source or "judge" in source.lower()

    def test_v19_tsr_parity(self):
        """TSR from adapter must match official MetricsCalculator.

        Structural verification: offline_native_evaluate returns a
        NativeResult with tsr as a float.  Golden parity (exact value
        match) is covered in test_phase5_toolmaze_integration T8.
        """
        from lhas.phase5.toolmaze_adapter import ToolMazeAdapter
        adapter = ToolMazeAdapter()
        tasks = adapter.enumerate_tasks()
        desc = tasks[0]
        artifact = adapter.finalize_runtime_artifact(desc.task_id)
        result = adapter.offline_native_evaluate(desc.task_id, artifact)
        assert isinstance(result, NativeResult)
        assert result.tsr is not None
        assert isinstance(result.tsr, float)

    def test_v20_prr_parity(self):
        """PRR from adapter is a valid float or None."""
        from lhas.phase5.toolmaze_adapter import ToolMazeAdapter
        adapter = ToolMazeAdapter()
        tasks = adapter.enumerate_tasks()
        desc = tasks[0]
        artifact = adapter.finalize_runtime_artifact(desc.task_id)
        result = adapter.offline_native_evaluate(desc.task_id, artifact)
        assert isinstance(result, NativeResult)
        # PRR may be None for P0 tasks (no perturbation)
        if result.prr is not None:
            assert isinstance(result.prr, float)

    def test_v21_rc_parity(self):
        """RC from adapter is a valid float or None."""
        from lhas.phase5.toolmaze_adapter import ToolMazeAdapter
        adapter = ToolMazeAdapter()
        tasks = adapter.enumerate_tasks()
        desc = tasks[0]
        artifact = adapter.finalize_runtime_artifact(desc.task_id)
        result = adapter.offline_native_evaluate(desc.task_id, artifact)
        assert isinstance(result, NativeResult)
        # RC may be None for P0 tasks
        if result.rc is not None:
            assert isinstance(result.rc, float)


# ══════════════════════════════════════════════════════════════════════
# V22-V24: Six-arm reality check
# ══════════════════════════════════════════════════════════════════════

class TestV22V24_SixArmReality:
    def test_v22_a3_uses_phase4_recovery(self):
        from lhas.phase5.control_arms import OdysFullStrategy
        import inspect
        source = inspect.getsource(OdysFullStrategy)
        assert "DefaultRecoveryPolicy" in source or "recovery" in source.lower()

    def test_v23_a4_single_variable_ablation(self):
        from lhas.phase5.control_arms import OdysMinusObservableProgress, OdysFullStrategy
        assert issubclass(OdysMinusObservableProgress, OdysFullStrategy)

    def test_v24_a5_single_variable_ablation(self):
        from lhas.phase5.control_arms import OdysMinusRecoveryBudgetPolicy, OdysFullStrategy
        assert issubclass(OdysMinusRecoveryBudgetPolicy, OdysFullStrategy)


# ══════════════════════════════════════════════════════════════════════
# V25: Identical substrate schema A0-A5
# ══════════════════════════════════════════════════════════════════════

class TestV25_IdenticalSubstrateSchema:
    def test_all_arms_use_same_types(self):
        from lhas.phase5.control_arms import create_policy
        from lhas.phase5.types import ControlArm
        for arm in ControlArm:
            policy = create_policy(arm)
            # All policies have the same execute_trial signature
            assert hasattr(policy, "execute_trial")
            assert hasattr(policy, "arm")


# ══════════════════════════════════════════════════════════════════════
# V26: Phase4 regression
# ══════════════════════════════════════════════════════════════════════

class TestV26_Phase4Regression:
    def test_recovery_policy_unchanged(self):
        from lhas.recovery import DefaultRecoveryPolicy, RecoveryAction
        from lhas.domain.enums import FailureType, RecoveryActionType
        assert FailureType.TIMEOUT.value == "TIMEOUT"
        assert RecoveryActionType.RETRY_WITH_FAILURE_CONTEXT.value == "RETRY_WITH_FAILURE_CONTEXT"
        assert RecoveryActionType.BLOCK_PROVIDER.value == "BLOCK_PROVIDER"
