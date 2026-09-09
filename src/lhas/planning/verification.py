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
from lhas.persistence.repositories import AttemptRepository, RunRepository
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

    def _resolve_attempt_id(self, step: Any) -> str | None:
        """Resolve the real Attempt.id from step.task_id → Task → Run → Attempt."""
        if not step.task_id:
            return None
        runs = RunRepository(self.db).list_for_task(step.task_id)
        if not runs:
            return None
        # Prefer the most recent run
        run = runs[-1]
        attempts = AttemptRepository(self.db).list_for_run(run.id)
        if not attempts:
            return None
        # Prefer the most recent attempt
        return attempts[-1].id

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
        # Only criteria with an explicitly supported deterministic rule may be
        # automatically verified. Unsupported free-form criteria must NOT auto-pass.
        SUPPORTED_CRITERIA_MARKERS = {
            "exit_code": lambda out: "exit_code" in out,
            "tests passed": lambda out: False,  # self-assertion — not independent
            "no errors": lambda out: "traceback" not in out and "exception" not in out and "error:" not in out,
        }
        if step.success_criteria:
            for criterion in step.success_criteria:
                # Check if this criterion has a supported deterministic rule
                rule = None
                for marker, checker in SUPPORTED_CRITERIA_MARKERS.items():
                    if marker in criterion.lower():
                        rule = checker
                        break
                if rule is not None:
                    criterion_met = rule(output_text.lower()) if output_text else False
                else:
                    # Unsupported free-form criterion — fail closed
                    # Agent text alone is never sufficient evidence
                    criterion_met = False
                checks.append(
                    ValidationCheck(
                        name=f"criterion:{criterion[:64]}",
                        passed=criterion_met,
                        detail=(
                            None
                            if criterion_met
                            else f"acceptance criterion not independently verified: {criterion}"
                        ),
                    )
                )

        # --- 3. Expected-effects verification against execution context ----
        # BLOCKER 3: check actual VALUES, not just key presence
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
            combined = {**output_dict, **artifacts}
            for key, expected_value in step.expected_effects.items():
                if key not in combined:
                    effect_ok = False
                    detail = f"expected effect '{key}' not found in step output or artifacts"
                else:
                    observed = combined[key]
                    # Exact equality for scalar values
                    effect_ok = observed == expected_value
                    detail = (
                        None if effect_ok
                        else f"expected effect '{key}': expected {expected_value!r}, got {observed!r}"
                    )
                checks.append(
                    ValidationCheck(
                        name=f"effect:{key}",
                        passed=effect_ok,
                        detail=detail,
                    )
                )

        # --- Build verdict --------------------------------------------------
        passed = all(c.passed for c in checks)
        evidence = "; ".join(
            f"{c.name}: {'ok' if c.passed else 'FAIL - ' + (c.detail or '')}"
            for c in checks
        )

        # --- Persist for durability -----------------------------------------
        # BLOCKER 2: resolve real Attempt.id, fail closed if unresolvable
        attempt_id = self._resolve_attempt_id(step)
        if attempt_id is None:
            # Cannot resolve producing attempt — fail closed
            return VerificationResult(
                accepted=False,
                reason="NO_PRODUCING_ATTEMPT: cannot resolve Attempt.id from step.task_id",
                validation=None,
            )

        validation = ValidationResult(
            attempt_id=attempt_id,
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
