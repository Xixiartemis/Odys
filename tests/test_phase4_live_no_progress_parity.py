"""Provider-free qualification of the actual Phase 4 recovery path."""

from __future__ import annotations

import json

from scripts.phase4_live_no_progress_parity import qualify


def test_02d_uses_live_path_and_gates_alternate_effects(tmp_path):
    report = qualify(tmp_path / "02d")

    assert report["provider_executed"] is False
    assert report["scripted_provider"] is True
    assert report["same_live_execution_path"] is True
    assert report["planned_runs"] == 2
    assert report["valid_runs"] == 2
    assert report["invalid_runs"] == 0
    assert report["initial_alternate_effect_blocked"] is True
    assert report["local_repair_alternate_effect_blocked"] is True
    assert report["post_replan_alternate_effect_allowed"] is True
    assert report["both_arms_enter_recovery"] is True
    assert report["fault_trigger_index"] is True
    assert report["acceptance"]["initial_validator_rejected"] is True
    assert report["acceptance"]["baseline_final_validator"] == "ACCEPTED"
    assert report["acceptance"]["v2_final_validator"] == "ACCEPTED"
    assert report["acceptance"]["replan_executed"] is True

    assert report["baseline"]["no_progress_observed"] is True
    assert report["baseline"]["no_progress_used_for_control"] is False
    assert report["baseline"]["macro_replan_executed"] is True
    assert report["v2"]["no_progress_observed"] is True
    assert report["v2"]["no_progress_used_for_control"] is True
    assert report["v2"]["macro_replan_executed"] is True

    for arm in ("baseline", "v2"):
        assert report[arm]["recovery_attempted"] is True
        assert report[arm]["recovery_success"] is True
        assert "FAULT_TRIGGERED" in report[arm]["event_types"]
        assert "REPLAN_ACCEPTED" in report[arm]["event_types"]
        assert "STEP_VERIFIED" in report[arm]["event_types"]
        assert "VERIFICATION_PASSED" in report[arm]["event_types"]

    qualification = json.loads(
        (tmp_path / "02d" / "qualification.json").read_text(encoding="utf-8")
    )
    assert qualification["experiment_id"] == "phase4-live-no-progress-parity-02d"
