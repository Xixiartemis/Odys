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
from lhas.persistence.phaseb_repos import ValidationResultRepository
from lhas.persistence.repositories import AttemptRepository, TaskRepository
from lhas.validation import ValidationCheck, ValidationLevel, ValidationResult


def _bounded_evidence(value: Any, limit: int = 8_000) -> str:
    if isinstance(value, str):
        return value[:limit]
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)[:limit]


def _structured_validation_evidence(validation: ValidationResult) -> dict[str, Any] | None:
    if not isinstance(validation.evidence, str):
        return None
    try:
        evidence = json.loads(validation.evidence)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return evidence if isinstance(evidence, dict) else None


def _has_authoritative_process_result(validation: ValidationResult) -> bool:
    evidence = _structured_validation_evidence(validation)
    exit_code = evidence.get("exit_code") if evidence is not None else None
    command = evidence.get("command") if evidence is not None else None
    return (
        validation.passed
        and type(exit_code) is int
        and exit_code == 0
        and evidence.get("timed_out") is False
        and isinstance(command, list)
        and bool(command)
        and all(isinstance(item, str) and item for item in command)
    )


class CompletionAuthority:
    def __init__(self, *, db, validator, fault_injector: Any = None):
        self.db = db
        self.validator = validator
        self.fault_injector = fault_injector or NoOpNativeFaultInjector()
        self.candidates = CompletionCandidateRepository(db)
        self.validations = ValidationResultRepository(db)
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
        self.validations.create(validation)
        evidence = _structured_validation_evidence(validation)
        validator_command = evidence.get("command") if evidence is not None else None
        candidate.validation = {
            "attempt_id": candidate.attempt_id,
            "run_id": candidate.run_id,
            "validation_result_id": validation.id,
            "passed": bool(validation.passed),
            "level": str(validation.level),
            "checks": [check.model_dump(mode="json") for check in validation.checks][:50],
            "evidence": _bounded_evidence(validation.evidence),
            "duration_ms": validation.duration_ms,
        }
        if isinstance(validator_command, list):
            candidate.validation["validator_command"] = validator_command
        if _has_authoritative_process_result(validation) and validation.attempt_id == attempt.id:
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
    """Outer validator projection requiring the candidate's durable process result."""

    def __init__(self, db):
        self.candidates = CompletionCandidateRepository(db)
        self.validations = ValidationResultRepository(db)

    async def validate(self, *, task, attempt, result) -> ValidationResult:
        candidate = self.candidates.latest_for_attempt(attempt.id)
        validation = None
        validation_id = candidate.validation.get("validation_result_id") if candidate else None
        if candidate and isinstance(validation_id, str):
            validation = next(
                (item for item in self.validations.list_for_attempt(attempt.id) if item.id == validation_id),
                None,
            )
        evidence = _structured_validation_evidence(validation) if validation else None
        command_matches = (
            evidence is not None
            and (
                "validator_command" not in candidate.validation
                or evidence.get("command") == candidate.validation.get("validator_command")
            )
        ) if candidate else False
        passed = bool(
            candidate
            and candidate.status is CandidateStatus.ACCEPTED
            and candidate.task_id == task.id
            and candidate.run_id == attempt.run_id
            and candidate.attempt_id == attempt.id
            and candidate.validation.get("run_id") == attempt.run_id
            and candidate.validation.get("attempt_id") == attempt.id
            and validation is not None
            and validation.attempt_id == attempt.id
            and _has_authoritative_process_result(validation)
            and command_matches
        )
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
                "validation_result_id": validation.id if validation else None,
                "validator_evidence": evidence if passed else None,
            }, sort_keys=True),
            duration_ms=0,
        )
