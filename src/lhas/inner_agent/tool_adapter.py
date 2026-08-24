import json
from typing import Any
from lhas.tools.protocol import ToolRequest, ToolResultStatus

def allowed_tools(registry, request, trace=None):
    """Build SDK FunctionTool objects only for safe allow-listed capabilities."""
    try:
        from agents import FunctionTool
    except ImportError as exc: raise RuntimeError("agent extra is required for FunctionTool adapter") from exc
    allowed=[]; filtered=[]
    for name in request.allowed_capabilities:
        try: tool=registry.resolve(name)
        except KeyError: filtered.append(name); continue
        spec=tool.capability
        if spec.side_effect or spec.requires_human_approval: filtered.append(name); continue
        async def invoke(ctx, raw, _name=name, _spec=spec):
            try: args=json.loads(raw) if isinstance(raw,str) else raw
            except (TypeError,json.JSONDecodeError): return {"status":"FAILURE","error_type":"INVALID_ARGUMENTS","error_message":"arguments must be valid JSON","output":None}
            try:
                result=await registry.resolve(_name).execute(ToolRequest(tool_call_id=getattr(ctx,"tool_call_id", "inner-tool"),task_id=request.task_id,run_id=request.run_id,attempt_id=request.attempt_id,capability=_name,arguments=args,context=request.context,metadata=request.metadata))
                return {"status":result.status.value,"output":result.output,"artifacts":result.artifacts,"usage":result.usage,"metadata":result.metadata,"error_type":result.error_type,"error_message":result.error_message}
            except Exception as exc: return {"status":"FAILURE","error_type":"TOOL_ADAPTER_ERROR","error_message":str(exc),"output":None}
        allowed.append(FunctionTool(name=spec.name,description=spec.description,params_json_schema=spec.input_schema or {"type":"object","additionalProperties":False},on_invoke_tool=invoke))
    return allowed, filtered
