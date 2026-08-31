"""Phase A Orchestrator (docs/01, docs/02, docs/08).

Owns the attempt loop: Task -> Run -> Attempt -> Executor -> Result ->
EventStore -> SQLite. Every state transition persists and emits an Event
before the next transition begins.

Phase A recovery decision is the deterministic V0 default policy from
docs/08_RECOVERY_POLICY.md (no FailureClassifier yet):
  - attempt 1 fails  -> RETRY_WITH_FAILURE_CONTEXT
  - attempt 2 fails  -> RETRY_WITH_EXPANDED_CONTEXT
  - attempt 3 fails  -> ESCALATE
Phase B replaces this with a classifier-driven pipeline (lhas/orchestrator_v2.py).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

from lhas.domain.enums import AttemptStatus, EventType, ExecutionStatus, RunStatus, TaskStatus
from lhas.domain.models import Attempt, Event, Run, Task, json_dumps
from lhas.executors.protocol import AgentExecutor, ExecutionRequest, ExecutionResult
from lhas.persistence.database import Database
from lhas.persistence.event_store import EventStore
from lhas.persistence.repositories import AttemptRepository, RunRepository, TaskRepository
from lhas.resume import CrashPoint, NoOpCrashInjector, invoke_crash_injector

logger = logging.getLogger("lhas.orchestrator")


class RetryAction(str):
    RETRY_WITH_FAILURE_CONTEXT = "RETRY_WITH_FAILURE_CONTEXT"
    RETRY_WITH_EXPANDED_CONTEXT = "RETRY_WITH_EXPANDED_CONTEXT"
    ESCALATE = "ESCALATE"


class RetryDecision:
    def __init__(self, action: str, reason: str, next_attempt: Optional[int] = None):
        self.action = action
        self.reason = reason
        self.next_attempt = next_attempt


class DeterministicRecoveryPolicy:
    """V0 default policy (docs/08). Pure function of attempt number."""

    def decide(self, *, attempt_number: int, max_attempts: int, last_result: Optional[ExecutionResult] = None) -> RetryDecision:
        if attempt_number >= max_attempts:
            return RetryDecision(RetryAction.ESCALATE, "max attempts reached", None)
        if attempt_number == 1:
            return RetryDecision(RetryAction.RETRY_WITH_FAILURE_CONTEXT, "first attempt failed; retry with failure context", attempt_number + 1)
        return RetryDecision(RetryAction.RETRY_WITH_EXPANDED_CONTEXT, "second attempt failed; retry with expanded context", attempt_number + 1)


class Orchestrator:
    """Minimal Phase A orchestrator. Executors are injected via a factory."""

    def __init__(
        self,
        db: Database,
        *,
        task_repo: Optional[TaskRepository] = None,
        run_repo: Optional[RunRepository] = None,
        attempt_repo: Optional[AttemptRepository] = None,
        event_store: Optional[EventStore] = None,
        executor_factory: Optional[Callable[[], AgentExecutor]] = None,
        workspace_executor_factory: Optional[Callable[[Any], AgentExecutor]] = None,
        workspace_manager: Any = None,
        crash_injector: Any = None,
        recovery_policy: Optional[DeterministicRecoveryPolicy] = None,
        executor_type: str = "MockExecutor",
        provider: str = "mock",
        model: str = "mock-v0",
        harness_version: str = "HV-0.1",
        context_policy_version: str = "CP-0",
        dataset_version: str = "RUNTIME-V0.1",
        experiment_id: Optional[str] = None,
        runtime_target: Any = None,
    ):
        self.db = db
        self.task_repo = task_repo or TaskRepository(db)
        self.run_repo = run_repo or RunRepository(db)
        self.attempt_repo = attempt_repo or AttemptRepository(db)
        self.event_store = event_store or EventStore(db)
        if executor_factory is None and workspace_executor_factory is None:
            raise ValueError("executor_factory or workspace_executor_factory is required")
        self.executor_factory = executor_factory
        self.workspace_executor_factory = workspace_executor_factory
        self.workspace_manager = workspace_manager
        self._workspace_session = None
        self.crash_injector = crash_injector or NoOpCrashInjector()
        self.recovery_policy = recovery_policy or DeterministicRecoveryPolicy()
        self.executor_type = executor_type
        self.provider = provider
        self.model = model
        self.harness_version = harness_version
        self.context_policy_version = context_policy_version
        self.dataset_version = dataset_version
        self.experiment_id = experiment_id
        self.runtime_target = runtime_target

    def _crash(self, point: CrashPoint, **context: Any) -> None:
        invoke_crash_injector(self.crash_injector, point, **context)

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
        terminal: Optional[Run] = None
        for n in range(1, task.max_attempts + 1):
            attempt = self._create_attempt(run, n)
            self._emit(EventType.ATTEMPT_STARTED, task=task, run=run, attempt=attempt, payload={"attempt_number": n})
            attempt.status = AttemptStatus.RUNNING
            attempt.started_at = self._now()
            self.attempt_repo.update(attempt)

            context = self._build_context(task, n, prior_attempts)
            attempt.context_snapshot_id = f"ctx-{attempt.id}"
            self.attempt_repo.update(attempt)
            self._emit(
                EventType.CONTEXT_BUILT, task=task, run=run, attempt=attempt,
                payload={"context_snapshot_id": attempt.context_snapshot_id, "policy": self.context_policy_version},
            )

            executor = self.executor_factory()
            self._emit(
                EventType.EXECUTOR_STARTED, task=task, run=run, attempt=attempt,
                payload={"executor": getattr(executor, "name", executor.__class__.__name__)},
            )

            result, outcome = await self._run_executor(task, run, attempt, executor)

            if outcome == "completed":
                run.result = json_dumps({"output": result.output, "status": result.status.value})
                run.status = RunStatus.COMPLETED
                run.finished_at = self._now()
                self.run_repo.update(run)
                self._emit(EventType.RUN_COMPLETED, task=task, run=run, attempt=attempt)
                task.status = TaskStatus.COMPLETED
                self.task_repo.update(task)
                self._emit(EventType.TASK_COMPLETED, task=task, run=run, attempt=attempt)
                return run

            # Non-success path: decision (Phase A deterministic policy).
            decision = self.recovery_policy.decide(
                attempt_number=n, max_attempts=task.max_attempts, last_result=result,
            )
            if decision.action == RetryAction.ESCALATE:
                run.status = RunStatus.ESCALATED
                run.finished_at = self._now()
                self.run_repo.update(run)
                self._emit(
                    EventType.RUN_ESCALATED, task=task, run=run, attempt=attempt,
                    payload={"reason": decision.reason},
                )
                task.status = TaskStatus.ESCALATED
                self.task_repo.update(task)
                self._emit(
                    EventType.TASK_ESCALATED, task=task, run=run, attempt=attempt,
                    payload={"reason": decision.reason},
                )
                return run

            self._emit(
                EventType.RETRY_SCHEDULED, task=task, run=run, attempt=attempt,
                payload={
                    "action": decision.action,
                    "reason": decision.reason,
                    "next_attempt": decision.next_attempt,
                    "from_attempt": n,
                },
            )
            prior_attempts.append(attempt)

        # Unreachable: the loop always returns (completed or escalated).
        run.status = RunStatus.FAILED
        self.run_repo.update(run)
        task.status = TaskStatus.FAILED
        self.task_repo.update(task)
        self._emit(EventType.RUN_FAILED, task=task, run=run)
        self._emit(EventType.TASK_FAILED, task=task, run=run)
        return run

    # ------------------------------------------------------------- internals

    async def _run_executor(
        self, task: Task, run: Run, attempt: Attempt, executor: AgentExecutor
    ) -> tuple[Optional[ExecutionResult], str]:
        """Execute with timeout; classify the outcome; persist attempt state.

        Returns (result_or_None, outcome) where outcome in
        {"completed", "failed", "timed_out", "crashed"}.
        """
        request = ExecutionRequest(
            task_id=task.id,
            run_id=run.id,
            attempt_id=attempt.id,
            attempt_number=attempt.attempt_number,
            task=self._executor_task_payload(task),
            context=self._executor_context(task, attempt),
            metadata={
                "executor_type": self.executor_type,
                "provider": self.provider,
                "model": self.model,
                "harness_version": self.harness_version,
                "context_policy_version": self.context_policy_version,
                "dataset_version": self.dataset_version,
                "configured_target": self.runtime_target,
            },
        )

        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                executor.execute(request), timeout=task.timeout_seconds
            )
        except asyncio.TimeoutError:
            self._emit(
                EventType.EXECUTOR_FAILED, task=task, run=run, attempt=attempt,
                payload={"reason": "timeout", "error_type": "TimeoutError",
                         "error_message": f"executor exceeded timeout of {task.timeout_seconds}s",
                         "duration_ms": int((time.monotonic() - started) * 1000)},
            )
            await self._finalize_attempt(
                task, run, attempt, status=AttemptStatus.TIMED_OUT,
                reason="timeout", error_type="TimeoutError",
                error_message=f"executor exceeded timeout of {task.timeout_seconds}s",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return None, "timed_out"
        except Exception as exc:  # noqa: BLE001 — crash is a first-class outcome
            self._emit(
                EventType.EXECUTOR_FAILED, task=task, run=run, attempt=attempt,
                payload={"reason": "crash", "error_type": type(exc).__name__,
                         "error_message": str(exc), "duration_ms": int((time.monotonic() - started) * 1000)},
            )
            await self._finalize_attempt(
                task, run, attempt, status=AttemptStatus.CRASHED,
                reason="crash", error_type=type(exc).__name__, error_message=str(exc),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return None, "crashed"

        duration_ms = int((time.monotonic() - started) * 1000)
        if result.status == ExecutionStatus.SUCCESS:
            await self._finalize_attempt(
                task, run, attempt, status=AttemptStatus.COMPLETED,
                reason=None, error_type=None, error_message=None,
                duration_ms=duration_ms, result=result,
            )
            self._emit(
                EventType.EXECUTOR_COMPLETED, task=task, run=run, attempt=attempt,
                payload={"duration_ms": duration_ms, "output": result.output},
            )
            self._emit(
                EventType.ATTEMPT_COMPLETED, task=task, run=run, attempt=attempt,
                payload={"attempt_number": attempt.attempt_number},
            )
            return result, "completed"

        # Executor returned a non-success status (FAILURE / TIMEOUT / CRASH as data).
        status_map = {
            ExecutionStatus.FAILURE: AttemptStatus.FAILED,
            ExecutionStatus.TIMEOUT: AttemptStatus.TIMED_OUT,
            ExecutionStatus.CRASH: AttemptStatus.CRASHED,
        }
        attempt_status = status_map.get(result.status, AttemptStatus.FAILED)
        self._emit(
            EventType.EXECUTOR_FAILED, task=task, run=run, attempt=attempt,
            payload={
                "reason": result.status.value.lower(),
                "error_type": result.error_type,
                "error_message": result.error_message,
                "duration_ms": duration_ms,
            },
        )
        await self._finalize_attempt(
            task, run, attempt, status=attempt_status,
            reason="failure", error_type=result.error_type or result.status.value,
            error_message=result.error_message or result.status.value,
            duration_ms=duration_ms, result=result,
        )
        return result, "failed"

    async def _finalize_attempt(
        self,
        task: Task, run: Run, attempt: Attempt, *,
        status: AttemptStatus, reason: Optional[str],
        error_type: Optional[str], error_message: Optional[str],
        duration_ms: int, result: Optional[ExecutionResult] = None,
    ) -> None:
        attempt.status = status
        attempt.finished_at = self._now()
        attempt.duration_ms = duration_ms
        attempt.error_type = error_type
        attempt.error_message = error_message
        if result is not None:
            attempt.output = result.output
            attempt.executor_result = json_dumps(result.model_dump(mode="json"))
            attempt.usage = result.usage
        self.attempt_repo.update(attempt)
        event_map = {
            AttemptStatus.FAILED: EventType.ATTEMPT_FAILED,
            AttemptStatus.TIMED_OUT: EventType.ATTEMPT_TIMED_OUT,
            AttemptStatus.CRASHED: EventType.ATTEMPT_CRASHED,
        }
        if status in event_map:
            self._emit(
                event_map[status], task=task, run=run, attempt=attempt,
                payload={"attempt_number": attempt.attempt_number, "reason": reason, "error_type": error_type},
            )

    def _build_context(self, task: Task, attempt_number: int, prior_attempts: list[Attempt]) -> dict[str, Any]:
        """CP-0 minimal context: goal + current task (docs/05).

        Phase B promotes this into lhas/context_builder.py with CP-1/CP-2.
        """
        return {
            "policy": self.context_policy_version,
            "goal": task.objective,
            "task": {
                "title": task.title,
                "objective": task.objective,
                "constraints": task.constraints,
                "acceptance_criteria": task.acceptance_criteria,
            },
            "attempt_number": attempt_number,
            "previous_attempts": [
                {"number": a.attempt_number, "status": a.status.value, "error": a.error_message}
                for a in prior_attempts
            ],
        }

    def _executor_context(self, task: Task, attempt: Attempt) -> dict[str, Any]:
        # Phase A: the executor sees the CP-0 context (goal + task) plus attempt number.
        return {
            "policy": self.context_policy_version,
            "objective": task.objective,
            "constraints": task.constraints,
            "acceptance_criteria": task.acceptance_criteria,
            "attempt_number": attempt.attempt_number,
        }

    def _make_executor(self) -> AgentExecutor:
        if self.workspace_executor_factory is not None and self._workspace_session is not None:
            return self.workspace_executor_factory(self._workspace_session.workspace)
        if self.executor_factory is None:
            raise ValueError("executor_factory is not configured")
        return self.executor_factory()

    def _executor_task_payload(self, task: Task) -> dict[str, Any]:
        """Return the task snapshot exposed to an executor.

        Domain-neutral Core keeps the default payload small; benchmark
        adapters may extend it with task-local source data without making the
        Core import a concrete provider.
        """
        return {
            "id": task.id,
            "title": task.title,
            "objective": task.objective,
            "constraints": task.constraints,
            "acceptance_criteria": task.acceptance_criteria,
        }

    def _create_run(self, task: Task) -> Run:
        run = Run(
            task_id=task.id,
            experiment_id=self.experiment_id,
            executor_type=self.executor_type,
            provider=self.provider,
            model=self.model,
            harness_version=self.harness_version,
            context_policy_version=self.context_policy_version,
            dataset_version=self.dataset_version,
        )
        return self.run_repo.create(run)

    def _create_attempt(self, run: Run, n: int) -> Attempt:
        return self.attempt_repo.create(
            Attempt(run_id=run.id, attempt_number=n)
        )

    def _require_task(self, task_id: str) -> Task:
        task = self.task_repo.get(task_id)
        if task is None:
            raise KeyError(f"Task {task_id} not found")
        if task.status not in (TaskStatus.CREATED, TaskStatus.READY):
            raise ValueError(f"Task {task_id} is {task.status.value}; only CREATED/READY tasks can be executed")
        return task

    def _emit(
        self, event_type: EventType, *,
        task: Optional[Task] = None, run: Optional[Run] = None,
        attempt: Optional[Attempt] = None, payload: Optional[dict[str, Any]] = None,
    ) -> Event:
        event = self.event_store.append(
            event_type,
            task_id=task.id if task else None,
            run_id=run.id if run else None,
            attempt_id=attempt.id if attempt else None,
            payload=payload,
        )
        logger.info(
            "event=%s task=%s run=%s attempt=%s seq=%s payload=%s",
            event_type.value, event.task_id, event.run_id, event.attempt_id, event.id, json_dumps(payload or {}),
        )
        return event

    @staticmethod
    def _now():
        from datetime import datetime, timezone
        return datetime.now(timezone.utc)
