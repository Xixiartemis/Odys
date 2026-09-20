from lhas.repair_progress import RepairProgressTracker


def _observation(checksum, *, args_sha256="a" * 64, capability="workspace.edit"):
    return {
        "capability": capability,
        "args_sha256": args_sha256,
        "status": "SUCCESS",
        "observed_mutation": True,
        "bounded_output": {"checksum": checksum},
    }


def test_repeated_edits_stop_before_provider_budget():
    tracker = RepairProgressTracker(expected_effects={"checksum": "target"})
    tracker.begin_turn()
    first = tracker.observe(_observation("wrong", args_sha256="1" * 64))
    tracker.begin_turn()
    second = tracker.observe(_observation("wrong", args_sha256="1" * 64))
    tracker.begin_turn()
    third = tracker.observe(_observation("wrong", args_sha256="1" * 64))

    assert first.continue_repair is True
    assert second.continue_repair is True
    assert third.continue_repair is False
    assert third.stop_reason == "REPEATED_ACTION"
    assert third.metrics["repair_turns"] == 3
    assert third.metrics["repeated_state_count"] == 2


def test_oscillating_states_are_bounded_without_task_specific_knowledge():
    tracker = RepairProgressTracker(
        expected_effects={"checksum": "target"},
        max_no_progress=99,
        max_repeated_state=1,
        max_repeated_action=99,
    )
    for checksum in ("a", "b", "a"):
        tracker.begin_turn()
        decision = tracker.observe(_observation(checksum, args_sha256=checksum * 64))

    assert decision.continue_repair is False
    assert decision.stop_reason == "NO_PROGRESS"
    assert decision.metrics["unique_repair_states"] == 2
    assert decision.metrics["repeated_state_count"] == 1


def test_distinct_non_candidate_edits_stop_after_bounded_non_progress():
    tracker = RepairProgressTracker(
        expected_effects={"checksum": "target"},
        max_no_progress=3,
        max_repeated_state=99,
        max_repeated_action=99,
    )
    for index in range(3):
        tracker.begin_turn()
        decision = tracker.observe(
            _observation(f"wrong-{index}", args_sha256=str(index) * 64)
        )

    assert decision.continue_repair is False
    assert decision.stop_reason == "NO_PROGRESS"
    assert decision.metrics["unique_repair_states"] == 3
    assert decision.metrics["no_progress_count"] == 3


def test_expected_effect_is_only_a_candidate_for_authoritative_validation():
    tracker = RepairProgressTracker(expected_effects={"checksum": "target"})
    tracker.begin_turn()
    decision = tracker.observe(_observation("target"))

    assert decision.continue_repair is True
    assert decision.candidate_for_validation is True
    assert decision.metrics["validation_candidate_count"] == 1
    assert decision.metrics["repair_stop_reason"] is None

    tracker.candidate_for_validation()
    tracker.stop("VERIFIED")
    assert tracker.snapshot()["repair_stop_reason"] == "VERIFIED"
