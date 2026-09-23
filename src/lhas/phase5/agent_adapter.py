"""Odys-to-ToolMaze Agent Adapter — implements the official ``BaseAgent`` interface.

``OdysToolMazeAgentAdapter`` bridges Odys's ``ModelDriver`` protocol to
the ToolMaze evaluation framework's ``BaseAgent`` abstract class.  The
official ``ExecutionEngine`` (sandbox.py) drives this adapter through
its standard ``initialize → step → receive_tool_result`` loop.

Design contract
───────────────
* The adapter owns a ``ModelDriver`` instance (injected at construction).
* It implements the **exact** ``BaseAgent`` interface — no more, no less.
* ``initialize()`` stores task description and tool definitions, then
  passes them to the model driver.
* ``step()`` delegates to ``model_driver.next_action()`` and records
  the action in conversation history.
* ``receive_tool_result()`` appends the tool result to conversation
  history and optionally records in the substrate ``EvidenceLedger``.
* Token usage is delegated to the model driver.
* The adapter does NOT receive task_json, execution_trace,
  expected_result, or any hidden benchmark state.
* Shadow observer and evidence ledger are optional injection points.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Lazy import of official ToolMaze agent types ─────────────────────
_TOOLMAZE_REPO = (
    Path(__file__).resolve().parents[3] / "experiments" / "phase5" / "benchmarks" / "toolmaze"
)

if str(_TOOLMAZE_REPO) not in sys.path:
    sys.path.insert(0, str(_TOOLMAZE_REPO))

from evaluation.agents.base_agent import BaseAgent, AgentAction, TokenUsage  # noqa: E402

# Remove from sys.path after import
if str(_TOOLMAZE_REPO) in sys.path:
    sys.path.remove(str(_TOOLMAZE_REPO))

from .model_driver import ModelDriver  # noqa: E402


class OdysToolMazeAgentAdapter(BaseAgent):
    """Adapter that bridges Odys ``ModelDriver`` to ToolMaze ``BaseAgent``.

    The official ``ExecutionEngine`` drives this adapter through the
    standard agent loop.  All model reasoning is delegated to the
    injected ``ModelDriver``.

    Parameters
    ----------
    model_driver : ModelDriver
        The model backend that produces actions.
    """

    def __init__(self, model_driver: ModelDriver):
        self._model_driver = model_driver

        # ── State set during initialize() ──
        self._task_description: str = ""
        self._tool_definitions: List[Dict[str, Any]] = []

        # ── Conversation history (BaseAgent contract) ──
        self._conversation_history: List[Dict[str, Any]] = []

        # ── Step counter ──
        self._step_count: int = 0

        # ── Optional integration points ──
        self._shadow_observer: Any = None  # ProgressObserver protocol
        self._evidence_ledger: Any = None  # EvidenceLedger from substrate

    # ── Injection points ─────────────────────────────────────────────

    def set_shadow_observer(self, observer: Any) -> None:
        """Inject a shadow progress observer (optional).

        The observer must satisfy the ``ProgressObserver`` protocol
        (see types.py).  It is called after each tool result.
        """
        self._shadow_observer = observer

    def set_evidence_ledger(self, ledger: Any) -> None:
        """Inject the substrate EvidenceLedger (optional).

        Tool results are appended to the ledger as TOOL_OBSERVED events.
        """
        self._evidence_ledger = ledger

    # ── BaseAgent interface ──────────────────────────────────────────

    def initialize(self, task_description: str, tool_definitions: List[Dict[str, Any]]) -> None:
        """Initialize with task and tool definitions.

        Stores the task description and tool definitions.  Also adds
        the initial user message to conversation history.
        """
        self._task_description = task_description
        self._tool_definitions = tool_definitions

        # Reset conversation for fresh start
        self._conversation_history = []
        self._step_count = 0

        # Record the initial user message
        self._conversation_history.append({
            "role": "user",
            "content": task_description,
        })

    def step(self, user_message: Optional[str] = None) -> AgentAction:
        """Execute one reasoning step via the model driver.

        Delegates to ``model_driver.next_action()`` with the current
        conversation history and tool definitions.  The returned action
        is recorded in the conversation history.
        """
        self._step_count += 1

        # If a user_message is provided (e.g., first round), add it
        if user_message is not None:
            self._conversation_history.append({
                "role": "user",
                "content": user_message,
            })

        # Delegate to the model driver
        action = self._model_driver.next_action(
            messages=list(self._conversation_history),
            tool_definitions=self._tool_definitions,
        )

        # Record the assistant action in conversation history
        action_msg: Dict[str, Any] = {
            "role": "assistant",
            "type": action.type,
            "content": action.content or action.thought or "",
        }
        if action.type == "tool_call":
            action_msg["tool_call"] = {
                "name": action.tool_name,
                "arguments": action.arguments or {},
            }
        if action.thought:
            action_msg.setdefault("metadata", {})["thought"] = action.thought
        self._conversation_history.append(action_msg)

        return action

    def receive_tool_result(self, tool_name: str, result: Dict[str, Any]) -> None:
        """Receive a tool result from the ExecutionEngine.

        Appends the result to conversation history, notifies the model
        driver (for failure detection hooks), and optionally records
        in the substrate EvidenceLedger and shadow observer.
        """
        # Append to conversation history
        self._conversation_history.append({
            "role": "tool",
            "name": tool_name,
            "content": result,
        })

        # Notify the model driver (failure detection hooks)
        self._model_driver.record_tool_result(tool_name, result)

        # Record in evidence ledger if available
        if self._evidence_ledger is not None:
            try:
                from .substrate.evidence import EvidenceEventType
                self._evidence_ledger.append(
                    task_id=self._task_description[:64],  # truncated for ID
                    attempt_id=f"step-{self._step_count}",
                    event_type=EvidenceEventType.TOOL_OBSERVED,
                    payload={
                        "tool_name": tool_name,
                        "result_keys": sorted(result.keys()),
                        "step": self._step_count,
                    },
                )
            except Exception:
                pass  # best-effort

        # Notify shadow observer if available
        if self._shadow_observer is not None:
            try:
                action_identity = f"{tool_name}@step-{self._step_count}"
                self._shadow_observer.observe(
                    task_id=self._task_description[:64],
                    step=self._step_count,
                    action_identity=action_identity,
                    tool_result=result,
                )
            except Exception:
                pass  # best-effort

    def get_total_tokens(self) -> int:
        """Get total tokens consumed."""
        return self._model_driver.get_total_tokens()

    def get_token_usage(self) -> TokenUsage:
        """Get detailed token usage statistics."""
        return self._model_driver.get_token_usage()

    def get_conversation_history(self) -> List[Dict[str, Any]]:
        """Get the full conversation history."""
        return list(self._conversation_history)

    def reset(self) -> None:
        """Reset all adapter state."""
        self._task_description = ""
        self._tool_definitions = []
        self._conversation_history = []
        self._step_count = 0
        self._model_driver.reset()
