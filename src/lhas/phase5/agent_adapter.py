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
  the action in conversation history.  When a pending recovery decision
  requests ``RETRY_WITH_CONTEXT``, failure context is injected into the
  model prompt before the call.  When ``ESCALATE`` is pending, the
  adapter sets an escalation flag and emits a final_answer.
* ``receive_tool_result()`` appends the tool result to conversation
  history, records in the substrate ``EvidenceLedger``, updates the
  shadow progress observer, and consults the ``PolicyStrategy`` for
  recovery decisions.
* Token usage is delegated to the model driver.
* The adapter does NOT receive task_json, execution_trace,
  expected_result, or any hidden benchmark state.
* Shadow observer, evidence ledger, strategy, and control state are
  optional injection points.
"""

from __future__ import annotations

import asyncio
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

from .control_arms import PolicyStrategy, RecoveryActionKind, RecoveryDecision  # noqa: E402
from .model_driver import ModelDriver  # noqa: E402


class OdysToolMazeAgentAdapter(BaseAgent):
    """Adapter that bridges Odys ``ModelDriver`` to ToolMaze ``BaseAgent``.

    The official ``ExecutionEngine`` drives this adapter through the
    standard agent loop.  All model reasoning is delegated to the
    injected ``ModelDriver``.  When a ``PolicyStrategy`` is injected,
    the adapter becomes policy-aware: it consults the strategy after
    each tool result for recovery decisions and injects recovery
    context into subsequent model calls.

    Parameters
    ----------
    model_driver : ModelDriver
        The model backend that produces actions.
    strategy : PolicyStrategy, optional
        Control-arm strategy for recovery decisions.  When set,
        ``receive_tool_result()`` calls ``strategy.on_step_result()``
        and stores the resulting ``RecoveryDecision``.
    control_state : ControlState, optional
        Runtime/control-plane state from the substrate.  Passed
        through to strategy calls when available.
    """

    def __init__(
        self,
        model_driver: ModelDriver,
        *,
        strategy: Optional[PolicyStrategy] = None,
        control_state: Any = None,
    ):
        self._model_driver = model_driver
        self._strategy = strategy
        self._control_state = control_state

        # ── State set during initialize() ──
        self._task_description: str = ""
        self._tool_definitions: List[Dict[str, Any]] = []

        # ── Conversation history (BaseAgent contract) ──
        self._conversation_history: List[Dict[str, Any]] = []

        # ── Step counter ──
        self._step_count: int = 0

        # ── Optional integration points ──
        self._shadow_observer: Any = None  # ShadowProgressObserver
        self._evidence_ledger: Any = None  # EvidenceLedger from substrate

        # ── Recovery state ──
        self._pending_recovery: Optional[RecoveryDecision] = None
        self._recovery_decisions: List[Dict[str, Any]] = []
        self._escalation_flag: bool = False
        self._escalation_reason: str = ""

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

    # ── Async/sync bridge ────────────────────────────────────────────

    @staticmethod
    def _run_async(coro):
        """Run an async coroutine synchronously.

        Uses ``asyncio.run()`` which creates a new event loop, runs the
        coroutine, and closes the loop.  This is safe because the
        adapter is driven synchronously by the ExecutionEngine — no
        outer event loop is expected.

        Exceptions propagate normally (no swallowing).
        """
        return asyncio.run(coro)

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
        self._pending_recovery = None
        self._recovery_decisions.clear()
        self._escalation_flag = False
        self._escalation_reason = ""

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

        Recovery integration:
        * If a pending recovery wants ``RETRY_WITH_CONTEXT``, failure
          context is injected as a system message before the model call.
        * If a pending recovery wants ``ESCALATE``, the adapter sets an
          escalation flag and returns a ``final_answer`` with the
          escalation reason.
        * Otherwise the model driver is called normally.
        """
        self._step_count += 1

        # If a user_message is provided (e.g., first round), add it
        if user_message is not None:
            self._conversation_history.append({
                "role": "user",
                "content": user_message,
            })

        # ── Check pending recovery decision ──────────────────────────
        if self._pending_recovery is not None:
            decision = self._pending_recovery
            self._pending_recovery = None

            if decision.action is RecoveryActionKind.ESCALATE:
                self._escalation_flag = True
                self._escalation_reason = decision.reason or "Policy strategy requested escalation"
                return AgentAction(
                    type="final_answer",
                    content=f"[ESCALATED] {self._escalation_reason}",
                )

            if decision.action is RecoveryActionKind.RETRY_WITH_CONTEXT:
                # Inject failure context as a system message so the model
                # can see what went wrong and adjust its approach.
                failure_context = self._build_failure_context(decision)
                self._conversation_history.append({
                    "role": "system",
                    "content": failure_context,
                })

            # For RETRY / NONE / STOP — fall through to normal model call.
            # STOP is handled at the harness level; here we just let the
            # model try again (the harness will check stop conditions).

        # ── Delegate to the model driver ─────────────────────────────
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

        This is the primary integration point for policy-aware execution.
        After recording the result, the adapter:
        1. Appends to conversation history (BaseAgent contract).
        2. Notifies the model driver (failure detection hooks).
        3. Records TOOL_OBSERVED in the evidence ledger (if set).
        4. Updates the shadow progress observer (if set).
        5. Consults the policy strategy for recovery (if set).
        6. Stores the recovery decision for the next step() call.
        """
        # ── 1. Append to conversation history ────────────────────────
        self._conversation_history.append({
            "role": "tool",
            "name": tool_name,
            "content": result,
        })

        # ── 2. Notify the model driver (failure detection hooks) ─────
        self._model_driver.record_tool_result(tool_name, result)

        # ── 3. Record in evidence ledger ─────────────────────────────
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

        # ── 4. Notify shadow observer ────────────────────────────────
        shadow_record = None
        if self._shadow_observer is not None:
            try:
                action_identity = f"{tool_name}@step-{self._step_count}"
                shadow_record = self._shadow_observer.observe(
                    task_id=self._task_description[:64],
                    step=self._step_count,
                    action_identity=action_identity,
                    tool_result=result,
                )
            except Exception:
                pass  # best-effort

        # ── 5. Consult policy strategy for recovery ──────────────────
        if self._strategy is not None:
            try:
                # Extract shadow signal for the strategy
                shadow_signal = None
                if shadow_record is not None and hasattr(shadow_record, "signal"):
                    shadow_signal = shadow_record.signal.value if hasattr(shadow_record.signal, "value") else str(shadow_record.signal)

                # Call async strategy synchronously
                decision = self._run_async(
                    self._strategy.on_step_result(
                        step=self._step_count,
                        result=result,
                        observer=self._shadow_observer,
                    )
                )

                # ── 6. Store recovery decision ───────────────────────
                if decision.action is not RecoveryActionKind.NONE:
                    self._pending_recovery = decision
                    self._recovery_decisions.append({
                        "step": self._step_count,
                        "tool_name": tool_name,
                        "action": decision.action.value,
                        "reason": decision.reason,
                        "signal": decision.signal,
                        "shadow_signal": shadow_signal,
                        "evidence": dict(decision.evidence) if decision.evidence else {},
                    })

            except Exception:
                pass  # best-effort — strategy failure must not break the agent loop

    def get_total_tokens(self) -> int:
        """Get total tokens consumed."""
        return self._model_driver.get_total_tokens()

    def get_token_usage(self) -> TokenUsage:
        """Get detailed token usage statistics."""
        return self._model_driver.get_token_usage()

    def get_conversation_history(self) -> List[Dict[str, Any]]:
        """Get the full conversation history."""
        return list(self._conversation_history)

    def get_recovery_decisions(self) -> List[Dict[str, Any]]:
        """Get all recovery decisions made during this agent run.

        Returns a list of dicts, each containing:
        * step: the step number
        * tool_name: the tool that was called
        * action: RecoveryActionKind value (e.g. "retry_with_context")
        * reason: human-readable reason
        * signal: the strategy's signal (if any)
        * shadow_signal: the shadow observer's signal (if any)
        * evidence: additional evidence from the strategy
        """
        return list(self._recovery_decisions)

    @property
    def is_escalated(self) -> bool:
        """Whether the adapter has been escalated by the policy."""
        return self._escalation_flag

    @property
    def escalation_reason(self) -> str:
        """The reason for escalation, or empty string if not escalated."""
        return self._escalation_reason

    def reset(self) -> None:
        """Reset all adapter state."""
        self._task_description = ""
        self._tool_definitions = []
        self._conversation_history = []
        self._step_count = 0
        self._pending_recovery = None
        self._recovery_decisions.clear()
        self._escalation_flag = False
        self._escalation_reason = ""
        self._model_driver.reset()

    # ── Private helpers ──────────────────────────────────────────────

    def _build_failure_context(self, decision: RecoveryDecision) -> str:
        """Build a failure-context message for retry_with_context recovery.

        Constructs a system message that tells the model what went wrong
        and provides recovery guidance, without exposing hidden benchmark
        state (task_json, execution_trace, expected_result).
        """
        parts = [
            f"[RECOVERY CONTEXT] The previous tool call encountered an issue.",
            f"Recovery action: {decision.action.value}",
            f"Reason: {decision.reason}",
        ]
        if decision.signal:
            parts.append(f"Signal: {decision.signal}")
        if decision.evidence:
            # Include non-sensitive evidence fields
            safe_keys = {k: v for k, v in decision.evidence.items()
                        if not k.startswith("phase4_") or k == "phase4_recovery_action"}
            if safe_keys:
                parts.append(f"Evidence: {safe_keys}")
        parts.append(
            "Please adjust your approach. Consider alternative tools, "
            "different arguments, or a different strategy to accomplish the task."
        )
        return "\n".join(parts)
