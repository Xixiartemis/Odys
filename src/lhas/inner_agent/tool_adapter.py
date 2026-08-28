from __future__ import annotations

import hashlib
import json
from typing import Any

from lhas.tools.protocol import ToolRequest


def _args_signature(args: dict[str, Any]) -> str:
    normalized = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def safe_tool_summary(name: str, args: dict[str, Any], result) -> dict[str, Any]:
    output = result.output if isinstance(result.output, dict) else {}
    summary: dict[str, Any] = {"capability": name, "status": result.status.value}
    if name == "workspace.read":
        summary.update({key: output.get(key) for key in ("path", "sha256", "truncated") if key in output})
    elif name == "workspace.search":
        summary.update({
            "match_count": len(output.get("matches", [])),
            "matched_paths": list(dict.fromkeys(
                match.get("path") for match in output.get("matches", [])
                if isinstance(match, dict) and match.get("path")
            ))[:100],
            "truncated": output.get("truncated", False),
        })
    elif name in {"workspace.edit", "workspace.edit_lines"}:
        summary.update({key: output.get(key) for key in ("path", "before_sha256", "after_sha256") if key in output})
    elif name == "workspace.diff":
        summary.update({key: output.get(key) for key in ("changed_files", "files_changed", "lines_added", "lines_removed", "truncated") if key in output})
    elif name == "cli.exec":
        summary.update({key: output.get(key) for key in ("exit_code", "timed_out", "duration_ms", "stdout_truncated", "stderr_truncated") if key in output})
        summary["command_name"] = args.get("argv", [None])[0]
    if result.status.value == "FAILURE":
        summary["error_type"] = result.error_type or "UNKNOWN"
        recovery = result.metadata if isinstance(result.metadata, dict) else {}
        for key in ("action", "retry_same_arguments"):
            if key in recovery:
                summary[key] = recovery[key]
    return summary


