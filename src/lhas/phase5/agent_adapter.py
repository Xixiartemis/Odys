"""ToolMaze Agent Adapter — thin boundary between Phase5 core and ToolMaze.

This module provides:
- ``OdysToolMazeAgentAdapter`` — wraps ``Phase5AgentCore``, converts
  between Odys types and official ToolMaze types.
- ``create_toolmaze_agent_adapter()`` — factory that verifies the
  frozen ToolMaze checkout exists, imports official types, and creates
  the adapter.  Fails closed if benchmark is missing.

The adapter owns NO policy logic.  All recovery/evidence/observer
logic lives in ``Phase5AgentCore`` (agent_core.py).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .agent_core import Phase5AgentCore
from .model_driver import ModelAction, DriverTokenUsage

logger = logging.getLogger(__name__)

# ── ToolMaze type cache (populated by create_toolmaze_agent_adapter) ──
_ToolMaze_BaseAgent = None
_ToolMaze_AgentAction = None
_ToolMaze_TokenUsage = None


class ToolMazeDependencyUnavailable(RuntimeError):
    """Raised when live ToolMaze execution is requested but the frozen
    benchmark checkout is not present."""


def _import_toolmaze_types():
    """Import official ToolMaze types.  Raises ToolMazeDependencyUnavailable."""
    global _ToolMaze_BaseAgent, _ToolMaze_AgentAction, _ToolMaze_TokenUsage
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
        from evaluation.agents.base_agent import BaseAgent, AgentAction, TokenUsage
        _ToolMaze_BaseAgent = BaseAgent
        _ToolMaze_AgentAction = AgentAction
        _ToolMaze_TokenUsage = TokenUsage
    except ImportError as exc:
        raise ToolMazeDependencyUnavailable(
            f"Failed to import ToolMaze types: {exc}"
        ) from exc
    finally:
        if repo_str in sys.path:
            sys.path.remove(repo_str)


def create_toolmaze_agent_adapter(core: Phase5AgentCore):
    """Create a live ToolMaze agent adapter wrapping the given core.

    1. Verifies frozen ToolMaze checkout exists.
    2. Imports official BaseAgent, AgentAction, TokenUsage.
    3. Returns an OdysToolMazeAgentAdapter instance.
    4. Fails closed if benchmark dependency is missing.
    """
    _import_toolmaze_types()

    # Dynamically create class inheriting from official BaseAgent
    BaseAgent = _ToolMaze_BaseAgent

    class _Adapter(BaseAgent):
        """Thin ToolMaze boundary — delegates all logic to Phase5AgentCore."""

        def __init__(self, core: Phase5AgentCore):
            self._core = core

        def initialize(self, task_description: str, tool_definitions: List[Dict[str, Any]]) -> None:
            self._core.initialize(task_description, tool_definitions)

        def step(self, user_message: Optional[str] = None):
            action = self._core.next_model_action(user_message)
            # Convert Odys ModelAction → official ToolMaze AgentAction
            return _ToolMaze_AgentAction(
                type=action.type,
                tool_name=action.tool_name,
                arguments=action.arguments,
                content=action.content,
                thought=action.thought,
                tool_calls=action.tool_calls,
            )

        def receive_tool_result(self, tool_name: str, result: Dict[str, Any]) -> None:
            self._core.receive_tool_result(tool_name, result)

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

        def reset(self) -> None:
            self._core.reset()

    return _Adapter(core)


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
