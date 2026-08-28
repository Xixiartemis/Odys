"""Offline contract tests for the HV-1.3 long-task validation harness."""

import json
from pathlib import Path

import pytest

from lhas.domain.enums import AttemptStatus, EventType, ExecutionStatus, RunStatus
from lhas.domain.models import Attempt, Project, Run
from lhas.executors.protocol import ExecutionResult
from lhas.persistence.event_store import EventStore
from lhas.persistence.phaseb_repos import ValidationResultRepository
from lhas.persistence.repositories import (
    AttemptRepository,
    ProjectRepository,
    RunRepository,
)
from lhas.task_service import create_task
from lhas.validation import ValidationCheck, ValidationResult
from scripts import hv12_longtask_recovery as hv12
from scripts import hv13_longtask_recovery as hv13


def _dry_result(tmp_path: Path) -> dict:
    return hv13.run_evaluation(
        output_path=tmp_path / "HV13-DRY-test.json",
        observation_timeout=15,
        poll_interval=0.1,
        resume_timeout=30,
    )


@pytest.fixture(scope="module")
def dry_result(tmp_path_factory) -> dict:
    return _dry_result(tmp_path_factory.mktemp("hv13-dry"))


def test_hv13_dry_process_recovery_exercises_failed_attempt_arbitration(dry_result):
    result = dry_result

    assert result["status"] == "PASS"
    assert result["long_horizon_result"] == "PASS"
    assert result["evaluation_id"] == "HV13-DRY-001"
    assert result["phase"] == "HV13_LONGTASK_VALIDATION"
    assert result["experiment_id"] == "HV13-LONGTASK-VALIDATION"
    assert result["harness_version"] == "HV-1.3"
    assert result["comparison_baseline_id"] == "HV12-LIVE-001"
    assert result["comparison_baseline_harness"] == "HV-1.2"
    assert result["initial_tests"]["status"] == "FAIL"
    assert result["final_tests"]["status"] == "PASS"
    assert result["crash"]["trigger"] == "DURABLE_MUTATION_STABLE"
    assert result["crash"]["forced_process_termination"] is True
    assert result["fresh_process_b_created"] is True
    assert result["resume_run_invoked"] is True


def test_hv13_attempt2_is_failed_turn_limit_after_resume(dry_result):
    result = dry_result

    attempts = result["attempts"]
    assert result["attempt_count"] == 2
    assert attempts[0]["status"] == "CRASHED"
    assert attempts[0]["error_type"] == "PROCESS_INTERRUPTED"
    assert attempts[1]["status"] == "FAILED"
    assert attempts[1]["error_type"] == "AGENT_TURN_LIMIT"
    assert result["attempt_3_exists"] is False


def test_hv13_attempt2_arbitration_is_durable_and_passes(dry_result):
    result = dry_result

    assert result["outcome_arbitration_observed"] is True
    assert result["outcome_arbitration_attempt_number"] == 2
    assert result["outcome_arbitration_executor_status"] == "FAILED"
    assert result["outcome_arbitration_error_type"] == "AGENT_TURN_LIMIT"
    assert result["outcome_arbitration_validation_passed"] is True
    assert result["next_attempt_suppressed_after_arbitration"] is True
    assert result["attempts_after_arbitration"] == 0
    assert result["outcome_arbitration_event_count"] == 1
    assert result["outcome_arbitration_events_truncated"] is False
    assert result["outcome_arbitration_events"] == [
        {
            "attempt_number": 2,
            "executor_status": "FAILED",
            "error_type": "AGENT_TURN_LIMIT",
            "validation_passed": True,
            "next_attempt_exists": False,
        }
    ]


def test_hv13_failed_attempt_remains_failed_after_task_completion(dry_result):
    result = dry_result

    attempt2 = result["attempts"][1]
    assert attempt2["status"] == "FAILED"
    assert attempt2["error_type"] == "AGENT_TURN_LIMIT"
    assert result["outer_task_completed"] is True
    assert result["agent_completion_passed"] is False
    assert result["functional_validation_passed"] is True
    assert result["process_recovery_passed"] is True
    assert result["process_recovery_mechanics_passed"] is True


