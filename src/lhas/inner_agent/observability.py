"""Safe E7-A tool metrics derived from bounded observation summaries."""

from collections import Counter
from typing import Any, Iterable


def _bounded_counts(values: Counter[str], limit: int = 32) -> dict[str, int]:
    return {key: values[key] for key in sorted(values)[:limit]}


def project_tool_metrics(items: Iterable[dict[str, Any]]) -> dict[str, Any]:
    observations=[
        item for item in items
        if isinstance(item,dict)
        and item.get("event") in {None,"TOOL_OBSERVATION_SUMMARY"}
        and item.get("capability")
    ]
    edit_categories: Counter[str]=Counter(); cli_categories: Counter[str]=Counter()
    edit_calls=edit_successes=edit_failures=repeated=changes=reads_after=0
    cli_calls=cli_failures=pytest_runs=pytest_pass=pytest_fail=0
    first_pytest_call=None
    total_failures=0
    for fallback_index,item in enumerate(observations,1):
        capability=str(item.get("capability")); failed=item.get("status") == "FAILURE"
        call_index=int(item.get("tool_call_index") or fallback_index)
        total_failures += int(failed)
        if capability == "workspace.edit":
            edit_calls += 1
            if failed:
                edit_failures += 1
                edit_categories[str(item.get("failure_category") or item.get("error_type") or "UNKNOWN")] += 1
                if int(item.get("similar_failure_count") or item.get("failure_repeat_count") or 0) >= 2:
                    repeated += 1
            else:
                edit_successes += 1
        if item.get("strategy_change_observed") is True:
            changes += 1
        if capability in {"workspace.read","workspace.search"} and item.get("after_edit_failure") is True:
            reads_after += 1
        if capability == "cli.exec":
            cli_calls += 1
            if failed:
                cli_failures += 1
                cli_categories[str(item.get("failure_category") or item.get("error_type") or "UNKNOWN")] += 1
            if item.get("verification_kind") == "PYTEST":
                pytest_runs += 1
                first_pytest_call=call_index if first_pytest_call is None else min(first_pytest_call,call_index)
                observation=item.get("pytest_observation")
                pytest_pass += int(observation == "PASS")
                pytest_fail += int(observation in {"FAIL","ERROR"})
    return {
        "workspace_edit_calls":edit_calls,
        "workspace_edit_successes":edit_successes,
        "workspace_edit_failures":edit_failures,
        "edit_failure_categories":_bounded_counts(edit_categories),
        "repeated_edit_failures":repeated,
        "strategy_changes_after_repeated_failure":changes,
        "workspace_read_search_after_edit_failure":reads_after,
        "cli_exec_calls":cli_calls,
        "cli_exec_failures":cli_failures,
        "cli_failure_categories":_bounded_counts(cli_categories),
        "pytest_executions":pytest_runs,
        "pytest_pass_observations":pytest_pass,
        "pytest_fail_observations":pytest_fail,
        "first_pytest_tool_call":first_pytest_call,
        "total_tool_calls":len(observations),
        "total_tool_failures":total_failures,
    }
