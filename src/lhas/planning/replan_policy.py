"""Deterministic macro-replan triggers derived from durable execution truth."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lhas.persistence.phaseb_repos import FailureReportRepository, ValidationResultRepository
from lhas.persistence.repositories import AttemptRepository


@dataclass(frozen=True)
class ReplanTrigger:
    reason: str
    evidence: dict[str, Any]


class ReplanTriggerPolicy:
    """Classify only evidence strong enough to invalidate the macro strategy."""

    repeated_failure_threshold = 2

    def __init__(self, db):
        self.attempts = AttemptRepository(db)
        self.failures = FailureReportRepository(db)
        self.validations = ValidationResultRepository(db)

    def evaluate(self, *, step, run_id: str) -> ReplanTrigger | None:
        attempts = self.attempts.list_for_run(run_id)
        if not attempts:
            return None

        error_types = [str(item.error_type or "").upper() for item in attempts]
        reports = [
            report
            for attempt in attempts
            for report in self.failures.list_for_attempt(attempt.id)
        ]
        report_text = " ".join(
            " ".join((
                str(report.failure_type.value),
                str(report.failure_class.value),
                report.summary,
                report.evidence,
            )).upper()
            for report in reports
        )
        if any(value in {"ASSUMPTION_INVALID", "INVALID_ASSUMPTION"} for value in error_types) or (
            "ASSUMPTION" in report_text and "INVALID" in report_text
        ):
            return ReplanTrigger(
                "INVALID_ASSUMPTION",
                {
                    "source": "DURABLE_FAILURE_EVIDENCE",
                    "capability": step.capability,
                    "error_types": error_types,
                    "failure_report_count": len(reports),
                },
            )

        rejected = [
            validation
            for attempt in attempts
            for validation in self.validations.list_for_attempt(attempt.id)
            if not validation.passed
        ]
        if rejected:
            return ReplanTrigger(
                "VALIDATOR_REJECTION",
                {
                    "source": "DURABLE_VALIDATION",
                    "capability": step.capability,
                    "validation_failure_count": len(rejected),
                },
            )

        signatures: dict[str, int] = {}
        for error_type in error_types:
            if error_type:
                signature = f"{step.id}|{step.capability}|{error_type}"
                signatures[signature] = signatures.get(signature, 0) + 1
        repeated = next(
            ((signature, count) for signature, count in signatures.items()
             if count >= self.repeated_failure_threshold),
            None,
        )
        if repeated is not None:
            signature, count = repeated
            return ReplanTrigger(
                "REPEATED_STEP_FAILURE",
                {
                    "source": "DURABLE_ATTEMPT_HISTORY",
                    "failure_signature": signature,
                    "failure_signature_count": count,
                    "threshold": self.repeated_failure_threshold,
                },
            )
        return None
