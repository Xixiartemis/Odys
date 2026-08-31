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
from lhas.persistence.repositories import WorkspaceSessionBindingRepository
from lhas.recovery import DefaultRecoveryPolicy, RecoveryAction, RecoveryPolicy
from lhas.checkpoint import CheckpointService, ContextReconstructionService
from lhas.resume import CrashPoint, ResumeAction, ResumeDecisionService
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
        self.resume_decisions = ResumeDecisionService(
            task_repo=self.task_repo,
            run_repo=self.run_repo,
            attempt_repo=self.attempt_repo,
            validation_repo=self.validation_repo,
            failure_repo=self.failure_repo,
            action_repo=self.action_repo,
            snapshot_repo=self.snapshot_repo,
            checkpoint_repo=self.checkpoint_service.repo,
            binding_repo=WorkspaceSessionBindingRepository(db),
            workspace_enabled=self.workspace_manager is not None,
        )
        self._resume_mode = False

    # ------------------------------------------------------------------ API

    async def execute_task(self, task_id: str) -> Run:
        run = await self.prepare_task_run(task_id)
        return await self.continue_prepared_run(run.id)

    async def prepare_task_run(self, task_id: str) -> Run:
        """Persist Task/Run/workspace ownership before executor dispatch."""
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
        self._crash(CrashPoint.AFTER_RUN_STARTED, task=task, run=run)
        if self.workspace_manager is not None:
            self._workspace_session = self.workspace_manager.create_for_run(task, run)
            self._crash(CrashPoint.AFTER_WORKSPACE_BOUND, task=task, run=run)
        return run

    async def continue_prepared_run(self, run_id: str) -> Run:
        run = self.run_repo.get(run_id)
        if run is None:
            raise KeyError(f"Run {run_id} not found")
        if run.status is not RunStatus.RUNNING:
            return run
        task = self.task_repo.get(run.task_id)
        if task is None:
            raise KeyError(f"Task {run.task_id} not found")
        if self.workspace_manager is not None and self._workspace_session is None:
            self._workspace_session = self.workspace_manager.ensure_for_run(task, run)
        return await self._continue_run(run.id)

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
        if self.workspace_manager is None and run.executor_type != "NativeAgentExecutor":
            raise RunNotResumable("WORKSPACE_SESSION_NOT_CONFIGURED")
        if self.workspace_manager is not None:
            self._workspace_session = self.workspace_manager.ensure_for_run(task, run)
            session_id = self._workspace_session.manifest.session_id
        else:
            session_id = None
        self._emit(EventType.RUN_RESUME_STARTED, task=task, run=run, payload={"session_id": session_id, "control_intent": "resume_run"})
        self._resume_mode = True
        try:
            return await self._continue_run(run_id)
        finally:
            self._resume_mode = False

    async def resume_task(self, task_id: str, *, runtime_target: Any = None) -> Run:
        """Resume a parked task through the control plane after migration."""
        task = self.task_repo.get(task_id)
        if task is None:
            raise KeyError(f"Task {task_id} not found")
        runs = self.run_repo.list_for_task(task_id)
        if not runs:
            raise RunNotResumable("TASK_HAS_NO_RUN")
        run = runs[-1]
        if task.status is TaskStatus.BLOCKED_PROVIDER:
            task.status = TaskStatus.RUNNING
            self.task_repo.update(task)
        if run.status is RunStatus.BLOCKED_PROVIDER:
            run.status = RunStatus.RUNNING
            self.run_repo.update(run)
        latest = self.attempt_repo.list_for_run(run.id)[-1] if self.attempt_repo.list_for_run(run.id) else None
        if latest is not None and latest.error_type in {"QUOTA_EXHAUSTED", "BILLING_OR_CREDIT_EXHAUSTED", "AUTH_INVALID"} and run.executor_type == "NativeAgentExecutor":
            # Re-open the same native Attempt so its durable snapshot and
            # completed side effects remain the continuation point.
            latest.status = AttemptStatus.RUNNING
            latest.error_type = None
            latest.error_message = None
            self.attempt_repo.update(latest)
        return await self.resume_run(run.id)

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

    async def _continue_run(self, run_id: str) -> Run:
        """Execute one durable resume action at a time until terminal/boundary."""

        for _ in range(256):
            state = self.resume_decisions.inspect(run_id)
            decision = self.resume_decisions.decide(state)
            action = decision.action
            if action is ResumeAction.RETURN_TERMINAL:
                return await self._finish_from_state(state)
            if action is ResumeAction.INITIALIZE_WORKSPACE:
                if self.workspace_manager is None:
                    raise RunNotResumable("WORKSPACE_SESSION_NOT_CONFIGURED")
                self._workspace_session = self.workspace_manager.ensure_for_run(state.task, state.run)
                continue
            if action is ResumeAction.START_FIRST_ATTEMPT:
                await self._execute_attempt(state, 1)
                continue
            if action is ResumeAction.START_PENDING_ATTEMPT:
                await self._execute_attempt(state, state.latest_attempt.attempt_number)
                continue
            if action is ResumeAction.RESUME_NATIVE_ATTEMPT:
                await self._resume_native_attempt(state)
                continue
            if action is ResumeAction.START_NEXT_ATTEMPT:
                await self._execute_attempt(state, state.latest_attempt.attempt_number + 1)
                continue
            if action is ResumeAction.RECOVER_INTERRUPTED_ATTEMPT:
                await self._recover_interrupted_attempt(state)
                continue
            if action is ResumeAction.VALIDATE_COMPLETED_ATTEMPT:
                await self._validate_persisted_attempt(state)
                continue
            if action is ResumeAction.VALIDATE_NON_SUCCESS_ATTEMPT:
                await self._validate_persisted_attempt(state, outcome_arbitration=True)
                continue
            if action is ResumeAction.COMPLETE_FROM_PERSISTED_VALIDATION:
                return self._complete_from_state(state)
            if action is ResumeAction.CLASSIFY_PERSISTED_VALIDATION_FAILURE:
                await self._classify_state(state)
                continue
            if action is ResumeAction.CLASSIFY_ATTEMPT_FAILURE:
                await self._classify_state(state)
                continue
            if action is ResumeAction.CONTINUE_PERSISTED_RECOVERY:
                await self._continue_recovery(state)
                continue
        raise RunNotResumable("RESUME_STATE_MACHINE_GUARD_EXCEEDED")

    async def _execute_attempt(self, state, number: int) -> None:
        task, run = state.task, state.run
        pending = state.latest_attempt if state.latest_attempt is not None and state.latest_attempt.status is AttemptStatus.PENDING and state.latest_attempt.attempt_number == number else None
        previous = [item for item in state.attempts if pending is None or item.id != pending.id]
        latest = state.latest_attempt
        report = state.failure_report
        action = state.recovery_action
        attempt = pending or self._create_attempt(run, number)
        self._emit(EventType.ATTEMPT_STARTED, task=task, run=run, attempt=attempt,
                    payload={"attempt_number": number, "resume_origin": "PROCESS_RESUME" if number > 1 else None})
        attempt.status = AttemptStatus.RUNNING
        attempt.started_at = self._now()
        self.attempt_repo.update(attempt)
        self._crash(CrashPoint.AFTER_ATTEMPT_STARTED, task=task, run=run, attempt=attempt)
        if number == 1:
            snapshot = self.context_builder.build(
                task=task, attempt_number=number, previous_attempts=previous,
                failure_report=None, recovery_action=None, run_id=run.id, attempt_id=attempt.id,
            )
        else:
            snapshot, _metrics = self.context_reconstruction_service.reconstruct(
                task=task, run_id=run.id, attempt_id=attempt.id, attempt_number=number,
                previous_attempts=previous, failure_report=report, recovery_action=action,
            )
        self.snapshot_repo.create(snapshot)
        attempt.context_snapshot_id = snapshot.id
        self.attempt_repo.update(attempt)
        self._emit(EventType.CONTEXT_BUILT, task=task, run=run, attempt=attempt,
                    payload={"context_snapshot_id": snapshot.id, "policy": snapshot.policy})
        self._crash(CrashPoint.AFTER_CONTEXT_BUILT, task=task, run=run, attempt=attempt)
        executor = self._make_executor()
        self._emit(EventType.EXECUTOR_STARTED, task=task, run=run, attempt=attempt,
                   payload={"executor": getattr(executor, "name", executor.__class__.__name__)})
        self._current_snapshot = snapshot
        await self._run_executor(task, run, attempt, executor)
        self._crash(CrashPoint.AFTER_EXECUTOR_PERSISTED, task=task, run=run, attempt=attempt)

    async def _recover_interrupted_attempt(self, state) -> None:
        task, run = state.task, state.run
        attempt = state.latest_attempt
        if attempt.status is AttemptStatus.RUNNING:
            attempt.status = AttemptStatus.CRASHED
            attempt.error_type = "PROCESS_INTERRUPTED"
            attempt.error_message = "previous process ended before attempt finalization"
            attempt.finished_at = self._now()
            self.attempt_repo.update(attempt)
            self._emit(EventType.ATTEMPT_CRASHED, task=task, run=run, attempt=attempt,
                        payload={"attempt_number": attempt.attempt_number, "error_type": attempt.error_type})
        if state.validation is not None:
            return
        recovery_state = await self._recovered_workspace_state()
        self._emit(EventType.WORKSPACE_RECOVERY_STATE, task=task, run=run, attempt=attempt,
                   payload=recovery_state)
        result = ExecutionResult(
            status=ExecutionStatus.SUCCESS,
            output=json.dumps({"recovery_origin": "PROCESS_RESUME", "workspace_recovery": recovery_state}, sort_keys=True),
        )
        await self._validate_result(task, run, attempt, result, recovery_origin=True)

    async def _resume_native_attempt(self, state) -> None:
        """Re-enter the Odys-owned loop at its durable same-Attempt snapshot."""
        task, run, attempt = state.task, state.run, state.latest_attempt
        if run.executor_type != "NativeAgentExecutor" or attempt.status is not AttemptStatus.RUNNING:
            raise RunNotResumable("NATIVE_ATTEMPT_RESUME_STATE_INVALID")
        snapshot = state.context_snapshot
        if snapshot is not None:
            self._current_snapshot = snapshot
        executor = self._make_executor()
        self._emit(
            EventType.EXECUTOR_STARTED,
            task=task,
            run=run,
            attempt=attempt,
            payload={"executor": getattr(executor, "name", executor.__class__.__name__), "resume_same_attempt": True},
        )
        await self._run_executor(task, run, attempt, executor)

    async def _validate_persisted_attempt(self, state, *, outcome_arbitration: bool = False) -> None:
        result = self._result_for_attempt(state.latest_attempt)
        await self._validate_result(
            state.task,
            state.run,
            state.latest_attempt,
            result,
            outcome_arbitration=outcome_arbitration,
        )

    async def _validate_result(
        self,
        task,
        run,
        attempt,
        result,
        recovery_origin: bool = False,
        outcome_arbitration: bool = False,
    ) -> ValidationResult:
        existing = self.validation_repo.get_for_attempt(attempt.id)
        if existing is not None:
            return existing
        payload: dict[str, Any] = {}
        if recovery_origin:
            payload["recovery_origin"] = "PROCESS_RESUME"
        if outcome_arbitration:
            payload.update(
                {
                    "outcome_arbitration": True,
                    "executor_attempt_status": attempt.status.value,
                    "executor_error_type": attempt.error_type,
                }
            )
        self._emit(EventType.VALIDATION_STARTED, task=task, run=run, attempt=attempt,
                   payload=payload or None)
        validation = await self.validator.validate(task=task, attempt=attempt, result=result)
        self.validation_repo.create(validation)
        self._crash(CrashPoint.AFTER_VALIDATION_PERSISTED, task=task, run=run, attempt=attempt)
        event = EventType.VALIDATION_PASSED if validation.passed else EventType.VALIDATION_FAILED
        self._emit(event, task=task, run=run, attempt=attempt,
                   payload={"evidence": validation.evidence, "recovery_origin": "PROCESS_RESUME"} if recovery_origin else {"evidence": validation.evidence})
        return validation

    async def _classify_state(self, state) -> FailureReport:
        attempt = state.latest_attempt
        existing = self.failure_repo.get_for_attempt(attempt.id)
        if existing is not None:
            return existing
        result = self._result_for_attempt(attempt)
        validation = state.validation
        report = await self.classifier.classify(task=state.task, attempt=attempt, result=result, validation=validation)
        self.failure_repo.create(report)
        attempt.failure_type = report.failure_type.value
        self.attempt_repo.update(attempt)
        self._emit(EventType.FAILURE_CLASSIFIED, task=state.task, run=state.run, attempt=attempt,
                   payload={"failure_type": report.failure_type.value, "failure_class": report.failure_class.value,
                            "confidence": report.confidence, "suggested_recovery": report.suggested_recovery})
        self._crash(CrashPoint.AFTER_FAILURE_CLASSIFIED, task=state.task, run=state.run, attempt=attempt)
        return report

    async def _continue_recovery(self, state) -> None:
        attempt = state.latest_attempt
        report = state.failure_report or await self._classify_state(state)
        action = state.recovery_action
        if action is None:
            history = self.action_repo.list_for_run(state.run.id)
            action = await self.recovery_policy.decide(
                task=state.task, attempt=attempt, failure_report=report,
                attempt_number=attempt.attempt_number, max_attempts=state.task.max_attempts,
                history=history,
            )
            self.action_repo.create(action)
            self._emit(EventType.RECOVERY_DECIDED, task=state.task, run=state.run, attempt=attempt,
                       payload={"action": action.action_type.value, "reason": action.reason,
                                "attempt_to": action.attempt_to, "added_context": action.added_context})
            self._crash(CrashPoint.AFTER_RECOVERY_DECIDED, task=state.task, run=state.run, attempt=attempt)
        checkpoint = self.checkpoint_service.repo.latest_for_run(state.run.id)
        if (
            checkpoint is None
            or checkpoint.attempt_id != attempt.id
            or checkpoint.attempt_number != attempt.attempt_number
        ):
            self.checkpoint_service.create_checkpoint(state.task, state.run.id, attempt.id, attempt.attempt_number)
            self._crash(CrashPoint.AFTER_CHECKPOINT_CREATED, task=state.task, run=state.run, attempt=attempt)
        if action.action_type in {RecoveryActionType.RETRY_WITH_FAILURE_CONTEXT, RecoveryActionType.RETRY_WITH_EXPANDED_CONTEXT}:
            self._emit(EventType.RECOVERY_STARTED, task=state.task, run=state.run, attempt=attempt,
                        payload={"action": action.action_type.value, "next_attempt": attempt.attempt_number + 1})

    def _result_for_attempt(self, attempt: Attempt) -> ExecutionResult:
        if attempt.executor_result:
            try:
                return ExecutionResult.model_validate(json.loads(attempt.executor_result))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RunNotResumable("PERSISTED_EXECUTOR_RESULT_INVALID") from exc
        status = ExecutionStatus.FAILURE if attempt.status is not AttemptStatus.COMPLETED else ExecutionStatus.SUCCESS
        return ExecutionResult(status=status, output=attempt.output, error_type=attempt.error_type, error_message=attempt.error_message)

    def _complete_from_state(self, state) -> Run:
        result = self._result_for_attempt(state.latest_attempt)
        return self._complete(state.task, state.run, state.latest_attempt, result, state.validation)

    async def _finish_from_state(self, state) -> Run:
        run = state.run
        if run.status is not RunStatus.RUNNING:
            return run
        action = state.recovery_action
        if action is None:
            raise RunNotResumable("RUN_NOT_RESUMABLE")
        if action.action_type is RecoveryActionType.BLOCK_PROVIDER:
            # Keep the durable Run resumable. The Task is parked until an
            # explicit resume_task control intent performs provider migration.
            state.task.status = TaskStatus.BLOCKED_PROVIDER
            self.task_repo.update(state.task)
            self._emit(EventType.TASK_FAILED, task=state.task, run=run, attempt=state.latest_attempt,
                        payload={"reason": action.reason, "blocked_provider": True})
            return run
        run.status = RunStatus.ESCALATED if action.action_type in {RecoveryActionType.ESCALATE, RecoveryActionType.HUMAN_APPROVAL} else RunStatus.FAILED
        run.finished_at = self._now()
        self.run_repo.update(run)
        state.task.status = TaskStatus.ESCALATED if run.status is RunStatus.ESCALATED else TaskStatus.FAILED
        self.task_repo.update(state.task)
        event = EventType.RUN_ESCALATED if run.status is RunStatus.ESCALATED else EventType.RUN_FAILED
        self._emit(event, task=state.task, run=run, attempt=state.latest_attempt,
                   payload={"reason": action.reason, "resume": self._resume_mode})
        task_event = EventType.TASK_ESCALATED if run.status is RunStatus.ESCALATED else EventType.TASK_FAILED
        self._emit(task_event, task=state.task, run=run, attempt=state.latest_attempt,
                   payload={"reason": action.reason})
        if self._resume_mode:
            self._emit(EventType.RUN_RESUME_FAILED, task=state.task, run=run, attempt=state.latest_attempt,
                       payload={"action": action.action_type.value})
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
        if self._resume_mode:
            self._emit(EventType.RUN_RESUME_COMPLETED, task=task, run=run, attempt=attempt,
                       payload={"resume_validation_passed": True})
        return run

    @staticmethod
    def _dump_json(obj: Any) -> str:
        from lhas.domain.models import json_dumps
        return json_dumps(obj)
