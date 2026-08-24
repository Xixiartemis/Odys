from typing import Any
try:
    from agents import RunHooks as _SdkRunHooks
except ImportError:
    class _SdkRunHooks:
        pass
class InnerAgentTrace:
    def __init__(self): self.items:list[dict[str,Any]]=[]
    def add(self,event:str,**fields): self.items.append({"event":event,**fields})

class OdysAgentsRunHooks(_SdkRunHooks):
    def __init__(self, trace=None):
        self.trace = trace if trace is not None else InnerAgentTrace(); self.turn_count = 0; self.tool_call_count = 0
    async def on_llm_start(self, *args, **kwargs):
        self.turn_count += 1; self.trace.add("LLM_TURN_STARTED", turn_number=self.turn_count)
    async def on_llm_end(self, *args, **kwargs): self.trace.add("LLM_TURN_COMPLETED", turn_number=self.turn_count)
    async def on_tool_start(self, context, agent, tool):
        self.tool_call_count += 1
        self.trace.add("TOOL_STARTED", tool_name=getattr(tool, "name", None), tool_call_id=getattr(context, "tool_call_id", None))
    async def on_tool_end(self, context, agent, tool, result):
        self.trace.add("TOOL_COMPLETED", tool_name=getattr(tool, "name", None), tool_call_id=getattr(context, "tool_call_id", None))