def test_hv13_reuses_same_durable_workspace_and_cp3_once(dry_result):
    result = dry_result

    assert result["same_workspace_session"] is True
    assert result["durable_workspace_session_reused"] is True
    assert result["outer_harness_metrics"]["attempts_before_resume"] == 1
    assert result["outer_harness_metrics"]["new_attempts_after_resume"] == 1
    assert result["outer_harness_metrics"]["executor_calls_after_resume"] == [2]
    assert result["outer_harness_metrics"]["validator_calls_after_resume"] == 2
    assert result["outer_harness_metrics"]["cp3_attempts"] == 1


def test_hv13_duplicate_durable_records_are_zero(dry_result):
    result = dry_result

    assert result["duplicate_durable_rows"] == {
        "validations": 0,
        "failure_reports": 0,
        "recovery_actions": 0,
        "checkpoints": 0,
    }
    metrics = result["outer_harness_metrics"]
    assert metrics["duplicate_validations"] == 0
    assert metrics["duplicate_failure_reports"] == 0
    assert metrics["duplicate_recovery_actions"] == 0
    assert metrics["duplicate_checkpoints"] == 0


def test_hv13_source_and_fixture_are_unchanged(dry_result):
    result = dry_result

    assert result["source_repository_unchanged"] is True
    assert result["repository_fixture_unchanged"] is True
    assert result["temporary_source_snapshot_unchanged"] is True
    assert result["final_patch_nonempty"] is True
    assert result["evidence_safety"]["precrash_trace_incomplete"] is True
    required_metric_fields = {
        "attempt_number",
        "attempt_status",
        "inner_agent_status",
        "error_type",
        "termination_status",
        "turn_count",
        "tool_call_count",
        "tool_calls_by_capability",
        "tool_failure_count",
        "tool_failures_by_type",
        "tool_failures_by_capability",
        "duration_ms",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "completion_claim_present",
        "context_policy",
        "checkpoint_used",
        "checkpoint_created",
        "pre_crash_tool_trace_complete",
    }
    assert all(required_metric_fields <= set(metric) for metric in result["attempt_metrics"])
    assert result["attempt_metrics"][0]["turn_count"] is None
    assert result["attempt_metrics"][0]["tool_call_count"] is None


def _create_persisted_attempt(
    db,
    *,
    status: AttemptStatus,
    execution_status: ExecutionStatus,
    completion_claim: bool | None,
    event_type: EventType,
):
    project = ProjectRepository(db).create(Project(name="hv13-metrics"))
    task = create_task(db, project_id=project.id, title="metrics", objective="measure")
    run = RunRepository(db).create(
        Run(task_id=task.id, experiment_id="HV13-LONGTASK-VALIDATION", status=RunStatus.RUNNING)
    )
    raw = {} if completion_claim is None else {"completion_claim": completion_claim}
    result = ExecutionResult(
        status=execution_status,
        error_type="AGENT_TURN_LIMIT" if execution_status is ExecutionStatus.FAILURE else None,
        raw=raw,
    )
    attempt = AttemptRepository(db).create(
        Attempt(
            run_id=run.id,
            attempt_number=1,
            status=status,
            error_type=result.error_type,
            executor_result=json.dumps(result.model_dump(mode="json")),
            duration_ms=12,
        )
    )
    EventStore(db).append(
        event_type,
        task_id=task.id,
        run_id=run.id,
        attempt_id=attempt.id,
        payload={"turn_count": 4, "tool_call_count": 3},
    )
    return run


def test_completion_claim_true_derives_agent_completion_from_durable_metrics(db):
    run = _create_persisted_attempt(
        db,
        status=AttemptStatus.COMPLETED,
        execution_status=ExecutionStatus.SUCCESS,
        completion_claim=True,
        event_type=EventType.INNER_AGENT_COMPLETED,
    )

    metrics = hv13._safe_attempt_metrics(db, run.id)

    assert metrics[0]["completion_claim_present"] is True
    assert metrics[0]["inner_agent_status"] == "SUCCESS"
    assert metrics[0]["turn_count"] == 4
    assert metrics[0]["tool_call_count"] == 3
    assert hv13._agent_completion_from_metrics(metrics) is True


