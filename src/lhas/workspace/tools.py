from __future__ import annotations

from lhas.planning.models import CapabilitySpec
from lhas.tools.protocol import ToolResult, ToolResultStatus

from .errors import BinaryFileError, WorkspacePathEscape
from .safe_cli import SafeCli

_OBJECT = {"type": "object", "additionalProperties": False}
_LIST_SCHEMA = {**_OBJECT, "properties": {"path": {"type": "string"}, "recursive": {"type": "boolean"}, "max_entries": {"type": "integer"}}}
_READ_SCHEMA = {**_OBJECT, "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, "required": ["path"]}
_SEARCH_SCHEMA = {**_OBJECT, "properties": {"query": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"}, "max_matches": {"type": "integer"}, "context_lines": {"type": "integer"}}, "required": ["query"]}
_CLI_SCHEMA = {**_OBJECT, "properties": {"argv": {"type": "array", "items": {"type": "string"}, "minItems": 1}, "cwd": {"type": "string"}, "timeout_seconds": {"type": "number"}}, "required": ["argv"]}
_EDIT_SCHEMA = {**_OBJECT, "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}, "expected_sha256": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}
_EDIT_LINES_SCHEMA = {
    **_OBJECT,
    "properties": {
        "path": {"type": "string"},
        "start_line": {"type": "integer", "minimum": 1},
        "end_line": {"type": "integer", "minimum": 1},
        "new_lines": {"type": "array", "items": {"type": "string"}},
        "expected_sha256": {"type": "string", "minLength": 64, "maxLength": 64},
    },
    "required": ["path", "start_line", "end_line", "new_lines", "expected_sha256"],
}
_DIFF_SCHEMA = {**_OBJECT, "properties": {"path": {"type": "string"}, "max_diff_bytes": {"type": "integer", "minimum": 1, "maximum": 131072}}}
_RESTORE_SCHEMA = {**_OBJECT, "properties": {"path": {"type": "string"}}, "required": ["path"]}


def recovery_metadata(error_type: str, *, allowed_prefixes=None) -> dict:
    mapping = {
        "EDIT_TARGET_NOT_FOUND": {
            "action": "REREAD_THEN_LINE_EDIT",
            "retry_same_arguments": False,
            "suggested_capabilities": ["workspace.read", "workspace.edit_lines"],
        },
        "EDIT_TARGET_AMBIGUOUS": {
            "action": "NARROW_TARGET_OR_LINE_EDIT",
            "retry_same_arguments": False,
            "suggested_capabilities": ["workspace.read", "workspace.edit_lines"],
        },
        "STALE_FILE_VERSION": {
            "action": "REFRESH_FILE_VERSION",
            "retry_same_arguments": False,
            "suggested_capabilities": ["workspace.read"],
        },
        "WORKSPACE_PATH_ESCAPE": {
            "action": "USE_WORKSPACE_RELATIVE_CWD",
            "retry_same_arguments": False,
        },
    }
    metadata = dict(mapping.get(error_type, {}))
    if error_type == "COMMAND_NOT_ALLOWED":
        metadata = {
            "action": "USE_ALLOWED_COMMAND_PREFIX",
            "retry_same_arguments": False,
            "allowed_command_prefixes": (allowed_prefixes or [])[:20],
        }
    return metadata


def _failure(error_type: str, message: str, *, allowed_prefixes=None) -> ToolResult:
    return ToolResult(
        status=ToolResultStatus.FAILURE,
        error_type=error_type,
        error_message=message,
        metadata=recovery_metadata(error_type, allowed_prefixes=allowed_prefixes),
    )


def _result(fn):
    async def run(self, request):
        try:
            return ToolResult(status=ToolResultStatus.SUCCESS, output=await fn(self, request))
        except WorkspacePathEscape:
            return _failure("WORKSPACE_PATH_ESCAPE", "path outside workspace")
        except BinaryFileError:
            return _failure("BINARY_FILE", "binary file")
        except FileNotFoundError:
            return _failure("FILE_NOT_FOUND", "file not found")
        except IsADirectoryError:
            return _failure("NOT_A_FILE", "directory is not a file")
        except ValueError as exc:
            known = {"STALE_FILE_VERSION", "EDIT_TARGET_NOT_FOUND", "EDIT_TARGET_AMBIGUOUS", "INVALID_LINE_RANGE", "INVALID_ARGUMENTS"}
            error_type = str(exc) if str(exc) in known else "INVALID_ARGUMENTS"
            return _failure(error_type, str(exc))
    return run


class WorkspaceListTool:
    capability = CapabilitySpec(name="workspace.list", description="List workspace files", input_schema=_LIST_SCHEMA)
    def __init__(self, workspace): self.workspace = workspace
    execute = _result(lambda self, request: self.workspace.list_files(**request.arguments))


class WorkspaceReadTool:
    capability = CapabilitySpec(name="workspace.read", description="Read a workspace text file and current SHA-256", input_schema=_READ_SCHEMA)
    def __init__(self, workspace): self.workspace = workspace
    execute = _result(lambda self, request: self.workspace.read_file(**request.arguments))


class WorkspaceSearchTool:
    capability = CapabilitySpec(name="workspace.search", description="Search workspace text", input_schema=_SEARCH_SCHEMA)
    def __init__(self, workspace): self.workspace = workspace
    execute = _result(lambda self, request: self.workspace.search_text(**request.arguments))


class SafeCliTool:
    capability = CapabilitySpec(name="cli.exec", description="Execute an explicitly allowed command", input_schema=_CLI_SCHEMA)
    def __init__(self, workspace, policy): self.cli = SafeCli(workspace, policy)
    async def execute(self, request):
        args = request.arguments
        try:
            output, error = await self.cli.execute(args.get("argv"), args.get("cwd", "."), args.get("timeout_seconds"))
        except WorkspacePathEscape:
            return _failure("WORKSPACE_PATH_ESCAPE", "cwd outside workspace")
        if error:
            error_type, error_message = error if isinstance(error, tuple) else (error, error)
            return _failure(error_type, error_message, allowed_prefixes=self.cli.policy.allowed_prefixes())
        return ToolResult(status=ToolResultStatus.SUCCESS, output=output)


class WorkspaceEditTool:
    capability = CapabilitySpec(name="workspace.edit", description="Exact replacement in staged workspace", input_schema=_EDIT_SCHEMA, risk_level="MEDIUM", side_effect=True)
    def __init__(self, workspace): self.workspace = workspace
    async def execute(self, request):
        try:
            return ToolResult(status=ToolResultStatus.SUCCESS, output=await self.workspace.edit_file(**request.arguments))
        except WorkspacePathEscape:
            return _failure("WORKSPACE_PATH_ESCAPE", "path outside workspace")
        except BinaryFileError:
            return _failure("BINARY_FILE", "binary file")
        except (FileNotFoundError, IsADirectoryError):
            return _failure("EDIT_TARGET_NOT_FOUND", "edit target unavailable")
        except ValueError as exc:
            return _failure(str(exc), str(exc))


class WorkspaceEditLinesTool:
    capability = CapabilitySpec(name="workspace.edit_lines", description="Version-checked inclusive line replacement in staged workspace", input_schema=_EDIT_LINES_SCHEMA, risk_level="MEDIUM", side_effect=True)
    def __init__(self, workspace): self.workspace = workspace
    async def execute(self, request):
        try:
            return ToolResult(status=ToolResultStatus.SUCCESS, output=await self.workspace.edit_lines(**request.arguments))
        except WorkspacePathEscape:
            return _failure("WORKSPACE_PATH_ESCAPE", "path outside workspace")
        except BinaryFileError:
            return _failure("BINARY_FILE", "binary file")
        except (FileNotFoundError, IsADirectoryError):
            return _failure("EDIT_TARGET_NOT_FOUND", "edit target unavailable")
        except ValueError as exc:
            return _failure(str(exc), str(exc))


class WorkspaceDiffTool:
    capability = CapabilitySpec(name="workspace.diff", description="Show staged workspace changes", input_schema=_DIFF_SCHEMA)
    def __init__(self, workspace): self.workspace = workspace
    async def execute(self, request):
        try:
            return ToolResult(status=ToolResultStatus.SUCCESS, output=await self.workspace.diff(**request.arguments))
        except WorkspacePathEscape:
            return _failure("WORKSPACE_PATH_ESCAPE", "path outside workspace")
        except ValueError as exc:
            return _failure("INVALID_ARGUMENTS", str(exc))


class WorkspaceRestoreTool:
    capability = CapabilitySpec(name="workspace.restore", description="Restore staged file to baseline", input_schema=_RESTORE_SCHEMA, risk_level="MEDIUM", side_effect=True)
    def __init__(self, workspace): self.workspace = workspace
    async def execute(self, request):
        try:
            return ToolResult(status=ToolResultStatus.SUCCESS, output=await self.workspace.restore_file(**request.arguments))
        except WorkspacePathEscape:
            return _failure("WORKSPACE_PATH_ESCAPE", "path outside workspace")
        except FileNotFoundError:
            return _failure("RESTORE_FAILED", "baseline unavailable")
        except Exception:
            return _failure("RESTORE_FAILED", "restore failed")


def register_workspace_tools(registry, workspace, command_policy):
    for tool in (WorkspaceListTool(workspace), WorkspaceReadTool(workspace), WorkspaceSearchTool(workspace), SafeCliTool(workspace, command_policy)):
        registry.register(tool)


def register_staged_workspace_tools(registry, workspace, command_policy):
    register_workspace_tools(registry, workspace, command_policy)
    for tool in (WorkspaceEditTool(workspace), WorkspaceEditLinesTool(workspace), WorkspaceDiffTool(workspace), WorkspaceRestoreTool(workspace)):
        registry.register(tool)
