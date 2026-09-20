"""Concrete test execution tool for the benchmark harness.

Implements cli.exec — runs a shell command and returns structured results.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from lhas.planning.models import CapabilitySpec
from lhas.tools.protocol import ToolRequest, ToolResult, ToolResultStatus


class RunTestTool:
    """Run a test command and return structured results (cli.exec)."""

    def __init__(self, workspace_root: Path) -> None:
        self._root = workspace_root

    @property
    def capability(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="cli.exec",
            description="Execute a shell command and return its output and exit code.",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string", "default": "."},
                    "timeout_seconds": {"type": "number", "default": 120},
                },
                "required": ["command"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "passed": {"type": "boolean"},
                    "failed_tests": {"type": "array", "items": {"type": "string"}},
                    "exit_code": {"type": "integer"},
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                },
            },
            side_effect=True,
        )

    async def execute(self, request: ToolRequest) -> ToolResult:
        command = request.arguments.get("command")
        if not command:
            return ToolResult(
                status=ToolResultStatus.FAILURE,
                error_type="INVALID_ARGUMENT",
                error_message="command is required",
            )
        cwd_arg = request.arguments.get("cwd", ".")
        timeout = request.arguments.get("timeout_seconds", 120)

        try:
            cwd = (self._root / cwd_arg).resolve()
            if not str(cwd).startswith(str(self._root.resolve())):
                return ToolResult(
                    status=ToolResultStatus.FAILURE,
                    error_type="INVALID_ARGUMENT",
                    error_message=f"cwd escapes workspace root: {cwd_arg}",
                )
        except Exception as exc:
            return ToolResult(
                status=ToolResultStatus.FAILURE,
                error_type="INVALID_ARGUMENT",
                error_message=f"invalid cwd: {exc}",
            )

        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                status=ToolResultStatus.FAILURE,
                error_type="TIMEOUT",
                error_message=f"command timed out after {timeout}s",
            )
        except Exception as exc:
            return ToolResult(
                status=ToolResultStatus.FAILURE,
                error_type="EXECUTION_FAILED",
                error_message=str(exc),
            )

        # Parse pytest-style failures from stdout
        failed_tests = _extract_failed_tests(proc.stdout)
        passed = proc.returncode == 0

        return ToolResult(
            status=ToolResultStatus.SUCCESS,
            output={
                "passed": passed,
                "failed_tests": failed_tests,
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            },
        )


def _extract_failed_tests(stdout: str) -> list[str]:
    """Best-effort extraction of failed test names from pytest output."""
    failed: list[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("FAILED "):
            failed.append(stripped.split("FAILED ", 1)[1].strip())
        elif stripped.startswith("FAIL ") and "::" in stripped:
            failed.append(stripped.split("FAIL ", 1)[1].strip())
    return failed
