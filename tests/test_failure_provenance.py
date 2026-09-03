"""Deterministic reproduction for the native budget provenance defect."""

from __future__ import annotations

import asyncio
import json
import pytest

from lhas.domain.enums import AttemptStatus, ExecutionStatus, FailureClass, RecoveryActionType, RunStatus
from lhas.domain.models import Attempt, Run, Task
from lhas.executors.protocol import ExecutionRequest, ExecutionResult
from lhas.failure import FailureReport, RuleFailureClassifier
from lhas.native.completion import CompletionAuthority
from lhas.native.executor import NativeAgentExecutor
from lhas.native.kernel import NativeAgentKernel
from lhas.native.models import ProviderResponse, ProviderToolCall
from lhas.native.provider import ScriptedProviderAdapter
from lhas.native.tools import NativeToolDispatcher
from lhas.orchestrator_v2 import RecoveringOrchestrator
from lhas.persistence.phaseb_repos import FailureReportRepository, RecoveryActionRepository
from lhas.persistence.repositories import AttemptRepository, RunRepository
from lhas.recovery import DefaultRecoveryPolicy
from lhas.tools.registry import ToolRegistry
from lhas.validation import ValidationCheck, ValidationResult


def test_native_budget_exhaustion_must_not_become_empty_result():
    attempt = Attempt(
        run_id="run",
        attempt_number=1,
        status=AttemptStatus.FAILED,
        error_type="BUDGET_EXHAUSTED",
        error_message="TURN_BUDGET exhausted",
    )
    result = ExecutionResult(
        status=ExecutionStatus.FAILURE,
        error_type="BUDGET_EXHAUSTED",
        error_message="TURN_BUDGET exhausted",
        output=None,
    )
    validation = ValidationResult(
        attempt_id=attempt.id,
        passed=False,
        checks=[ValidationCheck(name="completion", passed=False, detail="no accepted completion")],
        evidence="authoritative validation failed",
    )

    report = asyncio.run(RuleFailureClassifier().classify(
        task=Task(project_id="project", title="task", objective="objective"),
        attempt=attempt,
        result=result,
        validation=validation,
    ))

    assert report.failure_type.value == "BUDGET_EXHAUSTED"


def test_genuine_empty_output_remains_empty_result():
    attempt = Attempt(run_id="run", attempt_number=1, status=AttemptStatus.COMPLETED)
    report = asyncio.run(RuleFailureClassifier().classify(
        task=Task(project_id="project", title="task", objective="objective"),
        attempt=attempt,
        result=ExecutionResult(status=ExecutionStatus.SUCCESS, output=None),
        validation=ValidationResult(
            attempt_id=attempt.id,
            passed=False,
            checks=[ValidationCheck(name="output", passed=False)],
            evidence="authoritative validation failed",
        ),
    ))
    assert report.failure_type.value == "EMPTY_RESULT"


@pytest.mark.parametrize(
    ("error_type", "expected"),
    [
        ("PROVIDER_TIMEOUT", "PROVIDER_TIMEOUT"),
        ("PROVIDER_UNAVAILABLE", "PROVIDER_UNAVAILABLE"),
        ("MALFORMED_PROVIDER_RESPONSE", "MALFORMED_PROVIDER_RESPONSE"),
        ("UNKNOWN_PROVIDER_FAILURE", "UNKNOWN_PROVIDER_FAILURE"),
    ],
)
def test_provider_failure_classifications_remain_unchanged(error_type, expected):
    attempt = Attempt(
        run_id="run", attempt_number=1, status=AttemptStatus.FAILED,
        error_type=error_type, error_message=error_type,
    )
    report = asyncio.run(RuleFailureClassifier().classify(
        task=Task(project_id="project", title="task", objective="objective"),
        attempt=attempt,
        result=ExecutionResult(status=ExecutionStatus.FAILURE, error_type=error_type, error_message=error_type),
        validation=None,
    ))
    assert report.failure_type.value == expected


def test_tool_error_remains_tool_error():
    attempt = Attempt(
        run_id="run", attempt_number=1, status=AttemptStatus.FAILED,
        error_type="TOOL_ERROR", error_message="TOOL_ERROR",
    )
    report = asyncio.run(RuleFailureClassifier().classify(
        task=Task(project_id="project", title="task", objective="objective"),
        attempt=attempt,
        result=ExecutionResult(status=ExecutionStatus.FAILURE, error_type="TOOL_ERROR", error_message="TOOL_ERROR"),
        validation=None,
    ))
    assert report.failure_type.value == "TOOL_ERROR"


