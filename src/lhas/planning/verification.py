"""Workflow verification for plan-step acceptance.

Connects the P3.1 verification seam (PlanExecutionService) to the existing
validation infrastructure (ValidationResult, ValidationCheck,
ValidationResultRepository).

Does NOT create a parallel validation framework — reuses existing models.
Does NOT implement adaptive validation (Phase 5).

EVIDENCE PROVENANCE:
- TOOL_CONTRACT_EVIDENCE: output/artifacts from tool execution via ToolContract,
  stored in execution_context by the service (trusted).
- AGENT_CLAIM: step.output or any agent-produced text (untrusted).
- Verification only succeeds with explicit acceptance contract (success_criteria
  or expected_effects) satisfied by trusted evidence.
- No acceptance contract → fail closed (NOT VERIFIED).
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

    EVIDENCE PROVENANCE RULES:
    1. No acceptance contract (no success_criteria AND no expected_effects)
       → fail closed (NOT VERIFIED). Non-empty output alone is never sufficient.
    2. Success criteria are checked against TRUSTED evidence only:
       execution_context["steps"][step.id]["artifacts"] (tool contract evidence).
    3. Expected effects are checked against TRUSTED evidence only:
       execution_context["steps"][step.id]["artifacts"] or
       execution_context["steps"][step.id]["output"] (tool contract output).
    4. Agent output (step.output) is AGENT_CLAIM — never trusted for verification.
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
        run = runs[-1]
        attempts = AttemptRepository(self.db).list_for_run(run.id)
        if not attempts:
            return None
        return attempts[-1].id

    def _get_trusted_evidence(self, step: Any) -> dict[str, Any]:
        """Extract trusted evidence from execution_context (tool contract output).

        Returns a dict of trusted key-value pairs from the tool's actual
        execution result. Does NOT include step.output (AGENT_CLAIM).
        """
        step_record = (
            step.execution_context.get("steps", {}).get(step.id, {})
            if step.execution_context
            else {}
        )
        # Trusted: artifacts produced by tool execution
        artifacts = (
            step_record.get("artifacts", {})
            if isinstance(step_record.get("artifacts"), dict)
            else {}
        )
        # Trusted: structured output from tool contract execution
        tool_output = (
            step_record.get("output", {})
            if isinstance(step_record.get("output"), dict)
            else {}
        )
        return {**tool_output, **artifacts}

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

        # --- 0. GATE: No acceptance contract → fail closed -----------------
        has_criteria = bool(step.success_criteria)
        has_effects = bool(step.expected_effects)
        if not has_criteria and not has_effects:
            # No acceptance contract defined — agent output alone is never
            # sufficient evidence. Fail closed.
            checks.append(
                ValidationCheck(
                    name="acceptance_contract_present",
                    passed=False,
                    detail="no success_criteria or expected_effects defined — cannot verify without explicit acceptance contract",
                )
            )
            return self._build_and_persist(
                step, checks, passed=False,
                reason="NO_ACCEPTANCE_CONTRACT: verification requires explicit success_criteria or expected_effects",
            )

        # --- 1. Collect trusted evidence (tool contract output) ------------
        trusted = self._get_trusted_evidence(step)
        has_trusted = bool(trusted)

        checks.append(
            ValidationCheck(
                name="trusted_evidence_present",
                passed=has_trusted,
                detail=None if has_trusted else "no trusted evidence in execution_context",
            )
        )

        # --- 2. Success-criteria verification against trusted evidence -----
        if has_criteria:
            for criterion in step.success_criteria:
                criterion_lower = criterion.lower()
                criterion_met = False
                detail = f"criterion not verified by trusted evidence: {criterion}"

                if has_trusted:
                    for ekey in trusted:
                        if ekey.lower() == criterion_lower or criterion_lower.startswith(ekey.lower()):
                            if ":" in criterion:
                                _, expected_val = criterion.split(":", 1)
                                observed = trusted.get(ekey)
                                if str(observed) == expected_val:
                                    criterion_met = True
                                    detail = None
                            else:
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

        # --- 3. Expected-effects verification against trusted evidence -----
        if has_effects:
            for key, expected_value in step.expected_effects.items():
                if key not in trusted:
                    effect_ok = False
                    detail = f"expected effect '{key}' not found in trusted evidence"
                else:
                    observed = trusted[key]
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
        return self._build_and_persist(step, checks, passed=passed)

    def _build_and_persist(
        self,
        step: Any,
        checks: list[ValidationCheck],
        passed: bool,
        reason: str | None = None,
    ) -> VerificationResult:
        """Build verdict, persist ValidationResult, return VerificationResult."""
        if reason is None:
            reason = "; ".join(
                f"{c.name}: {'ok' if c.passed else 'FAIL - ' + (c.detail or '')}"
                for c in checks
            )

        attempt_id = self._resolve_attempt_id(step)
        if attempt_id is None:
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
            evidence=reason,
        )
        self.validations.create(validation)

        return VerificationResult(
            accepted=passed,
            reason=reason,
            validation=validation,
        )
