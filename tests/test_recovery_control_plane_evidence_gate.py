"""Tests for the offline recovery control-plane evidence gate."""

from pathlib import Path

from scripts.recovery_control_plane_evidence_gate import (
    BASE_SHA,
    CONTROL_PLANE_SHA,
    HISTORICAL_TRACES,
    load_expected_effects,
    measure_context_chars,
    replay_historical_trace,
    simulate_budget_chain,
)
from lhas.recovery_control import ProgressStatus, RecoveryController, RecoveryDecision


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_historical_replays_stop_at_first_bounded_non_progress_signal():
    expected = load_expected_effects(REPO_ROOT)
    r1 = replay_historical_trace(REPO_ROOT, *HISTORICAL_TRACES[0], expected)
    r2 = replay_historical_trace(REPO_ROOT, *HISTORICAL_TRACES[1], expected)

    assert (r1.stop_turn, r1.stop_reason, r1.typed_signal, r1.avoided_turns) == (
        3,
        "NO_PROGRESS",
        None,
        15,
    )
    assert (r2.stop_turn, r2.stop_reason, r2.typed_signal, r2.avoided_turns) == (
        3,
        "NO_PROGRESS",
        None,
        16,
    )
    assert r1.tracker_snapshot["validation_candidate_count"] == 0
    assert r2.tracker_snapshot["validation_candidate_count"] == 0


def test_context_projection_is_bounded_window_at_runtime_char_level():
    measurements = measure_context_chars(REPO_ROOT)
    assert set(measurements) == {1, 2, 4, 8, 16, 32}
    assert measurements[32] - measurements[16] <= 128
    assert measurements[16] < measurements[1] * 3


def test_budget_chain_keeps_escalation_reserves_under_one_root_authority():
    proof = simulate_budget_chain()
    assert proof["local_repair_calls_used"] == 3
    assert proof["local_repair_cannot_borrow"] is True
    assert proof["macro_replan_executed"] is True
    assert proof["post_replan_executed"] is True
    assert proof["validation_executed"] is True
    assert proof["reserve_remaining_at_escalation"]["reserved"] == {
        "local_repair": 1,
        "macro_replan": 2,
        "post_replan": 2,
        "validation": 1,
    }


def test_only_authoritative_validator_can_turn_candidate_into_verified():
    controller = RecoveryController(
        task_id="candidate-task",
        run_id="candidate-run",
        attempt_id="candidate-attempt",
        expected_effects={"ready": True},
    )
    decision, progress = controller.observe(
        before_state={"ready": False},
        after_state={"ready": True},
        action={"capability": "workspace.edit", "args_sha256": "a" * 64},
        observation={"bounded_output": {"ready": True}},
    )

    assert decision is RecoveryDecision.VALIDATE_CANDIDATE
    assert progress.status is ProgressStatus.SATISFIED
    assert progress.candidate_for_validation is True

    class OfflineAuthoritativeValidator:
        def __init__(self, accepted):
            self.accepted = accepted

        def validate(self, _candidate):
            return self.accepted

    assert OfflineAuthoritativeValidator(False).validate(progress) is False
    assert OfflineAuthoritativeValidator(True).validate(progress) is True
    assert not hasattr(controller, "verified")


def test_gate_is_pinned_to_the_control_plane_exact_sha():
    assert BASE_SHA == "db15e50c6e4a1df0d8c2e0706e048a1225510423"
    assert CONTROL_PLANE_SHA == "462383805542854661a23892c0236be208405335"
