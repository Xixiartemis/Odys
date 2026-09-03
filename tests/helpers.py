"""Small deterministic validator doubles used by native completion tests."""

from __future__ import annotations

import json

from lhas.validation import ValidationCheck, ValidationResult


class PassingCommandValidator:
    """Represents a completed command validator with observed exit code zero."""

    def __init__(self, command: list[str] | None = None):
        self.command = command or ["pytest", "-q"]

    async def validate(self, *, task, attempt, result):
        return ValidationResult(
            attempt_id=attempt.id,
            passed=True,
            checks=[ValidationCheck(name="explicit_command_exit_zero", passed=True)],
            evidence=json.dumps({"command": self.command, "exit_code": 0, "timed_out": False}),
        )
