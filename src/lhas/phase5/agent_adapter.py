"""ToolMaze Agent Adapter — thin boundary between Phase5 core and ToolMaze.

This module provides:
- ``OdysToolMazeAgentAdapter`` — wraps ``Phase5AgentCore``, converts
  between Odys types and official ToolMaze types.
- ``create_toolmaze_agent_adapter()`` — factory that verifies the
  frozen ToolMaze checkout exists, imports official types, and creates
  the adapter.  Fails closed if benchmark is missing.

Per-call execution authorization (P0-1)
───────────────────────────────────────
The adapter delivers tool calls to the frozen ToolMaze engine ONE AT A
TIME.  The model's full proposed batch (all calls and their original
tool-call ids) is retained; each call is released only after the shared
``ExecutionAuthorizationGate`` authorizes it:

1. The full batch is proposed once (control events: PROPOSED per call).
2. Each call is authorized individually right before delivery.
3. Every real tool result is recorded first, then the deterministic
   authorization judgment runs, then the next call may be released.
4. NOT_READY / FAILURE / affecting-UNKNOWN results block dependent
   remaining calls — in this batch and, for unresolved readiness state,
   in later model turns.  Blocked calls get a clearly-marked SYNTHETIC
   response for model-protocol pairing and a BLOCKED control event.
   They never enter real execution records (official trace, public tool
   observations, evidence ledger, shadow observer).

The adapter owns NO policy logic.  All recovery/evidence/observer logic
lives in ``Phase5AgentCore`` (agent_core.py); all authorization rules
live in ``execution_control.ExecutionAuthorizationGate``.  The frozen
ToolMaze engine is never modified.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .agent_core import Phase5AgentCore
from .execution_control import ControlEventKind, ExecutionAuthorizationGate
from .model_driver import ModelAction, DriverTokenUsage

logger = logging.getLogger(__name__)

# ── ToolMaze type cache (populated by create_toolmaze_agent_adapter) ──
_ToolMaze_BaseAgent = None
_ToolMaze_AgentAction = None
_ToolMaze_ToolCall = None
_ToolMaze_TokenUsage = None


class ToolMazeDependencyUnavailable(RuntimeError):
    """Raised when live ToolMaze execution is requested but the frozen
    benchmark checkout is not present."""


def _import_toolmaze_types():
    """Import official ToolMaze types.  Raises ToolMazeDependencyUnavailable."""
    global _ToolMaze_BaseAgent, _ToolMaze_AgentAction, _ToolMaze_ToolCall, _ToolMaze_TokenUsage
    if _ToolMaze_BaseAgent is not None:
        return

    repo = Path(__file__).resolve().parents[3] / "experiments" / "phase5" / "benchmarks" / "toolmaze"
    if not repo.is_dir():
        raise ToolMazeDependencyUnavailable(
            f"Frozen ToolMaze checkout not found at {repo}. "
            "Live ToolMaze execution requires the frozen benchmark."
        )

    repo_str = str(repo)
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)
    try:
        from evaluation.agents.base_agent import BaseAgent, AgentAction, ToolCall, TokenUsage
        _ToolMaze_BaseAgent = BaseAgent
        _ToolMaze_AgentAction = AgentAction
        _ToolMaze_ToolCall = ToolCall
        _ToolMaze_TokenUsage = TokenUsage
    except ImportError as exc:
        raise ToolMazeDependencyUnavailable(
            f"Failed to import ToolMaze types: {exc}"
        ) from exc
    finally:
        if repo_str in sys.path:
            sys.path.remove(repo_str)


def create_toolmaze_agent_adapter(
    core: Phase5AgentCore,
    *,
    execution_gate: Optional[ExecutionAuthorizationGate] = None,
):
    """Create a live ToolMaze agent adapter wrapping the given core.

    1. Verifies frozen ToolMaze checkout exists.
    2. Imports official BaseAgent, AgentAction, TokenUsage.
    3. Returns an adapter that delivers tool calls ONE AT A TIME through
       the shared execution authorization gate.
    4. Fails closed if benchmark dependency is missing.

    ``execution_gate`` is the shared base execution safety gate active in
    every arm.  When omitted, a fresh gate is created so the safety gate
    is never absent.  The same gate instance is wired into the core for
    observation recording and the retry-reasonableness judgment.
    """
    _import_toolmaze_types()

    gate: ExecutionAuthorizationGate = (
        execution_gate if execution_gate is not None else ExecutionAuthorizationGate()
    )
    core.set_execution_control(gate)

    # Dynamically create class inheriting from official BaseAgent
    BaseAgent = _ToolMaze_BaseAgent

    class _Adapter(BaseAgent):
        """Thin ToolMaze boundary — delegates all logic to Phase5AgentCore.

        Adds per-call release: the model's full proposed batch is held
        here and delivered to the engine one authorized call at a time.
        """

        def __init__(self, core: Phase5AgentCore, gate: ExecutionAuthorizationGate):
            self._core = core
            self._gate = gate
            self._held_calls: List[Dict[str, Any]] = []
            self._released: Optional[Dict[str, Any]] = None

        # ── lifecycle ────────────────────────────────────────────────

        def initialize(self, task_description: str, tool_definitions: List[Dict[str, Any]]) -> None:
            self._core.initialize(task_description, tool_definitions)
            self._gate.reset()
            self._gate.bind_tool_definitions(tool_definitions)
            self._held_calls = []
            self._released = None

        def reset(self) -> None:
            self._core.reset()
            self._gate.reset()
            self._held_calls = []
            self._released = None

        # ── per-call execution delivery (P0-1) ───────────────────────

        def step(self, user_message: Optional[str] = None):
            """Return exactly ONE authorized tool call, or a final answer.

            Retained batch calls that fail authorization are blocked:
            they get a synthetic protocol response and a BLOCKED control
            event, and never reach the execution engine.
            """
            first_request = True
            while True:
                # A terminal recovery decision halts every retained call.
                if self._held_calls and self._core.has_terminal_recovery_pending:
                    self._block_remaining(
                        rule="TERMINAL_RECOVERY_DECISION",
                        reason="pending terminal recovery decision halts remaining calls",
                    )

                if self._held_calls:
                    record = self._held_calls[0]
                    decision = self._gate.propose(
                        step=self._core.step_count,
                        tool_name=record["tool_name"],
                        arguments=record["arguments"],
                        tool_call_id=record["tool_call_id"],
                        provider_tool_call_id=record.get("provider_tool_call_id"),
                        origin=record.get("origin", "model_batch"),
                        batch_index=record.get("batch_index", 0),
                    )
                    if decision.authorized:
                        self._held_calls.pop(0)
                        self._released = record
                        return self._single_call_action(record)

                    # Blocked — synthetic response + BLOCKED event (recorded
                    # by gate.propose), never executed.
                    self._held_calls.pop(0)
                    self._core.record_blocked_tool_call(
                        record, rule=decision.rule, reason=decision.reason
                    )
                    if decision.terminal:
                        reason = f"{decision.rule}: {decision.reason}"
                        self._gate.mark_terminal(step=self._core.step_count, reason=reason)
                        self._block_remaining(
                            rule="TERMINAL_CONTROL_STATE", reason=reason
                        )
                        self._core.mark_control_escalation(reason)
                        return _ToolMaze_AgentAction(
                            type="final_answer",
                            tool_name=None,
                            arguments=None,
                            content=f"TERMINATED: {reason}",
                            thought=None,
                            tool_calls=None,
                        )
                    continue

                # No retained calls — one model request for the next batch.
                action = self._core.next_model_action(
                    user_message if first_request else None
                )
                first_request = False
                if action.type != "tool_call":
                    return self._to_agent_action(action)

                self._held_calls = self._batch_records(action)
                self._gate.begin_batch()
                continue

        def receive_tool_result(
            self,
            tool_name: str,
            result: Dict[str, Any],
            tool_call_index: int = 0,
        ) -> None:
            """Deliver a real tool result to the core (recorded first).

            The deterministic authorization state update runs inside the
            core right after recording (``ExecutionAuthorizationGate.
            observe``), before any recovery judgment.
            """
            record = self._released
            self._released = None
            if record is None:
                last = self._core.get_last_tool_call_record()
                record = dict(last) if last else {}
                record.setdefault("tool_name", tool_name)
                record.setdefault("arguments", {})
                record.setdefault("tool_call_id", None)
                record.setdefault("provider_tool_call_id", None)
                record.setdefault("conversation_recorded", True)
                record["batch_index"] = tool_call_index
            self._core.receive_tool_result(
                record.get("tool_name", tool_name),
                result,
                tool_call_index=record.get("batch_index", tool_call_index),
            )

        # ── held-batch bookkeeping ───────────────────────────────────

        def _batch_records(self, action: ModelAction) -> List[Dict[str, Any]]:
            """Retain the model's full proposed batch with original ids."""
            raw_calls = list(action.tool_calls or [])
            if not raw_calls:
                raw_calls = [action]
            records: List[Dict[str, Any]] = []
            for index, call in enumerate(raw_calls):
                tool_name = getattr(call, "tool_name", None)
                arguments = getattr(call, "arguments", None) or {}
                call_id = getattr(call, "tool_call_id", None)
                provider_id = getattr(call, "provider_tool_call_id", None)
                retry_of = getattr(call, "retry_of_call_id", None) or (
                    action.retry_of_call_id if len(raw_calls) == 1 else None
                )
                if not isinstance(tool_name, str) or not tool_name:
                    raise RuntimeError("tool-call batch contains an invalid tool name")
                records.append({
                    "tool_name": tool_name,
                    "arguments": dict(arguments),
                    "tool_call_id": call_id,
                    "provider_tool_call_id": provider_id,
                    "retry_of_call_id": retry_of,
                    "conversation_recorded": bool(provider_id),
                    "batch_index": index,
                    "origin": "recovery_retry" if retry_of else "model_batch",
                })
            return records

        def _single_call_action(self, record: Dict[str, Any]):
            """Build the official action for exactly one released call."""
            return _ToolMaze_AgentAction(
                type="tool_call",
                tool_name=record["tool_name"],
                arguments=dict(record["arguments"]),
                content=None,
                thought=None,
                tool_calls=[
                    _ToolMaze_ToolCall(
                        tool_name=record["tool_name"],
                        arguments=dict(record["arguments"]),
                    )
                ],
            )

        def _to_agent_action(self, action: ModelAction):
            tool_calls = None
            if action.tool_calls is not None:
                tool_calls = [
                    _ToolMaze_ToolCall(
                        tool_name=call.tool_name,
                        arguments=dict(call.arguments),
                    )
                    for call in action.tool_calls
                ]
            return _ToolMaze_AgentAction(
                type=action.type,
                tool_name=action.tool_name,
                arguments=action.arguments,
                content=action.content,
                thought=action.thought,
                tool_calls=tool_calls,
            )

        def _block_remaining(self, *, rule: str, reason: str) -> None:
            """Block every retained call with synthetic responses."""
            step = self._core.step_count
            while self._held_calls:
                record = self._held_calls.pop(0)
                self._gate.record_event(
                    ControlEventKind.PROPOSED,
                    step=step,
                    tool_name=record["tool_name"],
                    tool_call_id=record["tool_call_id"],
                    provider_tool_call_id=record.get("provider_tool_call_id"),
                    origin=record.get("origin", "model_batch"),
                    batch_index=record.get("batch_index", 0),
                )
                self._gate.record_event(
                    ControlEventKind.BLOCKED,
                    step=step,
                    tool_name=record["tool_name"],
                    tool_call_id=record["tool_call_id"],
                    provider_tool_call_id=record.get("provider_tool_call_id"),
                    origin=record.get("origin", "model_batch"),
                    batch_index=record.get("batch_index", 0),
                    rule=rule,
                    reason=reason,
                    terminal=True,
                )
                self._core.record_blocked_tool_call(record, rule=rule, reason=reason)

        # ── passthrough API ──────────────────────────────────────────

        def get_total_tokens(self) -> int:
            return self._core.get_total_tokens()

        def get_token_usage(self):
            usage = self._core.get_token_usage()
            # Convert Odys DriverTokenUsage → official ToolMaze TokenUsage
            return _ToolMaze_TokenUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )

        def get_conversation_history(self) -> List[Dict[str, Any]]:
            return self._core.get_conversation_history()

    return _Adapter(core, gate)


# ── Backward-compatible alias ────────────────────────────────────────
# Existing code that imports OdysToolMazeAgentAdapter from agent_adapter
# should use create_toolmaze_agent_adapter() instead.  This alias is
# kept only for tests that verify the adapter class exists.

def OdysToolMazeAgentAdapter(model_driver=None, *, strategy=None, control_state=None):
    """Deprecated: use create_toolmaze_agent_adapter() with a Phase5AgentCore."""
    raise DeprecationWarning(
        "OdysToolMazeAgentAdapter is no longer a class. "
        "Use Phase5AgentCore + create_toolmaze_agent_adapter() instead."
    )
