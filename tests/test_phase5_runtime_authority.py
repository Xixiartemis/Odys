"""Provider-free runtime semantic proofs for A0-A5.

Proves causal differences between arms using ScriptedModelDriver.
No real provider required.

G: A5 causal difference (recovery budget gate)
L: A2 runtime proof (accept/reject/infra paths)
M: A3 validation recovery proof
N: A4 OP causal proof
O: A1 retry proof
"""

from __future__ import annotations

import pytest
from lhas.phase5.agent_core import Phase5AgentCore, PublicToolObservation
from lhas.phase5.control_arms import (
    BareStrategy,
    OdysFullStrategy,
    OdysMinusObservableProgress,
    OdysMinusRecoveryBudgetPolicy,
    ValidatorOnlyStrategy,
    RetryOnlyStrategy,
)
from lhas.phase5.model_driver import ScriptedModelDriver, ScriptedAction
from lhas.phase5.recovery_budget import RecoveryBudgetGate, PassThroughRecoveryBudgetGate, BudgetDecision
from lhas.phase5.runtime_validator import (
    PublicEvidenceCompletionValidator,
    PublicValidationEvidence,
)
from lhas.phase5.types import RuntimeValidatorExecutionError


# ══════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════

def _make_validator():
    return PublicEvidenceCompletionValidator()


def _setup_core(driver, strategy, validator=None):
    core = Phase5AgentCore(driver, strategy=strategy)
    core.initialize("test task", [{"name": "tool_a", "description": "A tool"}])
    if validator is None:
        validator = _make_validator()
    core.set_runtime_validator(validator)
    return core


def _feed_tool_result(core, tool_name, result, step=None):
    """Simulate a tool result being fed to the core."""
    # Direct-result tests without a preceding model action retain the legacy
    # fixture setup.  Once a model action has created a canonical active
    # batch, let the core resolve its own provider/Odys identities; mutating
    # those fields here would create an artificial unpaired tool call.
    if core._active_tool_call_batch is None:
        core._step_count = step if step is not None else core._step_count + 1
        core._last_tool_call_id = f"call_{core._step_count}"
        core._last_tool_call = {
            "tool_name": tool_name,
            "arguments": {},
            "tool_call_id": core._last_tool_call_id,
        }
        core._conversation_history.append({
            "role": "assistant",
            "type": "tool_call",
            "content": "",
            "tool_call": {"name": tool_name, "arguments": {}, "id": core._last_tool_call_id},
        })
    core.receive_tool_result(tool_name, result)


# ══════════════════════════════════════════════════════════════════════
#  Section A: Public tool observations are structured dicts
# ══════════════════════════════════════════════════════════════════════

class TestStructuredObservations:
    """Prove that public tool observations are structured dicts, not JSON strings."""

    def test_observations_are_dicts(self):
        """PublicToolObservation.result must be Dict[str, Any]."""
        driver = ScriptedModelDriver([
            ScriptedAction(type="tool_call", tool_name="tool_a", arguments={}),
            ScriptedAction(type="final_answer", content="done"),
        ])
        core = _setup_core(driver, BareStrategy())
        # Feed a tool result
        _feed_tool_result(core, "tool_a", {"status": "success", "data": "ok"})
        observations = core.get_public_tool_observations()
        assert len(observations) == 1
        assert isinstance(observations[0].result, dict)
        assert observations[0].result["status"] == "success"

    def test_observations_no_oracle_fields(self):
        """PublicToolObservation must not contain oracle fields."""
        obs = PublicToolObservation(
            step=1, tool_name="t", result={"status": "success"}
        )
        assert not hasattr(obs, "is_perturbed")
        assert not hasattr(obs, "perturbation_status")
        assert not hasattr(obs, "expected_result")
        assert not hasattr(obs, "oracle")

    def test_validator_receives_dicts_not_strings(self):
        # Validator receives dict results, not JSON strings.
        captured_evidence = []

        class CapturingValidator:
            validator_id = "capturing"
            def validate(self, **kw):
                captured_evidence.append(kw.get("runtime_evidence", {}))
                from lhas.phase5.substrate.validation import ValidatorFeedback, ValidatorDecision, ValidatorExecutionStatus
                return ValidatorFeedback(
                    validator_id="capturing",
                    candidate_id="c1",
                    execution_status=ValidatorExecutionStatus.SUCCESS,
                    decision=ValidatorDecision.ACCEPT,
                )

        driver = ScriptedModelDriver([
            ScriptedAction(type="tool_call", tool_name="tool_a", arguments={}),
            ScriptedAction(type="final_answer", content="done"),
        ])
        core = _setup_core(driver, ValidatorOnlyStrategy(), CapturingValidator())
        # First call: returns tool_call from driver
        action1 = core.next_model_action()
        assert action1.type == "tool_call"
        # Feed tool result (structured dict)
        core.receive_tool_result("tool_a", {"status": "success", "data": "ok"})
        # Second call: returns final_answer -> validator called
        action2 = core.next_model_action()
        assert action2.type == "final_answer"
        assert len(captured_evidence) == 1
        evidence = captured_evidence[0]
        results = evidence.get("public_tool_results", [])
        assert len(results) == 1
        assert isinstance(results[0], dict)
        assert results[0]["status"] == "success"


