import json, hashlib
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
        if spec.requires_human_approval or (spec.side_effect and name not in request.allowed_side_effect_capabilities): filtered.append(name); continue
        async def invoke(ctx, raw, _name=name, _spec=spec):
            try: args=json.loads(raw) if isinstance(raw,str) else raw
            except (TypeError,json.JSONDecodeError):
                if trace is not None:
                    trace.add("TOOL_INVOCATION_SIGNATURE", capability=_name, args_sha256=None)
                    trace.add("TOOL_OBSERVATION_SUMMARY", capability=_name, status="FAILURE", error_type="INVALID_ARGUMENTS")
                return {"status":"FAILURE","error_type":"INVALID_ARGUMENTS","error_message":"arguments must be valid JSON","output":None}
            try:
                if trace is not None and _name == "workspace.edit":
                    trace.add("WORKSPACE_EDIT_STARTED", relative_path=args.get("path"), status="STARTED")
                runtime_context = getattr(ctx, "context", None)
                if hasattr(runtime_context, "execution_context"):
                    runtime_context = runtime_context.execution_context
                if not isinstance(runtime_context, dict):
                    runtime_context = request.context
                result=await registry.resolve(_name).execute(ToolRequest(tool_call_id=getattr(ctx,"tool_call_id", "inner-tool"),task_id=request.task_id,run_id=request.run_id,attempt_id=request.attempt_id,capability=_name,arguments=args,context=runtime_context,metadata=request.metadata))
                if trace is not None:
                    normalized_args=json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
                    trace.add("TOOL_INVOCATION_SIGNATURE", capability=_name, args_sha256=hashlib.sha256(normalized_args.encode()).hexdigest())
                    output=result.output if isinstance(result.output, dict) else {}
                    summary={"capability":_name,"status":result.status.value}
                    if _name == "workspace.read": summary.update({k:output.get(k) for k in ("path","sha256","truncated") if k in output})
                    elif _name == "workspace.search": summary.update({"match_count":len(output.get("matches",[])),"matched_paths":list(dict.fromkeys(m.get("path") for m in output.get("matches",[]) if isinstance(m,dict) and m.get("path")))[:100],"truncated":output.get("truncated",False)})
                    elif _name == "workspace.edit": summary.update({k:output.get(k) for k in ("path","before_sha256","after_sha256") if k in output})
                    elif _name == "workspace.diff": summary.update({k:output.get(k) for k in ("changed_files","files_changed","lines_added","lines_removed","truncated") if k in output})
                    elif _name == "cli.exec": summary.update({k:output.get(k) for k in ("exit_code","timed_out","duration_ms","stdout_truncated","stderr_truncated") if k in output}); summary["command_name"]=args.get("argv",[None])[0]
                    if result.status.value == "FAILURE": summary["error_type"] = result.error_type or "UNKNOWN"
                    trace.add("TOOL_OBSERVATION_SUMMARY", **summary)
                    trace.add("TOOL_ACCOUNTING", tool_name=_name, usage=result.usage)
                    if _name in {"workspace.edit", "workspace.diff", "workspace.restore"} and isinstance(result.output, dict):
                        safe_keys = {k: result.output[k] for k in ("path", "changed_files", "files_changed", "lines_added", "lines_removed", "truncated", "before_sha256", "after_sha256", "bytes_before", "bytes_after", "restored") if k in result.output}
                        trace.add("WORKSPACE_CHANGE", tool_name=_name, summary=safe_keys)
                        if _name == "workspace.edit": trace.add("WORKSPACE_EDIT_COMPLETED", relative_path=safe_keys.get("path"), before_sha256=safe_keys.get("before_sha256"), after_sha256=safe_keys.get("after_sha256"), status=result.status.value)
                        if _name == "workspace.diff": trace.add("WORKSPACE_PATCH", patch=result.output)
                        if _name == "workspace.restore": trace.add("WORKSPACE_RESTORED", relative_path=safe_keys.get("path"), status=result.status.value)
                    if trace is not None and _name == "workspace.edit" and result.status.value != "SUCCESS":
                        trace.add("WORKSPACE_EDIT_FAILED", relative_path=args.get("path"), status=result.status.value, error_type=result.error_type)
                return {"status":result.status.value,"output":result.output,"artifacts":result.artifacts,"metadata":result.metadata,"error_type":result.error_type,"error_message":result.error_message}
            except Exception as exc:
                if trace is not None:
                    trace.add("TOOL_INVOCATION_SIGNATURE", capability=_name, args_sha256=None)
                    trace.add("TOOL_OBSERVATION_SUMMARY", capability=_name, status="FAILURE", error_type="TOOL_ADAPTER_ERROR")
                if trace is not None and _name == "workspace.edit": trace.add("WORKSPACE_EDIT_FAILED", relative_path=args.get("path"), status="FAILURE", error_type=str(exc) if str(exc) in {"WORKSPACE_PATH_ESCAPE","BINARY_FILE","STALE_FILE_VERSION","EDIT_TARGET_NOT_FOUND","EDIT_TARGET_AMBIGUOUS"} else "TOOL_ADAPTER_ERROR")
                return {"status":"FAILURE","error_type":"TOOL_ADAPTER_ERROR","error_message":str(exc),"output":None}
        allowed.append(FunctionTool(name=spec.name,description=spec.description,params_json_schema=spec.input_schema or {"type":"object","additionalProperties":False},on_invoke_tool=invoke))
    return allowed, filtered
