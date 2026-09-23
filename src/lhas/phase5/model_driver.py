"""Model Driver — protocol for pluggable model backends + scripted driver.

``ModelDriver`` is the Protocol that any model backend must satisfy to
drive an ``OdysToolMazeAgentAdapter``.  It receives the conversation
history and tool definitions, and returns the next ``AgentAction``.

``ScriptedModelDriver`` is the deterministic test driver: it replays a
pre-scripted list of ``AgentAction`` objects in order.  When the script
is exhausted it emits a ``final_answer``.  This is the primary driver
for Phase 5 reproducibility tests — no live model calls.

Design contract
───────────────
* The driver owns **no** execution state — the adapter owns the
  conversation history and passes it on every call.
* ``next_action`` is synchronous — the adapter calls it once per step.
* ``TokenUsage`` is accumulated by the driver; the adapter reads it
  via ``get_token_usage()`` / ``get_total_tokens()``.
* Failure detection callbacks are optional hooks that let the driver
  observe tool results between steps (for recovery-aware scripted
  sequences).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, runtime_checkable

# ── Lazy import of official ToolMaze agent types ─────────────────────
# Follows the same pattern as runtime_backend.py / offline_evaluator.py.

_TOOLMAZE_REPO = (
    Path(__file__).resolve().parents[3] / "experiments" / "phase5" / "benchmarks" / "toolmaze"
)


def _import_toolmaze_types():
    """Import AgentAction, TokenUsage, ToolCall from official ToolMaze."""
    repo_str = str(_TOOLMAZE_REPO)
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)
    try:
        from evaluation.agents.base_agent import AgentAction, TokenUsage, ToolCall
        return AgentAction, TokenUsage, ToolCall
    finally:
        if repo_str in sys.path:
            sys.path.remove(repo_str)


AgentAction, TokenUsage, ToolCall = _import_toolmaze_types()


# ── Failure / recovery callback types ────────────────────────────────

FailureDetector = Callable[[str, Dict[str, Any]], bool]
"""Signature: (tool_name, tool_result) -> True if failure detected."""

RecoveryTrigger = Callable[[int, str, Dict[str, Any]], Optional[Any]]
"""Signature: (step_index, tool_name, tool_result) -> override AgentAction or None.