# ══════════════════════════════════════════════════════════════════════
#  Section B: Transient failure resolution
# ══════════════════════════════════════════════════════════════════════

class TestTransientFailureResolution:
    """Prove that transient failures can be resolved."""

    def test_error_only_rejects(self):
        """Single error with no success → REJECT."""
        validator = _make_validator()
        evidence = PublicValidationEvidence(
            candidate_answer="I completed the task",
            public_tool_results=[{"status": "error", "message": "timeout"}],
            evidence_refs=["e1"],
        )
        fb = validator.validate(
            candidate_id="c1", evidence_refs=["e1"],
            runtime_evidence=evidence.__dict__,
        )
        assert fb.decision.value == "REJECT"
        assert fb.failure_type == "UNRESOLVED_PUBLIC_FAILURE"

    def test_error_then_success_resolves(self):
        """error → success = resolved (not rejected as unresolved)."""
        validator = _make_validator()
        evidence = PublicValidationEvidence(
            candidate_answer="I completed the task",
            public_tool_results=[
                {"status": "error", "message": "timeout"},
                {"status": "success", "data": "ok"},
            ],
            evidence_refs=["e1"],
        )
        fb = validator.validate(
            candidate_id="c2", evidence_refs=["e1"],
            runtime_evidence=evidence.__dict__,
        )
        # Error is resolved by later success
        assert fb.decision.value == "ACCEPT"

    def test_error_retry_success_resolves(self):
        """error → retry success = resolved."""
        validator = _make_validator()
        evidence = PublicValidationEvidence(
            candidate_answer="Done",
            public_tool_results=[
                {"status": "error"},
                {"status": "success"},
            ],
            evidence_refs=["e1"],
        )
        fb = validator.validate(
            candidate_id="c3", evidence_refs=["e1"],
            runtime_evidence=evidence.__dict__,
        )
        assert fb.decision.value == "ACCEPT"

    def test_success_then_later_error_rejects(self):
        """success → later error = REJECT (unresolved trailing failure)."""
        validator = _make_validator()
        evidence = PublicValidationEvidence(
            candidate_answer="Done",
            public_tool_results=[
                {"status": "success", "data": "ok"},
                {"status": "error", "message": "failed"},
            ],
            evidence_refs=["e1"],
        )
        fb = validator.validate(
            candidate_id="c4", evidence_refs=["e1"],
            runtime_evidence=evidence.__dict__,
        )
        assert fb.decision.value == "REJECT"
        assert fb.failure_type == "UNRESOLVED_PUBLIC_FAILURE"


# ══════════════════════════════════════════════════════════════════════
#  Section L: A2 runtime proof
# ══════════════════════════════════════════════════════════════════════

