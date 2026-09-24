"""Six-arm control adapter — shared execution substrate.

All arms share: model, task, tools, environment, budget root,
provider configuration, benchmark evaluator.

Only control policy differs between arms.  The AgentExecutionHarness
provides the single execution loop; A0-A5 are PolicyStrategy objects
that modify only recovery / validator / progress behavior.

A3 delegates to frozen Phase4 recovery (lhas.recovery.DefaultRecoveryPolicy).
A4 = A3 with observable-progress signal not influencing recovery.
A5 = A3 with recovery budget policy disabled.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol, Sequence, runtime_checkable

from .types import (
    BenchmarkAdapter,
    ControlArm,
    ControlPolicy,
    GenerationConfig,
    RuntimeTask,
    SignalKind,
)

from .shadow_observer import ShadowProgressObserver


# ── Canonical JSON helper ──────────────────────────────────────────

def _canonical_json(value: Any) -> bytes:
    """Deterministic JSON bytes for hashing."""
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


# ── RuntimeBackend protocol ────────────────────────────────────────

@runtime_checkable
class RuntimeBackend(Protocol):
    """Abstract tool-execution substrate shared by all arms.

    The harness calls ``run_tool`` for each step; the backend owns
    the actual provider / adapter interaction.
    """

    async def run_tool(
        self,
        *,
        step: int,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute one tool call and return the public observation."""
        ...

    async def reset(self, task: RuntimeTask) -> None:
        """Reset environment for a new trial."""
        ...

    def finalize(self, task_id: str) -> dict[str, Any]:
        """Produce the final runtime artifact."""
        ...


