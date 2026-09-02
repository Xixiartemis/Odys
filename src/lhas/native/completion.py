"""Single completion-acceptance authority for native model claims."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from lhas.domain.enums import EventType, ExecutionStatus
from lhas.domain.models import utcnow
from lhas.executors.protocol import ExecutionResult
from lhas.native.models import (
    CandidateStatus,
    CompletionCandidate,
    ExecutionSnapshot,
    NativeFaultPoint,
    NoOpNativeFaultInjector,
    ReplanSignal,
    ValidationFailure,
)
from lhas.native.persistence import (
    CompletionCandidateRepository,
    ReplanSignalRepository,
    ValidationFailureRepository,
)
from lhas.persistence.event_store import EventStore
from lhas.persistence.repositories import AttemptRepository, TaskRepository
from lhas.validation import ValidationCheck, ValidationLevel, ValidationResult


def _bounded_evidence(value: Any, limit: int = 8_000) -> str:
    if isinstance(value, str):
        return value[:limit]
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)[:limit]


class CompletionAuthority:
    def __init__(self, *, db, validator, fault_injector: Any = None):
        self.db = db
        self.validator = validator
        self.fault_injector = fault_injector or NoOpNativeFaultInjector()
        self.candidates = CompletionCandidateRepository(db)
        self.failures = ValidationFailureRepository(db)
        self.replans = ReplanSignalRepository(db)
        self.events = EventStore(db)

    async def evaluate_claim(
        self,
        snapshot: ExecutionSnapshot,
        claim: str,
        *,
        source: str = "MODEL_CLAIM",
    ) -> CompletionCandidate:
        candidate = CompletionCandidate(
            task_id=snapshot.task_id,
            run_id=snapshot.run_id,
            attempt_id=snapshot.attempt_id,
            source=source,
            claim_sha256=hashlib.sha256(claim.encode("utf-8")).hexdigest(),
            summary=claim[:8_000],
        )
        self.candidates.create(candidate)
        self.events.append(
            EventType.COMPLETION_CANDIDATE_CREATED,
            task_id=snapshot.task_id,
            run_id=snapshot.run_id,
            attempt_id=snapshot.attempt_id,
            payload={"candidate_id": candidate.id, "source": source, "claim_sha256": candidate.claim_sha256},
        )
        self.fault_injector.hit(NativeFaultPoint.AFTER_CANDIDATE_PERSISTED, candidate=candidate)
        return await self._validate(candidate)

    async def resume_pending(self, attempt_id: str) -> CompletionCandidate | None:
        candidate = self.candidates.latest_for_attempt(attempt_id)
        if candidate is None:
            return None
        if candidate.status is CandidateStatus.CANDIDATE_COMPLETION:
            return await self._validate(candidate)
        return candidate

    async def _validate(self, candidate: CompletionCandidate) -> CompletionCandidate:
        task = TaskRepository(self.db).get(candidate.task_id)
        attempt = AttemptRepository(self.db).get(candidate.attempt_id)
        if task is None or attempt is None:
            raise KeyError("completion candidate references missing task or attempt")
        result = ExecutionResult(
            status=ExecutionStatus.SUCCESS,
            output=candidate.summary,
            raw={"completion_candidate_id": candidate.id, "source": candidate.source},
        )
        validation = await self.validator.validate(task=task, attempt=attempt, result=result)
        candidate.validation = {
            "passed": bool(validation.passed),
            "level": str(validation.level),
            "checks": [check.model_dump(mode="json") for check in validation.checks][:50],
            "evidence": _bounded_evidence(validation.evidence),
            "exit_code": validation.exit_code,
            "duration_ms": validation.duration_ms,
        }
        if validation.passed:
            candidate.status = CandidateStatus.ACCEPTED
            event = EventType.COMPLETION_CANDIDATE_ACCEPTED
        else:
            candidate.status = CandidateStatus.REJECTED
            event = EventType.COMPLETION_CANDIDATE_REJECTED
        self.candidates.update(candidate)
        self.events.append(event, task_id=candidate.task_id, run_id=candidate.run_id, attempt_id=candidate.attempt_id, payload={"candidate_id": candidate.id, "validation_passed": bool(validation.passed)})
        if not validation.passed:
            failed = next((check for check in validation.checks if not check.passed), None)
            failure = ValidationFailure(
                candidate_id=candidate.id,
                attempt_id=candidate.attempt_id,
                category="ACCEPTANCE_VALIDATION_FAILED",
                failed_criterion=(failed.name if failed else "completion acceptance")[:2_000],
                safe_evidence=_bounded_evidence(validation.evidence),
                recommended_recovery="REPAIR_AND_REVALIDATE",
            )
            self.failures.create(failure)
            self.events.append(EventType.VALIDATION_FAILURE_CREATED, task_id=candidate.task_id, run_id=candidate.run_id, attempt_id=candidate.attempt_id, payload={"validation_failure_id": failure.id, "candidate_id": candidate.id, "category": failure.category})
            signal = ReplanSignal(
                task_id=candidate.task_id,
                run_id=candidate.run_id,
                attempt_id=candidate.attempt_id,
                reason="VALIDATOR_REJECTION",
                scope="TASKGRAPH_NODE",
                evidence={"candidate_id": candidate.id, "failed_criterion": failure.failed_criterion},
            )
            self.replans.create(signal)
            self.events.append(EventType.REPLAN_SIGNAL_CREATED, task_id=candidate.task_id, run_id=candidate.run_id, attempt_id=candidate.attempt_id, payload={"signal_id": signal.id, "reason": signal.reason, "scope": signal.scope})
        self.fault_injector.hit(NativeFaultPoint.AFTER_CANDIDATE_VALIDATED, candidate=candidate)
        return candidate


class AcceptedCompletionValidator:
    """Outer validator projection: only a durable ACCEPTED candidate passes."""

    def __init__(self, db):
        self.candidates = CompletionCandidateRepository(db)

    async def validate(self, *, task, attempt, result) -> ValidationResult:
        candidate = self.candidates.latest_for_attempt(attempt.id)
        passed = bool(candidate and candidate.status is CandidateStatus.ACCEPTED)
        return ValidationResult(
            attempt_id=attempt.id,
            passed=passed,
            level=ValidationLevel.V2_RULE,
            checks=[ValidationCheck(
                name="native_completion_candidate_accepted",
                passed=passed,
                detail=None if passed else "no accepted native completion candidate",
            )],
            evidence=json.dumps({
                "candidate_id": candidate.id if candidate else None,
                "candidate_status": candidate.status.value if candidate else None,
                "acceptance_authority": "CompletionAuthority",
                "exit_code": candidate.validation.get("exit_code") if candidate else None,
            }, sort_keys=True),
            exit_code=(candidate.validation.get("exit_code") if candidate else None),
            duration_ms=0,
        )
