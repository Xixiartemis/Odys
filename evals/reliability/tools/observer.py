"""Workspace state observer tool for the benchmark harness.

Provides inspect_state — a read-only probe that reports workspace
files, checksums, and a quick test-pass indicator.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Any

from lhas.planning.models import CapabilitySpec
from lhas.tools.protocol import ToolRequest, ToolResult, ToolResultStatus


class InspectStateTool:
    """Inspect workspace state: file listing, checksums, test status."""

    def __init__(self, workspace_root: Path) -> None:
        self._root = workspace_root

    @property
    def capability(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="observer.inspect_state",
            description="Inspect workspace state: files, checksums, test status.",
            input_schema={
                "type": "object",
                "properties": {
                    "workspace": {"type": "string", "default": "."},
                },
                "required": [],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "files": {"type": "array", "items": {"type": "string"}},
                    "checksums": {"type": "object", "additionalProperties": {"type": "string"}},
                    "tests_passed": {"type": "boolean"},
                },
            },
            side_effect=False,
        )

    async def execute(self, request: ToolRequest) -> ToolResult:
        workspace = request.arguments.get("workspace", ".")
        try:
            target = (self._root / workspace).resolve()
            if not str(target).startswith(str(self._root.resolve())):
                return ToolResult(
                    status=ToolResultStatus.FAILURE,
                    error_type="INVALID_ARGUMENT",
                    error_message=f"workspace escapes root: {workspace}",
                )
            if not target.is_dir():
                return ToolResult(
                    status=ToolResultStatus.FAILURE,
                    error_type="INVALID_ARGUMENT",
                    error_message=f"not a directory: {workspace}",
                )

            # Collect files and checksums
            files: list[str] = []
            checksums: dict[str, str] = {}
            for fpath in sorted(target.rglob("*")):
                if not fpath.is_file():
                    continue
                rel = str(fpath.relative_to(self._root)).replace("\\", "/")
                files.append(rel)
                try:
                    data = fpath.read_bytes()
                    checksums[rel] = hashlib.sha256(data).hexdigest()
                except OSError:
                    checksums[rel] = "UNREADABLE"

            # Quick test probe
            tests_passed = self._probe_tests(target)

            return ToolResult(
                status=ToolResultStatus.SUCCESS,
                output={
                    "files": files,
                    "checksums": checksums,
                    "tests_passed": tests_passed,
                },
            )
        except Exception as exc:
            return ToolResult(
                status=ToolResultStatus.FAILURE,
                error_type="EXECUTION_FAILED",
                error_message=str(exc),
            )

    def _probe_tests(self, cwd: Path) -> bool:
        """Best-effort check: does `pytest --co -q` succeed in *cwd*?"""
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "--co", "-q"],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=30,
            )
            return proc.returncode == 0
        except Exception:
            return False