def test_failure_without_completion_claim_remains_not_agent_complete(db):
    run = _create_persisted_attempt(
        db,
        status=AttemptStatus.FAILED,
        execution_status=ExecutionStatus.FAILURE,
        completion_claim=False,
        event_type=EventType.INNER_AGENT_FAILED,
    )

    metrics = hv13._safe_attempt_metrics(db, run.id)

    assert metrics[0]["completion_claim_present"] is False
    assert metrics[0]["inner_agent_status"] == "FAILURE"
    assert metrics[0]["termination_status"] == "TURN_LIMIT"
    assert hv13._agent_completion_from_metrics(metrics) is False


def _create_arbitration_attempt(db, *, run: Run, task_id: str, number: int, passed: bool):
    attempt = AttemptRepository(db).create(
        Attempt(
            run_id=run.id,
            attempt_number=number,
            status=AttemptStatus.FAILED,
            error_type="AGENT_TURN_LIMIT",
        )
    )
    EventStore(db).append(
        EventType.VALIDATION_STARTED,
        task_id=task_id,
        run_id=run.id,
        attempt_id=attempt.id,
        payload={
            "outcome_arbitration": True,
            "executor_attempt_status": "FAILED",
            "executor_error_type": "AGENT_TURN_LIMIT",
        },
    )
    ValidationResultRepository(db).create(
        ValidationResult(
            attempt_id=attempt.id,
            passed=passed,
            checks=[ValidationCheck(name="fixture", passed=passed)],
            evidence=f"passed={passed}",
        )
    )
    return attempt


def test_multiple_arbitrations_preserve_history_and_summarize_latest(db):
    project = ProjectRepository(db).create(Project(name="hv13-arbitration"))
    task = create_task(db, project_id=project.id, title="arbitration", objective="repair")
    run = RunRepository(db).create(
        Run(task_id=task.id, experiment_id="HV13-LONGTASK-VALIDATION", status=RunStatus.RUNNING)
    )
    _create_arbitration_attempt(db, run=run, task_id=task.id, number=2, passed=False)
    _create_arbitration_attempt(db, run=run, task_id=task.id, number=3, passed=True)

    summary = hv13._arbitration_observation(db, run.id)

    assert summary["outcome_arbitration_event_count"] == 2
    assert summary["outcome_arbitration_events"] == [
        {
            "attempt_number": 2,
            "executor_status": "FAILED",
            "error_type": "AGENT_TURN_LIMIT",
            "validation_passed": False,
            "next_attempt_exists": True,
        },
        {
            "attempt_number": 3,
            "executor_status": "FAILED",
            "error_type": "AGENT_TURN_LIMIT",
            "validation_passed": True,
            "next_attempt_exists": False,
        },
    ]
    assert summary["outcome_arbitration_attempt_number"] == 3
    assert summary["outcome_arbitration_validation_passed"] is True
    assert summary["next_attempt_suppressed_after_arbitration"] is True
    assert summary["attempts_after_arbitration"] == 0


def test_arbitration_history_is_bounded(db):
    project = ProjectRepository(db).create(Project(name="hv13-arbitration-bound"))
    task = create_task(db, project_id=project.id, title="bounded", objective="repair")
    run = RunRepository(db).create(
        Run(task_id=task.id, experiment_id="HV13-LONGTASK-VALIDATION", status=RunStatus.RUNNING)
    )
    attempt = _create_arbitration_attempt(
        db, run=run, task_id=task.id, number=1, passed=True
    )
    for _ in range(hv13.MAX_ARBITRATION_EVENTS + 4):
        EventStore(db).append(
            EventType.VALIDATION_STARTED,
            task_id=task.id,
            run_id=run.id,
            attempt_id=attempt.id,
            payload={
                "outcome_arbitration": True,
                "executor_attempt_status": "FAILED",
                "executor_error_type": "AGENT_TURN_LIMIT",
            },
        )

    summary = hv13._arbitration_observation(db, run.id)

    assert summary["outcome_arbitration_event_count"] == hv13.MAX_ARBITRATION_EVENTS + 5
    assert len(summary["outcome_arbitration_events"]) == hv13.MAX_ARBITRATION_EVENTS
    assert summary["outcome_arbitration_events_truncated"] is True


