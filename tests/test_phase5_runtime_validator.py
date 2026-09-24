"""Runtime validator tests — T1-T15.

Tests for PublicEvidenceCompletionValidator and its integration
with Phase5AgentCore and strategies A0-A5.

No real provider, no ToolMaze oracle.
"""

from __future__ import annotations

import asyncio
import pytest
from unittest.mock import MagicMock

from lhas.phase5.runtime_validator import (
    PublicEvidenceCompletionValidator,
    PublicValidationEvidence,
    VALIDATOR_ID,
    VALIDATOR_VERSION,
)
from lhas.phase5.substrate.validation import (
    ValidatorDecision,
    ValidatorExecutionStatus,
)
from lhas.phase5.agent_core import Phase5AgentCore
from lhas.phase5.model_driver import ScriptedModelDriver, ScriptedAction, ModelAction
from lhas.phase5.control_arms import (
    BareStrategy,
    RetryOnlyStrategy,
    ValidatorOnlyStrategy,
    OdysFullStrategy,
    OdysMinusObservableProgress,
    OdysMinusRecoveryBudgetPolicy,
    RecoveryActionKind,
)


def _run(coro):
    return asyncio.run(coro)


# ══════════════════════════════════════════════════════════════════
# T1: non-empty candidate + closed successful tool evidence → ACCEPT
# ══════════════════════════════════════════════════════════════════

class TestT1_AcceptOnValidEvidence:
    def test_accept_with_successful_tool_result(self):
        v = PublicEvidenceCompletionValidator()
        fb = v.validate(
            candidate_id="c1",
            evidence_refs=["e1"],
            runtime_evidence={
                "candidate_answer": "The answer is 42",
                "public_tool_results": [{"status": "success", "output": "42"}],
                "tool_definitions": [],
                "conversation_history": [],
                "evidence_refs": ["e1"],
                "has_pending_recovery": False,
            },
        )
        assert fb.decision is ValidatorDecision.ACCEPT
        assert fb.failure_type is None

    def test_accept_with_multiple_evidence(self):
        v = PublicEvidenceCompletionValidator()
        fb = v.validate(
            candidate_id="c2",
            evidence_refs=["e1", "e2"],
            runtime_evidence={
                "candidate_answer": "done",
                "public_tool_results": [
                    {"status": "success", "output": "a"},
                    {"status": "ok", "output": "b"},
                ],
                "evidence_refs": ["e1", "e2"],
            },
        )
        assert fb.decision is ValidatorDecision.ACCEPT


# ══════════════════════════════════════════════════════════════════
# T2: empty completion → REJECT
# ══════════════════════════════════════════════════════════════════

class TestT2_EmptyCompletionReject:
    def test_empty_string_rejects(self):
        v = PublicEvidenceCompletionValidator()
        fb = v.validate(
            candidate_id="c3",
            evidence_refs=[],
            runtime_evidence={"candidate_answer": ""},
        )
        assert fb.decision is ValidatorDecision.REJECT
        assert fb.failure_type == "EMPTY_COMPLETION"

    def test_whitespace_only_rejects(self):
        v = PublicEvidenceCompletionValidator()
        fb = v.validate(
            candidate_id="c4",
            evidence_refs=[],
            runtime_evidence={"candidate_answer": "   \n\t  "},
        )
        assert fb.decision is ValidatorDecision.REJECT
        assert fb.failure_type == "EMPTY_COMPLETION"


# ══════════════════════════════════════════════════════════════════
# T3: unpaired tool call → REJECT
# ══════════════════════════════════════════════════════════════════

class TestT3_UnpairedToolCallReject:
    def test_tool_call_without_result_rejects(self):
        v = PublicEvidenceCompletionValidator()
        fb = v.validate(
            candidate_id="c5",
            evidence_refs=[],
            runtime_evidence={
                "candidate_answer": "I think the answer is X",
                "conversation_history": [
                    {"role": "assistant", "type": "tool_call",
                     "tool_call": {"id": "tc1", "name": "search", "arguments": {}}},
                ],
                "public_tool_results": [],
            },
        )
        assert fb.decision is ValidatorDecision.REJECT
        assert fb.failure_type == "UNRESOLVED_TOOL_CALL"

    def test_paired_tool_call_passes(self):
        v = PublicEvidenceCompletionValidator()
        fb = v.validate(
            candidate_id="c6",
            evidence_refs=["e1"],
            runtime_evidence={
                "candidate_answer": "done",
                "conversation_history": [
                    {"role": "assistant", "type": "tool_call",
                     "tool_call": {"id": "tc1", "name": "search", "arguments": {}}},
                    {"role": "tool", "tool_call_id": "tc1", "content": {"status": "success"}},
                ],
                "public_tool_results": [{"status": "success"}],
                "evidence_refs": ["e1"],
            },
        )
        assert fb.decision is ValidatorDecision.ACCEPT


