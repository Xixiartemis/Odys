"""Validators (docs/06_VALIDATION_SPEC.md).

Principles:
- "Agent says Done" is NOT "Task Complete" — completion is judged here.
- Validator only judges; it never modifies the task result, never fixes code,
  never fills forms, never retries.
- Deterministic (V1 structural / V2 rule) validators take priority over
  LLM judges. V3 semantic / V4 action validation arrive in later phases.
"""

from __future__ import annotations

from typing import Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field

from lhas.domain.enums import FailureType
from lhas.domain.models import Attempt, Task, new_id
from lhas.executors.protocol import ExecutionResult


class ValidationLevel(str):
    V1_STRUCTURAL = "V1_STRUCTURAL"
    V2_RULE = "V2_RULE"
    V3_SEMANTIC = "V3_SEMANTIC"
    V4_ACTION = "V4_ACTION"


class ValidationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    detail: Optional[str] = None


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    attempt_id: str
    passed: bool
    level: str = ValidationLevel.V2_RULE
    checks: list[ValidationCheck] = Field(default_factory=list)
    evidence: Optional[str] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    duration_ms: int = 0

class Validator(Protocol):
    async def validate(
        self, *, task: Task, attempt: Attempt, result: ExecutionResult
    ) -> ValidationResult:
        ...


class RuleValidator:
    """Deterministic V1+V2 validation.

    Checks (all V2 rule checks, V1 for structure):
    - executor output is non-empty (when require_nonempty_output)
    - every expected marker appears in the output (e.g. "expected:ok")
    """

    def __init__(
        self,
        require_nonempty_output: bool = True,
        expected_markers: Optional[list[str]] = None,
    ):
        self.require_nonempty_output = require_nonempty_output
        self.expected_markers = expected_markers or []

    async def validate(
        self, *, task: Task, attempt: Attempt, result: ExecutionResult
    ) -> ValidationResult:
        checks: list[ValidationCheck] = []
        output = result.output or ""
        stdout = output

        if self.require_nonempty_output:
            checks.append(
                ValidationCheck(
                    name="output_non_empty",
                    passed=bool(output.strip()),
                    detail="executor returned no output" if not output.strip() else None,
                )
            )
        for marker in self.expected_markers:
            checks.append(
                ValidationCheck(
                    name=f"output_contains_{marker}",
                    passed=marker in output,
                    detail=f"output missing required marker {marker!r}" if marker not in output else None,
                )
            )
        if not checks:
            checks.append(ValidationCheck(name="no_checks", passed=True, detail="no checks configured"))

        passed = all(c.passed for c in checks)
        evidence = "; ".join(
            f"{c.name}: {'ok' if c.passed else 'FAIL - ' + (c.detail or '')}"
            for c in checks
        )
        return ValidationResult(
            attempt_id=attempt.id,
            passed=passed,
            checks=checks,
            evidence=evidence,
            stdout=stdout,
        )


class NeverPassValidator:
    """Test double: always fails validation."""

    async def validate(self, *, task, attempt, result) -> ValidationResult:
        return ValidationResult(
            attempt_id=attempt.id,
            passed=False,
            checks=[ValidationCheck(name="always_fail", passed=False, detail="configured to fail")],
            evidence="configured to fail",
        )


class AlwaysPassValidator:
    """Test double: always passes validation."""

    async def validate(self, *, task, attempt, result) -> ValidationResult:
        return ValidationResult(
            attempt_id=attempt.id,
            passed=True,
            checks=[ValidationCheck(name="always_pass", passed=True)],
            evidence="configured to pass",
        )
