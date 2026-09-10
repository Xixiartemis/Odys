import hashlib
import json
import shutil
from pathlib import Path

import pytest

from evals.reliability.phase4_v1.validate import (
    ProtocolError,
    compare_fairness_identity,
    protocol_hash,
    validate_protocol,
    validate_raw_result,
)


ROOT = Path(__file__).resolve().parents[1] / "evals" / "reliability" / "phase4_v1"


def test_phase4_manifest_and_ablation_are_machine_validated():
    report = validate_protocol(ROOT)
    assert report["benchmark_version"] == "phase4-v1"
    assert report["task_count"] == 60
    assert report["family_count"] == 6
    assert report["headline_runs"] == 360
    assert report["ablation_runs"] == 144


def test_headline_and_ablation_protocol_counts_are_exact():
    protocol = json.loads((ROOT / "protocol.json").read_text(encoding="utf-8"))
    ablation = json.loads((ROOT / "ablation.json").read_text(encoding="utf-8"))
    assert protocol["headline"]["configs"] == ["minimal", "odys_p3"]
    assert protocol["headline"]["repeats"] == 3
    assert ablation["configs"] == ["minimal", "minimal_plus_verification", "workflow_no_selective_repair", "odys_p3"]
    assert len(ablation["task_ids"]) == 12
    assert ablation["repeats"] == 3


def test_minimal_is_not_given_p3_authority_and_odys_is_explicit():
    minimal = json.loads((ROOT / "configs" / "minimal.json").read_text(encoding="utf-8"))
    odys = json.loads((ROOT / "configs" / "odys_p3.json").read_text(encoding="utf-8"))
    assert not any(minimal["features"].values())
    assert all(odys["features"].values())
    assert odys["plan_mode"] == "SIMPLE_DEPENDENCY"


def test_fairness_mismatch_is_rejected():
    left = _fairness_identity()
    right = dict(left, fixture_set_hash="different")
    assert compare_fairness_identity(left, left)
    assert not compare_fairness_identity(left, right)


def _fairness_identity(**overrides):
    identity = {
        "benchmark_version": "phase4-v1",
        "protocol_hash": "protocol",
        "manifest_hash": "manifest",
        "fault_set_hash": "faults",
        "validator_hash": "validator",
        "fixture_set_hash": "fixtures",
        "model_identity": "model",
        "provider_identity": "provider",
        "budget_identity": "budget",
        "config_name": "minimal",
    }
    identity.update(overrides)
    return identity


def test_fairness_rejects_protocol_hash_mismatch():
    assert not compare_fairness_identity(_fairness_identity(), _fairness_identity(protocol_hash="other"))


def test_fairness_rejects_fault_set_hash_mismatch():
    assert not compare_fairness_identity(_fairness_identity(), _fairness_identity(fault_set_hash="other"))


def test_fairness_rejects_validator_hash_mismatch():
    assert not compare_fairness_identity(_fairness_identity(), _fairness_identity(validator_hash="other"))


def test_fairness_allows_minimal_vs_odys_with_same_experiment_identity():
    assert compare_fairness_identity(
        _fairness_identity(config_name="minimal"),
        _fairness_identity(config_name="odys_p3"),
    )


def test_every_task_fault_reference_and_category_are_semantically_valid():
    report = validate_protocol(ROOT)
    assert report["task_count"] == 60
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    ptf07 = next(task for task in manifest["tasks"] if task["task_id"] == "PTF-07")
    assert ptf07["fault_injection"] == "PROVIDER_UNAVAILABLE"
    assert "unavailability" in ptf07["objective"]
    assert "auth" not in ptf07["objective"].casefold()


def test_not_measured_is_supported_but_not_zero():
    result = {
        "benchmark_version":"phase4-v1", "benchmark_run_id":"run", "task_id":"CI-01", "family":"COMPLETION_INTEGRITY", "repeat_index":1, "configuration":"minimal",
        "repo_sha":"repo", "fixture_hash":"fixture", "manifest_hash":"manifest", "protocol_hash":"protocol", "model":"model", "provider":"provider", "runtime_environment":{}, "validator_id":"external-observable-v1", "fault_id":"PARTIAL_OUTPUT", "fault_type":"partial_output",
        "claimed_complete":False, "verified_completion":False, "false_completion":False, "failure_type":"NOT_MEASURED", "recovery_required":False, "recovery_attempted":False, "recovery_success":False, "repair_scope":None, "repair_attempts":0, "replan_count":0, "lost_work_units":"NOT_MEASURED", "duplicate_side_effect_count":0, "tool_calls":0, "model_calls":0, "attempt_count":0, "tokens_input":"NOT_MEASURED", "tokens_output":"NOT_MEASURED", "total_tokens":"NOT_MEASURED", "model_cost":"NOT_MEASURED", "tool_cost":"NOT_MEASURED", "wall_time_seconds":"NOT_MEASURED", "human_intervention":False, "validity":"VALIDATED_FAIL", "invalid_reason":None, "started_at":"2026-01-01T00:00:00Z", "finished_at":"2026-01-01T00:00:01Z"
    }
    validate_raw_result(result)
    assert result["model_cost"] == "NOT_MEASURED"
    assert result["model_cost"] != 0


def test_protocol_hash_is_deterministic_and_changes_on_frozen_input_mutation(tmp_path):
    copied = tmp_path / "phase4_v1"
    shutil.copytree(ROOT, copied)
    first = protocol_hash(copied)
    second = protocol_hash(copied)
    assert first == second
    protocol_path = copied / "protocol.json"
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    protocol["budgets"]["max_turns"] += 1
    protocol_path.write_text(json.dumps(protocol, sort_keys=True), encoding="utf-8")
    assert protocol_hash(copied) != first


def test_historical_phase2_phase3_evidence_is_not_mutated_by_validation():
    historical = [
        Path(__file__).resolve().parents[1] / "artifacts" / "phase2" / "p25-closeout.json",
        Path(__file__).resolve().parents[1] / "docs" / "phase2" / "P25_CLOSEOUT_REPORT.md",
    ]
    before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in historical]
    validate_protocol(ROOT)
    after = [hashlib.sha256(path.read_bytes()).hexdigest() for path in historical]
    assert before == after


def test_manifest_rejects_prohibited_result_bias_field(tmp_path):
    copied = tmp_path / "phase4_v1"
    shutil.copytree(ROOT, copied)
    manifest_path = copied / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tasks"][0]["expected_winner"] = "odys_p3"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ProtocolError, match="bias field"):
        validate_protocol(copied)