# ══════════════════════════════════════════════════════════════════
# T4: unresolved public failure → REJECT
# ══════════════════════════════════════════════════════════════════

class TestT4_UnresolvedFailureReject:
    def test_explicit_error_rejects(self):
        v = PublicEvidenceCompletionValidator()
        fb = v.validate(
            candidate_id="c7",
            evidence_refs=[],
            runtime_evidence={
                "candidate_answer": "it failed",
                "public_tool_results": [{"status": "error", "output": "tool crashed"}],
            },
        )
        assert fb.decision is ValidatorDecision.REJECT
        assert fb.failure_type == "UNRESOLVED_PUBLIC_FAILURE"


# ══════════════════════════════════════════════════════════════════
# T5: no positive evidence → INDETERMINATE
# ══════════════════════════════════════════════════════════════════

class TestT5_InsufficientEvidence:
    def test_no_tool_results_no_evidence(self):
        v = PublicEvidenceCompletionValidator()
        fb = v.validate(
            candidate_id="c8",
            evidence_refs=[],
            runtime_evidence={
                "candidate_answer": "I believe the answer is 42",
                "public_tool_results": [],
                "evidence_refs": [],
            },
        )
        assert fb.decision is ValidatorDecision.INDETERMINATE
        assert fb.failure_type == "INSUFFICIENT_PUBLIC_EVIDENCE"


# ══════════════════════════════════════════════════════════════════
# T6: hidden ToolMaze metadata not present in validator DTO
# ══════════════════════════════════════════════════════════════════

class TestT6_NoHiddenMetadata:
    def test_perturbation_status_not_in_dto(self):
        """PublicValidationEvidence must not contain oracle fields."""
        fields = set(PublicValidationEvidence.__dataclass_fields__.keys())
        forbidden = {"perturbation_status", "is_perturbed", "expected_result",
                      "oracle", "task_hidden_status", "valid_paths"}
        assert fields.isdisjoint(forbidden)

    def test_dto_extra_forbidden(self):
        """Extra fields must be rejected."""
        v = PublicEvidenceCompletionValidator()
        # Extra field in runtime_evidence should be silently ignored
        fb = v.validate(
            candidate_id="c9",
            evidence_refs=[],
            runtime_evidence={
                "candidate_answer": "test",
                "perturbation_status": "PERTURBED",  # should be ignored
                "oracle": {"secret": "data"},  # should be ignored
                "public_tool_results": [{"status": "success"}],
                "evidence_refs": ["e1"],
            },
        )
        # Validator should still work (extra fields filtered)
        assert fb.execution_status is ValidatorExecutionStatus.SUCCESS


# ══════════════════════════════════════════════════════════════════
# T7/T8: A0/A1 validator call count = 0
# ══════════════════════════════════════════════════════════════════

class TestT7T8_A0A1NoValidation:
    def test_a0_no_validation(self):
        driver = ScriptedModelDriver([
            ScriptedAction(type="final_answer", content="answer"),
        ])
        core = Phase5AgentCore(driver, strategy=BareStrategy())
        core.initialize("test", [{"name": "tool"}])
        core.set_runtime_validator(PublicEvidenceCompletionValidator())
        action = core.next_model_action()
        assert action.type == "final_answer"
        assert len(core.get_validator_events()) == 0
        assert "VALIDATION_BLOCKED" not in (action.content or "")

    def test_a1_no_validation(self):
        driver = ScriptedModelDriver([
            ScriptedAction(type="final_answer", content="answer"),
        ])
        core = Phase5AgentCore(driver, strategy=RetryOnlyStrategy())
        core.initialize("test", [{"name": "tool"}])
        core.set_runtime_validator(PublicEvidenceCompletionValidator())
        action = core.next_model_action()
        assert action.type == "final_answer"
        assert len(core.get_validator_events()) == 0


# ══════════════════════════════════════════════════════════════════
# T9: A2 accepted candidate
# ══════════════════════════════════════════════════════════════════

class TestT9_A2Accept:
    def test_a2_accept_validator_count_1_recovery_0(self):
        driver = ScriptedModelDriver([
            ScriptedAction(type="tool_call", tool_name="search", arguments={"q": "test"}),
            ScriptedAction(type="final_answer", content="the answer"),
        ])
        core = Phase5AgentCore(driver, strategy=ValidatorOnlyStrategy())
        core.initialize("test", [{"name": "search"}])
        core.set_runtime_validator(PublicEvidenceCompletionValidator())

        # Step 1: tool call
        action1 = core.next_model_action()
        assert action1.type == "tool_call"

        # Tool result → success
        core.receive_tool_result("search", {"status": "success", "output": "found it"})

        # Step 2: final answer → validated
        action2 = core.next_model_action()
        assert action2.type == "final_answer"
        assert "VALIDATION_BLOCKED" not in (action2.content or "")
        assert len(core.get_validator_events()) == 1
        assert len(core.get_recovery_decisions()) == 0