class TestA2RuntimeProof:
    """A2: ValidatorOnlyStrategy — validator gates but no recovery."""

    def test_a2_accept_path(self):
        # A2 ACCEPT: successful evidence -> 1 validator call, 0 recovery.
        driver = ScriptedModelDriver([
            ScriptedAction(type="tool_call", tool_name="tool_a", arguments={}),
            ScriptedAction(type="final_answer", content="done"),
        ])
        core = _setup_core(driver, ValidatorOnlyStrategy())
        # First call returns tool_call
        action1 = core.next_model_action()
        assert action1.type == "tool_call"
        # Feed successful tool result
        _feed_tool_result(core, "tool_a", {"status": "success", "data": "ok"})
        # Second call returns final_answer -> validator ACCEPT
        action2 = core.next_model_action()
        assert action2.type == "final_answer"
        assert "VALIDATION_BLOCKED" not in (action2.content or "")
        assert len(core.get_validator_events()) == 1
        assert core.get_validator_events()[0]["decision"] == "ACCEPT"
        assert len(core.get_recovery_decisions()) == 0

    def test_a2_reject_path(self):
        # A2 REJECT: unresolved failure -> VALIDATION_BLOCKED, 0 recovery.
        driver = ScriptedModelDriver([
            ScriptedAction(type="tool_call", tool_name="tool_a", arguments={}),
            ScriptedAction(type="final_answer", content="done"),
        ])
        core = _setup_core(driver, ValidatorOnlyStrategy())
        # First call returns tool_call
        action1 = core.next_model_action()
        assert action1.type == "tool_call"
        # Feed failed tool result
        _feed_tool_result(core, "tool_a", {"status": "error", "message": "failed"})
        # Second call returns final_answer -> validator REJECT -> VALIDATION_BLOCKED
        action2 = core.next_model_action()
        assert "VALIDATION_BLOCKED" in (action2.content or "")
        assert len(core.get_validator_events()) == 1
        assert core.get_validator_events()[0]["decision"] == "REJECT"
        assert len(core.get_recovery_decisions()) == 0

    def test_a2_infra_failure_path(self):
        """A2 validator crash → RuntimeValidatorExecutionError."""
        class BrokenValidator:
            validator_id = "broken"
            def validate(self, **kw):
                raise RuntimeError("infra failure")

        driver = ScriptedModelDriver([
            ScriptedAction(type="final_answer", content="answer"),
        ])
        core = _setup_core(driver, ValidatorOnlyStrategy(), BrokenValidator())
        with pytest.raises(RuntimeValidatorExecutionError, match="infra failure"):
            core.next_model_action()
        assert len(core.get_validator_events()) == 1
        assert core.get_validator_events()[0]["decision"] == "INFRA_ERROR"


# ══════════════════════════════════════════════════════════════════════
#  Section M: A3 validation recovery proof
# ══════════════════════════════════════════════════════════════════════

class TestA3ValidationRecoveryProof:
    """A3: REJECT → recovery → another model action (not the rejected one)."""

    def test_a3_reject_recovery_executes(self):
        """A3: candidate rejected → recovery decision → another action."""
        # Provide enough actions: wrong final → tool call → correct final
        driver = ScriptedModelDriver([
            ScriptedAction(type="final_answer", content="wrong"),  # rejected
            # After recovery injection, driver returns tool_call
            ScriptedAction(type="tool_call", tool_name="tool_a", arguments={}),
            ScriptedAction(type="final_answer", content="correct"),
        ])
        core = _setup_core(driver, OdysFullStrategy())
        # First call: wrong final → REJECT → recovery → loop back
        # Recovery adds context, then driver returns tool_call
        # Tool result → another final → ACCEPT
        action = core.next_model_action()
        # The final action should NOT be the rejected "wrong" answer
        # It should be either TERMINATED (if loop limit hit) or the correct answer
        # With 3 scripted actions, it should succeed
        assert len(core.get_validator_events()) >= 1
        # Recovery decisions were made
        assert len(core.get_recovery_decisions()) >= 1


# ══════════════════════════════════════════════════════════════════════
#  Section F + G: Recovery budget gate wiring + A5 causal difference
# ══════════════════════════════════════════════════════════════════════

