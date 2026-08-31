from __future__ import annotations

import hashlib
import json
from typing import Any

from lhas.tools.protocol import ToolRequest


_RECOVERY_CAPABILITIES = {"workspace.read", "workspace.search"}
_EDIT_CAPABILITIES = {"workspace.edit", "workspace.edit_lines"}
_MAX_REPEAT_COUNT = 100


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
        summary.update({key: output.get(key) for key in (
            "path", "before_sha256", "after_sha256", "match_mode",
            "candidate_count", "matched_start_line", "matched_end_line",
        ) if key in output})
    elif name == "workspace.diff":
        summary.update({key: output.get(key) for key in ("changed_files", "files_changed", "lines_added", "lines_removed", "truncated") if key in output})
    elif name == "cli.exec":
        summary.update({key: output.get(key) for key in ("exit_code", "timed_out", "duration_ms", "stdout_truncated", "stderr_truncated") if key in output})
        summary["command_name"] = args.get("argv", [None])[0]
    if result.status.value == "FAILURE":
        summary["error_type"] = result.error_type or "UNKNOWN"
        recovery = result.metadata if isinstance(result.metadata, dict) else {}
        for key in (
            "failure_category", "reason_category", "action",
            "retry_same_arguments", "refresh_target_required",
            "candidate_count", "candidates_truncated",
            "normalization_attempted", "path_policy", "command_policy",
        ):
            if key in recovery:
                summary[key] = recovery[key]
    return summary


def _is_pytest_argv(args: dict[str, Any]) -> bool:
    argv=args.get("argv")
    if not isinstance(argv,list) or not argv or not all(isinstance(item,str) for item in argv):
        return False
    command=argv[0].replace("\\","/").rsplit("/",1)[-1].lower()
    if command in {"pytest","pytest.exe"}:
        return True
    return len(argv) >= 3 and argv[1:3] == ["-m","pytest"] and command in {"python","python.exe","python3","python3.exe"}


class ToolAwareObserver:
    """Attempt-local, bounded tool recovery and verification observations."""

    def __init__(self):
        self.call_index=0
        self._signature_failures: dict[tuple[str,str,str],int]={}
        self._last_similar_failure: tuple[str,str] | None=None
        self._similar_failure_count=0
        self._strategy_pending: tuple[str,str] | None=None
        self._edit_failure_pending=False
        self._last_mutation_call: int | None=None
        self._mutation_inspected=False

    @staticmethod
    def _bounded_increment(value: int) -> tuple[int,bool]:
        actual=value+1
        return min(actual,_MAX_REPEAT_COUNT), actual > _MAX_REPEAT_COUNT

    def decorate(self, name: str, args: dict[str,Any], result, summary: dict[str,Any], signature: str) -> dict[str,Any]:
        self.call_index += 1
        if name.startswith("workspace.") or name == "cli.exec":
            summary["tool_call_index"]=self.call_index
        status=result.status.value
        recovery=result.metadata if isinstance(result.metadata,dict) else {}
        category=str(recovery.get("failure_category") or result.error_type or "UNKNOWN")

        exact=0; exact_truncated=False
        if status == "FAILURE":
            key=(name,signature,result.error_type or "UNKNOWN")
            exact,exact_truncated=self._bounded_increment(self._signature_failures.get(key,0))
            self._signature_failures[key]=exact
            if exact >= 2:
                summary["failure_repeat_count"]=exact
                summary["strategy_change_required"]=True
            if exact_truncated:
                summary["repeat_count_truncated"]=True

        if name in _EDIT_CAPABILITIES and status == "FAILURE":
            similar=(name,category)
            if self._last_similar_failure == similar:
                self._similar_failure_count,truncated_similar=self._bounded_increment(self._similar_failure_count)
            else:
                self._similar_failure_count=1; truncated_similar=False
            self._last_similar_failure=similar
            self._edit_failure_pending=True
            summary["failure_category"]=category
            if self._similar_failure_count >= 2:
                summary["similar_failure_count"]=self._similar_failure_count
                summary["strategy_change_required"]=True
                self._strategy_pending=similar
            if exact_truncated or truncated_similar:
                summary["repeat_count_truncated"]=True
        elif name in _EDIT_CAPABILITIES and status == "SUCCESS":
            self._last_similar_failure=None; self._similar_failure_count=0
            if self._strategy_pending is not None:
                failed_capability,failed_category=self._strategy_pending
                summary.update({
                    "strategy_change_observed":True,
                    "strategy_change_from":failed_capability,
                    "strategy_change_failure_category":failed_category,
                    "strategy_change_to":name,
                })
                self._strategy_pending=None
                self._edit_failure_pending=False
            before=summary.get("before_sha256"); after=summary.get("after_sha256")
            if before and after and before != after:
                self._last_mutation_call=self.call_index
                self._mutation_inspected=False
                summary["meaningful_mutation"]=True
                summary["verification_recommended"]=True
        else:
            self._last_similar_failure=None; self._similar_failure_count=0

        if name in _RECOVERY_CAPABILITIES and self._edit_failure_pending:
            summary["after_edit_failure"]=True
        if name in _RECOVERY_CAPABILITIES and self._strategy_pending is not None:
            failed_capability,failed_category=self._strategy_pending
            summary.update({
                "strategy_change_observed":True,
                "strategy_change_from":failed_capability,
                "strategy_change_failure_category":failed_category,
                "strategy_change_to":name,
            })
            self._strategy_pending=None
        if name in _RECOVERY_CAPABILITIES:
            self._edit_failure_pending=False

        if self._last_mutation_call is not None and name in {"workspace.read","workspace.diff"} and self.call_index > self._last_mutation_call:
            self._mutation_inspected=True
            summary["post_edit_inspection"]=True
            summary["calls_since_mutation"]=self.call_index-self._last_mutation_call

        if name == "cli.exec" and _is_pytest_argv(args):
            summary["verification_kind"]="PYTEST"
            if status == "SUCCESS" and isinstance(result.output,dict):
                summary["pytest_observation"]="PASS" if result.output.get("exit_code") == 0 else "FAIL"
            else:
                summary["pytest_observation"]="ERROR"
            if self._last_mutation_call is not None:
                summary["post_edit_verification"]=True
                summary["inspection_before_verification"]=self._mutation_inspected
                summary["calls_since_mutation"]=self.call_index-self._last_mutation_call
        return summary


