"""Concrete filesystem tool implementations for the benchmark harness.

Implements workspace.list, workspace.read, workspace.search,
workspace.edit, workspace.edit_lines, and workspace.diff.
"""

from __future__ import annotations

import difflib
import hashlib
import os
from pathlib import Path
from typing import Any

from lhas.planning.models import CapabilitySpec
from lhas.tools.protocol import ToolRequest, ToolResult, ToolResultStatus


class _BaseTool:
    """Shared helpers for benchmark filesystem tools."""

    def _resolve(self, path: str, workspace_root: Path) -> Path:
        """Resolve *path* against workspace_root, rejecting escapes."""
        resolved = (workspace_root / path).resolve()
        if not str(resolved).startswith(str(workspace_root.resolve())):
            raise ValueError(f"path escapes workspace root: {path}")
        return resolved

    def _failure(self, error_type: str, message: str) -> ToolResult:
        return ToolResult(
            status=ToolResultStatus.FAILURE,
            error_type=error_type,
            error_message=message,
        )

    def _success(self, output: Any, **meta: Any) -> ToolResult:
        return ToolResult(
            status=ToolResultStatus.SUCCESS,
            output=output,
            metadata=meta,
        )


# ---------------------------------------------------------------------------
# workspace.list
# ---------------------------------------------------------------------------

class ListFilesTool(_BaseTool):
    """List files in a directory (workspace.list)."""

    def __init__(self, workspace_root: Path) -> None:
        self._root = workspace_root

    @property
    def capability(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="workspace.list",
            description="List files in a directory relative to the workspace root.",
            input_schema={
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "default": "."},
                },
                "required": [],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "files": {"type": "array", "items": {"type": "string"}},
                },
            },
            side_effect=False,
        )

    async def execute(self, request: ToolRequest) -> ToolResult:
        directory = request.arguments.get("directory", ".")
        try:
            target = self._resolve(directory, self._root)
            if not target.is_dir():
                return self._failure("INVALID_ARGUMENT", f"not a directory: {directory}")
            files = sorted(
                str(p.relative_to(self._root)).replace("\\", "/")
                for p in target.rglob("*")
                if p.is_file()
            )
            return self._success({"files": files})
        except Exception as exc:
            return self._failure("EXECUTION_FAILED", str(exc))


# ---------------------------------------------------------------------------
# workspace.read
# ---------------------------------------------------------------------------

class ReadFileTool(_BaseTool):
    """Read file contents (workspace.read)."""

    def __init__(self, workspace_root: Path) -> None:
        self._root = workspace_root

    @property
    def capability(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="workspace.read",
            description="Read the contents of a file relative to the workspace root.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "required": ["path"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "size": {"type": "integer"},
                },
            },
            side_effect=False,
        )

    async def execute(self, request: ToolRequest) -> ToolResult:
        path = request.arguments.get("path")
        if not path:
            return self._failure("INVALID_ARGUMENT", "path is required")
        try:
            target = self._resolve(path, self._root)
            if not target.is_file():
                return self._failure("INVALID_ARGUMENT", f"not a file: {path}")
            content = target.read_text(encoding="utf-8")
            return self._success({"content": content, "size": len(content)})
        except Exception as exc:
            return self._failure("EXECUTION_FAILED", str(exc))


# ---------------------------------------------------------------------------
# workspace.search
# ---------------------------------------------------------------------------

class SearchFilesTool(_BaseTool):
    """Search for a pattern in workspace files (workspace.search)."""

    def __init__(self, workspace_root: Path) -> None:
        self._root = workspace_root

    @property
    def capability(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="workspace.search",
            description="Search for a text pattern across files in the workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "file_glob": {"type": "string", "default": "*"},
                },
                "required": ["pattern"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "matches": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file": {"type": "string"},
                                "line": {"type": "integer"},
                                "content": {"type": "string"},
                            },
                        },
                    },
                    "total": {"type": "integer"},
                },
            },
            side_effect=False,
        )

    async def execute(self, request: ToolRequest) -> ToolResult:
        pattern = request.arguments.get("pattern")
        if not pattern:
            return self._failure("INVALID_ARGUMENT", "pattern is required")
        subpath = request.arguments.get("path", ".")
        try:
            target = self._resolve(subpath, self._root)
            if not target.is_dir():
                return self._failure("INVALID_ARGUMENT", f"not a directory: {subpath}")
            matches: list[dict[str, Any]] = []
            for fpath in sorted(target.rglob("*")):
                if not fpath.is_file():
                    continue
                try:
                    text = fpath.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                for lineno, line in enumerate(text.splitlines(), 1):
                    if pattern in line:
                        matches.append({
                            "file": str(fpath.relative_to(self._root)).replace("\\", "/"),
                            "line": lineno,
                            "content": line,
                        })
            return self._success({"matches": matches, "total": len(matches)})
        except Exception as exc:
            return self._failure("EXECUTION_FAILED", str(exc))