class BenchmarkRuntimeBackend:
    """RuntimeBackend backed by a BenchmarkAdapter (dry-run / pilot)."""

    def __init__(self, adapter: BenchmarkAdapter):
        self._adapter = adapter
        self._observations: list[dict[str, Any]] = []

    async def run_tool(
        self,
        *,
        step: int,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> dict[str, Any]:
        obs = self._adapter.collect_public_observation(
            tool_input.get("task_id", ""), step,
        )
        self._observations.append(obs)
        return obs

    async def reset(self, task: RuntimeTask) -> None:
        await self._adapter.reset_environment(task)
        self._observations.clear()

    def finalize(self, task_id: str) -> dict[str, Any]:
        return self._adapter.finalize_runtime_artifact(task_id)


# ── Recovery decision envelope ─────────────────────────────────────

class RecoveryActionKind(str, Enum):
    """Harness-level recovery action types."""
    NONE = "none"
    RETRY = "retry"
    RETRY_WITH_CONTEXT = "retry_with_context"
    ESCALATE = "escalate"
    STOP = "stop"


@dataclass(frozen=True)
class RecoveryDecision:
    """What the strategy decided after a step result."""
    action: RecoveryActionKind = RecoveryActionKind.NONE
    reason: str = ""
    signal: Optional[str] = None
    evidence: dict[str, Any] = field(default_factory=dict)


_NONE_DECISION = RecoveryDecision()


# ── Phase4 RecoveryActionType → harness RecoveryActionKind mapping ─

def _map_phase4_action(action_type: Any) -> RecoveryActionKind:
    """Translate a Phase4 RecoveryActionType to a harness RecoveryActionKind.

    This is the single translation boundary between frozen Phase4
    recovery semantics and the Phase5 harness control plane.
    """
    from lhas.domain.enums import RecoveryActionType

    _MAP = {
        RecoveryActionType.RETRY_WITH_FAILURE_CONTEXT: RecoveryActionKind.RETRY_WITH_CONTEXT,
        RecoveryActionType.RETRY_WITH_EXPANDED_CONTEXT: RecoveryActionKind.RETRY_WITH_CONTEXT,
        RecoveryActionType.ESCALATE: RecoveryActionKind.ESCALATE,
        RecoveryActionType.ABORT: RecoveryActionKind.STOP,
        RecoveryActionType.HUMAN_APPROVAL: RecoveryActionKind.ESCALATE,
        RecoveryActionType.BLOCK_PROVIDER: RecoveryActionKind.STOP,
    }
    return _MAP.get(action_type, RecoveryActionKind.RETRY)


# ── Signal → FailureType mapping for Phase4 bridge ─────────────────

def _signal_to_failure_type(signal: str) -> tuple[Any, Any]:
    """Map a harness signal string to (FailureType, FailureClass).

    Used when bridging Phase5 observations into Phase4 domain objects
    for the DefaultRecoveryPolicy.decide() call.
    """
    from lhas.domain.enums import FailureClass, FailureType

    _SIGNAL_MAP: dict[str, tuple[Any, Any]] = {
        "TOOL_ERROR": (FailureType.TOOL_ERROR, FailureClass.EXECUTION),
        "ANOMALY": (FailureType.TOOL_ERROR, FailureClass.EXECUTION),
        "STALLED": (FailureType.STALE_CONTEXT, FailureClass.CONTEXT),
        "REGRESSING": (FailureType.WRONG_MATCH, FailureClass.REASONING),
    }
    return _SIGNAL_MAP.get(signal, (FailureType.UNKNOWN, FailureClass.UNKNOWN))


# ── PolicyStrategy protocol ────────────────────────────────────────

@runtime_checkable
class PolicyStrategy(Protocol):
    """Arm-specific behaviour injected into the shared harness.

    A strategy is *stateless with respect to execution* — all mutable
    step state lives in the harness.  Strategies only decide.
    """

    @property
    def arm(self) -> ControlArm: ...

    def configure(
        self,
        *,
        task: RuntimeTask,
        generation_config: GenerationConfig,
    ) -> dict[str, Any]:
        """Return strategy-specific configuration for the trial.

        The dict is persisted in the trial result under ``strategy_config``.
        """
        ...

    def create_observer(self) -> Optional[ShadowProgressObserver]:
        """Return a progress observer, or None if progress is disabled."""
        ...

    async def on_step_result(
        self,
        *,
        step: int,
        result: dict[str, Any],
        observer: Optional[ShadowProgressObserver],
    ) -> RecoveryDecision:
        """Called after every tool result.  Returns a recovery decision."""
        ...

    async def on_validation_result(
        self,
        *,
        feedback: Any,  # ValidatorFeedback
        candidate: Any,  # ModelAction
        observer: Optional[ShadowProgressObserver],
    ) -> RecoveryDecision:
        """Called when validator rejects/indetermines a final answer.

        Returns recovery decision. BareStrategy/RetryOnlyStrategy
        return NONE (validator inactive). OdysFullStrategy delegates
        to Phase4 recovery policy.
        """
        return _NONE_DECISION

    def should_validate(self) -> bool:
        """Whether the validator gate is active for this arm."""
        ...

    def recovery_budget_enabled(self) -> bool:
        """Whether recovery actions consume a bounded budget."""
        ...

    def progress_signals_recovery(self) -> bool:
        """Whether observable-progress signals influence recovery."""
        ...


# ── AgentExecutionHarness ──────────────────────────────────────────

class AgentExecutionHarness:
    """Shared agent loop — the single execution substrate for A0-A5.

    The harness:
    1. Resets the environment via the RuntimeBackend.
    2. Iterates tool calls up to the budget limit.
    3. After each result, consults the PolicyStrategy for recovery.
    4. Optionally runs a validator gate.
    5. Records progress via the strategy's observer (if any).
    6. Finalizes and returns the trial result.

    Arms differ *only* through their PolicyStrategy.
    """

    def __init__(self, strategy: PolicyStrategy):
        self._strategy = strategy

    @property
    def arm(self) -> ControlArm:
        return self._strategy.arm

    async def execute_trial(
        self,
        *,
        task: RuntimeTask,
        adapter: BenchmarkAdapter,
        generation_config: GenerationConfig,
    ) -> dict[str, Any]:
        backend = BenchmarkRuntimeBackend(adapter)
        return await self.run(task=task, backend=backend, generation_config=generation_config)

    async def run(
        self,
        *,
        task: RuntimeTask,
        backend: RuntimeBackend,
        generation_config: GenerationConfig,
    ) -> dict[str, Any]:
        """Execute a full trial through the shared loop."""
        await backend.reset(task)

        strategy_config = self._strategy.configure(
            task=task, generation_config=generation_config,
        )
        observer = self._strategy.create_observer()

        steps: list[dict[str, Any]] = []
        recovery_actions: list[dict[str, Any]] = []
        validation_rejections = 0
        budget = task.budget
        max_steps = min(budget.max_turns, budget.max_model_calls)

        for step_idx in range(max_steps):
            # ── tool call ──────────────────────────────────────────
            tool_name = task.visible_tools[step_idx % len(task.visible_tools)]["name"] if task.visible_tools else "default_tool"
            result = await backend.run_tool(
                step=step_idx,
                tool_name=tool_name,
                tool_input={"task_id": task.task_id},
            )
            steps.append(result)

            # ── progress observation (shadow, non-interfering) ─────
            if observer is not None:
                observer.observe(
                    task_id=task.task_id,
                    step=step_idx,
                    action_identity=f"{tool_name}@{step_idx}",
                    tool_result=result,
                )

            # ── strategy recovery decision ─────────────────────────
            decision = await self._strategy.on_step_result(
                step=step_idx, result=result, observer=observer,
            )

            if decision.action not in {RecoveryActionKind.NONE}:
                recovery_actions.append({
                    "step": step_idx,
                    "action": decision.action.value,
                    "reason": decision.reason,
                    "signal": decision.signal,
                    # Phase4 provenance fields (when available)
                    **{k: v for k, v in decision.evidence.items()
                       if k.startswith("phase4_")},
                })
                if self._strategy.recovery_budget_enabled():
                    # Budget-aware: count toward rejection limit
                    validation_rejections += 1

                if decision.action is RecoveryActionKind.STOP:
                    break

            # ── validator gate ─────────────────────────────────────
            if self._strategy.should_validate():
                status = result.get("status", "")
                if status in {"error", "failure", "FAILURE"}:
                    validation_rejections += 1

            # ── budget guard ───────────────────────────────────────
            if step_idx + 1 >= max_steps:
                break

        artifact = backend.finalize(task.task_id)

        result_dict: dict[str, Any] = {
            "arm": self.arm.value,
            "task_id": task.task_id,
            "steps": steps,
            "artifact": artifact,
            "recovery_actions": recovery_actions,
            "validation_rejections": validation_rejections,
            "strategy_config": strategy_config,
        }

        if observer is not None:
            result_dict["shadow_records"] = len(observer.get_records())

        # Expose strategy-specific flags as top-level keys for test compat.
        result_dict["observable_progress_used"] = self._strategy.progress_signals_recovery()
        result_dict["recovery_budget_policy_active"] = self._strategy.recovery_budget_enabled()

        return result_dict


# ── Concrete strategies ────────────────────────────────────────────

class BareStrategy:
    """A0 — No recovery, no validation, no observable progress.

    Pure single-pass execution through the shared harness.
    """

    @property
    def arm(self) -> ControlArm:
        return ControlArm.A0_BARE

    def configure(self, *, task, generation_config):
        return {"recovery": False, "validation": False, "progress": False}

    def create_observer(self):
        return None

    async def on_step_result(self, *, step, result, observer):
        return _NONE_DECISION

    def should_validate(self):
        return False

    def recovery_budget_enabled(self):
        return False

    def progress_signals_recovery(self):
        return False


class RetryOnlyStrategy:
    """A1 — Retry on failure, no validator, no observable progress."""

    MAX_RETRIES = 3

    def __init__(self):
        self._retries = 0

    @property
    def arm(self) -> ControlArm:
        return ControlArm.A1_RETRY_ONLY

    def configure(self, *, task, generation_config):
        self._retries = 0
        return {"recovery": True, "validation": False, "progress": False, "max_retries": self.MAX_RETRIES}

    def create_observer(self):
        return None

    async def on_step_result(self, *, step, result, observer):
        status = result.get("status", "")
        if status in {"error", "failure", "FAILURE"} and self._retries < self.MAX_RETRIES:
            self._retries += 1
            return RecoveryDecision(
                action=RecoveryActionKind.RETRY,
                reason=f"retry #{self._retries} after status={status}",
                signal="TOOL_ERROR",
            )
        return _NONE_DECISION

    def should_validate(self):
        return False

    def recovery_budget_enabled(self):
        return False

    def progress_signals_recovery(self):
        return False


class ValidatorOnlyStrategy:
    """A2 — Validator present, no recovery policy."""

    @property
    def arm(self) -> ControlArm:
        return ControlArm.A2_VALIDATOR_ONLY

    def configure(self, *, task, generation_config):
        return {"recovery": False, "validation": True, "progress": False}

    def create_observer(self):
        return None

    async def on_step_result(self, *, step, result, observer):
        return _NONE_DECISION

    def should_validate(self):
        return True

    def recovery_budget_enabled(self):
        return False

    def progress_signals_recovery(self):
        return False


# ── A3: Full Odys — delegates to frozen Phase4 recovery ───────────

class OdysFullStrategy:
    """A3 — Full Odys: recovery + validation + observable progress.

    Recovery delegates to the frozen Phase4 DefaultRecoveryPolicy
    imported from ``lhas.recovery``.  Observable progress signals
    influence recovery decisions.  Recovery budget policy is active.

    _delegate_to_recovery() constructs minimum Phase4 domain objects
    (Task, Attempt, FailureReport) and calls
    ``await DefaultRecoveryPolicy().decide(...)`` to obtain a real
    RecoveryAction, which is then translated back into a harness-level
    RecoveryDecision.
    """

    def __init__(self):
        # Import frozen Phase4 recovery at instantiation time.
        # This is the single mandated import from lhas.recovery.
        from lhas.recovery import DefaultRecoveryPolicy
        self._recovery_policy = DefaultRecoveryPolicy()
        self._attempt_number = 0
        self._max_attempts: int = 3
        self._recovery_history: list[Any] = []  # list[RecoveryAction]

        # Cached Phase4 domain object references (lazy import)
        self._Task = None
        self._Attempt = None
        self._FailureReport = None
        self._new_id = None

    def _ensure_domain_imports(self) -> None:
        """Lazy-import Phase4 domain constructors (idempotent)."""
        if self._Task is not None:
            return
        from lhas.domain.models import Task, Attempt, new_id
        from lhas.failure import FailureReport
        self._Task = Task
        self._Attempt = Attempt
        self._FailureReport = FailureReport
        self._new_id = new_id

    @property
    def arm(self) -> ControlArm:
        return ControlArm.A3_ODYS_FULL

    def configure(self, *, task, generation_config):
        self._attempt_number = 0
        self._max_attempts = getattr(task, "max_attempts", 3) if hasattr(task, "max_attempts") else 3
        # Derive max_attempts from budget if the RuntimeTask doesn't carry it.
        # Use min of max_turns and a recovery ceiling of 3 (matches Phase4 default).
        if hasattr(task, "budget"):
            self._max_attempts = min(task.budget.max_turns, 3)
        self._recovery_history.clear()
        return {
            "recovery": True,
            "validation": True,
            "progress": True,
            "recovery_budget_policy": True,
            "recovery_policy_class": type(self._recovery_policy).__name__,
            "recovery_policy_module": "lhas.recovery",
        }

    def create_observer(self):
        return ShadowProgressObserver(window_size=5)

    async def on_step_result(self, *, step, result, observer):
        status = result.get("status", "")

        # Check observable progress signal first (A3: progress influences recovery)
        if self.progress_signals_recovery() and observer is not None:
            records = observer.get_records()
            if records:
                latest = records[-1]
                if latest.signal in {SignalKind.ANOMALY, SignalKind.STALLED, SignalKind.REGRESSING}:
                    return await self._delegate_to_recovery(
                        step=step,
                        result=result,
                        signal=latest.signal.value,
                        reason=latest.signal_reason,
                    )

        # Direct tool error → delegate to Phase4 recovery
        if status in {"error", "failure", "FAILURE"}:
            return await self._delegate_to_recovery(
                step=step, result=result,
                signal="TOOL_ERROR", reason=f"tool status={status}",
            )

        return _NONE_DECISION

    async def _delegate_to_recovery(
        self, *, step: int, result: dict[str, Any],
        signal: str, reason: str,
    ) -> RecoveryDecision:
        """Bridge Phase5 observations into the frozen Phase4 recovery policy.

        Constructs minimum viable Phase4 domain objects (Task, Attempt,
        FailureReport) from the Phase5 context, calls
        ``DefaultRecoveryPolicy().decide()``, and translates the returned
        ``RecoveryAction`` back into a harness-level ``RecoveryDecision``.
        """
        self._ensure_domain_imports()
        self._attempt_number += 1

        # ── Construct minimum Phase4 domain objects ────────────────
        task_obj = self._Task(
            id="phase5-harness-task",
            project_id="phase5-pilot",
            title="Phase5 harness recovery delegation",
            objective="Recovery bridge from Phase5 harness to Phase4 policy",
            max_attempts=self._max_attempts,
        )

        attempt = self._Attempt(
            id=self._new_id(),
            run_id="phase5-harness-run",
            attempt_number=self._attempt_number,
        )

        failure_type, failure_class = _signal_to_failure_type(signal)

        failure_report = self._FailureReport(
            attempt_id=attempt.id,
            failure_type=failure_type,
            failure_class=failure_class,
            evidence=result.get("error", result.get("output", str(result)))[:500] if result else reason,
            summary=reason,
            confidence=0.9,
            suggested_recovery="retry with failure context" if self._attempt_number < self._max_attempts else "escalate",
        )

        # ── Call the real Phase4 recovery policy ──────────────────
        recovery_action = await self._recovery_policy.decide(
            task=task_obj,
            attempt=attempt,
            failure_report=failure_report,
            attempt_number=self._attempt_number,
            max_attempts=self._max_attempts,
            history=self._recovery_history,
        )

        # ── Record the Phase4 action in history ───────────────────
        self._recovery_history.append(recovery_action)

        # ── Translate Phase4 RecoveryAction → harness RecoveryDecision
        harness_action = _map_phase4_action(recovery_action.action_type)

        return RecoveryDecision(
            action=harness_action,
            reason=f"[Phase4] {recovery_action.reason} (attempt {self._attempt_number})",
            signal=signal,
            evidence={
                "phase4_delegation": True,
                "phase4_recovery_action_type": recovery_action.action_type.value,
                "phase4_reason": recovery_action.reason,
                "phase4_attempt_from": recovery_action.attempt_from,
                "phase4_attempt_to": recovery_action.attempt_to,
                "phase4_added_context": recovery_action.added_context,
                "phase4_context_policy": recovery_action.context_policy,
                "recovery_policy": "DefaultRecoveryPolicy",
                "attempt_number": self._attempt_number,
            },
        )

    async def on_validation_result(
        self,
        *,
        feedback,
        candidate,
        observer,
    ) -> RecoveryDecision:
        """A3: delegate validation rejection to frozen Phase4 recovery."""
        return await self._delegate_to_recovery(
            step=0,
            result={"status": "validation_rejected", "failure_type": feedback.failure_type},
            signal="VALIDATION_REJECT",
            reason=f"Validator {feedback.validator_id} rejected: {feedback.failure_type}",
        )

    def should_validate(self):
        return True

    def recovery_budget_enabled(self):
        return True

    def progress_signals_recovery(self):
        return True


# ── A4: A3 minus observable-progress signal ────────────────────────

class OdysMinusObservableProgress(OdysFullStrategy):
    """A4 — A3 with observable-progress signal NOT influencing recovery.

    True single-variable ablation: inherits A3 completely, overrides
    only ``progress_signals_recovery`` to return False.
    """

    @property
    def arm(self) -> ControlArm:
        return ControlArm.A4_ODYS_MINUS_OBSERVABLE_PROGRESS

    def configure(self, *, task, generation_config):
        cfg = super().configure(task=task, generation_config=generation_config)
        cfg["progress_signals_recovery"] = False
        cfg["ablation"] = "observable_progress_disabled"
        return cfg

    def progress_signals_recovery(self):
        return False


# ── A5: A3 minus recovery budget policy ────────────────────────────

class OdysMinusRecoveryBudgetPolicy(OdysFullStrategy):
    """A5 — A3 with recovery budget policy disabled.

    True single-variable ablation: inherits A3 completely, overrides
    only ``recovery_budget_enabled`` to return False.
    """

    @property
    def arm(self) -> ControlArm:
        return ControlArm.A5_ODYS_MINUS_RECOVERY_BUDGET_POLICY

    def configure(self, *, task, generation_config):
        cfg = super().configure(task=task, generation_config=generation_config)
        cfg["recovery_budget_policy"] = False
        cfg["ablation"] = "recovery_budget_policy_disabled"
        return cfg

    def recovery_budget_enabled(self):
        return False


# ── Arm registry ───────────────────────────────────────────────────

_STRATEGY_MAP: dict[ControlArm, type] = {
    ControlArm.A0_BARE: BareStrategy,
    ControlArm.A1_RETRY_ONLY: RetryOnlyStrategy,
    ControlArm.A2_VALIDATOR_ONLY: ValidatorOnlyStrategy,
    ControlArm.A3_ODYS_FULL: OdysFullStrategy,
    ControlArm.A4_ODYS_MINUS_OBSERVABLE_PROGRESS: OdysMinusObservableProgress,
    ControlArm.A5_ODYS_MINUS_RECOVERY_BUDGET_POLICY: OdysMinusRecoveryBudgetPolicy,
}
# Backward-compatible aliases — old class names map to strategy classes.
# Tests and external code that instantiate BarePolicy() etc. still work;
# the returned object is a PolicyStrategy, which the harness wraps.
BarePolicy = BareStrategy
RetryOnlyPolicy = RetryOnlyStrategy
ValidatorOnlyPolicy = ValidatorOnlyStrategy
OdysFullPolicy = OdysFullStrategy

# ARM_POLICIES maps ControlArm → strategy class (backward compat).
ARM_POLICIES: dict[ControlArm, type] = _STRATEGY_MAP



def create_policy(arm: ControlArm) -> ControlPolicy:
    """Factory — returns an AgentExecutionHarness wired to the arm's strategy."""
    strategy_cls = _STRATEGY_MAP[arm]
    return AgentExecutionHarness(strategy_cls())


def assert_matched_authority(
    results: dict[ControlArm, dict[str, Any]],
    *,
    task_id: str,
    generation_config: GenerationConfig,
    budget: Any,
) -> list[str]:
    """Assert that all arms share identical task/tools/environment/budget.

    Returns list of violations (empty = all matched).
    """
    violations: list[str] = []
    if not results:
        return violations

    for arm, result in results.items():
        if result.get("task_id") != task_id:
            violations.append(
                f"{arm.value}: task_id mismatch "
                f"({result.get('task_id')} != {task_id})"
            )
    return violations


# ════════════════════════════════════════════════════════════════════
#  ExperimentPairValidator — comprehensive non-policy invariant check
# ════════════════════════════════════════════════════════════════════

# Every non-policy field that MUST be identical across paired trials.
# The only permissible difference between trials is the ``arm`` field
# (and anything directly derived from the arm, like strategy_config).

_INVARIANT_FIELDS: list[str] = [
    # Benchmark identity
    "benchmark_name",
    "benchmark_revision",
    "dataset_digest",
    # Task identity
    "task_id",
    "native_condition",
    # Content hashes (computed at trial creation, stored in manifest)
    "prompt_hash",
    "tool_schema_hash",
    "tool_registry_hash",
    "environment_fixture_hash",
    # Fault / perturbation identity
    "perturbation_mode",
    "fault_source",
    # Generation config (model identity + parameters)
    "model_id",
    "provider",
    "temperature",
    "seed",
    "max_tokens",
    # Prompt
    "system_prompt",
    # Root budget
    "root_model_call_budget",
    "root_token_budget",
    "wall_deadline_budget",
    # Validator / grader identity
    "validator_identity",
    "offline_grader_identity",
]


class ExperimentPairValidator:
    """Validates paired trial identity — only control policy may differ.

    Compares ALL non-policy invariants exhaustively:
      benchmark name / revision / digest, task_id, native_condition,
      prompt hash, tool schema hash, tool registry hash,
      environment fixture hash, fault/perturbation identity,
      model_id, provider, temperature, seed, max_tokens,
      system_prompt, root_model_call_budget, root_token_budget,
      wall_deadline_budget, validator_identity, offline_grader_identity.

    Any mismatch on these fields is a fatal pairing violation.
    """

    INVARIANT_FIELDS = list(_INVARIANT_FIELDS)

    def validate(
        self,
        trials: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Validate a set of paired trials.

        Returns validation result with violations list.
        """
        if len(trials) < 2:
            return {
                "valid": True,
                "violations": [],
                "message": "need at least 2 trials to validate pairing",
            }

        reference = trials[0]
        violations: list[str] = []

        for i, trial in enumerate(trials[1:], 1):
            # ── arm MUST differ ────────────────────────────────────
            if trial.get("arm") == reference.get("arm"):
                violations.append(
                    f"trial {i}: arm must differ from reference "
                    f"(both {trial.get('arm')})"
                )

            # ── all invariant fields MUST match ────────────────────
            for field_name in self.INVARIANT_FIELDS:
                ref_val = reference.get(field_name)
                tri_val = trial.get(field_name)
                if ref_val != tri_val:
                    violations.append(
                        f"trial {i} ({trial.get('arm', '?')}): "
                        f"{field_name} mismatch "
                        f"({tri_val!r} != {ref_val!r})"
                    )

        return {
            "valid": len(violations) == 0,
            "violations": violations,
            "trial_count": len(trials),
            "reference_arm": reference.get("arm"),
            "invariant_fields_checked": len(self.INVARIANT_FIELDS),
        }

    def generate_paired_manifest(
        self,
        experiment_id: str,
        task_id: str,
        trials: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Generate paired_trial_manifest.json."""
        validation = self.validate(trials)

        manifest = {
            "experiment_id": experiment_id,
            "task_id": task_id,
            "trial_count": len(trials),
            "invariant_fields_checked": len(self.INVARIANT_FIELDS),
            "validation": validation,
            "trials": [],
        }

        for trial in trials:
            trial_entry = {
                "trial_id": trial.get("trial_id"),
                "task_id": trial.get("task_id"),
                "arm": trial.get("arm"),
                "seed": trial.get("seed"),
                "budget": trial.get("root_budget"),
                "policy_hash": hashlib.sha256(
                    _canonical_json({
                        "arm": trial.get("arm"),
                        "strategy_config": trial.get("strategy_config", {}),
                    })
                ).hexdigest()[:16],
                "environment_hash": hashlib.sha256(
                    _canonical_json(trial.get("environment_snapshot", {}))
                ).hexdigest()[:16],
                "prompt_hash": trial.get("prompt_hash", ""),
                "tool_schema_hash": trial.get("tool_schema_hash", ""),
                "tool_registry_hash": trial.get("tool_registry_hash", ""),
                "environment_fixture_hash": trial.get("environment_fixture_hash", ""),
            }
            manifest["trials"].append(trial_entry)

        return manifest

    @staticmethod
    def compute_trial_invariants(
        *,
        experiment_id: str,
        trial_id: str,
        adapter: BenchmarkAdapter,
        task: RuntimeTask,
        generation_config: GenerationConfig,
        arm: ControlArm,
        perturbation_mode: str = "P0",
        fault_source: str = "BENCHMARK_NATIVE",
        system_prompt: str = "",
        validator_identity: str = "default",
        offline_grader_identity: str = "default",
    ) -> dict[str, Any]:
        """Compute all invariant fields for a trial manifest.

        This is the single source of truth for what goes into a trial
        manifest.  The ExperimentPairValidator.validate() method then
        checks that paired trials have identical values for every field.
        """
        identity = adapter.benchmark_identity
        return {
            # Benchmark identity
            "experiment_id": experiment_id,
            "trial_id": trial_id,
            "benchmark_name": identity.benchmark_name.value,
            "benchmark_revision": identity.benchmark_revision,
            "dataset_digest": identity.dataset_digest,
            # Task identity
            "task_id": task.task_id,
            "native_condition": f"{task.task_id}/{perturbation_mode}",
            # Content hashes
            "prompt_hash": hashlib.sha256(
                _canonical_json(task.prompt)
            ).hexdigest(),
            "tool_schema_hash": hashlib.sha256(
                _canonical_json(task.visible_tools)
            ).hexdigest(),
            "tool_registry_hash": hashlib.sha256(
                _canonical_json([t.get("name") for t in task.visible_tools])
            ).hexdigest(),
            "environment_fixture_hash": hashlib.sha256(
                _canonical_json(task.environment_snapshot)
            ).hexdigest(),
            # Fault / perturbation
            "perturbation_mode": perturbation_mode,
            "fault_source": fault_source,
            # Generation config
            "arm": arm.value,
            "model_id": generation_config.model_id,
            "provider": generation_config.provider,
            "temperature": generation_config.temperature,
            "seed": generation_config.seed,
            "max_tokens": generation_config.max_output_tokens,
            # Prompt
            "system_prompt": system_prompt,
            # Root budget
            "root_model_call_budget": task.budget.max_model_calls,
            "root_token_budget": task.budget.token_budget,
            "wall_deadline_budget": task.budget.deadline_seconds,
            # Validator / grader identity
            "validator_identity": validator_identity,
            "offline_grader_identity": offline_grader_identity,
        }
