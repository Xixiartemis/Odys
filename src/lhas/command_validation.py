"""Explicit-command Outer Validator for ordinary local repositories."""

from __future__ import annotations

import json
import os
import shlex
import time
from typing import Sequence

from lhas.persistence.repositories import RunRepository
from lhas.validation import ValidationCheck, ValidationLevel, ValidationResult
from lhas.workspace.command_policy import CommandPolicy, CommandRule
from lhas.workspace.safe_cli import SafeCli


def parse_verification_command(command: str) -> list[str]:
    if not isinstance(command, str) or not command.strip():
        raise ValueError("VERIFICATION_COMMAND_REQUIRED")
    try:
        argv = shlex.split(command, posix=os.name != "nt")
    except ValueError as exc:
        raise ValueError("VERIFICATION_COMMAND_INVALID") from exc
    if os.name == "nt":
        argv = [
            item[1:-1] if len(item) >= 2 and item[0] == item[-1] and item[0] in {"'", '"'} else item
            for item in argv
        ]
    if not argv or not all(isinstance(item, str) and item for item in argv):
        raise ValueError("VERIFICATION_COMMAND_INVALID")
    # SafeCli performs the authoritative composition check as well. Reject
    # obvious shell operators before any Project/Task is created.
    forbidden = {"&&", "||", ";", "|", ">", ">>", "<", "`"}
    if any(item in forbidden for item in argv):
        raise ValueError("VERIFICATION_COMMAND_INVALID")
    return argv


def explicit_command_policy(argv: Sequence[str]) -> CommandPolicy:
    return CommandPolicy([CommandRule(list(argv), allow_extra_args=False)])


class ExplicitCommandValidator:
    """Run exactly one configured argv in the durable staged workspace.

    Output is bounded by SafeCli and persisted only as a short validation
    summary plus bounded stdout/stderr. No shell is involved.
    """

    def __init__(
        self,
        db,
        workspace_manager,
        argv: Sequence[str],
        *,
        timeout_seconds: float = 120.0,
        max_output_bytes: int = 8192,
    ):
        self.db = db
        self.workspace_manager = workspace_manager
        self.argv = list(argv)
        self.timeout_seconds = min(max(float(timeout_seconds), 0.1), 120.0)
        self.max_output_bytes = min(max(int(max_output_bytes), 1024), 64 * 1024)
        self.policy = explicit_command_policy(self.argv)

    async def validate(self, *, task, attempt, result) -> ValidationResult:
        run = RunRepository(self.db).get(attempt.run_id)
        if run is None:
            raise KeyError(f"Run {attempt.run_id} not found")
        session = self.workspace_manager.reopen_for_run(task, run)
        cli = SafeCli(
            session.workspace,
            self.policy,
            default_timeout=self.timeout_seconds,
            max_timeout=self.timeout_seconds,
            max_output_bytes=self.max_output_bytes,
        )
        started = time.monotonic()
        output, error = await cli.execute(self.argv, ".", self.timeout_seconds)
        measured_ms = int((time.monotonic() - started) * 1000)
        if output is None:
            timed_out = error == "COMMAND_TIMEOUT"
            evidence = {
                "command": self.argv,
                "exit_code": None,
                "timed_out": timed_out,
                "duration_ms": measured_ms,
                "stdout_truncated": False,
                "stderr_truncated": False,
                "error_type": error[0] if isinstance(error, tuple) else error,
            }
            stdout = ""
            stderr = ""
            exit_code = None
            passed = False
        else:
            evidence = {
                "command": self.argv,
                "exit_code": output["exit_code"],
                "timed_out": bool(output["timed_out"]),
                "duration_ms": int(output["duration_ms"]),
                "stdout_truncated": bool(output["stdout_truncated"]),
                "stderr_truncated": bool(output["stderr_truncated"]),
            }
            stdout = output["stdout"]
            stderr = output["stderr"]
            exit_code = output["exit_code"]
            passed = output["exit_code"] == 0 and not output["timed_out"]
        return ValidationResult(
            attempt_id=attempt.id,
            passed=passed,
            level=ValidationLevel.V2_RULE,
            checks=[
                ValidationCheck(
                    name="explicit_command_exit_zero",
                    passed=passed,
                    detail=None if passed else "configured verification command did not exit zero",
                )
            ],
            evidence=json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            duration_ms=int(evidence["duration_ms"]),
        )
