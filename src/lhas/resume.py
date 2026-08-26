"""Durable resume inspection and deterministic lifecycle decisions.

The resume service intentionally reads repositories rather than inferring a
phase from an in-memory call stack.  Events remain useful audit evidence, but
the persisted domain rows are the state authority for continuation decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from lhas.domain.enums import AttemptStatus, RecoveryActionType, RunStatus


class CrashPoint(str, Enum):
    AFTER_RUN_STARTED = "AFTER_RUN_STARTED"
    AFTER_WORKSPACE_BOUND = "AFTER_WORKSPACE_BOUND"
    AFTER_ATTEMPT_STARTED = "AFTER_ATTEMPT_STARTED"
    AFTER_EXECUTOR_PERSISTED = "AFTER_EXECUTOR_PERSISTED"
    AFTER_VALIDATION_PERSISTED = "AFTER_VALIDATION_PERSISTED"
    AFTER_FAILURE_CLASSIFIED = "AFTER_FAILURE_CLASSIFIED"
    AFTER_RECOVERY_DECIDED = "AFTER_RECOVERY_DECIDED"
    AFTER_CHECKPOINT_CREATED = "AFTER_CHECKPOINT_CREATED"
    AFTER_CONTEXT_BUILT = "AFTER_CONTEXT_BUILT"


class ResumeAction(str, Enum):
    INITIALIZE_WORKSPACE = "INITIALIZE_WORKSPACE"
    START_FIRST_ATTEMPT = "START_FIRST_ATTEMPT"
    RECOVER_INTERRUPTED_ATTEMPT = "RECOVER_INTERRUPTED_ATTEMPT"
    VALIDATE_COMPLETED_ATTEMPT = "VALIDATE_COMPLETED_ATTEMPT"
    COMPLETE_FROM_PERSISTED_VALIDATION = "COMPLETE_FROM_PERSISTED_VALIDATION"
    CLASSIFY_PERSISTED_VALIDATION_FAILURE = "CLASSIFY_PERSISTED_VALIDATION_FAILURE"
    CLASSIFY_ATTEMPT_FAILURE = "CLASSIFY_ATTEMPT_FAILURE"
    CONTINUE_PERSISTED_RECOVERY = "CONTINUE_PERSISTED_RECOVERY"
    START_NEXT_ATTEMPT = "START_NEXT_ATTEMPT"
    RETURN_TERMINAL = "RETURN_TERMINAL"


class NoOpCrashInjector:
    """Production default: lifecycle hooks have no side effects."""

    def hit(self, point: CrashPoint, **context: Any) -> None:
        return None


def invoke_crash_injector(injector: Any, point: CrashPoint, **context: Any) -> None:
    """Call an optional test fault injector without baking test logic in core."""

    if injector is None:
        return
    hit = getattr(injector, "hit", None)
    if hit is not None:
        hit(point, **context)
    elif callable(injector):
        injector(point, **context)


@dataclass(frozen=True)
class ResumeInspection:
    run: Any
    task: Any
    attempts: list[Any]
    latest_attempt: Optional[Any]
    workspace_binding: Optional[Any]
    validation: Optional[Any]
    failure_report: Optional[Any]
    recovery_action: Optional[Any]
    checkpoint: Optional[Any]
    context_snapshot: Optional[Any]


@dataclass(frozen=True)
class ResumeDecision:
    action: ResumeAction
    reason: str
    attempt_id: Optional[str] = None


class ResumeDecisionService:
    """Derive exactly one next action from durable lifecycle facts."""

    def __init__(self, *, task_repo, run_repo, attempt_repo, validation_repo,
                 failure_repo, action_repo, snapshot_repo, checkpoint_repo,
                 binding_repo, workspace_enabled: bool):
        self.task_repo = task_repo
        self.run_repo = run_repo
        self.attempt_repo = attempt_repo
        self.validation_repo = validation_repo
        self.failure_repo = failure_repo
        self.action_repo = action_repo
        self.snapshot_repo = snapshot_repo
        self.checkpoint_repo = checkpoint_repo
        self.binding_repo = binding_repo
        self.workspace_enabled = workspace_enabled

    def inspect(self, run_id: str) -> ResumeInspection:
        run = self.run_repo.get(run_id)
        if run is None:
            raise KeyError(f"Run {run_id} not found")
        task = self.task_repo.get(run.task_id)
        if task is None:
            raise KeyError(f"Task {run.task_id} not found")
        attempts = self.attempt_repo.list_for_run(run_id)
        latest = attempts[-1] if attempts else None
        binding = self.binding_repo.get_by_run(run_id) if self.workspace_enabled else None
        validation = self.validation_repo.get_for_attempt(latest.id) if latest else None
        report = self.failure_repo.get_for_attempt(latest.id) if latest else None
        action = self.action_repo.get_for_attempt(latest.id) if latest else None
        checkpoint = self.checkpoint_repo.latest_for_run(run_id)
        snapshot = self.snapshot_repo.latest_for_attempt(latest.id) if latest else None
        return ResumeInspection(
            run=run, task=task, attempts=attempts, latest_attempt=latest,
            workspace_binding=binding, validation=validation,
            failure_report=report, recovery_action=action,
            checkpoint=checkpoint, context_snapshot=snapshot,
        )

    def decide(self, state: ResumeInspection) -> ResumeDecision:
        run = state.run
        if run.status is not RunStatus.RUNNING:
            return ResumeDecision(ResumeAction.RETURN_TERMINAL, "run is terminal")
        if self.workspace_enabled and (
            state.workspace_binding is None
            or state.workspace_binding.state == "CREATING"
        ):
            return ResumeDecision(ResumeAction.INITIALIZE_WORKSPACE, "workspace binding is not OPEN")
        if self.workspace_enabled and state.workspace_binding.state not in {"OPEN", "COMPLETED"}:
            return ResumeDecision(ResumeAction.RETURN_TERMINAL, "workspace binding is not resumable")
        latest = state.latest_attempt
        if latest is None:
            return ResumeDecision(ResumeAction.START_FIRST_ATTEMPT, "no attempt is persisted")
        if latest.status is AttemptStatus.RUNNING:
            action = ResumeAction.RECOVER_INTERRUPTED_ATTEMPT if self.workspace_enabled else ResumeAction.CLASSIFY_ATTEMPT_FAILURE
            return ResumeDecision(action, "latest attempt is still RUNNING", latest.id)
        if latest.status is AttemptStatus.COMPLETED:
            if state.validation is None:
                return ResumeDecision(ResumeAction.VALIDATE_COMPLETED_ATTEMPT, "executor result is durable; validation is absent", latest.id)
            if state.validation.passed:
                return ResumeDecision(ResumeAction.COMPLETE_FROM_PERSISTED_VALIDATION, "validation pass is durable", latest.id)
            if state.failure_report is None:
                return ResumeDecision(ResumeAction.CLASSIFY_PERSISTED_VALIDATION_FAILURE, "validation failure is durable; report is absent", latest.id)
        elif latest.status in {AttemptStatus.FAILED, AttemptStatus.TIMED_OUT}:
            if state.failure_report is None:
                return ResumeDecision(ResumeAction.CLASSIFY_ATTEMPT_FAILURE, "executor failure is durable; report is absent", latest.id)
        elif latest.status is AttemptStatus.CRASHED:
            if self.workspace_enabled and latest.error_type == "PROCESS_INTERRUPTED" and state.validation is None:
                return ResumeDecision(ResumeAction.RECOVER_INTERRUPTED_ATTEMPT, "interrupted attempt needs workspace validation", latest.id)
            if state.validation is not None and state.validation.passed:
                return ResumeDecision(ResumeAction.COMPLETE_FROM_PERSISTED_VALIDATION, "recovered workspace validation passed", latest.id)
            if state.failure_report is None:
                return ResumeDecision(ResumeAction.CLASSIFY_ATTEMPT_FAILURE, "crashed attempt has no failure report", latest.id)
        if state.recovery_action is None:
            return ResumeDecision(ResumeAction.CONTINUE_PERSISTED_RECOVERY, "failure report exists; recovery decision is absent", latest.id)
        if state.recovery_action.action_type in {
            RecoveryActionType.RETRY_WITH_FAILURE_CONTEXT,
            RecoveryActionType.RETRY_WITH_EXPANDED_CONTEXT,
        }:
            checkpoint = state.checkpoint
            checkpoint_matches_current = (
                checkpoint is not None
                and checkpoint.attempt_id == latest.id
                and checkpoint.attempt_number == latest.attempt_number
            )
            if not checkpoint_matches_current:
                return ResumeDecision(
                    ResumeAction.CONTINUE_PERSISTED_RECOVERY,
                    "persisted retry action requires an exact checkpoint for the current attempt",
                    latest.id,
                )
            next_number = latest.attempt_number + 1
            if not any(a.attempt_number == next_number for a in state.attempts):
                return ResumeDecision(ResumeAction.START_NEXT_ATTEMPT, "persisted retry action has no next attempt", latest.id)
        return ResumeDecision(ResumeAction.RETURN_TERMINAL, "persisted recovery action is terminal or exhausted", latest.id)
