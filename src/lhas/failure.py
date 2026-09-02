"""Failure classification (docs/07_FAILURE_TAXONOMY.md).

Every failure must answer:
1. What happened?          -> summary
2. What is the evidence?   -> evidence
3. Which failure type?     -> failure_type / failure_class
4. What is needed next?    -> suggested_recovery

Phase B ships a deterministic rule classifier. An LLM classifier can replace
it later behind the same Protocol — the orchestrator never depends on the
implementation.
"""

from __future__ import annotations

from typing import Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field

from lhas.domain.enums import FailureClass, FailureType
from lhas.domain.models import Attempt, Task, new_id
from lhas.executors.protocol import ExecutionResult
from lhas.validation import ValidationResult


class FailureReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    attempt_id: str
    failure_type: FailureType
    failure_class: FailureClass
    evidence: str
    summary: str
    confidence: float = Field(ge=0.0, le=1.0)
    suggested_recovery: str


class FailureClassifier(Protocol):
    async def classify(
        self,
        *,
        task: Task,
        attempt: Attempt,
        result: Optional[ExecutionResult],
        validation: Optional[ValidationResult],
    ) -> FailureReport:
        ...


class RuleFailureClassifier:
    """Deterministic Phase B classifier (docs/07 rules, no LLM)."""

    async def classify(
        self,
        *,
        task: Task,
        attempt: Attempt,
        result: Optional[ExecutionResult],
        validation: Optional[ValidationResult],
    ) -> FailureReport:
        status = attempt.status
        error_text = " ".join(
            x for x in [attempt.error_message, attempt.error_type,
                        result.error_message if result else None,
                        result.error_type if result else None]
            if x
        ).upper()
        evidence_bits = []
        if validation is not None:
            evidence_bits.append(f"validation[{validation.level}]: {validation.evidence}")
        if attempt.error_message:
            evidence_bits.append(f"error: {attempt.error_message}")
        if result is not None and result.output:
            evidence_bits.append(f"output: {result.output[:200]}")
        evidence = " | ".join(evidence_bits) if evidence_bits else "no evidence recorded"

        # --- EXECUTION family -------------------------------------------------
        if "QUOTA_EXHAUSTED" in error_text:
            return self._report(attempt, FailureType.QUOTA_EXHAUSTED, FailureClass.EXECUTION, evidence,
                                "provider quota is exhausted for the selected route", 1.0,
                                "switch provider route; same-target retry is disabled")
        if "BILLING_OR_CREDIT_EXHAUSTED" in error_text:
            return self._report(attempt, FailureType.BILLING_OR_CREDIT_EXHAUSTED, FailureClass.EXECUTION, evidence,
                                "provider billing or credits are exhausted", 1.0, "switch provider route or restore billing")
        if "AUTH_INVALID" in error_text:
            return self._report(attempt, FailureType.AUTH_INVALID, FailureClass.EXECUTION, evidence,
                                "provider authentication is invalid", 1.0, "switch credential route or repair credentials")
        if "PROVIDER_TIMEOUT" in error_text:
            return self._report(attempt, FailureType.PROVIDER_TIMEOUT, FailureClass.EXECUTION, evidence,
                                "provider request timed out", 1.0, "controlled retry if budget allows")
        if "PROVIDER_UNAVAILABLE" in error_text:
            return self._report(attempt, FailureType.PROVIDER_UNAVAILABLE, FailureClass.EXECUTION, evidence,
                                "provider endpoint is unavailable", 0.9, "controlled migration or retry")
        if "MALFORMED_PROVIDER_RESPONSE" in error_text or "PROVIDER_MALFORMED_RESPONSE" in error_text:
            return self._report(attempt, FailureType.MALFORMED_PROVIDER_RESPONSE, FailureClass.EXECUTION, evidence,
                                "provider returned a malformed response", 0.95, "fail closed and inspect provider")
        if "UNKNOWN_PROVIDER_FAILURE" in error_text:
            return self._report(attempt, FailureType.UNKNOWN_PROVIDER_FAILURE, FailureClass.EXECUTION, evidence,
                                "provider failed without a recognizable provider-specific signature", 0.4,
                                "retry within budget; inspect provider evidence before changing route")
        if "BUDGET_EXHAUSTED" in error_text:
            return self._report(
                attempt, FailureType.BUDGET_EXHAUSTED, FailureClass.EXECUTION, evidence,
                "native executor exhausted its bounded turn or tool-call budget", 1.0,
                "retry with the recorded budget-exhaustion evidence if attempts remain",
            )
        if status.value == "TIMED_OUT":
            return self._report(attempt, FailureType.TIMEOUT, FailureClass.EXECUTION, evidence,
                                "executor exceeded its time budget",
                                1.0, "controlled retry if budget allows; else escalate")
        if status.value == "CRASHED":
            return self._report(attempt, FailureType.EXECUTOR_CRASH, FailureClass.EXECUTION, evidence,
                                "executor raised an unexpected exception",
                                1.0, "retry; if persistent, escalate")
        if "NETWORK_ERROR" in error_text or "NETWORK" in error_text:
            return self._report(attempt, FailureType.NETWORK_ERROR, FailureClass.EXECUTION, evidence,
                                "external network failure",
                                0.8, "verify external environment, then retry")
        if "TOOL_ERROR" in error_text:
            return self._report(attempt, FailureType.TOOL_ERROR, FailureClass.EXECUTION, evidence,
                                "a tool used by the executor failed",
                                0.8, "check tool configuration, then retry")

        # --- CONTEXT family ----------------------------------------------------
        if "MISSING_CONTEXT" in error_text:
            return self._report(attempt, FailureType.MISSING_CONTEXT, FailureClass.CONTEXT, evidence,
                                "required context is missing",
                                0.95, "supply the missing context, then retry")
        if "STALE_CONTEXT" in error_text:
            return self._report(attempt, FailureType.STALE_CONTEXT, FailureClass.CONTEXT, evidence,
                                "context is stale",
                                0.9, "rebuild context from scratch; do not reuse old snapshot")
        if "CONTEXT_CONFLICT" in error_text:
            return self._report(attempt, FailureType.CONTEXT_CONFLICT, FailureClass.CONTEXT, evidence,
                                "context sources contradict each other",
                                0.9, "escalate or ask for human clarification")
        if "CONTEXT_OVERLOAD" in error_text:
            return self._report(attempt, FailureType.CONTEXT_OVERLOAD, FailureClass.CONTEXT, evidence,
                                "too much context supplied",
                                0.8, "trim context to the minimal needed")

        # --- DATA family --------------------------------------------------------
        if validation is not None and not validation.passed:
            failed_checks = {c.name for c in validation.checks if not c.passed}
            if "direction_conflict" in failed_checks:
                return self._report(
                    attempt, FailureType.WRONG_MATCH, FailureClass.REASONING, evidence,
                    "predicted fit conflicts with the career direction",
                    0.95, "retry with career goal and direction-conflict evidence",
                )
            if failed_checks & {"ranking_order", "ranking_quality", "bad_ranking"}:
                return self._report(
                    attempt, FailureType.BAD_RANKING, FailureClass.REASONING, evidence,
                    "predicted ranking failed the benchmark ordering checks",
                    0.95, "retry with ranking feedback and relevant history",
                )
            if not (result and result.output and result.output.strip()):
                return self._report(attempt, FailureType.EMPTY_RESULT, FailureClass.DATA, evidence,
                                    "executor produced no usable output",
                                    0.85, "re-run with validation feedback")
            return self._report(attempt, FailureType.MISSING_REQUIRED_FIELD, FailureClass.DATA, evidence,
                                "output failed rule validation",
                                0.85, "re-run with validation feedback")

        # --- FALLBACK ------------------------------------------------------------
        if result is not None and result.status.value == "FAILURE":
            return self._report(attempt, FailureType.UNKNOWN, FailureClass.UNKNOWN, evidence,
                                "executor returned a failure without a recognizable signature",
                                0.4, "retry with expanded context")
        return self._report(attempt, FailureType.UNKNOWN, FailureClass.UNKNOWN, evidence,
                            "unclassifiable failure",
                            0.3, "escalate for manual review")

    @staticmethod
    def _report(attempt, failure_type, failure_class, evidence, summary, confidence, recovery) -> FailureReport:
        return FailureReport(
            attempt_id=attempt.id,
            failure_type=failure_type,
            failure_class=failure_class,
            evidence=evidence,
            summary=summary,
            confidence=confidence,
            suggested_recovery=recovery,
        )
