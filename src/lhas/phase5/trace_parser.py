"""Extract derived views from official ToolMaze TraceLogger output.

The official TraceLogger emits messages in this format:
  - {"role": "user", "content": "..."}
  - {"role": "assistant", "type": "tool_call", "tool_call": {"name": ..., "arguments": ...}}
  - {"role": "tool", "name": ..., "content": ..., "metadata": {"perturbation_status": ...}}
  - {"role": "assistant", "type": "final_answer", "content": "..."}

This module extracts derived views WITHOUT mutating the official trace.
"""

from __future__ import annotations

from typing import Any, Dict, List


def extract_tool_calls(official_trace: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract tool calls from official TraceLogger messages.

    Returns a list of dicts, each containing:
    - tool_name: str
    - arguments: dict
    - step: int (0-based)
    - has_result: bool
    - perturbation_status: str or None (from tool result metadata)

    Does NOT mutate official_trace.
    """
    messages = official_trace.get("messages", [])
    tool_calls = []
    step = 0

    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("type") == "tool_call":
            tc = msg.get("tool_call", {})
            tool_calls.append({
                "tool_name": tc.get("name", ""),
                "arguments": tc.get("arguments", {}),
                "step": step,
                "has_result": False,
                "perturbation_status": None,
            })
            step += 1

    # Correlate tool results
    result_idx = 0
    for msg in messages:
        if msg.get("role") == "tool":
            if result_idx < len(tool_calls):
                tool_calls[result_idx]["has_result"] = True
                meta = msg.get("metadata", {})
                tool_calls[result_idx]["perturbation_status"] = meta.get("perturbation_status")
                result_idx += 1

    return tool_calls


def count_tool_calls(official_trace: Dict[str, Any]) -> int:
    """Count tool calls from official TraceLogger messages.

    Counts assistant messages where type == "tool_call".
    """
    messages = official_trace.get("messages", [])
    count = 0
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("type") == "tool_call":
            count += 1
    return count


def build_derived_view(official_trace: Dict[str, Any]) -> Dict[str, Any]:
    """Build a derived runtime view from official trace.

    Returns a dict with:
    - tool_calls: extracted tool call details
    - tool_call_count: count
    - round_count: number of assistant turns
    - has_final_answer: bool
    """
    messages = official_trace.get("messages", [])
    tool_calls = extract_tool_calls(official_trace)
    round_count = sum(1 for m in messages if m.get("role") == "assistant")
    has_final_answer = any(
        m.get("role") == "assistant" and m.get("type") == "final_answer"
        for m in messages
    )

    return {
        "tool_calls": tool_calls,
        "tool_call_count": len(tool_calls),
        "round_count": round_count,
        "has_final_answer": has_final_answer,
    }
