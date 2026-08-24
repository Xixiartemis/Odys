"""Phase B Orchestrator — Validation + Failure + Recovery loop (docs/01).

The core Phase B closed loop:

    Executor
       ↓
    Validator
       ↓
    FAIL
       ↓
    FailureClassifier
       ↓
    RecoveryPolicy
       ↓
    ContextBuilder
       ↓
    Attempt #2  ->  FAIL → CLASSIFY → RECOVER → PASS

Reuses the Phase A executor handling (timeout / crash / result finalization and
their events) from Orchestrator; only the decision layer is replaced.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from lhas.context_builder import ContextBuilder, ContextSnapshot
from lhas.domain.enums import (
    AttemptStatus,
    EventType,
    RecoveryActionType,
    RunStatus,
    TaskStatus,
)
from lhas.domain.models import Attempt, Run, Task
from lhas.executors.protocol import AgentExecutor, ExecutionResult
from lhas.failure import FailureClassifier, FailureReport, RuleFailureClassifier
from lhas.orchestrator import Orchestrator
from lhas.persistence.database import Database
from lhas.persistence.phaseb_repos import (
    ContextSnapshotRepository,
    FailureReportRepository,
    RecoveryActionRepository,
    ValidationResultRepository,
)
from lhas.recovery import DefaultRecoveryPolicy, RecoveryAction, RecoveryPolicy
from lhas.checkpoint import CheckpointService
from lhas.validation import RuleValidator, ValidationResult, Validator

logger = logging.getLogger("lhas.orchestrator_v2")


class RecoveringOrchestrator(Orchestrator):
    """Orchestrator with the full Phase B pipeline.

    Executor success is not enough: the Validator decides whether the task is
    complete. Any failure (executor or validation) is classified, then the
    RecoveryPolicy decides the next action, and the ContextBuilder assembles
    the recovery context for the next attempt.
    """

    def __init__(
        self,
        db: Database,
        *,
        executor_factory: Callable[[], AgentExecutor],
        validator: Optional[Validator] = None,
        classifier: Optional[FailureClassifier] = None,
        recovery_policy: Optional[RecoveryPolicy] = None,
        context_builder: Optional[ContextBuilder] = None,
        **kwargs: Any,
    ):
        super().__init__(db, executor_factory=executor_factory, **kwargs)
        self.validator = validator or RuleValidator()
        self.classifier = classifier or RuleFailureClassifier()
        self.recovery_policy = recovery_policy or DefaultRecoveryPolicy(context_policy=self.context_policy_version)
        self.context_builder = context_builder or ContextBuilder(policy=self.context_policy_version)
        self.snapshot_repo = ContextSnapshotRepository(db)
        self.validation_repo = ValidationResultRepository(db)
        self.failure_repo = FailureReportRepository(db)
        self.action_repo = RecoveryActionRepository(db)
        self.checkpoint_service = CheckpointService(db)

    # ------------------------------------------------------------------ API

    async def execute_task(self, task_id: str) -> Run:
        task = self._require_task(task_id)
        self._emit(EventType.TASK_STARTED, task=task, payload={"status": TaskStatus.RUNNING.value})
        task.status = TaskStatus.RUNNING
        self.task_repo.update(task)

        run = self._create_run(task)
        self._emit(EventType.RUN_CREATED, task=task, run=run)
        run.status = RunStatus.RUNNING
        run.started_at = self._now()
        self.run_repo.update(run)
        self._emit(EventType.RUN_STARTED, task=task, run=run)

        prior_attempts: list[Attempt] = []
        recovery_history: list[RecoveryAction] = []
        last_report: Optional[FailureReport] = None
        last_action: Optional[RecoveryAction] = None

        for n in range(1, task.max_attempts + 1):
            attempt = self._create_attempt(run, n)
            self._emit(EventType.ATTEMPT_STARTED, task=task, run=run, attempt=attempt, payload={"attempt_number": n})
            attempt.status = AttemptStatus.RUNNING
            attempt.started_at = self._now()
            self.attempt_repo.update(attempt)

            # --- Context (CP-2: previous attempts + failure evidence + recovery) ---
            snapshot = self.context_builder.build(
                task=task,
                attempt_number=n,
                previous_attempts=prior_attempts,
                failure_report=last_report,
                recovery_action=last_action,
                run_id=run.id,
                attempt_id=attempt.id,
            )
            self.snapshot_repo.create(snapshot)
            attempt.context_snapshot_id = snapshot.id
            self.attempt_repo.update(attempt)
            self._emit(
                EventType.CONTEXT_BUILT, task=task, run=run, attempt=attempt,
                payload={"context_snapshot_id": snapshot.id, "policy": snapshot.policy},
            )

            executor = self.executor_factory()
            self._emit(
                EventType.EXECUTOR_STARTED, task=task, run=run, attempt=attempt,
                payload={"executor": getattr(executor, "name", executor.__class__.__name__)},
            )
            self._current_snapshot = snapshot
            result, outcome = await self._run_executor(task, run, attempt, executor)

            # --- Validation: executor success alone does not complete the task ---
            validation: Optional[ValidationResult] = None
            if outcome == "completed":
                self._emit(EventType.VALIDATION_STARTED, task=task, run=run, attempt=attempt)
                validation = await self.validator.validate(task=task, attempt=attempt, result=result)  # type: ignore[arg-type]
                self.validation_repo.create(validation)
                if validation.passed:
                    self._emit(
                        EventType.VALIDATION_PASSED, task=task, run=run, attempt=attempt,
                        payload={"evidence": validation.evidence},
                    )
                    self.checkpoint_service.create_checkpoint(task, run.id, attempt.id, n)
                    return self._complete(task, run, attempt, result, validation)
                self._emit(
                    EventType.VALIDATION_FAILED, task=task, run=run, attempt=attempt,
                    payload={"evidence": validation.evidence},
                )

            # --- Failure path: classify -> decide -> (recover | terminate) ---
            report = await self.classifier.classify(
                task=task, attempt=attempt, result=result, validation=validation,
            )
            self.failure_repo.create(report)
            attempt.failure_type = report.failure_type.value
            self.attempt_repo.update(attempt)
            self._emit(
                EventType.FAILURE_CLASSIFIED, task=task, run=run, attempt=attempt,
                payload={
                    "failure_type": report.failure_type.value,
                    "failure_class": report.failure_class.value,
                    "confidence": report.confidence,
                    "suggested_recovery": report.suggested_recovery,
                },
            )

            action = await self.recovery_policy.decide(
                task=task,
                attempt=attempt,
                failure_report=report,
                attempt_number=n,
                max_attempts=task.max_attempts,
                history=recovery_history,
            )
            self.action_repo.create(action)
            recovery_history.append(action)
            self._emit(
                EventType.RECOVERY_DECIDED, task=task, run=run, attempt=attempt,
                payload={
                    "action": action.action_type.value,
                    "reason": action.reason,
                    "attempt_to": action.attempt_to,
                    "added_context": action.added_context,
                },
            )
            self.checkpoint_service.create_checkpoint(task, run.id, attempt.id, n)

            if action.action_type == RecoveryActionType.ESCALATE:
                run.status = RunStatus.ESCALATED
                run.finished_at = self._now()
                self.run_repo.update(run)
                self._emit(
                    EventType.RUN_ESCALATED, task=task, run=run, attempt=attempt,
                    payload={"reason": action.reason},
                )
                task.status = TaskStatus.ESCALATED
                self.task_repo.update(task)
                self._emit(
                    EventType.TASK_ESCALATED, task=task, run=run, attempt=attempt,
                    payload={"reason": action.reason, "failure_type": report.failure_type.value},
                )
                return run

            if action.action_type == RecoveryActionType.ABORT:
                run.status = RunStatus.FAILED
                run.finished_at = self._now()
                self.run_repo.update(run)
                self._emit(
                    EventType.RUN_FAILED, task=task, run=run, attempt=attempt,
                    payload={"reason": action.reason},
                )
                task.status = TaskStatus.FAILED
                self.task_repo.update(task)
                self._emit(
                    EventType.TASK_FAILED, task=task, run=run, attempt=attempt,
                    payload={"reason": action.reason},
                )
                return run

            if action.action_type == RecoveryActionType.HUMAN_APPROVAL:
                # Phase B: no approval gate yet (Phase F). Record and escalate.
                run.status = RunStatus.ESCALATED
                run.finished_at = self._now()
                self.run_repo.update(run)
                self._emit(
                    EventType.HUMAN_APPROVAL_REQUIRED, task=task, run=run, attempt=attempt,
                    payload={"reason": action.reason},
                )
                task.status = TaskStatus.ESCALATED
                self.task_repo.update(task)
                self._emit(
                    EventType.TASK_ESCALATED, task=task, run=run, attempt=attempt,
                    payload={"reason": action.reason},
                )
                return run

            # --- RETRY_*: recovery context flows into the next attempt ---
            self._emit(
                EventType.RECOVERY_STARTED, task=task, run=run, attempt=attempt,
                payload={"action": action.action_type.value, "next_attempt": n + 1},
            )
            prior_attempts.append(attempt)
            last_report = report
            last_action = action

        # Unreachable: every loop iteration returns or schedules a retry.
        run.status = RunStatus.FAILED
        self.run_repo.update(run)
        task.status = TaskStatus.FAILED
        self.task_repo.update(task)
        self._emit(EventType.RUN_FAILED, task=task, run=run)
        self._emit(EventType.TASK_FAILED, task=task, run=run)
        return run

    # ------------------------------------------------------------- internals

    def _executor_context(self, task: Task, attempt: Attempt) -> dict[str, Any]:
        """Phase B: the executor sees the full ContextBuilder snapshot (CP-2),
        including failure evidence and recovery guidance for retries."""
        snapshot: Optional[ContextSnapshot] = getattr(self, "_current_snapshot", None)
        if snapshot is not None:
            return self.context_builder.to_executor_context(snapshot)
        return super()._executor_context(task, attempt)

    def _complete(
        self,
        task: Task,
        run: Run,
        attempt: Attempt,
        result: ExecutionResult,
        validation: ValidationResult,
    ) -> Run:
        run.result = self._dump_json({"output": result.output, "status": result.status.value, "validation": validation.passed})
        run.status = RunStatus.COMPLETED
        run.finished_at = self._now()
        self.run_repo.update(run)
        self._emit(
            EventType.RUN_COMPLETED, task=task, run=run, attempt=attempt,
            payload={"validation": validation.passed},
        )
        task.status = TaskStatus.COMPLETED
        self.task_repo.update(task)
        self._emit(EventType.TASK_COMPLETED, task=task, run=run, attempt=attempt)
        return run

    @staticmethod
    def _dump_json(obj: Any) -> str:
        from lhas.domain.models import json_dumps
        return json_dumps(obj)
