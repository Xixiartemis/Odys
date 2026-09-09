"""Workflow verification for plan-step acceptance.

Connects the P3.1 verification seam (PlanExecutionService) to the existing
validation infrastructure (ValidationResult, ValidationCheck,
ValidationResultRepository).

Does NOT create a parallel validation framework — reuses existing models.
Does NOT implement adaptive validation (Phase 5).
"""

from __future__ import annotations

from typing import Any

from lhas.persistence.event_store import EventStore
from lhas.persistence.phaseb_repos import ValidationResultRepository
from lhas.validation import ValidationCheck, ValidationLevel, ValidationResult


class VerificationResult:
    """Durable result of workflow verification.

    Returned by WorkflowVerifier.verify().  Attributes ``accepted`` and
    ``reason`` satisfy the seam contract in PlanExecutionService.
    """

    def __init__(
        self,
        accepted: bool,
        reason: str,
        validation: ValidationResult | None = None,
    ):
        self.accepted = accepted
        self.reason = reason
        self.validation = validation


class WorkflowVerifier:
    """Evaluate plan-step completion against declared acceptance criteria.

    Implements the ``.verify(step, plan, events)`` interface expected by the
    P3.1 verification seam in PlanExecutionService.

    Delegates to existing ``ValidationResult`` / ``ValidationCheck`` models
    and persists each verdict through ``ValidationResultRepository`` so the
    result survives persistence/reload.

    Verification criteria (V2 rule level):
    1. Structural — step output must be non-empty.
    2. Success criteria — each criterion in ``step.success_criteria`` must
       appear in the step output (substring / marker match).
    3. Expected effects — if ``step.expected_effects`` is declared, each key
       must be present in the step's execution context output or artifacts.
    """

    def __init__(self, db: Any):
        self.db = db
        self.validations = ValidationResultRepository(db)

    def verify(
        self,
        step: Any,
        plan: Any,
        events: EventStore,
    ) -> VerificationResult:
        """Verify a step that has reached CLAIMED_COMPLETE.

        Args:
            step: ``PlanStep`` (just transitioned to CLAIMED_COMPLETE).
            plan: The containing ``Plan``.
            events: ``EventStore`` instance for emitting provenance events.

        Returns:
            ``VerificationResult`` with ``.accepted`` and ``.reason``.
        """
        checks: list[ValidationCheck] = []

        # --- 1. Structural: step must have produced output -----------------
        output_text = str(step.output).strip() if step.output is not None else ""
        has_output = bool(output_text)
        checks.append(
            ValidationCheck(
                name="step_output_non_empty",
                passed=has_output,
                detail=None if has_output else "step produced no output",
            )
        )

        # --- 2. Success-criteria evaluation (V2 rule: marker in output) ----
        if step.success_criteria:
            for criterion in step.success_criteria:
                criterion_met = (
                    criterion.lower() in output_text.lower() if output_text else False
                )
                checks.append(
                    ValidationCheck(
                        name=f"criterion:{criterion[:64]}",
                        passed=criterion_met,
                        detail=(
                            None
                            if criterion_met
                            else f"acceptance criterion not verified in output: {criterion}"
                        ),
                    )
                )

        # --- 3. Expected-effects verification against execution context ----
        if step.expected_effects:
            step_record = (
                step.execution_context.get("steps", {}).get(step.id, {})
                if step.execution_context
                else {}
            )
            output_dict = (
                step_record.get("output", {})
                if isinstance(step_record.get("output"), dict)
                else {}
            )
            artifacts = (
                step_record.get("artifacts", {})
                if isinstance(step_record.get("artifacts"), dict)
                else {}
            )
            for key, _expected_value in step.expected_effects.items():
                effect_present = key in output_dict or key in artifacts
                checks.append(
                    ValidationCheck(
                        name=f"effect:{key}",
                        passed=effect_present,
                        detail=(
                            None
                            if effect_present
                            else f"expected effect '{key}' not found in step output or artifacts"
                        ),
                    )
                )

        # --- Build verdict --------------------------------------------------
        passed = all(c.passed for c in checks)
        evidence = "; ".join(
            f"{c.name}: {'ok' if c.passed else 'FAIL - ' + (c.detail or '')}"
            for c in checks
        )

        # --- Persist for durability -----------------------------------------
        validation = ValidationResult(
            attempt_id=step.task_id or step.id,
            passed=passed,
            level=ValidationLevel.V2_RULE,
            checks=checks,
            evidence=evidence,
        )
        self.validations.create(validation)

        return VerificationResult(
            accepted=passed,
            reason=evidence,
            validation=validation,
        )
