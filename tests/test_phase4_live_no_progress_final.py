from __future__ import annotations

import json

from scripts.phase4_live_no_progress_final import (
    EXPECTED_RUNS,
    EXPECTED_POLICY_ID,
    _preflight,
    _smoke,
)


def test_02g_preflight_binds_one_policy_per_run_without_provider(tmp_path):
    output = tmp_path / "02g-preflight"
    report = _preflight(output)

    assert report["expected_runs"] == EXPECTED_RUNS == 6
    assert report["shared_effect_policy_implementation"] is True
    assert report["effect_policy_id"] == EXPECTED_POLICY_ID
    assert report["all_runs_have_effect_policy"] is True
    assert report["policy_instance_count"] == 6
    assert report["unique_policy_instance_count"] == 6
    assert report["real_runner_config_contains_phase_effect_policy"] is True
    assert report["provider_executed"] is False
    assert report["output_created"] is False
    assert not output.exists()


def test_02g_provider_free_smoke_uses_the_same_policy_path(tmp_path):
    output = tmp_path / "02g-smoke"
    report = __import__("asyncio").run(_smoke(output))

    assert report["provider_executed"] is False
    assert report["planned_runs"] == 6
    assert report["valid_runs"] == 6
    assert report["invalid_runs"] == 0
    assert report["policy_instance_count"] == 6
    assert report["unique_policy_instance_count"] == 6
    assert report["all_runs_have_effect_policy"] is True
    assert report["initial_alternate_mutation_denied"] is True
    assert report["local_repair_alternate_mutation_denied"] is True
    assert report["post_replan_alternate_mutation_allowed"] is True
    assert report["post_replan_mutation_count"] == 6
    assert report["post_replan_redundant_tool_calls"] == 0
    assert report["post_replan_redundant_provider_calls"] == 0
    assert report["baseline_final_validator_accepted"] is True
    assert report["v2_final_validator_accepted"] is True
    assert report["fault_trigger_index_1"] is True
    assert json.loads((output / "qualification.json").read_text()) == report
