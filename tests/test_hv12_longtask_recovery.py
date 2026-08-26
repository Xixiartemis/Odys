"""Offline contract tests for the manual HV-1.2 evaluation infrastructure."""

from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

from scripts.hv12_longtask_recovery import (
    AttemptStatus,
    FIXTURE_ROOT,
    _diff_summary,
    _persisted_completion_claim,
    _pytest,
    _termination_status,
    _worker_python_command,
    _write_evidence,
    run_evaluation,
)


def test_hv12_fixture_starts_red_and_has_no_source_mutation(tmp_path):
    fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURE_ROOT, fixture)

    before = _pytest(fixture)
    assert before["status"] == "FAIL"
    assert _diff_summary(fixture, fixture)["files_changed"] == 0


def test_hv12_dry_run_crosses_real_subprocess_boundary_and_resumes(tmp_path):
    output = tmp_path / "HV12-DRY-test.json"

    result = run_evaluation(
        output_path=output,
        observation_timeout=15,
        poll_interval=0.1,
        resume_timeout=30,
    )

    assert result["status"] == "PASS"
    assert result["mode"] == "deterministic_process_recovery_fixture"
    assert result["initial_tests"]["status"] == "FAIL"
    assert result["final_tests"]["status"] == "PASS"
    assert result["crash"]["trigger"] == "DURABLE_MUTATION_STABLE"
    assert result["crash"]["process_a_forced_termination"] is True
    assert result["process_recovery_passed"] is True
    assert result["durable_workspace_session_reused"] is True
    assert result["outer_harness_metrics"]["cp3_attempts"] >= 1
    assert result["outer_harness_metrics"]["attempts_before_resume"] == 1
    assert result["outer_harness_metrics"]["new_attempts_after_resume"] == 1
    assert result["outer_harness_metrics"]["executor_calls_after_resume"] == [2]
    assert result["repository_fixture_unchanged"] is True
    assert result["temporary_source_snapshot_unchanged"] is True
    assert result["evidence_safety"]["raw_diff_persisted"] is False
    assert output.is_file()


def test_worker_bootstrap_uses_active_interpreter():
    assert _worker_python_command() == [sys.executable]


def test_completion_claim_is_read_from_persisted_inner_result():
    assert _persisted_completion_claim({"raw": {"completion_claim": True}}) is True
    assert _persisted_completion_claim({"raw": {"completion_claim": False}}) is False
    assert _persisted_completion_claim({"completion_claim": True}) is True
    assert _persisted_completion_claim({"raw": {"final_output": "done"}}) is None


def test_termination_status_uses_stable_classification():
    completion_event = SimpleNamespace(event_type=SimpleNamespace(value="INNER_AGENT_COMPLETED"))
    assert _termination_status(
        SimpleNamespace(error_type="PROCESS_INTERRUPTED", status=AttemptStatus.CRASHED), None
    ) == "FORCED_PROCESS_TERMINATION"
    assert _termination_status(
        SimpleNamespace(error_type="AGENT_TURN_LIMIT", status=AttemptStatus.FAILED), None
    ) == "TURN_LIMIT"
    assert _termination_status(
        SimpleNamespace(error_type=None, status=AttemptStatus.FAILED), completion_event
    ) == "COMPLETED"
    assert _termination_status(
        SimpleNamespace(error_type="PROVIDER_TIMEOUT", status=AttemptStatus.FAILED), None
    ) == "PROVIDER_TIMEOUT"


def test_live_evidence_creation_is_exclusive(tmp_path):
    output = tmp_path / "live.json"
    output.write_text("{}\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        _write_evidence(output, {"status": "PASS"}, exclusive=True)


def test_live_result_is_canonical_even_without_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("ODYS_AGENT_MODEL", raising=False)
    monkeypatch.delenv("ODYS_AGENT_API_KEY", raising=False)
    output = tmp_path / "live.json"
    output.write_text("{}\n", encoding="utf-8")

    result = run_evaluation(mode="live_real_model", output_path=output)

    assert result == {
        "status": "LIVE_RESULT_EXISTS",
        "mode": "live_real_model",
        "live_run_executed": False,
    }


def test_hv12_live_mode_is_config_gated(monkeypatch, tmp_path):
    monkeypatch.delenv("ODYS_AGENT_MODEL", raising=False)
    monkeypatch.delenv("ODYS_AGENT_API_KEY", raising=False)

    result = run_evaluation(mode="live_real_model", output_path=tmp_path / "live.json")

    assert result == {
        "status": "SKIPPED_CONFIG",
        "mode": "live_real_model",
        "live_run_executed": False,
    }
