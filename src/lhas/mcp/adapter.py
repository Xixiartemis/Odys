from __future__ import annotations

from lhas.mcp.manager import MCPManager
from lhas.mcp.models import MCPToolInfo
from lhas.planning.models import CapabilitySpec
from lhas.tools.protocol import ToolRequest, ToolResult, ToolResultStatus
from lhas.tools.registry import ToolRegistry


class MCPToolAdapter:
    def __init__(self, manager: MCPManager, info: MCPToolInfo):
        self.manager = manager
        self.info = info

    @property
    def capability(self) -> CapabilitySpec:
        return CapabilitySpec(
            name=self.info.name,
            description=self.info.description,
            input_schema=self.info.input_schema,
            risk_level=self.info.risk,
            side_effect=self.info.side_effect,
            requires_human_approval=self.info.requires_approval,
            origin="mcp",
            server_name=self.info.server_name,
        )

    async def execute(self, request: ToolRequest) -> ToolResult:
        try:
            output = await self.manager.call_tool(self.info.name, request.arguments)
            return ToolResult(status=ToolResultStatus.SUCCESS, output=output, metadata={"origin":"mcp","server_name":self.info.server_name})
        except Exception as exc:
            return ToolResult(status=ToolResultStatus.FAILURE,error_type=type(exc).__name__,error_message=str(exc)[:500],metadata={"origin":"mcp","server_name":self.info.server_name})


def register_mcp_tools(registry: ToolRegistry, manager: MCPManager, tools: list[MCPToolInfo]) -> list[str]:
    capabilities=[]
    for info in tools:
        registry.register(MCPToolAdapter(manager,info))
        capabilities.append(info.name)
    return capabilities
