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
    1. Structural — step must have produced output.
    2. Success criteria — each criterion is checked against STRUCTURED evidence
       only (artifacts, execution result keys). Agent textual output alone is
       NEVER sufficient. Unsupported criteria fail closed.
    3. Expected effects — exact value match against structured execution context.
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

        # --- 2. Success-criteria verification against STRUCTURED evidence ----
        # Agent textual output alone is NEVER sufficient evidence.
        # Only structured artifacts / execution result keys may verify criteria.
        # Build structured evidence set from execution context AND step.output
        step_record = (
            step.execution_context.get("steps", {}).get(step.id, {})
            if step.execution_context
            else {}
        )
        structured_output = (
            step_record.get("output", {})
            if isinstance(step_record.get("output"), dict)
            else {}
        )
        # Also consider step.output if it's a dict (direct structured output)
        if not structured_output and isinstance(step.output, dict):
            structured_output = step.output
        structured_artifacts = (
            step_record.get("artifacts", {})
            if isinstance(step_record.get("artifacts"), dict)
            else {}
        )
        structured_evidence_keys = set(structured_output.keys()) | set(structured_artifacts.keys())
        structured_evidence = {**structured_output, **structured_artifacts}

        if step.success_criteria:
            for criterion in step.success_criteria:
                criterion_lower = criterion.lower()
                criterion_met = False
                detail = f"criterion not independently verified: {criterion}"

                # Check against structured evidence keys only
                # Match criterion name to a key in structured evidence
                for ekey in structured_evidence_keys:
                    if ekey.lower() == criterion_lower or criterion_lower.startswith(ekey.lower()):
                        # Key found — check value if criterion implies a value
                        if ":" in criterion:
                            # criterion like "exit_code:0" — check value
                            _, expected_val = criterion.split(":", 1)
                            observed = structured_evidence.get(ekey)
                            if str(observed) == expected_val:
                                criterion_met = True
                                detail = None
                        else:
                            # Key exists in structured evidence — accept
                            criterion_met = True
                            detail = None
                        break

                checks.append(
                    ValidationCheck(
                        name=f"criterion:{criterion[:64]}",
                        passed=criterion_met,
                        detail=detail,
                    )
                )

        # --- 3. Expected-effects verification against execution context ----
        # Uses structured_evidence already built in section 2
        if step.expected_effects:
            for key, expected_value in step.expected_effects.items():
                if key not in structured_evidence:
                    effect_ok = False
                    detail = f"expected effect '{key}' not found in structured evidence"
                else:
                    observed = structured_evidence[key]
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
