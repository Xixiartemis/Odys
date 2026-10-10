"""PublicEvidenceCompletionValidator — production runtime validator.

Validates completion candidates using ONLY public runtime evidence.
No benchmark oracle, no hidden perturbation data, no LLM calls.

VALIDATOR_SCOPE=COMPLETION_EVIDENCE_CONSISTENCY
VALIDATOR_IS_TASK_CORRECTNESS_ORACLE=NO

Deterministic v1 rules:
- V1: empty/whitespace candidate → REJECT (EMPTY_COMPLETION)
- V2: unpaired tool call → REJECT (UNRESOLVED_TOOL_CALL)
- V3: unresolved explicit public failure → REJECT (UNRESOLVED_PUBLIC_FAILURE)
  Resolution is based on public lineage: a later success resolves a failure
  from the same tool, or an explicitly declared replacement relationship.
- V4: pending recovery → REJECT (PENDING_RECOVERY)
- V5: no positive public evidence → INDETERMINATE (INSUFFICIENT_PUBLIC_EVIDENCE)
- V6: all checks pass → ACCEPT
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .substrate.validation import (
    ValidatorDecision,
    ValidatorExecutionStatus,
    ValidatorFeedback,
)


VALIDATOR_ID = "public-evidence-completion-v1"
VALIDATOR_VERSION = "1.0"


class PublicFailureType(str, Enum):
    """Public failure types observable from tool results."""
    EMPTY_COMPLETION = "EMPTY_COMPLETION"
    UNRESOLVED_TOOL_CALL = "UNRESOLVED_TOOL_CALL"
    UNRESOLVED_PUBLIC_FAILURE = "UNRESOLVED_PUBLIC_FAILURE"
    PENDING_RECOVERY = "PENDING_RECOVERY"
    INSUFFICIENT_PUBLIC_EVIDENCE = "INSUFFICIENT_PUBLIC_EVIDENCE"


# Known extra fields that must be rejected by the input firewall.
_FORBIDDEN_EXTRA_FIELDS = frozenset({
    "is_perturbed", "perturbation_status", "expected_result",
    "oracle", "oracle_solution", "ground_truth", "native_judge",
    "hidden_task_metadata", "perturbation_point",
})


@dataclass
class PublicValidationEvidence:
    """Input DTO for runtime validation. Extra fields forbidden (Section J).

    Contains ONLY public runtime information — no oracle, no hidden data.
    """
    candidate_answer: str
    conversation_history: List[Dict[str, Any]] = field(default_factory=list)
    tool_definitions: List[Dict[str, Any]] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)
    has_pending_recovery: bool = False
    observed_state_digest: str = ""
    public_tool_results: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        # Section J: Enforce strict input firewall
        # This is checked at construction via the caller, but we also
        # validate in the validator's validate() method.
        pass


def _public_result_payload(result: Dict[str, Any]) -> Dict[str, Any]:
    """Return the public result payload, including nested ``result`` DTOs.

    The canonical agent path flattens public fields for compatibility, but the
    validator also accepts the structured form written to evidence artifacts.
    Keeping this normalization here prevents the two representations from
    acquiring different semantics.
    """
    nested = result.get("result")
    return nested if isinstance(nested, dict) else result


def _public_field(result: Dict[str, Any], field: str, default: Any = None) -> Any:
    if field in result:
        return result[field]
    payload = _public_result_payload(result)
    return payload.get(field, default)


def _is_explicit_failure(result: Dict[str, Any]) -> bool:
    """Check if a public tool result indicates explicit failure.

    Uses ONLY public fields from the result dict passed to
    Phase5AgentCore.receive_tool_result().
    """
    status = str(_public_field(result, "status", "")).lower()
    return status in {"error", "failure", "failed"} or bool(_public_field(result, "error"))


def _has_positive_evidence(evidence: PublicValidationEvidence) -> bool:
    """Check for positive public runtime evidence.

    Conservative: a successful public tool result counts.
    Model text alone does NOT count.
    """
    for result in evidence.public_tool_results:
        if _is_explicit_success(result):
            return True
        event_type = str(_public_field(result, "event_type", "")).upper()
        effect_status = str(_public_field(result, "effect_status", "")).upper()
        if event_type == "SIDE_EFFECT_CONFIRMED" or effect_status == "SIDE_EFFECT_CONFIRMED":
            return True
    return False


def _is_explicit_success(result: Dict[str, Any]) -> bool:
    """Check if a public tool result indicates explicit success."""
    status = str(_public_field(result, "status", "")).lower()
    return status in {"success", "ok", "completed", "done"}


class PublicEvidenceCompletionValidator:
    """Production runtime validator — deterministic, no oracle access.

    Validates completion candidates using public evidence only.
    Implements RuntimeValidatorProtocol.
    """

    def __init__(self):
        self._events: List[Dict[str, Any]] = []

    @property
    def validator_id(self) -> str:
        return VALIDATOR_ID

    @property
    def validator_version(self) -> str:
        return VALIDATOR_VERSION

    def validate(
        self,
        *,
        candidate_id: str,
        evidence_refs: List[str],
        observed_state_digest: str = "",
        runtime_evidence: Optional[Dict[str, Any]] = None,
    ) -> ValidatorFeedback:
        """Validate a completion candidate using public evidence only.

        runtime_evidence must be a PublicValidationEvidence dict.
        """
        # Parse evidence with input firewall (Section J)
        if runtime_evidence is None:
            evidence = PublicValidationEvidence(candidate_answer="")
        else:
            # Section J: reject *all* extra fields. Filtering unknown fields
            # would hide wiring errors and is not a strict DTO boundary.
            known_fields = set(PublicValidationEvidence.__dataclass_fields__)
            extra_fields = set(runtime_evidence.keys()) - known_fields
            if extra_fields:
                raise ValueError(
                    f"PublicValidationEvidence received forbidden extra fields / unknown fields: "
                    f"{sorted(extra_fields)}. DTO input is strict extra=forbid."
                )
            evidence = PublicValidationEvidence(**runtime_evidence)

        # Run deterministic rules
        decision, failure_type = self._apply_rules(evidence)

        feedback = ValidatorFeedback(
            validator_id=VALIDATOR_ID,
            validator_version=VALIDATOR_VERSION,
            candidate_id=candidate_id,
            execution_status=ValidatorExecutionStatus.SUCCESS,
            decision=decision,
            evidence_refs=evidence_refs,
            observed_state_digest=observed_state_digest,
            failure_type=failure_type.value if failure_type else None,
        )

        # Record event with config hash for identity verification (Section K)
        self._events.append({
            "candidate_id": candidate_id,
            "validator_id": VALIDATOR_ID,
            "validator_version": VALIDATOR_VERSION,
            "validator_config_hash": self.config_hash(),
            "execution_status": "SUCCESS",
            "decision": decision.value,
            "failure_type": failure_type.value if failure_type else None,
            "evidence_refs": evidence_refs,
            "observed_state_digest": observed_state_digest,
        })

        return feedback

    def _apply_rules(
        self, evidence: PublicValidationEvidence
    ) -> tuple[ValidatorDecision, Optional[PublicFailureType]]:
        """Apply deterministic v1 rules."""

        # V1: empty/whitespace candidate
        if not evidence.candidate_answer or not evidence.candidate_answer.strip():
            return ValidatorDecision.REJECT, PublicFailureType.EMPTY_COMPLETION

        # V2: unpaired tool call
        for msg in evidence.conversation_history:
            if msg.get("role") == "assistant" and msg.get("type") == "tool_call":
                tc = msg.get("tool_call", {})
                tc_id = tc.get("id")
                if tc_id:
                    # Check if there's a matching tool result
                    has_result = any(
                        m.get("role") == "tool"
                        and (
                            m.get("tool_call_id") == tc_id
                            or m.get("provider_tool_call_id") == tc_id
                            or m.get("invocation_id") == tc_id
                        )
                        for m in evidence.conversation_history
                    )
                    if not has_result:
                        return ValidatorDecision.REJECT, PublicFailureType.UNRESOLVED_TOOL_CALL

        # V3: resolve failures only through public lineage. A same-tool
        # success is sufficient; a different tool must explicitly declare
        # that it replaces the failed call.
        has_unresolved_failure = False
        for i, result in enumerate(evidence.public_tool_results):
            if not _is_explicit_failure(result):
                continue
            failed_tool = _public_field(result, "tool_name")
            failed_call = _public_field(result, "tool_call_id")
            resolved = False
            for later in evidence.public_tool_results[i + 1:]:
                if not _is_explicit_success(later):
                    continue
                later_tool = _public_field(later, "tool_name")
                later_retry_of = _public_field(later, "retry_of_call_id")
                same_tool = bool(failed_tool) and later_tool == failed_tool
                explicit_replacement = (
                    failed_call is not None and
                    later_retry_of == failed_call
                )
                # Legacy result-only observations have no tool identity. They
                # remain same-lineage when both observations omit it.
                identity_omitted = not failed_tool and not later_tool
                if same_tool or explicit_replacement or identity_omitted:
                    resolved = True
                    break
            if not resolved:
                has_unresolved_failure = True

        if has_unresolved_failure:
            return ValidatorDecision.REJECT, PublicFailureType.UNRESOLVED_PUBLIC_FAILURE

        # V4: pending recovery
        if evidence.has_pending_recovery:
            return ValidatorDecision.REJECT, PublicFailureType.PENDING_RECOVERY

        # V5: no positive evidence
        if not _has_positive_evidence(evidence):
            return ValidatorDecision.INDETERMINATE, PublicFailureType.INSUFFICIENT_PUBLIC_EVIDENCE

        # V6: all checks pass
        return ValidatorDecision.ACCEPT, None

    def get_events(self) -> List[Dict[str, Any]]:
        """Return recorded validator events."""
        return list(self._events)

    def config_hash(self) -> str:
        """Deterministic config hash for identity verification."""
        return hashlib.sha256(
            f"{VALIDATOR_ID}:{VALIDATOR_VERSION}".encode()
        ).hexdigest()[:16]
