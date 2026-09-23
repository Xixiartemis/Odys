"""Mock JudgeSystem for synthetic fixture tests.

Provides a simplified judge that checks task completion
without requiring the real ToolMaze judge.
"""

from __future__ import annotations
from typing import Any


class JudgeSystem:
    """Mock judge that evaluates task traces against expected results."""

    def judge(self, task: dict[str, Any], trace: dict[str, Any]) -> dict[str, Any]:
        """Evaluate a trace against a task.

        Returns a result dict with 'pass', 'failure_reason', and 'trace_check'.
        """
        expected = task.get("expected_result", {})
        execution_trace = task.get("execution_trace", [])
        messages = trace.get("messages", [])

        # Extract tool calls from the trace
        trace_tools = []
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("type") == "tool_call":
                tc = msg.get("tool_call", {})
                trace_tools.append(tc.get("name", ""))

        # Expected tools
        expected_tools = expected.get("tool_calls", [])

        # Check if trace has all expected tools
        if not trace_tools:
            return {
                "pass": False,
                "failure_reason": "empty trace — no tool calls",
                "trace_check": {
                    "expected_tools": expected_tools,
                    "actual_tools": trace_tools,
                    "match": False,
                },
            }

        # Check perturbation handling
        perturbation_mode = task.get("perturbation_mode", "P0")
        victim_tool = None
        if perturbation_mode != "P0" and execution_trace:
            pp = task.get("perturbation_point")
            if pp is not None and 0 <= pp < len(execution_trace):
                victim_tool = execution_trace[pp].get("tool_name")

        # For P0 tasks: all expected tools must appear in trace
        # For P1-P4: we accept if trace includes the expected tools
        all_present = all(t in trace_tools for t in expected_tools) if expected_tools else True

        return {
            "pass": all_present,
            "failure_reason": None if all_present else "expected tools not all present",
            "trace_check": {
                "expected_tools": expected_tools,
                "actual_tools": trace_tools,
                "match": all_present,
                "victim_tool": victim_tool,
                "perturbation_mode": perturbation_mode,
            },
        }