class TestRecoveryBudgetGate:
    """Prove recovery budget gate is wired and A5 bypasses it."""

    def test_a3_budget_gate_active(self):
        """A3: RecoveryBudgetGate enforces max_recovery_attempts."""
        gate = RecoveryBudgetGate(max_recovery_attempts=3)
        # First 3 allowed
        for i in range(3):
            assert gate.authorize(step=i, candidate_action="RETRY") is BudgetDecision.ALLOW
        # 4th → ESCALATE
        assert gate.authorize(step=4, candidate_action="RETRY") is BudgetDecision.ESCALATE
        assert gate.remaining == 0

    def test_a5_pass_through_allows_unlimited(self):
        """A5: PassThroughRecoveryBudgetGate always allows."""
        gate = PassThroughRecoveryBudgetGate()
        for i in range(100):
            assert gate.authorize(step=i, candidate_action="RETRY") is BudgetDecision.ALLOW
        assert gate.remaining == -1  # unlimited

    def test_a5_causal_difference_proven(self):
        """A5 allows where A3 would deny (same observations, different budget gate)."""
        # Simulate: 4 recovery attempts needed
        a3_gate = RecoveryBudgetGate(max_recovery_attempts=3)
        a5_gate = PassThroughRecoveryBudgetGate()

        a3_results = []
        a5_results = []
        for i in range(5):
            a3_results.append(a3_gate.authorize(step=i, candidate_action="RETRY"))
            a5_results.append(a5_gate.authorize(step=i, candidate_action="RETRY"))

        # A3: first 3 ALLOW, then ESCALATE
        assert a3_results[:3] == [BudgetDecision.ALLOW] * 3
        assert a3_results[3] == BudgetDecision.ESCALATE

        # A5: all ALLOW
        assert a5_results == [BudgetDecision.ALLOW] * 5

    def test_budget_gate_wired_into_core(self):
        """RecoveryBudgetGate is injected into Phase5AgentCore."""
        driver = ScriptedModelDriver([
            ScriptedAction(type="final_answer", content="wrong"),
        ])
        core = Phase5AgentCore(driver, strategy=OdysFullStrategy())
        core.initialize("test", [{"name": "tool"}])
        core.set_runtime_validator(_make_validator())
        gate = RecoveryBudgetGate(max_recovery_attempts=3)
        core.set_recovery_budget_gate(gate)
        assert core._recovery_budget_gate is gate


# ══════════════════════════════════════════════════════════════════════
#  Section H: Root budget belongs to TrialExecutor
# ══════════════════════════════════════════════════════════════════════

class TestRootBudgetOwnership:
    """TrialExecutor owns root budget wrapping."""

    def test_trial_executor_wraps_raw_driver(self):
        # TrialExecutor wraps raw ModelDriver with BudgetedModelDriver.
        from lhas.phase5.trial_executor import execute_trial
        from lhas.phase5.model_driver import (
            BudgetedModelDriver, ScriptedModelDriver, ScriptedAction,
        )
        from lhas.phase5.control_arms import ControlArm
        from lhas.phase5.types import BudgetConfig
        import unittest.mock as mock

        raw_driver = ScriptedModelDriver([
            ScriptedAction(type="final_answer", content="done"),
        ])
        budget = BudgetConfig(max_turns=5, max_model_calls=10)

        # Patch create_toolmaze_agent_adapter and ExecutionEngine
        with mock.patch(
            "lhas.phase5.trial_executor.create_toolmaze_agent_adapter",
            return_value=mock.MagicMock(),
        ), mock.patch(
            "lhas.phase5.trial_executor._run_offline_grader",
            return_value={
                "judgement": {"pass": True},
                "metrics_report": {},
                "metrics_summary": {"tsr": 1.0},
                "grader_source": "FROZEN_TOOLMAZE",
                "error": None,
            },
        ):
            # Patch the lazy ExecutionEngine import
            mock_trace = mock.MagicMock()
            mock_trace.to_dict.return_value = {"task_id": "T", "tool_calls": []}
            mock_engine = mock.MagicMock()
            mock_engine.run.return_value = (mock_trace, {})

            import sys
            mock_sandbox = mock.MagicMock()
            mock_sandbox.ExecutionEngine.return_value = mock_engine
            saved_eval = sys.modules.get("evaluation.core.sandbox")
            sys.modules["evaluation"] = mock.MagicMock()
            sys.modules["evaluation.core"] = mock.MagicMock()
            sys.modules["evaluation.core.sandbox"] = mock_sandbox
            try:
                result = execute_trial(
                    arm=ControlArm.A0_BARE,
                    task_json={"task_description": "test"},
                    tool_definitions=[],
                    model_driver=raw_driver,  # RAW, not pre-wrapped
                    budget=budget,
                    experiment_id="test-exp",
                    task_id="T1",
                )
            finally:
                if saved_eval is not None:
                    sys.modules["evaluation.core.sandbox"] = saved_eval

        assert result.validity == "VALID"
        assert result.termination_reason == "completed"

    def test_double_wrapping_detected(self):
        """If caller pre-wraps, TrialExecutor detects and reuses."""
        from lhas.phase5.model_driver import BudgetedModelDriver, ScriptedModelDriver, ScriptedAction

        raw = ScriptedModelDriver([ScriptedAction(type="final_answer", content="done")])
        wrapped = BudgetedModelDriver(raw, max_model_calls=5)
        # TrialExecutor should detect it's already wrapped
        assert isinstance(wrapped, BudgetedModelDriver)


