"""Provider-free deterministic qualification for Experiment 02B."""

from __future__ import annotations

from scripts.phase4_live_no_progress_qualification import qualify


def test_experiment_02b_qualification_is_complete_and_provider_free():
    report = qualify()

    assert report["provider_executed"] is False
    assert report["phase_gate_initial_to_macro"] is True
    assert report["both_arms_enter_recovery"] is True
    assert report["authoritative_state_source"] == "environment_state"
    assert report["control_plane_state_separated"] is True
    assert report["local_repair_syntactic_success"] is True
    assert report["authoritative_state_unchanged_after_local"] is True
    assert report["repair_fingerprint_recorded"] is True
    assert report["repair_equivalence_recorded"] is True
    assert report["repair_no_progress_observed"] is True
    assert report["baseline_no_progress_used_for_control"] is False
    assert report["v2_no_progress_used_for_control"] is True
    assert report["v2_local_reserve_at_escalation"] > 0
    assert report["macro_replan_consume_probe"] is True
    assert report["macro_replan_reserve_before"] >= 1
    assert report["macro_replan_reserve_after"] == 0
    assert report["fault_trigger_index_recorded"] is True
    assert report["durable_signal_persisted"] is True
    assert report["durable_replan_acceptance_event"] is True
    assert report["baseline_final_validator"] == "ACCEPTED"
    assert report["v2_final_validator"] == "ACCEPTED"
    assert report["only_validator_can_verify"] is True
    assert report["verified_work_preserved"] is True
    assert report["affected_subgraph_only"] is True
    assert report["root_budget_single_authority"] is True


def test_qualification_records_distinct_control_decisions():
    report = qualify()
    baseline = report["baseline"]
    v2 = report["v2"]

    assert baseline["durable_signal_reason"] == "LOCAL_REPAIR_BUDGET_EXHAUSTED"
    assert v2["durable_signal_reason"] == "REPAIR_NO_PROGRESS"
    assert baseline["fault_trigger_index"] == 1
    assert v2["fault_trigger_index"] == 1
    assert baseline["macro_replan_executed"] is True
    assert v2["macro_replan_executed"] is True
    assert baseline["post_replan_executed"] is True
    assert v2["post_replan_executed"] is True
    assert baseline["durable_signal_persisted"] is True
    assert v2["durable_signal_persisted"] is True