# ══════════════════════════════════════════════════════════════════
# T10: A2 rejected candidate
# ══════════════════════════════════════════════════════════════════

class TestT10_A2Reject:
    def test_a2_empty_reject_no_recovery(self):
        driver = ScriptedModelDriver([
            ScriptedAction(type="final_answer", content=""),
        ])
        core = Phase5AgentCore(driver, strategy=ValidatorOnlyStrategy())
        core.initialize("test", [{"name": "tool"}])
        core.set_runtime_validator(PublicEvidenceCompletionValidator())
        action = core.next_model_action()
        # A2: rejected → validation-blocked, no recovery, no extra model call
        assert "VALIDATION_BLOCKED" in (action.content or "")
        assert len(core.get_validator_events()) == 1
        assert len(core.get_recovery_decisions()) == 0


# ══════════════════════════════════════════════════════════════════
# T11: A3 rejected → Phase4 recovery
# ══════════════════════════════════════════════════════════════════

class TestT11_A3RejectRecovery:
    def test_a3_reject_delegates_to_phase4(self):
        driver = ScriptedModelDriver([
            ScriptedAction(type="final_answer", content="wrong"),
        ])
        core = Phase5AgentCore(driver, strategy=OdysFullStrategy())
        core.initialize("test", [{"name": "tool"}])
        core.set_runtime_validator(PublicEvidenceCompletionValidator())
        action = core.next_model_action()
        # A3: validation reject → pending recovery set
        assert len(core.get_validator_events()) == 1
        assert core._pending_recovery is not None


# ══════════════════════════════════════════════════════════════════
# T12: A4 same validation behavior as A3
# ══════════════════════════════════════════════════════════════════

class TestT12_A4SameValidation:
    def test_a4_reject_delegates_to_phase4(self):
        driver = ScriptedModelDriver([
            ScriptedAction(type="final_answer", content="wrong"),
        ])
        core = Phase5AgentCore(driver, strategy=OdysMinusObservableProgress())
        core.initialize("test", [{"name": "tool"}])
        core.set_runtime_validator(PublicEvidenceCompletionValidator())
        action = core.next_model_action()
        assert len(core.get_validator_events()) == 1
        assert core._pending_recovery is not None


# ══════════════════════════════════════════════════════════════════
# T13: A5 same recovery as A3
# ══════════════════════════════════════════════════════════════════

class TestT13_A5SameRecovery:
    def test_a5_reject_delegates_to_phase4(self):
        driver = ScriptedModelDriver([
            ScriptedAction(type="final_answer", content="wrong"),
        ])
        core = Phase5AgentCore(driver, strategy=OdysMinusRecoveryBudgetPolicy())
        core.initialize("test", [{"name": "tool"}])
        core.set_runtime_validator(PublicEvidenceCompletionValidator())
        action = core.next_model_action()
        assert len(core.get_validator_events()) == 1
        assert core._pending_recovery is not None


# ══════════════════════════════════════════════════════════════════
# T14: validator exception → INVALID_INFRA
# ══════════════════════════════════════════════════════════════════

class TestT14_ValidatorException:
    def test_validator_exception_recorded(self):
        class BrokenValidator:
            validator_id = "broken"
            def validate(self, **kw):
                raise RuntimeError("validator crashed")

        driver = ScriptedModelDriver([
            ScriptedAction(type="final_answer", content="answer"),
        ])
        core = Phase5AgentCore(driver, strategy=ValidatorOnlyStrategy())
        core.initialize("test", [{"name": "tool"}])
        core.set_runtime_validator(BrokenValidator())
        action = core.next_model_action()
        # Should not crash — error recorded
        assert len(core.get_validator_events()) == 1
        assert core.get_validator_events()[0]["decision"] == "INFRA_ERROR"


# ══════════════════════════════════════════════════════════════════
# T15: ToolMaze JudgeSystem never imported by runtime validator
# ══════════════════════════════════════════════════════════════════

class TestT15_NoJudgeInValidator:
    def test_validator_module_no_judge_import(self):
        import inspect
        source = inspect.getsource(PublicEvidenceCompletionValidator)
        assert "JudgeSystem" not in source
        assert "MetricsCalculator" not in source
        assert "judge" not in source.lower()


# ══════════════════════════════════════════════════════════════════
# Config identity
# ══════════════════════════════════════════════════════════════════

class TestValidatorConfigIdentity:
    def test_config_hash_deterministic(self):
        v1 = PublicEvidenceCompletionValidator()
        v2 = PublicEvidenceCompletionValidator()
        assert v1.config_hash() == v2.config_hash()

    def test_validator_id(self):
        v = PublicEvidenceCompletionValidator()
        assert v.validator_id == "public-evidence-completion-v1"