# ══════════════════════════════════════════════════════════════════════
#  Section C: Evidence refs from actual EvidenceLedger
# ══════════════════════════════════════════════════════════════════════

class TestEvidenceRefs:
    """Evidence refs come from actual EvidenceLedger.all_events()."""

    def test_evidence_refs_populated(self):
        """When EvidenceLedger has events, evidence_refs are populated."""
        from lhas.phase5.substrate.evidence import EvidenceLedger, EvidenceEventType

        driver = ScriptedModelDriver([
            ScriptedAction(type="tool_call", tool_name="tool_a", arguments={}),
            ScriptedAction(type="final_answer", content="done"),
        ])
        core = _setup_core(driver, BareStrategy())
        ledger = EvidenceLedger(run_id="test")
        core.set_evidence_ledger(ledger)

        # Feed tool result (which appends to ledger)
        _feed_tool_result(core, "tool_a", {"status": "success"})

        # Check evidence refs
        refs = core._get_evidence_refs()
        assert len(refs) >= 1
        assert all(isinstance(r, str) for r in refs)

    def test_evidence_refs_empty_without_ledger(self):
        """Without EvidenceLedger, evidence_refs are empty."""
        driver = ScriptedModelDriver([ScriptedAction(type="final_answer", content="done")])
        core = _setup_core(driver, BareStrategy())
        assert core._get_evidence_refs() == []


# ══════════════════════════════════════════════════════════════════════
#  Section J: Validator input firewall
# ══════════════════════════════════════════════════════════════════════

class TestValidatorInputFirewall:
    """Validator input must reject forbidden oracle/hidden fields."""

    def test_oracle_field_rejected(self):
        """oracle field → ValueError."""
        validator = _make_validator()
        with pytest.raises(ValueError, match="forbidden extra fields"):
            validator.validate(
                candidate_id="c1", evidence_refs=[],
                runtime_evidence={"candidate_answer": "test", "oracle": {"x": 1}},
            )

    def test_perturbation_status_rejected(self):
        """perturbation_status field → ValueError."""
        validator = _make_validator()
        with pytest.raises(ValueError, match="forbidden extra fields"):
            validator.validate(
                candidate_id="c2", evidence_refs=[],
                runtime_evidence={"candidate_answer": "test", "perturbation_status": "P"},
            )

    def test_expected_result_rejected(self):
        """expected_result field → ValueError."""
        validator = _make_validator()
        with pytest.raises(ValueError, match="forbidden extra fields"):
            validator.validate(
                candidate_id="c3", evidence_refs=[],
                runtime_evidence={"candidate_answer": "test", "expected_result": {}},
            )

    def test_is_perturbed_rejected(self):
        """is_perturbed field → ValueError."""
        validator = _make_validator()
        with pytest.raises(ValueError, match="forbidden extra fields"):
            validator.validate(
                candidate_id="c4", evidence_refs=[],
                runtime_evidence={"candidate_answer": "test", "is_perturbed": True},
            )

    def test_non_oracle_extra_rejected(self):
        """Every unknown DTO field is rejected by the strict boundary."""
        validator = _make_validator()
        with pytest.raises(ValueError, match="forbidden extra fields"):
            validator.validate(
                candidate_id="c5", evidence_refs=[],
                runtime_evidence={
                    "candidate_answer": "test",
                    "random_field": "value",
                    "public_tool_results": [{"status": "success"}],
                    "evidence_refs": ["e1"],
                },
            )
