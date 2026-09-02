"""Deterministic regressions for Phase 1 evidence integrity."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from lhas.cli_runtime import encode_cli_config, inspect_run
from lhas.command_validation import ExplicitCommandValidator
from lhas.domain.enums import AttemptStatus, ExecutionStatus, FailureClass, FailureType, RunStatus
from lhas.domain.models import Attempt, Project, Run
from lhas.executors.protocol import ExecutionRequest, ExecutionResult
from lhas.failure import RuleFailureClassifier
from lhas.native.completion import AcceptedCompletionValidator, CompletionAuthority
from lhas.native.executor import NativeAgentExecutor
from lhas.native.kernel import NativeAgentKernel
from lhas.native.models import ExecutionSnapshot, ProviderResponse, ProviderToolCall
from lhas.native.tools import NativeToolDispatcher
from lhas.native.provider import ScriptedProviderAdapter
from lhas.orchestrator_v2 import RecoveringOrchestrator
from lhas.persistence.database import Database
from lhas.persistence.phaseb_repos import FailureReportRepository, RecoveryActionRepository, ValidationResultRepository
from lhas.persistence.repositories import AttemptRepository, ProjectRepository, RunRepository
from lhas.recovery import DefaultRecoveryPolicy
from lhas.task_service import create_task
from lhas.tools.registry import ToolRegistry
from lhas.validation import NeverPassValidator, ValidationCheck, ValidationResult


def _validation_case(tmp_path, argv):
    source = tmp_path / "repo"
    source.mkdir()
    (source / "marker.txt").write_text("ok", encoding="utf-8")
    db_path = tmp_path / "runtime.db"
    db = Database(db_path)
    db.init_db()
    project = ProjectRepository(db).create(Project(name="validator", root_path=str(source)))
    task = create_task(
        db,
        project_id=project.id,
        title="validate",
        objective="validate",
        constraints=[encode_cli_config(verify_argv=list(argv), max_turns=1, provider="offline", model="deterministic")],
    )
    run = RunRepository(db).create(Run(task_id=task.id, status=RunStatus.RUNNING))
    attempt = AttemptRepository(db).create(Attempt(run_id=run.id, attempt_number=1))
    from lhas.workspace import RunWorkspaceManager

    manager = RunWorkspaceManager(db, tmp_path / "sessions", source_root=source)
    manager.create_for_run(task, run)
    return db, db_path, task, run, attempt, manager


def _run_validator(tmp_path, argv):
    db, db_path, task, run, attempt, manager = _validation_case(tmp_path, argv)
    result = asyncio.run(ExplicitCommandValidator(db, manager, argv).validate(task=task, attempt=attempt, result=None))
    ValidationResultRepository(db).create(result)
    db.close()
    reopened = Database(db_path)
    reopened.init_db()
    return reopened, task.id, run.id, result


def test_validator_pass_exit_code_is_durable_and_inspectable(tmp_path):
    argv = [sys.executable, "-c", "raise SystemExit(0)"]
    db, task_id, run_id, result = _run_validator(tmp_path, argv)

    persisted = ValidationResultRepository(db).get_for_attempt(result.attempt_id)
    projection = inspect_run(db, run_id)
    assert result.exit_code == 0
    assert persisted is not None and persisted.exit_code == 0
    assert projection["validation"]["exit_code"] == 0
    db.close()


def test_validator_nonzero_exit_code_is_preserved_exactly(tmp_path):
    argv = [sys.executable, "-c", "raise SystemExit(23)"]
    db, _task_id, run_id, result = _run_validator(tmp_path, argv)

    persisted = ValidationResultRepository(db).get_for_attempt(result.attempt_id)
    assert result.passed is False and result.exit_code == 23
    assert persisted is not None and persisted.exit_code == 23
    assert inspect_run(db, run_id)["validation"]["exit_code"] == 23
    db.close()


def test_validator_without_process_result_does_not_infer_exit_code(tmp_path):
    argv = [str(tmp_path / "does-not-exist-validator")]
    db, _task_id, _run_id, result = _run_validator(tmp_path, argv)

    persisted = ValidationResultRepository(db).get_for_attempt(result.attempt_id)
    assert result.passed is False and result.exit_code is None
    assert persisted is not None and persisted.exit_code is None
    assert json.loads(result.evidence)["exit_code"] is None
    db.close()


class ExitCodeValidator:
    async def validate(self, *, task, attempt, result):
        return ValidationResult(
            attempt_id=attempt.id,
            passed=False,
            checks=[ValidationCheck(name="exit", passed=False)],
            evidence="deterministic validator result",
            exit_code=17,
        )


def test_completion_authority_preserves_validator_exit_code_in_candidate_evidence(db, make_task):
    task = make_task()
    run = RunRepository(db).create(Run(task_id=task.id, status=RunStatus.RUNNING))
    attempt = AttemptRepository(db).create(Attempt(run_id=run.id, attempt_number=1))
    authority = CompletionAuthority(db=db, validator=ExitCodeValidator())
    snapshot = ExecutionSnapshot(task_id=task.id, run_id=run.id, attempt_id=attempt.id, goal=task.objective)

    candidate = asyncio.run(authority.evaluate_claim(snapshot, "candidate"))
    projected = asyncio.run(AcceptedCompletionValidator(db).validate(task=task, attempt=attempt, result=None))
    assert candidate.validation["exit_code"] == 17
    assert projected.exit_code == 17


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
        dispatcher=NativeToolDispatcher(db=db, registry=registry, allowed_capabilities=set(), allowed_side_effect_capabilities=set()),
        completion_authority=CompletionAuthority(db=db, validator=NeverPassValidator()),
    )
    executor = NativeAgentExecutor(kernel, allowed_capabilities=set(), allowed_side_effect_capabilities=set(), max_turns=1, max_tool_calls=1)
    request = ExecutionRequest(
        task_id=task.id,
        run_id=run.id,
        attempt_id=attempt.id,
        attempt_number=1,
        task={"objective": task.objective, "acceptance_criteria": []},
    )

    result = asyncio.run(executor.execute(request))
    assert result.status is ExecutionStatus.FAILURE
    assert result.error_type == "BUDGET_EXHAUSTED"


class BudgetExhaustedExecutor:
    name = "NativeAgentExecutor"

    async def execute(self, request):
        return ExecutionResult(
            status=ExecutionStatus.FAILURE,
            error_type="BUDGET_EXHAUSTED",
            error_message="TURN_BUDGET exhausted",
        )


def test_budget_exhaustion_lineage_reaches_attempt_report_and_recovery(db, make_task):
    task = make_task(max_attempts=2)
    orchestrator = RecoveringOrchestrator(
        db,
        executor_factory=BudgetExhaustedExecutor,
        validator=NeverPassValidator(),
        classifier=RuleFailureClassifier(),
        recovery_policy=DefaultRecoveryPolicy(),
    )

    run = asyncio.run(orchestrator.execute_task(task.id))
    attempt = AttemptRepository(db).list_for_run(run.id)[0]
    report = FailureReportRepository(db).get_for_attempt(attempt.id)
    action = RecoveryActionRepository(db).get_for_attempt(attempt.id)
    persisted_result = json.loads(attempt.executor_result)

    assert attempt.error_type == "BUDGET_EXHAUSTED"
    assert persisted_result["error_type"] == "BUDGET_EXHAUSTED"
    assert report is not None and report.failure_type is FailureType.BUDGET_EXHAUSTED
    assert report.failure_class is FailureClass.EXECUTION
    assert action is not None and action.added_context["failure_type"] == "BUDGET_EXHAUSTED"


def test_genuine_empty_result_remains_empty_result():
    attempt = Attempt(run_id="run", attempt_number=1, status=AttemptStatus.COMPLETED)
    result = ExecutionResult(status=ExecutionStatus.SUCCESS, output=None)
    validation = ValidationResult(
        attempt_id=attempt.id,
        passed=False,
        checks=[ValidationCheck(name="output", passed=False)],
        evidence="no output",
    )

    report = asyncio.run(RuleFailureClassifier().classify(
        task=type("TaskLike", (), {})(), attempt=attempt, result=result, validation=validation,
    ))
    assert report.failure_type is FailureType.EMPTY_RESULT


@pytest.mark.parametrize(
    ("error_type", "expected"),
    [
        ("TOOL_ERROR", FailureType.TOOL_ERROR),
        ("MALFORMED_PROVIDER_RESPONSE", FailureType.MALFORMED_PROVIDER_RESPONSE),
        ("PROVIDER_UNAVAILABLE", FailureType.PROVIDER_UNAVAILABLE),
    ],
)
def test_other_failure_classifications_do_not_regress(error_type, expected):
    attempt = Attempt(run_id="run", attempt_number=1, status=AttemptStatus.FAILED, error_type=error_type)
    result = ExecutionResult(status=ExecutionStatus.FAILURE, error_type=error_type, error_message=error_type)
    report = asyncio.run(RuleFailureClassifier().classify(
        task=type("TaskLike", (), {})(), attempt=attempt, result=result, validation=None,
    ))
    assert report.failure_type is expected