def test_process_recovery_preserves_historical_outer_completion_semantics():
    common = {
        "crash_trigger": "DURABLE_MUTATION_STABLE",
        "forced": True,
        "process_b_started": True,
        "process_b_exit": 0,
        "same_workspace": True,
        "final_pytest_passed": True,
        "repository_fixture_unchanged": True,
        "temporary_source_snapshot_unchanged": True,
        "duplicate_rows_zero": True,
    }

    historical, mechanics = hv13._process_recovery_metrics(
        **common, outer_task_completed=False
    )
    assert historical is False
    assert mechanics is True

    historical, mechanics = hv13._process_recovery_metrics(
        **common, outer_task_completed=True
    )
    assert historical is True
    assert mechanics is True


def test_hv12_historical_main_refuses_harness_mismatch(capsys):
    assert hv12.main([]) == 2
    assert "STATUS=HISTORICAL_HARNESS_MISMATCH" in capsys.readouterr().out


def test_hv13_live_claim_is_atomic_and_refuses_second_run(monkeypatch, tmp_path):
    monkeypatch.setenv("ODYS_AGENT_MODEL", "mimo-test-model")
    monkeypatch.setenv("ODYS_AGENT_API_KEY", "secret-must-not-persist")
    output = tmp_path / "HV13-LIVE-001.json"
    claim = tmp_path / "HV13-LIVE-001.claim.json"
    identity_calls = 0

    def clean_identity():
        nonlocal identity_calls
        identity_calls += 1
        return "clean-pre-claim-sha", True

    def fail_before_worker(*args, **kwargs):
        raise RuntimeError("test stops before any model worker starts")

    monkeypatch.setattr(hv13, "_git_identity", clean_identity)
    monkeypatch.setattr(hv13, "_spawn_worker", fail_before_worker)
    with pytest.raises(RuntimeError, match="test stops"):
        hv13.run_evaluation(mode="live_real_model", output_path=output, claim_path=claim)

    claim_data = json.loads(claim.read_text(encoding="utf-8"))
    assert claim_data == {
        "evaluation_id": "HV13-LIVE-001",
        "git_sha": "clean-pre-claim-sha",
        "code_commit_clean": True,
        "harness_version": "HV-1.3",
        "execution_claimed": True,
    }
    assert identity_calls == 1
    assert "secret-must-not-persist" not in claim.read_text(encoding="utf-8")
    refused = hv13.run_evaluation(mode="live_real_model", output_path=output, claim_path=claim)
    assert refused == {
        "status": "LIVE_CLAIM_EXISTS",
        "mode": "live_real_model",
        "live_run_executed": False,
    }


def test_hv13_live_existing_result_refuses_before_config(monkeypatch, tmp_path):
    monkeypatch.delenv("ODYS_AGENT_MODEL", raising=False)
    monkeypatch.delenv("ODYS_AGENT_API_KEY", raising=False)
    output = tmp_path / "HV13-LIVE-001.json"
    output.write_text("{}\n", encoding="utf-8")

    result = hv13.run_evaluation(mode="live_real_model", output_path=output)

    assert result == {
        "status": "LIVE_RESULT_EXISTS",
        "mode": "live_real_model",
        "live_run_executed": False,
    }


def test_hv13_missing_live_config_does_not_consume_claim(monkeypatch, tmp_path):
    monkeypatch.delenv("ODYS_AGENT_MODEL", raising=False)
    monkeypatch.delenv("ODYS_AGENT_API_KEY", raising=False)
    output = tmp_path / "HV13-LIVE-001.json"
    claim = tmp_path / "HV13-LIVE-001.claim.json"

    result = hv13.run_evaluation(mode="live_real_model", output_path=output, claim_path=claim)

    assert result == {
        "status": "SKIPPED_CONFIG",
        "mode": "live_real_model",
        "live_run_executed": False,
    }
    assert not claim.exists()
    assert not output.exists()