# ---------------------------------------------------------------------------
# workspace.edit
# ---------------------------------------------------------------------------

class WriteFileTool(_BaseTool):
    """Write (create/overwrite) a file (workspace.edit)."""

    def __init__(self, workspace_root: Path) -> None:
        self._root = workspace_root

    @property
    def capability(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="workspace.edit",
            description="Write content to a file, creating parent directories as needed.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "bytes_written": {"type": "integer"},
                    "checksum": {"type": "string"},
                },
            },
            side_effect=True,
        )

    async def execute(self, request: ToolRequest) -> ToolResult:
        path = request.arguments.get("path")
        content = request.arguments.get("content")
        if not path or content is None:
            return self._failure("INVALID_ARGUMENT", "path and content are required")
        try:
            target = self._resolve(path, self._root)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
            return self._success({
                "path": path,
                "bytes_written": len(content.encode("utf-8")),
                "checksum": checksum,
            })
        except Exception as exc:
            return self._failure("EXECUTION_FAILED", str(exc))


# ---------------------------------------------------------------------------
# workspace.edit_lines
# ---------------------------------------------------------------------------

class EditLinesTool(_BaseTool):
    """Find-and-replace edit in a file (workspace.edit_lines)."""

    def __init__(self, workspace_root: Path) -> None:
        self._root = workspace_root

    @property
    def capability(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="workspace.edit_lines",
            description="Find and replace text in a file (targeted patch).",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "replaced": {"type": "boolean"},
                    "before_sha256": {"type": "string"},
                    "after_sha256": {"type": "string"},
                    "old_string_sha256": {"type": "string"},
                    "new_string_sha256": {"type": "string"},
                    "old_string_length": {"type": "integer"},
                    "new_string_length": {"type": "integer"},
                },
            },
            side_effect=True,
        )

    async def execute(self, request: ToolRequest) -> ToolResult:
        path = request.arguments.get("path")
        old_string = request.arguments.get("old_string")
        new_string = request.arguments.get("new_string")
        if not path or old_string is None or new_string is None:
            return self._failure("INVALID_ARGUMENT", "path, old_string, and new_string are required")
        try:
            target = self._resolve(path, self._root)
            if not target.is_file():
                return self._failure("INVALID_ARGUMENT", f"file not found: {path}")
            content = target.read_text(encoding="utf-8")
            if old_string not in content:
                return self._failure("INVALID_ARGUMENT", "old_string not found in file")
            updated = content.replace(old_string, new_string, 1)
            before_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
            after_sha256 = hashlib.sha256(updated.encode("utf-8")).hexdigest()
            target.write_text(updated, encoding="utf-8")
            return self._success({
                "path": path,
                "replaced": True,
                "before_sha256": before_sha256,
                "after_sha256": after_sha256,
                "old_string_sha256": hashlib.sha256(old_string.encode("utf-8")).hexdigest(),
                "new_string_sha256": hashlib.sha256(new_string.encode("utf-8")).hexdigest(),
                "old_string_length": len(old_string),
                "new_string_length": len(new_string),
            })
        except Exception as exc:
            return self._failure("EXECUTION_FAILED", str(exc))


# ---------------------------------------------------------------------------
# workspace.diff
# ---------------------------------------------------------------------------

class DiffTool(_BaseTool):
    """Show a unified diff between a file's current state and new content (workspace.diff)."""

    def __init__(self, workspace_root: Path) -> None:
        self._root = workspace_root

    @property
    def capability(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="workspace.diff",
            description="Compute a unified diff between a file and proposed new content.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "new_content": {"type": "string"},
                },
                "required": ["path", "new_content"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "diff": {"type": "string"},
                    "changed": {"type": "boolean"},
                },
            },
            side_effect=False,
        )

    async def execute(self, request: ToolRequest) -> ToolResult:
        path = request.arguments.get("path")
        new_content = request.arguments.get("new_content")
        if not path or new_content is None:
            return self._failure("INVALID_ARGUMENT", "path and new_content are required")
        try:
            target = self._resolve(path, self._root)
            old_content = ""
            if target.is_file():
                old_content = target.read_text(encoding="utf-8")
            diff_lines = list(difflib.unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            ))
            diff_text = "".join(diff_lines)
            return self._success({"diff": diff_text, "changed": bool(diff_lines)})
        except Exception as exc:
            return self._failure("EXECUTION_FAILED", str(exc))