def allowed_tools(registry, request, trace=None):
    """Build SDK FunctionTool objects only for safe allow-listed capabilities."""
    try:
        from agents import FunctionTool
    except ImportError as exc:
        raise RuntimeError("agent extra is required for FunctionTool adapter") from exc

    allowed = []
    filtered = []
    failure_counts: dict[tuple[str, str, str], int] = {}
    for name in request.allowed_capabilities:
        try:
            tool = registry.resolve(name)
        except KeyError:
            filtered.append(name)
            continue
        spec = tool.capability
        if spec.requires_human_approval or (spec.side_effect and name not in request.allowed_side_effect_capabilities):
            filtered.append(name)
            continue

        async def invoke(ctx, raw, _name=name, _spec=spec):
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
                if not isinstance(args, dict):
                    raise TypeError
            except (TypeError, json.JSONDecodeError):
                if trace is not None:
                    trace.add("TOOL_INVOCATION_SIGNATURE", capability=_name, args_sha256=None)
                    trace.add("TOOL_OBSERVATION_SUMMARY", capability=_name, status="FAILURE", error_type="INVALID_ARGUMENTS")
                return {"status": "FAILURE", "error_type": "INVALID_ARGUMENTS", "error_message": "arguments must be valid JSON", "output": None}

            signature = _args_signature(args)
            try:
                if trace is not None and _name in {"workspace.edit", "workspace.edit_lines"}:
                    trace.add("WORKSPACE_EDIT_STARTED", relative_path=args.get("path"), status="STARTED", capability=_name)
                runtime_context = getattr(ctx, "context", None)
                if hasattr(runtime_context, "execution_context"):
                    runtime_context = runtime_context.execution_context
                if not isinstance(runtime_context, dict):
                    runtime_context = request.context
                result = await registry.resolve(_name).execute(ToolRequest(
                    tool_call_id=getattr(ctx, "tool_call_id", "inner-tool"),
                    task_id=request.task_id,
                    run_id=request.run_id,
                    attempt_id=request.attempt_id,
                    capability=_name,
                    arguments=args,
                    context=runtime_context,
                    metadata=request.metadata,
                ))

                summary = safe_tool_summary(_name, args, result)
                repeat_count = 0
                strategy_change_required = False
                if result.status.value == "FAILURE":
                    failure_key = (_name, signature, result.error_type or "UNKNOWN")
                    repeat_count = failure_counts.get(failure_key, 0) + 1
                    failure_counts[failure_key] = repeat_count
                    strategy_change_required = repeat_count >= 2
                    if strategy_change_required:
                        summary["failure_repeat_count"] = repeat_count
                        summary["strategy_change_required"] = True

                if trace is not None:
                    trace.add("TOOL_INVOCATION_SIGNATURE", capability=_name, args_sha256=signature)
                    trace.add("TOOL_OBSERVATION_SUMMARY", **summary)
                    trace.add("TOOL_ACCOUNTING", tool_name=_name, usage=result.usage)
                    if _name in {"workspace.edit", "workspace.edit_lines", "workspace.diff", "workspace.restore"} and isinstance(result.output, dict):
                        safe_keys = {
                            key: result.output[key]
                            for key in (
                                "path", "changed_files", "files_changed", "lines_added",
                                "lines_removed", "truncated", "before_sha256",
                                "after_sha256", "bytes_before", "bytes_after",
                                "start_line", "end_line", "lines_written", "restored",
                            )
                            if key in result.output
                        }
                        trace.add("WORKSPACE_CHANGE", tool_name=_name, summary=safe_keys)
                        if _name in {"workspace.edit", "workspace.edit_lines"}:
                            trace.add("WORKSPACE_EDIT_COMPLETED", relative_path=safe_keys.get("path"), before_sha256=safe_keys.get("before_sha256"), after_sha256=safe_keys.get("after_sha256"), status=result.status.value, capability=_name)
                        if _name == "workspace.diff":
                            trace.add("WORKSPACE_PATCH", patch=result.output)
                        if _name == "workspace.restore":
                            trace.add("WORKSPACE_RESTORED", relative_path=safe_keys.get("path"), status=result.status.value)
                    if _name in {"workspace.edit", "workspace.edit_lines"} and result.status.value != "SUCCESS":
                        trace.add("WORKSPACE_EDIT_FAILED", relative_path=args.get("path"), status=result.status.value, error_type=result.error_type, capability=_name)

                observed = {
                    "status": result.status.value,
                    "output": result.output,
                    "artifacts": result.artifacts,
                    "metadata": result.metadata,
                    "error_type": result.error_type,
                    "error_message": result.error_message,
                }
                if result.status.value == "FAILURE" and strategy_change_required:
                    observed["failure_repeat_count"] = repeat_count
                    observed["strategy_change_required"] = True
                return observed
            except Exception as exc:
                message = str(exc)[:512]
                if trace is not None:
                    trace.add("TOOL_INVOCATION_SIGNATURE", capability=_name, args_sha256=signature)
                    trace.add("TOOL_OBSERVATION_SUMMARY", capability=_name, status="FAILURE", error_type="TOOL_ADAPTER_ERROR")
                    if _name in {"workspace.edit", "workspace.edit_lines"}:
                        known = {"WORKSPACE_PATH_ESCAPE", "BINARY_FILE", "STALE_FILE_VERSION", "EDIT_TARGET_NOT_FOUND", "EDIT_TARGET_AMBIGUOUS", "INVALID_LINE_RANGE"}
                        trace.add("WORKSPACE_EDIT_FAILED", relative_path=args.get("path"), status="FAILURE", error_type=message if message in known else "TOOL_ADAPTER_ERROR", capability=_name)
                return {"status": "FAILURE", "error_type": "TOOL_ADAPTER_ERROR", "error_message": message, "output": None}

        allowed.append(FunctionTool(
            name=spec.name,
            description=spec.description,
            params_json_schema=spec.input_schema or {"type": "object", "additionalProperties": False},
            on_invoke_tool=invoke,
        ))
    return allowed, filtered
