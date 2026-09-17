from __future__ import annotations

from scripts.phase4_live_effect_policy_parity import qualify_02f


def test_02f_shared_effect_policy_reaches_the_official_runner(tmp_path):
    report = qualify_02f(tmp_path / "02f")

    assert report["experiment_id"] == "phase4-live-effect-policy-parity-02f"
    assert report["provider_executed"] is False
    assert report["same_live_execution_path"] is True

    parity = report["effect_policy_parity"]
    assert parity["shared_effect_policy_implementation"] is True
    assert parity["script_local_monkeypatch"] is False
    assert parity["real_runner_uses_same_policy"] is True
    assert parity["runtime_tool_policy_id"] == "phase4-effect-policy-v1"
    assert parity["registry_policy_install_count"] == 2

    assert report["initial_alternate_effect_blocked"] is True
    assert report["local_repair_alternate_effect_blocked"] is True
    assert report["post_replan_alternate_effect_allowed"] is True
    assert report["both_arms_enter_recovery"] is True
    assert report["acceptance"]["initial_validator_rejected"] is True
    assert report["acceptance"]["baseline_final_validator"] == "ACCEPTED"
    assert report["acceptance"]["v2_final_validator"] == "ACCEPTED"
    assert report["acceptance"]["replan_executed"] is True

    assert report["baseline"]["no_progress_observed"] is True
    assert report["baseline"]["no_progress_used_for_control"] is False
    assert report["v2"]["no_progress_observed"] is True
    assert report["v2"]["no_progress_used_for_control"] is True
