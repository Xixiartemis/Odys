import hashlib
from pathlib import Path

import pytest

from scripts import hv15_e7a_tool_recovery as e7a


@pytest.fixture(scope="module")
def dry_result(tmp_path_factory):
    output=tmp_path_factory.mktemp("e7a-dry") / "result.json"
    result=e7a.run_evaluation(output)
    assert output.exists()
    return result


def test_e7a_dry_identity_and_safety(dry_result):
    assert dry_result["evaluation_id"] == "HV15-E7A-DRY-001"
    assert dry_result["harness_version"] == "HV-1.5"
    assert dry_result["mode"] == "deterministic_dry_comparison"
    assert dry_result["real_model_executed"] is False
    assert dry_result["stochastic_success_rate_claimed"] is False
    assert dry_result["status"] == "PASS"


def test_e7a_dry_measures_edit_and_tool_failure_reduction(dry_result):
    comparison=dry_result["comparison"]
    assert comparison["edit_failure_rate_baseline"] == 1.0
    assert comparison["edit_failure_rate_e7a"] == 0.0
    assert comparison["tool_failure_rate_baseline"] == pytest.approx(2 / 9,abs=0.000001)
    assert comparison["tool_failure_rate_e7a"] == 0.0
    assert comparison["repeated_edit_target_not_found_baseline"] == 1
    assert comparison["repeated_edit_target_not_found_e7a"] == 0


def test_e7a_dry_verifies_earlier_and_finishes_functionally(dry_result):
    comparison=dry_result["comparison"]
    assert comparison["first_verification_turn_baseline"] == 9
    assert comparison["first_verification_turn_e7a"] == 8
    assert comparison["final_functional_validation_baseline"] == "PASS"
    assert comparison["final_functional_validation_e7a"] == "PASS"
    assert comparison["outer_task_result_baseline"] == comparison["outer_task_result_e7a"] == "COMPLETED"
    assert dry_result["baseline"]["outer_run_result"] == dry_result["e7a"]["outer_run_result"] == "COMPLETED"
    assert dry_result["baseline"]["outer_validator_calls"] == dry_result["e7a"]["outer_validator_calls"] == 1
    assert dry_result["baseline"]["attempt_budget"] == dry_result["e7a"]["attempt_budget"] == {"max_attempts":3,"inner_turn_budget":20}
    assert dry_result["baseline"]["task_contract_sha256"] == dry_result["e7a"]["task_contract_sha256"]
    assert dry_result["baseline"]["orchestrator"] == dry_result["e7a"]["orchestrator"] == "RecoveringOrchestrator"
    assert dry_result["baseline"]["validator"] == dry_result["e7a"]["validator"] == "FixturePytestValidator"
    assert dry_result["baseline"]["production_tool_invocations"] is True and dry_result["e7a"]["production_tool_invocations"] is True


def test_e7a_dry_preserves_fixture_and_canonical_artifacts(dry_result):
    assert dry_result["fixture_tree_sha256_before"] == dry_result["fixture_tree_sha256_after"]
    assert dry_result["checks"]["canonical_artifacts_unchanged"] is True
    for name,expected in e7a.CANONICAL_HASHES.items():
        path=e7a.REPO_ROOT / "evals" / "runs" / name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
