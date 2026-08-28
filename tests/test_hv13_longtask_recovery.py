"""Offline contract tests for the HV-1.3 long-task validation harness."""

import json
from pathlib import Path

import pytest

from scripts import hv12_longtask_recovery as hv12
from scripts import hv13_longtask_recovery as hv13


def _dry_result(tmp_path: Path) -> dict:
    return hv13.run_evaluation(
        output_path=tmp_path / "HV13-DRY-test.json",
        observation_timeout=15,
        poll_interval=0.1,
        resume_timeout=30,
    )


def test_hv13_dry_process_recovery_exercises_failed_attempt_arbitration(tmp_path):
    result = _dry_result(tmp_path)

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


def test_hv13_attempt2_is_failed_turn_limit_after_resume(tmp_path):
    result = _dry_result(tmp_path)

    attempts = result["attempts"]
    assert result["attempt_count"] == 2
    assert attempts[0]["status"] == "CRASHED"
    assert attempts[0]["error_type"] == "PROCESS_INTERRUPTED"
    assert attempts[1]["status"] == "FAILED"
    assert attempts[1]["error_type"] == "AGENT_TURN_LIMIT"
    assert result["attempt_3_exists"] is False


def test_hv13_attempt2_arbitration_is_durable_and_passes(tmp_path):
    result = _dry_result(tmp_path)

    assert result["outcome_arbitration_observed"] is True
    assert result["outcome_arbitration_attempt_number"] == 2
    assert result["outcome_arbitration_executor_status"] == "FAILED"
    assert result["outcome_arbitration_error_type"] == "AGENT_TURN_LIMIT"
    assert result["outcome_arbitration_validation_passed"] is True
    assert result["next_attempt_suppressed_after_arbitration"] is True
    assert result["attempts_after_arbitration"] == 0


def test_hv13_failed_attempt_remains_failed_after_task_completion(tmp_path):
    result = _dry_result(tmp_path)

    attempt2 = result["attempts"][1]
    assert attempt2["status"] == "FAILED"
    assert attempt2["error_type"] == "AGENT_TURN_LIMIT"
    assert result["outer_task_completed"] is True
    assert result["agent_completion_passed"] is False
    assert result["functional_validation_passed"] is True
    assert result["process_recovery_passed"] is True


def test_hv13_reuses_same_durable_workspace_and_cp3_once(tmp_path):
    result = _dry_result(tmp_path)

    assert result["same_workspace_session"] is True
    assert result["durable_workspace_session_reused"] is True
    assert result["outer_harness_metrics"]["attempts_before_resume"] == 1
    assert result["outer_harness_metrics"]["new_attempts_after_resume"] == 1
    assert result["outer_harness_metrics"]["executor_calls_after_resume"] == [2]
    assert result["outer_harness_metrics"]["validator_calls_after_resume"] == 2
    assert result["outer_harness_metrics"]["cp3_attempts"] == 1


def test_hv13_duplicate_durable_records_are_zero(tmp_path):
    result = _dry_result(tmp_path)

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


def test_hv13_source_and_fixture_are_unchanged(tmp_path):
    result = _dry_result(tmp_path)

    assert result["source_repository_unchanged"] is True
    assert result["repository_fixture_unchanged"] is True
    assert result["temporary_source_snapshot_unchanged"] is True
    assert result["final_patch_nonempty"] is True
    assert result["evidence_safety"]["precrash_trace_incomplete"] is True


def test_hv12_historical_main_refuses_harness_mismatch(capsys):
    assert hv12.main([]) == 2
    assert "STATUS=HISTORICAL_HARNESS_MISMATCH" in capsys.readouterr().out


def test_hv13_live_claim_is_atomic_and_refuses_second_run(monkeypatch, tmp_path):
    monkeypatch.setenv("ODYS_AGENT_MODEL", "mimo-test-model")
    monkeypatch.setenv("ODYS_AGENT_API_KEY", "secret-must-not-persist")
    output = tmp_path / "HV13-LIVE-001.json"
    claim = tmp_path / "HV13-LIVE-001.claim.json"

    def fail_before_worker(*args, **kwargs):
        raise RuntimeError("test stops before any model worker starts")

    monkeypatch.setattr(hv13, "_spawn_worker", fail_before_worker)
    with pytest.raises(RuntimeError, match="test stops"):
        hv13.run_evaluation(mode="live_real_model", output_path=output, claim_path=claim)

    claim_data = json.loads(claim.read_text(encoding="utf-8"))
    assert claim_data == {
        "evaluation_id": "HV13-LIVE-001",
        "git_sha": claim_data["git_sha"],
        "harness_version": "HV-1.3",
        "execution_claimed": True,
    }
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
