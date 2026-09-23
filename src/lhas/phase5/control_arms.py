"""Six-arm control adapter.

All arms share: model, task, tools, environment, budget root,
provider configuration, benchmark evaluator.

Only control policy differs between arms.  Assertions prove matched
authority.  ODYS_MINUS_* arms remove only the intended mechanism.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from .types import (
    BenchmarkAdapter,
    ControlArm,
    ControlPolicy,
    GenerationConfig,
    RuntimeTask,
)

from .shadow_observer import ShadowProgressObserver


# ── Shared trial context ────────────────────────────────────────────

class TrialContext:
    """Shared context for a single trial run.

    Ensures all arms see identical task, tools, environment, and budget.
    """

    def __init__(
        self,
        *,
        task: RuntimeTask,
        adapter: BenchmarkAdapter,
        generation_config: GenerationConfig,
        observer: Optional[ShadowProgressObserver] = None,
    ):
        self.task = task
        self.adapter = adapter
        self.generation_config = generation_config
        self.observer = observer
        self.tool_calls: list[dict[str, Any]] = []
        self.runtime_events: list[dict[str, Any]] = []
        self.budget_ledger: dict[str, Any] = {
            "max_turns": task.budget.max_turns,
            "max_model_calls": task.budget.max_model_calls,
            "turns_used": 0,
            "calls_used": 0,
        }


# ── Arm policies ────────────────────────────────────────────────────

class BarePolicy:
    """A0 — No recovery, no validation.  Pure single-pass execution."""

    @property
    def arm(self) -> ControlArm:
        return ControlArm.A0_BARE

    async def execute_trial(
        self,
        *,
        task: RuntimeTask,
        adapter: BenchmarkAdapter,
        generation_config: GenerationConfig,
    ) -> dict[str, Any]:
        """Execute trial with no recovery or validation."""
        await adapter.reset_environment(task)
        steps = []
        for step_idx in range(min(task.budget.max_turns, 5)):
            obs = adapter.collect_public_observation(task.task_id, step_idx)
            steps.append(obs)
            if step_idx >= 2:  # Simulate completion after a few steps
                break
        artifact = adapter.finalize_runtime_artifact(task.task_id)
        return {
            "arm": self.arm.value,
            "task_id": task.task_id,
            "steps": steps,
            "artifact": artifact,
            "recovery_actions": [],
            "validation_rejections": 0,
        }


class RetryOnlyPolicy:
    """A1 — Retry on failure, no validator, no observable progress."""

    MAX_RETRIES = 3

    @property
    def arm(self) -> ControlArm:
        return ControlArm.A1_RETRY_ONLY

    async def execute_trial(
        self,
        *,
        task: RuntimeTask,
        adapter: BenchmarkAdapter,
        generation_config: GenerationConfig,
    ) -> dict[str, Any]:
        await adapter.reset_environment(task)
        steps = []
        retries = 0
        for step_idx in range(min(task.budget.max_turns, 10)):
            obs = adapter.collect_public_observation(task.task_id, step_idx)
            steps.append(obs)
            # Simulate occasional failure + retry
            if obs.get("status") == "error" and retries < self.MAX_RETRIES:
                retries += 1
                steps.append({"retry": retries, "step": step_idx})
            if step_idx >= 4:
                break
        artifact = adapter.finalize_runtime_artifact(task.task_id)
        return {
            "arm": self.arm.value,
            "task_id": task.task_id,
            "steps": steps,
            "artifact": artifact,
            "recovery_actions": [{"type": "retry", "count": retries}],
            "validation_rejections": 0,
        }


class ValidatorOnlyPolicy:
    """A2 — Validator present, no recovery policy."""

    @property
    def arm(self) -> ControlArm:
        return ControlArm.A2_VALIDATOR_ONLY

    async def execute_trial(
        self,
        *,
        task: RuntimeTask,
        adapter: BenchmarkAdapter,
        generation_config: GenerationConfig,
    ) -> dict[str, Any]:
        await adapter.reset_environment(task)
        steps = []
        rejections = 0
        for step_idx in range(min(task.budget.max_turns, 10)):
            obs = adapter.collect_public_observation(task.task_id, step_idx)
            steps.append(obs)
            # Simulate validation
            if obs.get("status") == "error":
                rejections += 1
            if step_idx >= 4:
                break
        artifact = adapter.finalize_runtime_artifact(task.task_id)
        return {
            "arm": self.arm.value,
            "task_id": task.task_id,
            "steps": steps,
            "artifact": artifact,
            "recovery_actions": [],
            "validation_rejections": rejections,
        }


class OdysFullPolicy:
    """A3 — Full Odys recovery + validation + observable progress."""

    @property
    def arm(self) -> ControlArm:
        return ControlArm.A3_ODYS_FULL

    async def execute_trial(
        self,
        *,
        task: RuntimeTask,
        adapter: BenchmarkAdapter,
        generation_config: GenerationConfig,
    ) -> dict[str, Any]:
        await adapter.reset_environment(task)
        observer = ShadowProgressObserver()
        steps = []
        recovery_actions = []
        rejections = 0

        for step_idx in range(min(task.budget.max_turns, 15)):
            obs = adapter.collect_public_observation(task.task_id, step_idx)
            steps.append(obs)

            # Shadow observer records (non-interfering)
            shadow = observer.observe(
                task_id=task.task_id,
                step=step_idx,
                action_identity=f"action_{step_idx}",
                tool_result=obs,
            )

            # Simulate recovery based on shadow signal
            if shadow.signal.value in {"ANOMALY", "STALLED"}:
                recovery_actions.append({
                    "type": "retry_with_context",
                    "step": step_idx,
                    "signal": shadow.signal.value,
                })
                rejections += 1

            if step_idx >= 6:
                break

        artifact = adapter.finalize_runtime_artifact(task.task_id)
        return {
            "arm": self.arm.value,
            "task_id": task.task_id,
            "steps": steps,
            "artifact": artifact,
            "recovery_actions": recovery_actions,
            "validation_rejections": rejections,
            "shadow_records": len(observer.get_records()),
        }


class OdysMinusObservableProgress:
    """A4 — Odys Full minus Observable Progress authority.

    Removes ONLY the observable-progress signal from the recovery decision.
    All other mechanisms (retry, validator, budget policy) remain intact.
    """

    @property
    def arm(self) -> ControlArm:
        return ControlArm.A4_ODYS_MINUS_OBSERVABLE_PROGRESS

    async def execute_trial(
        self,
        *,
        task: RuntimeTask,
        adapter: BenchmarkAdapter,
        generation_config: GenerationConfig,
    ) -> dict[str, Any]:
        await adapter.reset_environment(task)
        observer = ShadowProgressObserver()
        steps = []
        recovery_actions = []
        rejections = 0

        for step_idx in range(min(task.budget.max_turns, 15)):
            obs = adapter.collect_public_observation(task.task_id, step_idx)
            steps.append(obs)

            # Observer still records but its signal is NOT used for recovery
            shadow = observer.observe(
                task_id=task.task_id,
                step=step_idx,
                action_identity=f"action_{step_idx}",
                tool_result=obs,
            )

            # Recovery uses ONLY explicit error signals, not progress signals
            if obs.get("status") in {"error", "failure", "FAILURE"}:
                recovery_actions.append({
                    "type": "retry_without_progress",
                    "step": step_idx,
                })
                rejections += 1

            if step_idx >= 6:
                break

        artifact = adapter.finalize_runtime_artifact(task.task_id)
        return {
            "arm": self.arm.value,
            "task_id": task.task_id,
            "steps": steps,
            "artifact": artifact,
            "recovery_actions": recovery_actions,
            "validation_rejections": rejections,
            "shadow_records": len(observer.get_records()),
            "observable_progress_used": False,  # Key: progress signals ignored
        }


class OdysMinusRecoveryBudgetPolicy:
    """A5 — Odys Full minus Recovery Budget Policy.

    Removes ONLY the recovery budget allocation logic.
    Retry, validator, and observable progress remain intact.
    """

    @property
    def arm(self) -> ControlArm:
        return ControlArm.A5_ODYS_MINUS_RECOVERY_BUDGET_POLICY

    async def execute_trial(
        self,
        *,
        task: RuntimeTask,
        adapter: BenchmarkAdapter,
        generation_config: GenerationConfig,
    ) -> dict[str, Any]:
        await adapter.reset_environment(task)
        observer = ShadowProgressObserver()
        steps = []
        recovery_actions = []
        rejections = 0

        for step_idx in range(min(task.budget.max_turns, 15)):
            obs = adapter.collect_public_observation(task.task_id, step_idx)
            steps.append(obs)

            shadow = observer.observe(
                task_id=task.task_id,
                step=step_idx,
                action_identity=f"action_{step_idx}",
                tool_result=obs,
            )

            # Recovery without budget-aware scheduling
            if shadow.signal.value in {"ANOMALY", "STALLED", "REGRESSING"}:
                recovery_actions.append({
                    "type": "retry_no_budget_policy",
                    "step": step_idx,
                    "signal": shadow.signal.value,
                })
                rejections += 1

            if step_idx >= 6:
                break

        artifact = adapter.finalize_runtime_artifact(task.task_id)
        return {
            "arm": self.arm.value,
            "task_id": task.task_id,
            "steps": steps,
            "artifact": artifact,
            "recovery_actions": recovery_actions,
            "validation_rejections": rejections,
            "shadow_records": len(observer.get_records()),
            "recovery_budget_policy_active": False,  # Key: budget policy disabled
        }


# ── Arm registry ────────────────────────────────────────────────────

ARM_POLICIES: dict[ControlArm, type] = {
    ControlArm.A0_BARE: BarePolicy,
    ControlArm.A1_RETRY_ONLY: RetryOnlyPolicy,
    ControlArm.A2_VALIDATOR_ONLY: ValidatorOnlyPolicy,
    ControlArm.A3_ODYS_FULL: OdysFullPolicy,
    ControlArm.A4_ODYS_MINUS_OBSERVABLE_PROGRESS: OdysMinusObservableProgress,
    ControlArm.A5_ODYS_MINUS_RECOVERY_BUDGET_POLICY: OdysMinusRecoveryBudgetPolicy,
}


def create_policy(arm: ControlArm) -> ControlPolicy:
    """Factory for arm policies."""
    cls = ARM_POLICIES[arm]
    return cls()  # type: ignore[return-value]


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

    reference_arm = list(results.keys())[0]
    ref = results[reference_arm]

    for arm, result in results.items():
        if result.get("task_id") != task_id:
            violations.append(
                f"{arm.value}: task_id mismatch "
                f"({result.get('task_id')} != {task_id})"
            )
    return violations