def allowed_tools(registry, request, trace=None):
    """Build SDK FunctionTool objects only for safe allow-listed capabilities."""
    try:
        from agents import FunctionTool
    except ImportError as exc:
        raise RuntimeError("agent extra is required for FunctionTool adapter") from exc

    allowed = []
    filtered = []
    observer=ToolAwareObserver()
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

                summary = observer.decorate(
                    _name,args,result,safe_tool_summary(_name,args,result),signature
                )

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
                                "match_mode", "candidate_count", "matched_start_line",
                                "matched_end_line",
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
                for key in (
                    "failure_repeat_count", "similar_failure_count",
                    "strategy_change_required", "repeat_count_truncated",
                ):
                    if key in summary:
                        observed[key]=summary[key]
                return observed
            except Exception as exc:
                message = str(exc)[:512]
                if trace is not None:
                    trace.add("TOOL_INVOCATION_SIGNATURE", capability=_name, args_sha256=signature)
                    trace.add("TOOL_OBSERVATION_SUMMARY", capability=_name, status="FAILURE", error_type="TOOL_ADAPTER_ERROR")
                    if _name in {"workspace.edit", "workspace.edit_lines"}:
                        known = {"WORKSPACE_PATH_ESCAPE", "BINARY_FILE", "STALE_FILE_VERSION", "EDIT_TARGET_NOT_FOUND", "EDIT_TARGET_AMBIGUOUS", "INVALID_EDIT_RANGE", "NO_CHANGE"}
                        trace.add("WORKSPACE_EDIT_FAILED", relative_path=args.get("path"), status="FAILURE", error_type=message if message in known else "TOOL_ADAPTER_ERROR", capability=_name)
                return {"status": "FAILURE", "error_type": "TOOL_ADAPTER_ERROR", "error_message": message, "output": None}

        allowed.append(FunctionTool(
            name=spec.name,
            description=spec.description,
            params_json_schema=spec.input_schema or {"type": "object", "additionalProperties": False},
            on_invoke_tool=invoke,
        ))
    return allowed, filtered