def test_delegation_budget_is_not_matched_as_native_budget():
    attempt = Attempt(
        run_id="run", attempt_number=1, status=AttemptStatus.FAILED,
        error_type="DELEGATION_BUDGET_EXHAUSTED", error_message="DELEGATION_BUDGET_EXHAUSTED",
    )
    report = asyncio.run(RuleFailureClassifier().classify(
        task=Task(project_id="project", title="task", objective="objective"),
        attempt=attempt,
        result=ExecutionResult(
            status=ExecutionStatus.FAILURE,
            error_type="DELEGATION_BUDGET_EXHAUSTED",
            error_message="DELEGATION_BUDGET_EXHAUSTED",
        ),
        validation=None,
    ))
    assert report.failure_type.value == "UNKNOWN"


def test_recovery_policy_receives_corrected_budget_report():
    task = Task(project_id="project", title="task", objective="objective", max_attempts=2)
    attempt = Attempt(run_id="run", attempt_number=1, status=AttemptStatus.FAILED, error_type="BUDGET_EXHAUSTED")
    report = FailureReport(
        attempt_id=attempt.id,
        failure_type="BUDGET_EXHAUSTED",
        failure_class=FailureClass.EXECUTION,
        evidence="BUDGET_EXHAUSTED",
        summary="native executor budget exhausted",
        confidence=1.0,
        suggested_recovery="retry",
    )
    action = asyncio.run(DefaultRecoveryPolicy().decide(
        task=task, attempt=attempt, failure_report=report,
        attempt_number=1, max_attempts=2, history=[],
    ))
    assert action.action_type is RecoveryActionType.RETRY_WITH_FAILURE_CONTEXT
    assert action.added_context["failure_type"] == "BUDGET_EXHAUSTED"


def test_native_executor_forwards_budget_terminal_signal(db, make_task):
    task = make_task()
    run = RunRepository(db).create(Run(task_id=task.id, status=RunStatus.RUNNING))
    attempt = AttemptRepository(db).create(Attempt(run_id=run.id, attempt_number=1, status=AttemptStatus.RUNNING))
    registry = ToolRegistry()
    kernel = NativeAgentKernel(
        db=db,
        provider=ScriptedProviderAdapter([
            ProviderResponse(tool_calls=[ProviderToolCall(id="missing", name="missing.tool", arguments={})]),
        ]),
        dispatcher=NativeToolDispatcher(
            db=db, registry=registry, allowed_capabilities=set(), allowed_side_effect_capabilities=set(),
        ),
        completion_authority=CompletionAuthority(db=db, validator=FailingValidator()),
    )
    executor = NativeAgentExecutor(
        kernel, allowed_capabilities=set(), allowed_side_effect_capabilities=set(), max_turns=1, max_tool_calls=1,
    )
    result = asyncio.run(executor.execute(ExecutionRequest(
        task_id=task.id,
        run_id=run.id,
        attempt_id=attempt.id,
        attempt_number=1,
        task={"objective": task.objective, "acceptance_criteria": []},
    )))
    assert result.error_type == "BUDGET_EXHAUSTED"


class BudgetExhaustedExecutor:
    name = "NativeAgentExecutor"

    async def execute(self, request):
        return ExecutionResult(
            status=ExecutionStatus.FAILURE,
            error_type="BUDGET_EXHAUSTED",
            error_message="TURN_BUDGET exhausted",
        )


def test_attempt_failure_report_and_recovery_preserve_budget_lineage(db, make_task):
    task = make_task(max_attempts=2)
    orchestrator = RecoveringOrchestrator(
        db,
        executor_factory=BudgetExhaustedExecutor,
        validator=FailingValidator(),
        classifier=RuleFailureClassifier(),
        recovery_policy=DefaultRecoveryPolicy(),
    )

    run = asyncio.run(orchestrator.execute_task(task.id))
    attempt = AttemptRepository(db).list_for_run(run.id)[0]
    report = FailureReportRepository(db).get_for_attempt(attempt.id)
    action = RecoveryActionRepository(db).get_for_attempt(attempt.id)
    assert json.loads(attempt.executor_result)["error_type"] == "BUDGET_EXHAUSTED"
    assert report is not None and report.failure_type.value == "BUDGET_EXHAUSTED"
    assert action is not None and action.added_context["failure_type"] == "BUDGET_EXHAUSTED"


def _failed_validation(attempt_id):
    return ValidationResult(
        attempt_id=attempt_id,
        passed=False,
        checks=[ValidationCheck(name="completion", passed=False)],
        evidence="authoritative validation failed",
    )


class FailingValidator:
    async def validate(self, *, task, attempt, result):
        return _failed_validation(attempt.id)
