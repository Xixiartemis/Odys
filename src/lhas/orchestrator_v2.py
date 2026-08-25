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
import hashlib
import json
from typing import Any, Callable, Optional

from lhas.context_builder import ContextBuilder, ContextSnapshot
from lhas.domain.enums import (
    AttemptStatus,
    EventType,
    ExecutionStatus,
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
from lhas.checkpoint import CheckpointService, ContextReconstructionService
from lhas.validation import RuleValidator, ValidationResult, Validator

logger = logging.getLogger("lhas.orchestrator_v2")


class RunNotResumable(RuntimeError):
    pass


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
        executor_factory: Optional[Callable[[], AgentExecutor]] = None,
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
        # Retry attempts use the durable CP-3 reconstruction service.  The
        # first attempt retains the ordinary initial-context path.
        self.context_reconstruction_service = ContextReconstructionService(
            db, builder=ContextBuilder(
                policy="CP-3", profile=getattr(self.context_builder, "profile", None)
            )
        )

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
        if self.workspace_manager is not None:
            self._workspace_session = self.workspace_manager.create_for_run(task, run)

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

            # First attempt uses the ordinary context path.  Every retry is
            # reconstructed from the latest checkpoint plus the event delta;
            # ContextBuilder remains the sole assembly boundary in both paths.
            if n == 1:
                snapshot = self.context_builder.build(
                    task=task,
                    attempt_number=n,
                    previous_attempts=prior_attempts,
                    failure_report=last_report,
                    recovery_action=last_action,
                    run_id=run.id,
                    attempt_id=attempt.id,
                )
            else:
                snapshot, _reconstruction_metrics = self.context_reconstruction_service.reconstruct(
                    task=task,
                    run_id=run.id,
                    attempt_id=attempt.id,
                    attempt_number=n,
                    previous_attempts=prior_attempts,
                    failure_report=last_report,
                    recovery_action=last_action,
                )
            self.snapshot_repo.create(snapshot)
            attempt.context_snapshot_id = snapshot.id
            self.attempt_repo.update(attempt)
            self._emit(
                EventType.CONTEXT_BUILT, task=task, run=run, attempt=attempt,
                payload={"context_snapshot_id": snapshot.id, "policy": snapshot.policy},
            )

            executor = self._make_executor()
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

    async def resume_run(self, run_id: str) -> Run:
        """Manually resume a persisted RUNNING outer run.

        This restores the outer state and durable workspace only.  It never
        calls ``AgentExecutor.resume`` or claims to restore provider-internal
        conversation state.
        """
        run = self.run_repo.get(run_id)
        if run is None:
            raise KeyError(f"Run {run_id} not found")
        if run.status is not RunStatus.RUNNING:
            return run
        task = self.task_repo.get(run.task_id)
        if task is None:
            raise KeyError(f"Task {run.task_id} not found")
        if self.workspace_manager is None:
            raise RunNotResumable("WORKSPACE_SESSION_NOT_CONFIGURED")
        self._workspace_session = self.workspace_manager.reopen_for_run(task, run)
        self._emit(EventType.RUN_RESUME_STARTED, task=task, run=run, payload={"session_id": self._workspace_session.manifest.session_id})
        attempts = self.attempt_repo.list_for_run(run.id)
        if not attempts or attempts[-1].status is not AttemptStatus.RUNNING:
            raise RunNotResumable("RUN_NOT_RESUMABLE")
        interrupted = attempts[-1]
        interrupted.status = AttemptStatus.CRASHED
        interrupted.error_type = "PROCESS_INTERRUPTED"
        interrupted.error_message = "previous process ended before attempt finalization"
        interrupted.finished_at = self._now()
        self.attempt_repo.update(interrupted)
        self._emit(
            EventType.ATTEMPT_CRASHED, task=task, run=run, attempt=interrupted,
            payload={"attempt_number": interrupted.attempt_number, "error_type": "PROCESS_INTERRUPTED"},
        )

        recovery_state = await self._recovered_workspace_state()
        self._emit(EventType.WORKSPACE_RECOVERY_STATE, task=task, run=run, attempt=interrupted, payload=recovery_state)
        recovered_result = ExecutionResult(
            status=ExecutionStatus.SUCCESS,
            output=json.dumps({"recovery_origin": "PROCESS_RESUME", "workspace_recovery": recovery_state}, sort_keys=True),
        )
        self._emit(EventType.VALIDATION_STARTED, task=task, run=run, attempt=interrupted, payload={"recovery_origin": "PROCESS_RESUME"})
        validation = await self.validator.validate(task=task, attempt=interrupted, result=recovered_result)
        self.validation_repo.create(validation)
        if validation.passed:
            self._emit(EventType.VALIDATION_PASSED, task=task, run=run, attempt=interrupted, payload={"recovery_origin": "PROCESS_RESUME", "evidence": validation.evidence})
            completed = self._complete(task, run, interrupted, recovered_result, validation)
            self._emit(EventType.RUN_RESUME_COMPLETED, task=task, run=run, attempt=interrupted, payload={"resume_validation_passed": True, "new_attempts_created": 0})
            return completed

        self._emit(EventType.VALIDATION_FAILED, task=task, run=run, attempt=interrupted, payload={"recovery_origin": "PROCESS_RESUME", "evidence": validation.evidence})
        report = await self.classifier.classify(task=task, attempt=interrupted, result=recovered_result, validation=validation)
        self.failure_repo.create(report)
        interrupted.failure_type = report.failure_type.value
        self.attempt_repo.update(interrupted)
        self._emit(EventType.FAILURE_CLASSIFIED, task=task, run=run, attempt=interrupted, payload={"failure_type": report.failure_type.value, "failure_class": report.failure_class.value, "suggested_recovery": report.suggested_recovery})
        action = await self.recovery_policy.decide(task=task, attempt=interrupted, failure_report=report, attempt_number=interrupted.attempt_number, max_attempts=task.max_attempts, history=[])
        self.action_repo.create(action)
        self._emit(EventType.RECOVERY_DECIDED, task=task, run=run, attempt=interrupted, payload={"action": action.action_type.value, "attempt_to": action.attempt_to, "reason": action.reason})
        # Recovery state is appended before the checkpoint so the checkpoint
        # absorbs the authoritative durable-workspace candidate summary.
        self.checkpoint_service.create_checkpoint(task, run.id, interrupted.id, interrupted.attempt_number)
        if action.action_type is not RecoveryActionType.RETRY_WITH_FAILURE_CONTEXT and action.action_type is not RecoveryActionType.RETRY_WITH_EXPANDED_CONTEXT:
            return await self._resume_terminal_failure(task, run, action)
        self._emit(EventType.RECOVERY_STARTED, task=task, run=run, attempt=interrupted, payload={"action": action.action_type.value, "next_attempt": interrupted.attempt_number + 1})
        return await self._resume_retry_once(task, run, interrupted, report, action)

    async def _recovered_workspace_state(self) -> dict[str, Any]:
        diff = await self._workspace_session.workspace.diff()
        patch = diff.get("diff", "")
        return {
            "changed_files": list(diff.get("changed_files", []))[:100],
            "files_changed": int(diff.get("files_changed", 0)),
            "lines_added": int(diff.get("lines_added", 0)),
            "lines_removed": int(diff.get("lines_removed", 0)),
            "truncated": bool(diff.get("truncated", False)),
            "patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
        }

    async def _resume_terminal_failure(self, task, run, action):
        run.status = RunStatus.ESCALATED if action.action_type is RecoveryActionType.ESCALATE else RunStatus.FAILED
        run.finished_at = self._now()
        self.run_repo.update(run)
        task.status = TaskStatus.ESCALATED if run.status is RunStatus.ESCALATED else TaskStatus.FAILED
        self.task_repo.update(task)
        self._emit(EventType.RUN_RESUME_FAILED, task=task, run=run, payload={"action": action.action_type.value})
        return run

    async def _resume_retry_once(self, task, run, interrupted, report, action):
        n = interrupted.attempt_number + 1
        attempt = self._create_attempt(run, n)
        self._emit(EventType.ATTEMPT_STARTED, task=task, run=run, attempt=attempt, payload={"attempt_number": n, "resume_origin": "PROCESS_RESUME"})
        attempt.status = AttemptStatus.RUNNING
        attempt.started_at = self._now()
        self.attempt_repo.update(attempt)
        snapshot, _metrics = self.context_reconstruction_service.reconstruct(
            task=task, run_id=run.id, attempt_id=attempt.id, attempt_number=n,
            previous_attempts=[interrupted], failure_report=report, recovery_action=action,
        )
        self.snapshot_repo.create(snapshot)
        attempt.context_snapshot_id = snapshot.id
        self.attempt_repo.update(attempt)
        self._emit(EventType.CONTEXT_BUILT, task=task, run=run, attempt=attempt, payload={"context_snapshot_id": snapshot.id, "policy": snapshot.policy, "resume_origin": "PROCESS_RESUME"})
        executor = self._make_executor()
        self._emit(EventType.EXECUTOR_STARTED, task=task, run=run, attempt=attempt, payload={"executor": getattr(executor, "name", executor.__class__.__name__)})
        self._current_snapshot = snapshot
        result, outcome = await self._run_executor(task, run, attempt, executor)
        if outcome == "completed":
            self._emit(EventType.VALIDATION_STARTED, task=task, run=run, attempt=attempt)
            validation = await self.validator.validate(task=task, attempt=attempt, result=result)
            self.validation_repo.create(validation)
            if validation.passed:
                self._emit(EventType.VALIDATION_PASSED, task=task, run=run, attempt=attempt, payload={"resume_origin": "PROCESS_RESUME", "evidence": validation.evidence})
                completed = self._complete(task, run, attempt, result, validation)
                self._emit(
                    EventType.RUN_RESUME_COMPLETED,
                    task=task,
                    run=run,
                    attempt=attempt,
                    payload={"resume_validation_passed": True, "new_attempts_created": 1},
                )
                return completed
        run.status = RunStatus.FAILED
        run.finished_at = self._now()
        self.run_repo.update(run)
        task.status = TaskStatus.FAILED
        self.task_repo.update(task)
        self._emit(EventType.RUN_RESUME_FAILED, task=task, run=run, attempt=attempt, payload={"reason": "retry_failed"})
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
        if self.workspace_manager is not None:
            self.workspace_manager.mark_completed(run.id)
        return run

    @staticmethod
    def _dump_json(obj: Any) -> str:
        from lhas.domain.models import json_dumps
        return json_dumps(obj)
