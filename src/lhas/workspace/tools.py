from lhas.planning.models import CapabilitySpec
from lhas.tools.protocol import ToolResult, ToolResultStatus
from .errors import BinaryFileError, WorkspacePathEscape
from .safe_cli import SafeCli
from .staged import StagedWorkspace
_OBJECT = {"type": "object", "additionalProperties": False}
_LIST_SCHEMA = {**_OBJECT, "properties": {"path": {"type": "string"}, "recursive": {"type": "boolean"}, "max_entries": {"type": "integer"}}}
_READ_SCHEMA = {**_OBJECT, "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, "required": ["path"]}
_SEARCH_SCHEMA = {**_OBJECT, "properties": {"query": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"}, "max_matches": {"type": "integer"}, "context_lines": {"type": "integer"}}, "required": ["query"]}
_CLI_SCHEMA = {**_OBJECT, "properties": {"argv": {"type": "array", "items": {"type": "string"}, "minItems": 1}, "cwd": {"type": "string"}, "timeout_seconds": {"type": "number"}}, "required": ["argv"]}
_EDIT_SCHEMA = {**_OBJECT, "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}, "expected_sha256": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}
_DIFF_SCHEMA = {**_OBJECT, "properties": {"path": {"type": "string"}, "max_diff_bytes": {"type": "integer"}}}
_RESTORE_SCHEMA = {**_OBJECT, "properties": {"path": {"type": "string"}}, "required": ["path"]}
def _result(fn):
    async def run(self, request):
        try: return ToolResult(status=ToolResultStatus.SUCCESS, output=await fn(self, request))
        except WorkspacePathEscape: return ToolResult(status=ToolResultStatus.FAILURE, error_type="WORKSPACE_PATH_ESCAPE", error_message="path outside workspace")
        except BinaryFileError: return ToolResult(status=ToolResultStatus.FAILURE, error_type="BINARY_FILE", error_message="binary file")
        except FileNotFoundError: return ToolResult(status=ToolResultStatus.FAILURE, error_type="FILE_NOT_FOUND", error_message="file not found")
        except IsADirectoryError: return ToolResult(status=ToolResultStatus.FAILURE, error_type="NOT_A_FILE", error_message="directory is not a file")
        except ValueError as exc: return ToolResult(status=ToolResultStatus.FAILURE, error_type=str(exc) if str(exc) in {"STALE_FILE_VERSION","EDIT_TARGET_NOT_FOUND","EDIT_TARGET_AMBIGUOUS","INVALID_ARGUMENTS"} else "INVALID_ARGUMENTS", error_message=str(exc))
    return run
class WorkspaceListTool:
    capability=CapabilitySpec(name="workspace.list", description="List workspace files", input_schema=_LIST_SCHEMA)
    def __init__(self, workspace): self.workspace=workspace
    execute=_result(lambda self, request: self.workspace.list_files(**request.arguments))
class WorkspaceReadTool:
    capability=CapabilitySpec(name="workspace.read", description="Read a workspace text file", input_schema=_READ_SCHEMA)
    def __init__(self, workspace): self.workspace=workspace
    execute=_result(lambda self, request: self.workspace.read_file(**request.arguments))
class WorkspaceSearchTool:
    capability=CapabilitySpec(name="workspace.search", description="Search workspace text", input_schema=_SEARCH_SCHEMA)
    def __init__(self, workspace): self.workspace=workspace
    execute=_result(lambda self, request: self.workspace.search_text(**request.arguments))
class SafeCliTool:
    capability=CapabilitySpec(name="cli.exec", description="Execute an explicitly allowed command", input_schema=_CLI_SCHEMA)
    def __init__(self, workspace, policy): self.cli=SafeCli(workspace, policy)
    async def execute(self, request):
        args=request.arguments
        try: output,error=await self.cli.execute(args.get("argv"), args.get("cwd", "."), args.get("timeout_seconds"))
        except WorkspacePathEscape: return ToolResult(status=ToolResultStatus.FAILURE,error_type="WORKSPACE_PATH_ESCAPE",error_message="cwd outside workspace")
        if error:
            error_type, error_message = error if isinstance(error, tuple) else (error, error)
            return ToolResult(status=ToolResultStatus.FAILURE,error_type=error_type,error_message=error_message)
        return ToolResult(status=ToolResultStatus.SUCCESS,output=output)
class WorkspaceEditTool:
    capability=CapabilitySpec(name="workspace.edit", description="Exact replacement in staged workspace", input_schema=_EDIT_SCHEMA, risk_level="MEDIUM", side_effect=True)
    def __init__(self, workspace): self.workspace=workspace
    async def execute(self, request):
        try: return ToolResult(status=ToolResultStatus.SUCCESS, output=await self.workspace.edit_file(**request.arguments))
        except WorkspacePathEscape: return ToolResult(status=ToolResultStatus.FAILURE,error_type="WORKSPACE_PATH_ESCAPE",error_message="path outside workspace")
        except BinaryFileError: return ToolResult(status=ToolResultStatus.FAILURE,error_type="BINARY_FILE",error_message="binary file")
        except (FileNotFoundError,IsADirectoryError): return ToolResult(status=ToolResultStatus.FAILURE,error_type="EDIT_TARGET_NOT_FOUND",error_message="edit target unavailable")
        except ValueError as exc: return ToolResult(status=ToolResultStatus.FAILURE,error_type=str(exc),error_message=str(exc))
class WorkspaceDiffTool:
    capability=CapabilitySpec(name="workspace.diff", description="Show staged workspace changes", input_schema=_DIFF_SCHEMA)
    def __init__(self, workspace): self.workspace=workspace
    async def execute(self, request): return ToolResult(status=ToolResultStatus.SUCCESS, output=await self.workspace.diff(**request.arguments))
class WorkspaceRestoreTool:
    capability=CapabilitySpec(name="workspace.restore", description="Restore staged file to baseline", input_schema=_RESTORE_SCHEMA, risk_level="MEDIUM", side_effect=True)
    def __init__(self, workspace): self.workspace=workspace
    async def execute(self, request):
        try: return ToolResult(status=ToolResultStatus.SUCCESS, output=await self.workspace.restore_file(**request.arguments))
        except WorkspacePathEscape: return ToolResult(status=ToolResultStatus.FAILURE,error_type="WORKSPACE_PATH_ESCAPE",error_message="path outside workspace")
        except FileNotFoundError: return ToolResult(status=ToolResultStatus.FAILURE,error_type="RESTORE_FAILED",error_message="baseline unavailable")
        except Exception: return ToolResult(status=ToolResultStatus.FAILURE,error_type="RESTORE_FAILED",error_message="restore failed")
def register_workspace_tools(registry, workspace, command_policy):
    for tool in (WorkspaceListTool(workspace), WorkspaceReadTool(workspace), WorkspaceSearchTool(workspace), SafeCliTool(workspace, command_policy)): registry.register(tool)
def register_staged_workspace_tools(registry, workspace, command_policy):
    register_workspace_tools(registry, workspace, command_policy)
    for tool in (WorkspaceEditTool(workspace), WorkspaceDiffTool(workspace), WorkspaceRestoreTool(workspace)): registry.register(tool)
