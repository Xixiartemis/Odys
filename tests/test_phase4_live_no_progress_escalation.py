"""Offline contract tests for Experiment 02 preparation.

These tests never construct a provider and never execute a benchmark run.
"""

from __future__ import annotations

import inspect

from evals.reliability.p45_executor import P45BenchmarkExecutor
from evals.reliability.run_phase4 import ProtocolSnapshot
from evals.reliability.runtime_factory.recovery import OfficialOdysRecoveryCoordinator
from lhas.recovery_control import RecoveryController, RecoveryDecision

from scripts.phase4_live_no_progress_escalation import (
    ARMS,
    DEFAULT_OUTPUT,
    EXPERIMENT_TASK_ID,
    _arm_config,
    _effective_config_diff,
    _experiment_task,
    _preflight,
)


def _snapshot() -> ProtocolSnapshot:
    return ProtocolSnapshot.load()


def test_official_default_blocks_macro_replan_and_opt_in_enables_it():
    snapshot = _snapshot()
    default = P45BenchmarkExecutor(factory_type="real")
    default.configure_frozen_budget(snapshot.protocol["budgets"])
    opt_in = P45BenchmarkExecutor(
        factory_type="real", experiment_macro_replan_enabled=True
    )
    opt_in.configure_frozen_budget(snapshot.protocol["budgets"])
    assert default._frozen_budgets["max_replan_attempts"] == 0
    assert opt_in._frozen_budgets["max_replan_attempts"] == 1
    assert "P410IntegrationExecutor" not in inspect.getsource(
        P45BenchmarkExecutor.execute
    )


def test_both_arms_share_odys_config_and_only_policy_differs():
    snapshot = _snapshot()
    baseline = _arm_config(snapshot, "LEGACY_BOUNDED", "baseline")
    v2 = _arm_config(snapshot, "NO_PROGRESS_AWARE", "v2")
    assert baseline["config_id"] == v2["config_id"] == "odys_p3"
    assert baseline["_experiment_macro_replan_enabled"] is True
    assert v2["_experiment_macro_replan_enabled"] is True
    assert _effective_config_diff(baseline, v2) == set()
    assert baseline["escalation_trigger_policy"] != v2["escalation_trigger_policy"]


def test_legacy_observes_no_progress_without_early_escalation():
    controller = RecoveryController(
        task_id=EXPERIMENT_TASK_ID,
        run_id="run",
        attempt_id="attempt",
        max_repeated_action=1,
        escalation_policy="LEGACY_BOUNDED",
    )
    first, _ = controller.observe(
        before_state={"route": "local"},
        after_state={"route": "local"},
        action={"capability": "workspace.edit", "args_sha256": "same"},
    )
    assert first is RecoveryDecision.CONTINUE_LOCAL_REPAIR
    second, _ = controller.observe(
        before_state={"route": "local"},
        after_state={"route": "local"},
        action={"capability": "workspace.edit", "args_sha256": "same"},
    )
    assert second is RecoveryDecision.CONTINUE_LOCAL_REPAIR
    assert controller.detections
    assert controller.signals == []


def test_no_progress_aware_consumes_typed_signal():
    controller = RecoveryController(
        task_id=EXPERIMENT_TASK_ID,
        run_id="run",
        attempt_id="attempt",
        max_repeated_action=1,
        escalation_policy="NO_PROGRESS_AWARE",
    )
    decision, _ = controller.observe(
        before_state={"route": "local"},
        after_state={"route": "local"},
        action={"capability": "workspace.edit", "args_sha256": "same"},
    )
    # The first observation is a no-progress observation, but repeated action
    # is only proven on a second identical action.
    assert decision is RecoveryDecision.CONTINUE_LOCAL_REPAIR
    decision, _ = controller.observe(
        before_state={"route": "local"},
        after_state={"route": "local"},
        action={"capability": "workspace.edit", "args_sha256": "same"},
    )
    assert decision is RecoveryDecision.ESCALATE_MACRO_REPLAN
    assert controller.signals[-1]["reason"] == "REPAIR_REPEATED_ACTION"


def test_preflight_is_provider_free_and_does_not_create_output(tmp_path):
    report = _preflight(tmp_path / "experiment-02")
    assert report["provider_executed"] is False
    assert report["result_created"] is False
    assert report["output_exists_after_preflight"] is False
    assert report["experiment_01_unchanged"] is True
    assert report["official_default_replan_behavior_preserved"] is True
    assert report["experiment_opt_in_replan_enabled"] is True
    assert report["official_execution_path"] is True
    assert report["validator_unchanged"] is True
    assert report["expected_runs"] == len(ARMS) * 3
    assert not (tmp_path / "experiment-02").exists()


def test_task_projection_has_frozen_fault_and_experiment_alternate_strategy():
    task = _experiment_task(_snapshot())
    assert task["task_id"] == EXPERIMENT_TASK_ID
    assert task["fault_injection"] == "FAIL_TOOL_ON_CALL_1"
    assert task["experiment_initial_plan_steps"] == ["workspace.edit"]
    assert task["experiment_replan_plan_steps"] == ["workspace.edit_lines"]
    assert task["expected_observable_effects"] == {
        "route": "alternate",
        "state_status": "verified",
    }