If the trigger returns an ``AgentAction``, the scripted driver will use
that action instead of the next scripted entry — allowing dynamic
recovery injection into an otherwise deterministic sequence.
"""


# ── ModelDriver Protocol ─────────────────────────────────────────────

@runtime_checkable
class ModelDriver(Protocol):
    """Protocol for model backends that drive agent step() calls."""

    def next_action(
        self,
        messages: List[Dict[str, Any]],
        tool_definitions: List[Dict[str, Any]],
        generation_config: Optional[Dict[str, Any]] = None,
    ) -> AgentAction:
        """Return the next action given the conversation so far.

        Parameters
        ----------
        messages : list[dict]
            Full conversation history (user / assistant / tool messages).
        tool_definitions : list[dict]
            Tool schemas available to the model.
        generation_config : dict, optional
            Model generation parameters (temperature, etc.).

        Returns
        -------
        AgentAction
            The action to take — either a tool_call or final_answer.
        """
        ...

    def get_token_usage(self) -> TokenUsage:
        """Return accumulated token usage."""
        ...

    def get_total_tokens(self) -> int:
        """Return total token count."""
        ...

    def record_tool_result(
        self,
        tool_name: str,
        result: Dict[str, Any],
    ) -> None:
        """Notify the driver of a tool result (for failure detection hooks)."""
        ...

    def reset(self) -> None:
        """Reset driver state for a new trial."""
        ...


# ── Scripted entry (one step in the pre-scripted sequence) ──────────

@dataclass
class ScriptedAction:
    """One entry in a scripted action sequence.

    Specifies the tool to call and its arguments.  For ``final_answer``
    entries, set ``type='final_answer'`` and ``content`` to the answer
    text (``tool_name`` / ``arguments`` are ignored).
    """

    type: str  # "tool_call" or "final_answer"
    tool_name: Optional[str] = None
    arguments: Optional[Dict[str, Any]] = None
    content: Optional[str] = None
    thought: Optional[str] = None
    # Optional token usage attribution for this step
    input_tokens: int = 0
    output_tokens: int = 0


# ── ScriptedModelDriver ─────────────────────────────────────────────

class ScriptedModelDriver:
    """Deterministic driver that replays pre-scripted actions.

    Parameters
    ----------
    script : sequence[ScriptedAction]
        Ordered list of actions to replay.  Each entry must specify
        ``type``, ``tool_name`` + ``arguments`` (for tool_call), or
        ``content`` (for final_answer).
    failure_detector : FailureDetector, optional
        Called after each tool result.  Returns True if the result
        indicates a failure.
    recovery_trigger : RecoveryTrigger, optional
        Called when ``failure_detector`` fires.  May return an override
        ``AgentAction`` to replace the next scripted entry.
    """

    def __init__(
        self,
        script: Sequence[ScriptedAction],
        *,
        failure_detector: Optional[FailureDetector] = None,
        recovery_trigger: Optional[RecoveryTrigger] = None,
    ):
        self._script = list(script)
        self._index = 0
        self._failure_detector = failure_detector
        self._recovery_trigger = recovery_trigger

        # Token accounting
        self._token_usage = TokenUsage(input_tokens=0, output_tokens=0)

        # Pending recovery override (set by recovery_trigger)
        self._pending_override: Optional[AgentAction] = None

        # Failure log for diagnostics
        self._failure_log: List[Dict[str, Any]] = []

    # ── ModelDriver interface ────────────────────────────────────────

    def next_action(
        self,
        messages: List[Dict[str, Any]],
        tool_definitions: List[Dict[str, Any]],
        generation_config: Optional[Dict[str, Any]] = None,
    ) -> AgentAction:
        """Return the next scripted action, or a recovery override.

        If a recovery trigger fired during ``record_tool_result``, the
        override action is returned instead of the next script entry.
        """
        # Check for pending recovery override
        if self._pending_override is not None:
            action = self._pending_override
            self._pending_override = None
            return action

        # If script is exhausted, return final_answer
        if self._index >= len(self._script):
            return AgentAction(
                type="final_answer",
                content="[ScriptedModelDriver] Script exhausted — no more actions.",
            )

        entry = self._script[self._index]
        self._index += 1

        # Accumulate token usage
        self._token_usage.input_tokens += entry.input_tokens
        self._token_usage.output_tokens += entry.output_tokens

        if entry.type == "final_answer":
            return AgentAction(
                type="final_answer",
                content=entry.content or "",
                thought=entry.thought,
            )

        # tool_call
        return AgentAction(
            type="tool_call",
            tool_name=entry.tool_name,
            arguments=entry.arguments or {},
            thought=entry.thought,
        )

    def get_token_usage(self) -> TokenUsage:
        return self._token_usage

    def get_total_tokens(self) -> int:
        return self._token_usage.total_tokens

    def record_tool_result(
        self,
        tool_name: str,
        result: Dict[str, Any],
    ) -> None:
        """Notify the driver of a tool result.

        If a ``failure_detector`` is installed, it is called here.  If
        it returns True and a ``recovery_trigger`` is installed, the
        trigger is called to produce an override action for the next
        ``next_action`` call.
        """
        if self._failure_detector is None:
            return

        is_failure = self._failure_detector(tool_name, result)
        if not is_failure:
            return

        # Log the failure
        self._failure_log.append({
            "step": self._index,
            "tool_name": tool_name,
            "result_keys": sorted(result.keys()),
        })

        if self._recovery_trigger is not None:
            override = self._recovery_trigger(self._index, tool_name, result)
            if override is not None:
                self._pending_override = override

    def reset(self) -> None:
        """Reset to the beginning of the script."""
        self._index = 0
        self._token_usage = TokenUsage(input_tokens=0, output_tokens=0)
        self._pending_override = None
        self._failure_log.clear()

    # ── Diagnostic accessors ─────────────────────────────────────────

    @property
    def script_position(self) -> int:
        """Current index into the script (0-based)."""
        return self._index

    @property
    def script_remaining(self) -> int:
        """Number of scripted entries remaining."""
        return max(0, len(self._script) - self._index)

    @property
    def failure_log(self) -> List[Dict[str, Any]]:
        """Recorded failures (read-only copy)."""
        return list(self._failure_log)
