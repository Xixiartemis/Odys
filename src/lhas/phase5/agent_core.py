"""Phase5 Agent Core — benchmark-neutral execution core.

``Phase5AgentCore`` owns ALL policy/recovery/evidence/observer logic.
It imports ZERO ToolMaze modules.  It returns ``ModelAction`` and
``DriverTokenUsage`` (Odys-owned types).

The ToolMaze-specific adapter (``agent_adapter.py``) wraps this core
and converts to official ToolMaze types at the boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .control_arms import PolicyStrategy, RecoveryActionKind, RecoveryDecision
from .model_driver import ModelAction, ModelToolCall, DriverTokenUsage, ModelDriver
from .types import (
    EvidenceLedgerExecutionError,
    PolicyExecutionError,
    ProgressObserverExecutionError,
    RuntimeValidatorExecutionError,
)

logger = logging.getLogger(__name__)

# Terminal recovery actions that must halt the agent loop immediately.
_TERMINAL_RECOVERY_ACTIONS = frozenset({
    RecoveryActionKind.STOP,
    RecoveryActionKind.ESCALATE,
})


@dataclass(frozen=True)
class PublicToolObservation:
    """Structured public tool observation — no oracle, no hidden data.

    This is the canonical storage format for tool results used by the
    runtime validator.  Conversation history may continue storing JSON
    text for model compatibility.
    """
    step: int
    tool_name: str
    result: Dict[str, Any]
    tool_call_id: Optional[str] = None
    provider_tool_call_id: Optional[str] = None
    retry_of_call_id: Optional[str] = None
    arguments_digest: Optional[str] = None
    result_digest: Optional[str] = None
    tool_call_index: int = 0
    tool_call_batch_size: int = 1

    def __post_init__(self):
        if self.result_digest is None:
            object.__setattr__(
                self, "result_digest",
                hashlib.sha256(
                    json.dumps(self.result, sort_keys=True, default=str).encode()
                ).hexdigest()[:16],
            )


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

        # ── Structured public tool observations (Section A) ──
        self._public_tool_observations: List[PublicToolObservation] = []

        # ── Optional integration points ──
        self._shadow_observer: Any = None
        self._evidence_ledger: Any = None
        self._runtime_validator: Any = None
        self._validator_events: List[Dict[str, Any]] = []

        # ── Recovery state ──
        self._pending_recovery: Optional[RecoveryDecision] = None
        self._recovery_decisions: List[Dict[str, Any]] = []
        self._escalation_flag: bool = False
        self._escalation_reason: str = ""
        self._last_tool_call_id: Optional[str] = None
        self._last_provider_tool_call_id: Optional[str] = None
        self._last_tool_call: Optional[Dict[str, Any]] = None  # For A1 retry
        self._active_tool_call_batch: Optional[Dict[str, Any]] = None
        self._pending_recovery_call: Optional[Dict[str, Any]] = None
        self._invocation_sequence: int = 0
        self._candidate_sequence: int = 0
        self._invocation_ids: set[str] = set()

        # ── Recovery budget gate (Section F) ──
        self._recovery_budget_gate: Any = None  # RecoveryBudgetGate or PassThrough

    # ── Injection points ─────────────────────────────────────────────

    def set_shadow_observer(self, observer: Any) -> None:
        """Inject a shadow progress observer (optional)."""
        self._shadow_observer = observer

    def set_evidence_ledger(self, ledger: Any) -> None:
        """Inject the substrate EvidenceLedger (optional)."""
        self._evidence_ledger = ledger

    def set_runtime_validator(self, validator: Any) -> None:
        """Inject production runtime validator (RuntimeValidatorProtocol)."""
        self._runtime_validator = validator

    def set_recovery_budget_gate(self, gate: Any) -> None:
        """Inject the recovery budget gate (Section F).

        RecoveryBudgetGate for A3/A4, PassThroughRecoveryBudgetGate for A5.
        Every non-NONE recovery decision must pass through this gate.
        """
        self._recovery_budget_gate = gate

    def get_validator_events(self) -> List[Dict[str, Any]]:
        """Return recorded validator events."""
        return list(self._validator_events)

    def get_public_tool_observations(self) -> List[PublicToolObservation]:
        """Return structured public tool observations."""
        return list(self._public_tool_observations)

    # ── Async/sync bridge ────────────────────────────────────────────

    @staticmethod
    def _run_async(coro):
        """Run an async coroutine synchronously.

        Handles two contexts:
        - No running loop: use asyncio.run()
        - Running loop exists: run on a dedicated thread with its own loop
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is None:
            # No running loop — safe to use asyncio.run()
            return asyncio.run(coro)
        else:
            # Running loop exists — run on a dedicated thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()

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
        self._public_tool_observations.clear()
        self._active_tool_call_batch = None
        self._pending_recovery_call = None

        self._conversation_history.append({
            "role": "user",
            "content": task_description,
        })

    def next_model_action(self, user_message: Optional[str] = None) -> ModelAction:
        """Execute one reasoning step, returning Odys ModelAction.

        Implements the bounded internal action-selection loop (Section E):

        1. Apply pending recovery (if any)
        2. Get action from model driver
        3. If not final_answer → return immediately
        4. If validation disabled → return final_answer
        5. Validate candidate
        6. If ACCEPT → return
        7. If A2 (no recovery hook) → return VALIDATION_BLOCKED
        8. If A3/A4/A5 → attempt recovery, loop back

        All internal continuation bounded by root budget.
        """
        self._step_count += 1

        if user_message is not None and not (
            self._conversation_history
            and self._conversation_history[-1].get("role") == "user"
            and self._conversation_history[-1].get("content") == user_message
        ):
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

        # ── Internal action-selection loop (Section E) ──
        while True:
            # ── Check pending recovery decision ──
            if self._pending_recovery is not None:
                decision = self._pending_recovery
                self._pending_recovery = None
                recovery_call = self._pending_recovery_call
                self._pending_recovery_call = None

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

                if decision.action is RecoveryActionKind.RETRY:
                    # A1: same tool, same arguments — replay last tool call
                    retry_source = recovery_call or self._last_tool_call
                    if retry_source is not None:
                        previous_id = retry_source.get("tool_call_id")
                        conversation_recorded = retry_source.get(
                            "conversation_recorded", True
                        )
                        retry_id = self._new_invocation_id()
                        replay = ModelAction(
                            type="tool_call",
                            tool_name=retry_source["tool_name"],
                            arguments=retry_source["arguments"],
                            thought=f"[RETRY] {decision.reason}",
                            tool_call_id=retry_id,
                            provider_tool_call_id=f"odys-retry-provider-{self._invocation_sequence}",
                            retry_of_call_id=previous_id,
                        )
                        # Record in conversation history
                        action_msg: Dict[str, Any] = {
                            "role": "assistant",
                            "type": "tool_call",
                            "content": replay.thought or "",
                            "tool_call": {
                                "name": replay.tool_name,
                                "arguments": replay.arguments or {},
                            },
                        }
                        if replay.provider_tool_call_id and conversation_recorded:
                            action_msg["tool_call"]["id"] = replay.provider_tool_call_id
                            action_msg["tool_call"]["provider_tool_call_id"] = replay.provider_tool_call_id
                        action_msg["tool_call"]["invocation_id"] = retry_id
                        if replay.retry_of_call_id:
                            action_msg["tool_call"]["retry_of_call_id"] = replay.retry_of_call_id
                        self._conversation_history.append(action_msg)
                        self._last_tool_call_id = retry_id
                        self._last_provider_tool_call_id = replay.provider_tool_call_id
                        self._last_tool_call = {
                            "tool_name": replay.tool_name,
                            "arguments": replay.arguments or {},
                            "tool_call_id": retry_id,
                            "provider_tool_call_id": replay.provider_tool_call_id,
                            "retry_of_call_id": previous_id,
                            "conversation_recorded": conversation_recorded,
                        }
                        return replay
                    # No last tool call to replay — fall through to model

                if decision.action is RecoveryActionKind.RETRY_WITH_CONTEXT:
                    failure_context = self._build_failure_context(decision)
                    self._conversation_history.append({
                        "role": "system",
                        "content": failure_context,
                    })

            # ── Delegate to model driver ──
            if self._active_tool_call_batch is not None:
                pending = self._active_tool_call_batch
                if len(pending["processed_indices"]) < len(pending["calls"]):
                    raise RuntimeError(
                        "cannot request the next provider response before all tool results in the active batch are recorded"
                    )
                self._active_tool_call_batch = None
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
                action, batch_calls = self._normalize_tool_call_batch(action)
                action_msg["tool_calls"] = [
                    {
                        "name": call["tool_name"],
                        "arguments": call["arguments"],
                        "invocation_id": call["tool_call_id"],
                        **({"id": call["provider_tool_call_id"], "provider_tool_call_id": call["provider_tool_call_id"]}
                           if call.get("provider_tool_call_id") else {}),
                    }
                    for call in batch_calls
                ]
                if len(batch_calls) == 1:
                    action_msg["tool_call"] = action_msg["tool_calls"][0]
                self._active_tool_call_batch = {
                    "calls": batch_calls,
                    "processed_indices": set(),
                    "batch_size": len(batch_calls),
                }
                self._last_tool_call = batch_calls[0]
                self._last_tool_call_id = batch_calls[0]["tool_call_id"]
                self._last_provider_tool_call_id = batch_calls[0].get("provider_tool_call_id")
            if action.provider_reasoning_content is not None:
                action_msg["_provider_transport"] = {
                    "reasoning_content": action.provider_reasoning_content,
                }
            if action.thought:
                action_msg.setdefault("metadata", {})["thought"] = action.thought
            self._conversation_history.append(action_msg)

            # ── Non-final actions return immediately ──
            if action.type != "final_answer":
                return action

            # ── Validation gate (Section 5/7) ──
            if (self._strategy is None
                    or not self._strategy.should_validate()
                    or self._runtime_validator is None):
                return action

            # ── Build validation evidence from structured observations ──
            from .runtime_validator import PublicValidationEvidence
            evidence = PublicValidationEvidence(
                candidate_answer=action.content or "",
                conversation_history=list(self._conversation_history),
                tool_definitions=list(self._tool_definitions),
                evidence_refs=self._get_evidence_refs(),
                has_pending_recovery=self._pending_recovery is not None,
                observed_state_digest="",
                public_tool_results=[
                    {
                        "tool_name": obs.tool_name,
                        "tool_call_id": obs.tool_call_id,
                        "retry_of_call_id": obs.retry_of_call_id,
                        "arguments_digest": obs.arguments_digest,
                        "result": obs.result,
                        **obs.result,
                    }
                    for obs in self._public_tool_observations
                ],
            )

            # ── Run validator (Section D: fail-closed) ──
            try:
                feedback = self._runtime_validator.validate(
                    candidate_id=self._next_candidate_id(),
                    evidence_refs=evidence.evidence_refs,
                    observed_state_digest="",
                    runtime_evidence=evidence.__dict__,
                )
                self._validator_events.append({
                    "step": self._step_count,
                    "validator_id": feedback.validator_id,
                    "validator_config_hash": getattr(self._runtime_validator, 'config_hash', lambda: 'unknown')(),
                    "decision": feedback.decision.value,
                    "failure_type": feedback.failure_type,
                    "candidate_answer_preview": (action.content or "")[:200],
                })
            except Exception as exc:
                # Validator exception → FAIL CLOSED (Section D)
                # Record event, then RAISE RuntimeValidatorExecutionError
                self._validator_events.append({
                    "step": self._step_count,
                    "validator_id": "unknown",
                    "decision": "INFRA_ERROR",
                    "failure_type": str(type(exc).__name__),
                    "error": str(exc)[:200],
                })
                raise RuntimeValidatorExecutionError(
                    f"Runtime validator infrastructure failure at step {self._step_count}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

            # ── ACCEPT → return final answer ──
            if feedback.decision.value == "ACCEPT":
                return action

            # ── REJECT / INDETERMINATE → check recovery capability ──
            # A2: no on_validation_result hook → validation-blocked termination
            hook = getattr(self._strategy, 'on_validation_result', None)
            if hook is None:
                return ModelAction(
                    type="final_answer",
                    content=f"[VALIDATION_BLOCKED] {feedback.failure_type}: {action.content}",
                )

            # ── A3/A4/A5: delegate to strategy for recovery decision ──
            try:
                recovery_decision = self._run_async(hook(
                    feedback=feedback,
                    candidate=action,
                    observer=self._shadow_observer,
                ))
            except Exception as exc:
                raise PolicyExecutionError(
                    f"on_validation_result failed at step {self._step_count}"
                ) from exc

            if recovery_decision is None or recovery_decision.action is RecoveryActionKind.NONE:
                # Strategy decided no recovery — return blocked answer
                return ModelAction(
                    type="final_answer",
                    content=f"[VALIDATION_BLOCKED] {feedback.failure_type}: {action.content}",
                )

            # ── Section F: authorize recovery through budget gate ──
            if self._recovery_budget_gate is not None:
                from .recovery_budget import BudgetDecision
                budget_decision = self._recovery_budget_gate.authorize(
                    step=self._step_count,
                    candidate_action=recovery_decision.action.value,
                )
                if budget_decision is BudgetDecision.ESCALATE:
                    self._escalation_flag = True
                    self._escalation_reason = (
                        f"Recovery budget exhausted at step {self._step_count}"
                    )
                    return ModelAction(
                        type="final_answer",
                        content=f"TERMINATED: {self._escalation_reason}",
                    )
                # ALLOW → continue with recovery

            # Record recovery decision
            self._pending_recovery = recovery_decision
            self._recovery_decisions.append({
                "step": self._step_count,
                "action": recovery_decision.action.value,
                "reason": recovery_decision.reason,
                "signal": "VALIDATION_REJECT",
            })

            # Do NOT return the rejected candidate — loop back for recovery
            logger.info(
                "Validation REJECT at step %d — recovery action: %s, looping back",
                self._step_count, recovery_decision.action.value,
            )

    def receive_tool_result(
        self,
        tool_name: str,
        result: Dict[str, Any],
        tool_call_index: int = 0,
    ) -> None:
        """Process a tool result: record, observe, consult strategy.

        Section A: Stores structured PublicToolObservation for the runtime
        validator.  Conversation history continues storing JSON text for
        model compatibility.
        """
        # 1. Conversation history — JSON text for model compatibility
        tool_msg: Dict[str, Any] = {
            "role": "tool",
            "name": tool_name,
            "content": json.dumps(result, default=str, ensure_ascii=False) if isinstance(result, dict) else str(result),
        }
        batch = self._active_tool_call_batch
        if batch is not None:
            if not isinstance(tool_call_index, int) or not 0 <= tool_call_index < len(batch["calls"]):
                raise RuntimeError(f"tool_call_index out of range for active batch: {tool_call_index}")
            if tool_call_index in batch["processed_indices"]:
                raise RuntimeError(f"tool result already recorded for tool_call_index={tool_call_index}")
            call_record = batch["calls"][tool_call_index]
            if tool_name != call_record["tool_name"]:
                raise RuntimeError(
                    f"tool result name mismatch at tool_call_index={tool_call_index}: {tool_name} != {call_record['tool_name']}"
                )
            batch["processed_indices"].add(tool_call_index)
        else:
            call_record = self._last_tool_call or {
                "tool_name": tool_name,
                "arguments": {},
                "tool_call_id": self._last_tool_call_id,
                "provider_tool_call_id": self._last_provider_tool_call_id,
                "conversation_recorded": True,
            }
        self._last_tool_call = call_record
        call_id = call_record.get("tool_call_id")
        provider_call_id = call_record.get("provider_tool_call_id")
        conversation_recorded = call_record.get(
            "conversation_recorded", True
        )
        # Keep the Odys invocation identity for validator/public lineage and
        # the provider-native identity for the OpenAI wire serializer.  The
        # latter is selected by provider_wire.py; neither identity is lost.
        if call_id:
            tool_msg["tool_call_id"] = call_id
            tool_msg["invocation_id"] = call_id
        if provider_call_id and conversation_recorded:
            tool_msg["provider_tool_call_id"] = provider_call_id
        self._conversation_history.append(tool_msg)

        # 2. Store structured public tool observation (Section A)
        obs = PublicToolObservation(
            step=self._step_count,
            tool_name=tool_name,
            result=result if isinstance(result, dict) else {"raw": str(result)},
            tool_call_id=call_id,
            provider_tool_call_id=provider_call_id,
            retry_of_call_id=call_record.get("retry_of_call_id"),
            arguments_digest=hashlib.sha256(
                json.dumps(
                    call_record.get("arguments", {}),
                    sort_keys=True, default=str,
                ).encode()
            ).hexdigest()[:16],
            tool_call_index=tool_call_index,
            tool_call_batch_size=(batch or {}).get("batch_size", 1),
        )
        self._public_tool_observations.append(obs)

        # 3. Notify model driver (failure detection hooks)
        self._model_driver.record_tool_result(tool_name, result)

        # 4. Record in evidence ledger
        if self._evidence_ledger is not None:
            try:
                from .substrate.evidence import EvidenceEventType
                self._evidence_ledger.append(
                    task_id=self._task_description[:64],
                    attempt_id=f"step-{self._step_count}",
                    event_type=EvidenceEventType.TOOL_OBSERVED,
                    payload={
                        "tool_name": tool_name,
                        "tool_call_id": call_id,
                        "provider_tool_call_id": provider_call_id,
                        "invocation_id": call_id,
                        "retry_of_call_id": call_record.get("retry_of_call_id"),
                        "arguments_digest": obs.arguments_digest,
                        "result_keys": sorted(result.keys()) if isinstance(result, dict) else [],
                        "step": self._step_count,
                        "tool_call_index": tool_call_index,
                        "tool_call_batch_size": (batch or {}).get("batch_size", 1),
                    },
                )
            except Exception as exc:
                raise EvidenceLedgerExecutionError(
                    f"Evidence ledger append failed at step {self._step_count}"
                ) from exc

        # 5. Notify shadow observer
        shadow_record = None
        if self._shadow_observer is not None:
            try:
                action_identity = self._stable_action_identity(
                    tool_name,
                    call_record.get("arguments", {}),
                )
                shadow_record = self._shadow_observer.observe(
                    task_id=self._task_description[:64],
                    step=self._step_count,
                    action_identity=action_identity,
                    tool_result=result,
                )
            except Exception as exc:
                raise ProgressObserverExecutionError(
                    f"Shadow observer failed at step {self._step_count}"
                ) from exc

        # 6. Consult policy strategy for recovery (Section F: authorize)
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
                    # Section F: authorize through recovery budget gate
                    authorized = self._authorize_recovery(decision, call_record=call_record)
                    if authorized:
                        self._recovery_decisions.append({
                            "step": self._step_count,
                            "tool_name": tool_name,
                            "action": decision.action.value,
                            "reason": decision.reason,
                            "signal": decision.signal,
                            "shadow_signal": shadow_signal,
                            "evidence": dict(decision.evidence) if decision.evidence else {},
                        })
                    elif self._pending_recovery is not None:
                        self._recovery_decisions.append({
                            "step": self._step_count,
                            "tool_name": tool_name,
                            "action": decision.action.value,
                            "reason": decision.reason,
                            "signal": decision.signal,
                            "suppressed": True,
                            "batch_rule": "FIRST_NON_NONE_RECOVERY_DECISION_WINS_WITHIN_BATCH",
                        })

            except PolicyExecutionError:
                raise
            except Exception as exc:
                raise PolicyExecutionError(
                    f"Policy strategy on_step_result failed at step {self._step_count}"
                ) from exc

        if batch is not None and len(batch["processed_indices"]) == len(batch["calls"]):
            # Keep the batch record available during the final result's policy
            # callback, then make the next model request the only transition.
            pass

    def _authorize_recovery(
        self,
        decision: RecoveryDecision,
        *,
        call_record: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Authorize a recovery decision through the budget gate (Section F).

        Returns True if authorized and set as pending recovery.
        Returns False if denied (ESCALATE → terminal).
        """
        # Deterministic batch rule: the first non-NONE decision owns the
        # pending recovery slot. Later results remain observable but cannot
        # replace it or consume another recovery-budget unit.
        if self._pending_recovery is not None:
            return False
        if self._recovery_budget_gate is not None:
            from .recovery_budget import BudgetDecision
            budget_decision = self._recovery_budget_gate.authorize(
                step=self._step_count,
                candidate_action=decision.action.value,
            )
            if budget_decision is BudgetDecision.ESCALATE:
                self._escalation_flag = True
                self._escalation_reason = (
                    f"Recovery budget exhausted at step {self._step_count}"
                )
                return False
        self._pending_recovery = decision
        self._pending_recovery_call = call_record
        return True

    def _get_evidence_refs(self) -> List[str]:
        """Get evidence refs from the actual EvidenceLedger (Section C).

        Uses all_events() which is the canonical export method.
        """
        if self._evidence_ledger is None:
            # Canonical TrialExecutor rejects missing ledgers before runtime;
            # the core keeps a harmless empty reference for standalone policy
            # unit tests and non-canonical callers.
            return []
        try:
            events = self._evidence_ledger.all_events()
            return [e.evidence_id for e in events]
        except Exception as exc:
            raise EvidenceLedgerExecutionError("EvidenceLedger export failed") from exc

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
        self._last_tool_call_id = None
        self._last_provider_tool_call_id = None
        self._last_tool_call = None
        self._active_tool_call_batch = None
        self._pending_recovery_call = None
        self._invocation_sequence = 0
        self._candidate_sequence = 0
        self._invocation_ids.clear()
        self._public_tool_observations.clear()
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

    def _new_invocation_id(self) -> str:
        self._invocation_sequence += 1
        invocation_id = f"odys-invocation-{self._invocation_sequence}"
        self._invocation_ids.add(invocation_id)
        return invocation_id

    def _normalize_invocation(self, action: ModelAction) -> ModelAction:
        """Ensure every execution has a unique public invocation id."""
        requested = action.tool_call_id
        call_id = requested if requested and requested not in self._invocation_ids else None
        if call_id is None:
            call_id = self._new_invocation_id()
        else:
            self._invocation_ids.add(call_id)
        self._last_tool_call_id = call_id
        return ModelAction(
            type=action.type,
            tool_name=action.tool_name,
            arguments=action.arguments,
            content=action.content,
            thought=action.thought,
            tool_calls=action.tool_calls,
            tool_call_id=call_id,
            provider_tool_call_id=action.provider_tool_call_id,
            provider_reasoning_content=action.provider_reasoning_content,
            retry_of_call_id=action.retry_of_call_id,
        )

    def _normalize_tool_call_batch(
        self, action: ModelAction
    ) -> tuple[ModelAction, List[Dict[str, Any]]]:
        """Allocate one Odys identity for every call in one provider response."""
        raw_calls = action.tool_calls
        if not raw_calls:
            raw_calls = [
                type("_SingleCall", (), {
                    "tool_name": action.tool_name,
                    "arguments": action.arguments or {},
                    "tool_call_id": action.tool_call_id,
                    "provider_tool_call_id": action.provider_tool_call_id,
                })()
            ]
        batch_calls: List[Dict[str, Any]] = []
        seen_provider_ids: set[str] = set()
        for index, raw_call in enumerate(raw_calls):
            if isinstance(raw_call, dict):
                tool_name = raw_call.get("tool_name")
                arguments = raw_call.get("arguments", {})
                requested_id = raw_call.get("tool_call_id")
                provider_id = raw_call.get("provider_tool_call_id")
            else:
                tool_name = getattr(raw_call, "tool_name", None)
                arguments = getattr(raw_call, "arguments", {})
                requested_id = getattr(raw_call, "tool_call_id", None)
                provider_id = getattr(raw_call, "provider_tool_call_id", None)
            if index == 0 and provider_id is None:
                provider_id = action.provider_tool_call_id
            if not isinstance(tool_name, str) or not tool_name:
                raise RuntimeError("tool-call batch contains an invalid tool name")
            if not isinstance(arguments, dict):
                raise RuntimeError("tool-call batch arguments must be objects")
            if provider_id is not None:
                if not isinstance(provider_id, str) or not provider_id:
                    raise RuntimeError("tool-call batch provider id is invalid")
                if provider_id in seen_provider_ids:
                    raise RuntimeError("duplicate provider tool_call_id in tool-call batch")
                seen_provider_ids.add(provider_id)
            invocation_id = requested_id if requested_id and requested_id not in self._invocation_ids else None
            if invocation_id is None:
                invocation_id = self._new_invocation_id()
            else:
                self._invocation_ids.add(invocation_id)
            batch_calls.append({
                "tool_name": tool_name,
                "arguments": dict(arguments),
                "tool_call_id": invocation_id,
                "provider_tool_call_id": provider_id,
                "retry_of_call_id": getattr(raw_call, "retry_of_call_id", None),
                "conversation_recorded": bool(provider_id),
            })
        first = batch_calls[0]
        normalized_calls = [
            ModelToolCall(
                tool_name=call["tool_name"],
                arguments=call["arguments"],
                tool_call_id=call["tool_call_id"],
                provider_tool_call_id=call.get("provider_tool_call_id"),
            )
            for call in batch_calls
        ]
        return ModelAction(
            type="tool_call",
            tool_name=first["tool_name"],
            arguments=first["arguments"],
            content=action.content,
            thought=action.thought,
            tool_calls=normalized_calls,
            tool_call_id=first["tool_call_id"],
            provider_tool_call_id=first.get("provider_tool_call_id"),
            provider_reasoning_content=action.provider_reasoning_content,
            retry_of_call_id=action.retry_of_call_id,
        ), batch_calls

    def _next_candidate_id(self) -> str:
        self._candidate_sequence += 1
        return f"candidate-{self._step_count}-{self._candidate_sequence}"

    @staticmethod
    def _stable_action_identity(tool_name: str, arguments: Any) -> str:
        digest = hashlib.sha256(
            json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        return f"{tool_name}:{digest}"
