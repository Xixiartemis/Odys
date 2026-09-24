"""Phase5 Agent Core — benchmark-neutral execution core.

``Phase5AgentCore`` owns ALL policy/recovery/evidence/observer logic.
It imports ZERO ToolMaze modules.  It returns ``ModelAction`` and
``DriverTokenUsage`` (Odys-owned types).

The ToolMaze-specific adapter (``agent_adapter.py``) wraps this core
and converts to official ToolMaze types at the boundary.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from .control_arms import PolicyStrategy, RecoveryActionKind, RecoveryDecision
from .model_driver import ModelAction, DriverTokenUsage, ModelDriver
from .types import PolicyExecutionError

logger = logging.getLogger(__name__)

# Terminal recovery actions that must halt the agent loop immediately.
_TERMINAL_RECOVERY_ACTIONS = frozenset({
    RecoveryActionKind.STOP,
    RecoveryActionKind.ESCALATE,
})


class Phase5AgentCore:
    """Benchmark-neutral agent execution core.

    Owns: ModelDriver, conversation history, PolicyStrategy,
    EvidenceLedger, ShadowProgressObserver, recovery state.

    Returns Odys-owned types only (ModelAction, DriverTokenUsage).
    No ToolMaze imports anywhere in this class.

    Parameters
    ----------
    model_driver : ModelDriver
        The model backend that produces actions.
    strategy : PolicyStrategy, optional
        Control-arm strategy for recovery decisions.
    control_state : optional
        Runtime/control-plane state from the substrate.
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

        # ── Conversation history ──
        self._conversation_history: List[Dict[str, Any]] = []

        # ── Step counter ──
        self._step_count: int = 0

        # ── Optional integration points ──
        self._shadow_observer: Any = None
        self._evidence_ledger: Any = None

        # ── Recovery state ──
        self._pending_recovery: Optional[RecoveryDecision] = None
        self._recovery_decisions: List[Dict[str, Any]] = []
        self._escalation_flag: bool = False
        self._escalation_reason: str = ""

    # ── Injection points ─────────────────────────────────────────────

    def set_shadow_observer(self, observer: Any) -> None:
        """Inject a shadow progress observer (optional)."""
        self._shadow_observer = observer

    def set_evidence_ledger(self, ledger: Any) -> None:
        """Inject the substrate EvidenceLedger (optional)."""
        self._evidence_ledger = ledger

    # ── Async/sync bridge ────────────────────────────────────────────

    @staticmethod
    def _run_async(coro):
        """Run an async coroutine synchronously."""
        return asyncio.run(coro)

    # ── Public API (benchmark-neutral) ───────────────────────────────

    def initialize(self, task_description: str, tool_definitions: List[Dict[str, Any]]) -> None:
        """Initialize with task and tool definitions."""
        self._task_description = task_description
        self._tool_definitions = tool_definitions

        self._conversation_history = []
        self._step_count = 0
        self._pending_recovery = None
        self._recovery_decisions.clear()
        self._escalation_flag = False
        self._escalation_reason = ""

        self._conversation_history.append({
            "role": "user",
            "content": task_description,
        })

    def next_model_action(self, user_message: Optional[str] = None) -> ModelAction:
        """Execute one reasoning step, returning Odys ModelAction.

        Handles recovery decisions, terminal actions, and context injection.
        """
        self._step_count += 1

        if user_message is not None:
            self._conversation_history.append({
                "role": "user",
                "content": user_message,
            })

        # ── Terminal recovery gate ──
        if self._escalation_flag:
            return ModelAction(
                type="final_answer",
                content=f"TERMINATED: {self._escalation_reason}",
            )

        # ── Check pending recovery decision ──
        if self._pending_recovery is not None:
            decision = self._pending_recovery
            self._pending_recovery = None

            if decision.action is RecoveryActionKind.ESCALATE:
                self._escalation_flag = True
                self._escalation_reason = decision.reason or "Policy strategy requested escalation"
                return ModelAction(
                    type="final_answer",
                    content=f"TERMINATED: {self._escalation_reason}",
                )

            if decision.action is RecoveryActionKind.STOP:
                reason = decision.reason or "Policy strategy requested stop"
                self._escalation_flag = True
                self._escalation_reason = reason
                return ModelAction(
                    type="final_answer",
                    content=f"TERMINATED: {reason}",
                )

            if decision.action is RecoveryActionKind.RETRY_WITH_CONTEXT:
                failure_context = self._build_failure_context(decision)
                self._conversation_history.append({
                    "role": "system",
                    "content": failure_context,
                })

        # ── Delegate to model driver ──
        action = self._model_driver.next_action(
            messages=list(self._conversation_history),
            tool_definitions=self._tool_definitions,
        )

        # Record in conversation history
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
        """Process a tool result: record, observe, consult strategy."""
        # 1. Conversation history
        self._conversation_history.append({
            "role": "tool",
            "name": tool_name,
            "content": result,
        })

        # 2. Notify model driver (failure detection hooks)
        self._model_driver.record_tool_result(tool_name, result)

        # 3. Record in evidence ledger
        if self._evidence_ledger is not None:
            try:
                from .substrate.evidence import EvidenceEventType
                self._evidence_ledger.append(
                    task_id=self._task_description[:64],
                    attempt_id=f"step-{self._step_count}",
                    event_type=EvidenceEventType.TOOL_OBSERVED,
                    payload={
                        "tool_name": tool_name,
                        "result_keys": sorted(result.keys()),
                        "step": self._step_count,
                    },
                )
            except ImportError as exc:
                logger.debug("Evidence event type import failed (non-critical): %s", exc)
            except AttributeError as exc:
                logger.debug("Evidence ledger attribute error (non-critical): %s", exc)
            except Exception as exc:
                raise PolicyExecutionError(
                    f"Evidence ledger append failed at step {self._step_count}"
                ) from exc

        # 4. Notify shadow observer
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
            except AttributeError as exc:
                logger.warning("Shadow observer attribute error (non-critical): %s", exc)
            except Exception as exc:
                logger.warning("Shadow observer failed at step %d: %s", self._step_count, exc)

        # 5. Consult policy strategy for recovery
        if self._strategy is not None:
            try:
                shadow_signal = None
                if shadow_record is not None and hasattr(shadow_record, "signal"):
                    shadow_signal = shadow_record.signal.value if hasattr(shadow_record.signal, "value") else str(shadow_record.signal)

                decision = self._run_async(
                    self._strategy.on_step_result(
                        step=self._step_count,
                        result=result,
                        observer=self._shadow_observer,
                    )
                )

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

            except PolicyExecutionError:
                raise
            except Exception as exc:
                raise PolicyExecutionError(
                    f"Policy strategy on_step_result failed at step {self._step_count}"
                ) from exc

    def get_token_usage(self) -> DriverTokenUsage:
        """Get token usage as Odys-owned DriverTokenUsage."""
        return self._model_driver.get_token_usage()

    def get_total_tokens(self) -> int:
        return self._model_driver.get_total_tokens()

    def get_conversation_history(self) -> List[Dict[str, Any]]:
        return list(self._conversation_history)

    def get_recovery_decisions(self) -> List[Dict[str, Any]]:
        return list(self._recovery_decisions)

    @property
    def is_escalated(self) -> bool:
        return self._escalation_flag

    @property
    def escalation_reason(self) -> str:
        return self._escalation_reason

    @property
    def step_count(self) -> int:
        return self._step_count

    def reset(self) -> None:
        """Reset all core state."""
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
        """Build failure-context message for retry_with_context."""
        parts = [
            f"[RECOVERY CONTEXT] The previous tool call encountered an issue.",
            f"Recovery action: {decision.action.value}",
            f"Reason: {decision.reason}",
        ]
        if decision.signal:
            parts.append(f"Signal: {decision.signal}")
        if decision.evidence:
            safe_keys = {k: v for k, v in decision.evidence.items()
                        if not k.startswith("phase4_") or k == "phase4_recovery_action"}
            if safe_keys:
                parts.append(f"Evidence: {safe_keys}")
        parts.append(
            "Please adjust your approach. Consider alternative tools, "
            "different arguments, or a different strategy to accomplish the task."
        )
        return "\n".join(parts)
