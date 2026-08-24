from lhas.planning.models import CapabilitySpec
from lhas.tools.protocol import ToolResult, ToolResultStatus
from .errors import BinaryFileError, WorkspacePathEscape
from .safe_cli import SafeCli
def _result(fn):
    async def run(self, request):
        try: return ToolResult(status=ToolResultStatus.SUCCESS, output=await fn(self, request))
        except WorkspacePathEscape: return ToolResult(status=ToolResultStatus.FAILURE, error_type="WORKSPACE_PATH_ESCAPE", error_message="path outside workspace")
        except BinaryFileError: return ToolResult(status=ToolResultStatus.FAILURE, error_type="BINARY_FILE", error_message="binary file")
        except FileNotFoundError: return ToolResult(status=ToolResultStatus.FAILURE, error_type="FILE_NOT_FOUND", error_message="file not found")
        except IsADirectoryError: return ToolResult(status=ToolResultStatus.FAILURE, error_type="NOT_A_FILE", error_message="directory is not a file")
        except ValueError as exc: return ToolResult(status=ToolResultStatus.FAILURE, error_type="INVALID_ARGUMENTS", error_message=str(exc))
    return run
class WorkspaceListTool:
    capability=CapabilitySpec(name="workspace.list", description="List workspace files", input_schema={"type":"object"})
    def __init__(self, workspace): self.workspace=workspace
    execute=_result(lambda self, request: self.workspace.list_files(**request.arguments))
class WorkspaceReadTool:
    capability=CapabilitySpec(name="workspace.read", description="Read a workspace text file", input_schema={"type":"object"})
    def __init__(self, workspace): self.workspace=workspace
    execute=_result(lambda self, request: self.workspace.read_file(**request.arguments))
class WorkspaceSearchTool:
    capability=CapabilitySpec(name="workspace.search", description="Search workspace text", input_schema={"type":"object"})
    def __init__(self, workspace): self.workspace=workspace
    execute=_result(lambda self, request: self.workspace.search_text(**request.arguments))
class SafeCliTool:
    capability=CapabilitySpec(name="cli.exec", description="Execute an explicitly allowed command", input_schema={"type":"object"})
    def __init__(self, workspace, policy): self.cli=SafeCli(workspace, policy)
    async def execute(self, request):
        args=request.arguments
        try: output,error=await self.cli.execute(args.get("argv"), args.get("cwd", "."), args.get("timeout_seconds"))
        except WorkspacePathEscape: return ToolResult(status=ToolResultStatus.FAILURE,error_type="WORKSPACE_PATH_ESCAPE",error_message="cwd outside workspace")
        if error: return ToolResult(status=ToolResultStatus.FAILURE,error_type=error,error_message=error)
        return ToolResult(status=ToolResultStatus.SUCCESS,output=output)
def register_workspace_tools(registry, workspace, command_policy):
    for tool in (WorkspaceListTool(workspace), WorkspaceReadTool(workspace), WorkspaceSearchTool(workspace), SafeCliTool(workspace, command_policy)): registry.register(tool)
