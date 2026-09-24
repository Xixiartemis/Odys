"""Budgeted Model Driver — wraps any ModelDriver with call/turn budget enforcement.

``BudgetedModelDriver`` delegates all model interactions to a wrapped
``ModelDriver`` while enforcing a hard budget on the number of model
calls (``next_action`` invocations).  When the budget is exhausted,
``BudgetExhausted`` is raised — fail closed, never silently exceeded.

Design contract
───────────────
* Wraps any object satisfying the ``ModelDriver`` protocol.
* Counts ``next_action`` calls; raises ``BudgetExhausted`` when
  ``max_model_calls`` is exceeded.
* Optionally tracks turns (``max_turns``) as a secondary guard.
* Token usage and ``record_tool_result`` delegate transparently to
  the wrapped driver.
* ``reset()`` resets the internal counters (not the wrapped driver's
  counters — callers should reset both if needed).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Lazy import of official ToolMaze agent types ─────────────────────
_TOOLMAZE_REPO = (
    Path(__file__).resolve().parents[3] / "experiments" / "phase5" / "benchmarks" / "toolmaze"
)


def _import_toolmaze_types():
    """Import AgentAction, TokenUsage from official ToolMaze."""
    repo_str = str(_TOOLMAZE_REPO)
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)
    try:
        from evaluation.agents.base_agent import AgentAction, TokenUsage
        return AgentAction, TokenUsage
    finally:
        if repo_str in sys.path:
            sys.path.remove(repo_str)


AgentAction, TokenUsage = _import_toolmaze_types()


# ── Budget exception ────────────────────────────────────────────────

class BudgetExhausted(Exception):
    """Raised when the model call or turn budget is exceeded.

    Attributes
    ----------
    calls_used : int
        Number of model calls consumed before exhaustion.
    max_calls : int
        The configured maximum model calls.
    turns_used : int
        Number of turns consumed (if turn tracking is active).
    max_turns : int or None
        The configured maximum turns, or None if not set.
    """

    def __init__(
        self,
        message: str,
        *,
        calls_used: int = 0,
        max_calls: int = 0,
        turns_used: int = 0,
        max_turns: Optional[int] = None,
    ):
        super().__init__(message)
        self.calls_used = calls_used
        self.max_calls = max_calls
        self.turns_used = turns_used
        self.max_turns = max_turns


# ── BudgetedModelDriver ─────────────────────────────────────────────

class BudgetedModelDriver:
    """Wraps a ``ModelDriver`` and enforces a hard budget on model calls.

    Parameters
    ----------
    model_driver : ModelDriver
        The underlying model driver to wrap.
    max_model_calls : int
        Maximum number of ``next_action`` calls before raising
        ``BudgetExhausted``.
    max_turns : int, optional
        Maximum number of turns (secondary guard).  If set, also
        raises ``BudgetExhausted`` when exceeded.
    """

    def __init__(
        self,
        model_driver: Any,
        *,
        max_model_calls: int,
        max_turns: Optional[int] = None,
    ):
        self._model_driver = model_driver
        self._max_model_calls = max_model_calls
        self._max_turns = max_turns

        # ── Counters ──
        self._calls_used: int = 0
        self._turns_used: int = 0

    # ── Properties ───────────────────────────────────────────────────

    @property
    def model_calls_used(self) -> int:
        """Number of model calls consumed so far."""
        return self._calls_used

    @property
    def budget_remaining(self) -> int:
        """Number of model calls remaining before budget exhaustion."""
        return max(0, self._max_model_calls - self._calls_used)

    # ── ModelDriver interface ────────────────────────────────────────

    def next_action(
        self,
        messages: List[Dict[str, Any]],
        tool_definitions: List[Dict[str, Any]],
        generation_config: Optional[Dict[str, Any]] = None,
    ) -> AgentAction:
        """Return the next action, enforcing budget constraints.

        Checks budget **before** calling the wrapped driver.  If the
        call budget is exhausted, raises ``BudgetExhausted``.  After a
        successful call, increments the counter.

        Parameters
        ----------
        messages : list[dict]
            Full conversation history.
        tool_definitions : list[dict]
            Tool schemas available to the model.
        generation_config : dict, optional
            Model generation parameters.

        Returns
        -------
        AgentAction
            The action from the wrapped driver.

        Raises
        ------
        BudgetExhausted
            If the model call budget or turn budget is exceeded.
        """
        # ── Check model call budget ─────────────────────────────────
        if self._calls_used >= self._max_model_calls:
            raise BudgetExhausted(
                f"Model call budget exhausted: {self._calls_used}/{self._max_model_calls} calls used",
                calls_used=self._calls_used,
                max_calls=self._max_model_calls,
                turns_used=self._turns_used,
                max_turns=self._max_turns,
            )

        # ── Check turn budget ───────────────────────────────────────
        if self._max_turns is not None and self._turns_used >= self._max_turns:
            raise BudgetExhausted(
                f"Turn budget exhausted: {self._turns_used}/{self._max_turns} turns used",
                calls_used=self._calls_used,
                max_calls=self._max_model_calls,
                turns_used=self._turns_used,
                max_turns=self._max_turns,
            )

        # ── Delegate to wrapped driver ──────────────────────────────
        action = self._model_driver.next_action(
            messages=messages,
            tool_definitions=tool_definitions,
            generation_config=generation_config,
        )

        # ── Increment counters after successful call ────────────────
        self._calls_used += 1
        self._turns_used += 1

        return action

    def get_token_usage(self) -> TokenUsage:
        """Delegate token usage to the wrapped driver."""
        return self._model_driver.get_token_usage()

    def get_total_tokens(self) -> int:
        """Delegate total tokens to the wrapped driver."""
        return self._model_driver.get_total_tokens()

    def record_tool_result(
        self,
        tool_name: str,
        result: Dict[str, Any],
    ) -> None:
        """Delegate tool result recording to the wrapped driver."""
        self._model_driver.record_tool_result(tool_name, result)

    def reset(self) -> None:
        """Reset budget counters.

        Does **not** reset the wrapped driver — callers should reset
        both if starting a new trial.
        """
        self._calls_used = 0
        self._turns_used = 0
