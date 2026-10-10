"""Provider-free proofs for the final Phase5 semantic closure."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lhas.phase5.agent_core import Phase5AgentCore
from lhas.phase5.control_arms import (
    OdysFullStrategy,
    OdysMinusObservableProgress,
)
from lhas.phase5.model_driver import ScriptedAction, ScriptedModelDriver
from lhas.phase5.runtime_validator import PublicEvidenceCompletionValidator
from lhas.phase5.shadow_observer import ShadowProgressObserver
from lhas.phase5.substrate.evidence import EvidenceLedger
from lhas.phase5.toolmaze_adapter import ToolMazeAdapter
from lhas.phase5.types import BudgetConfig


def _run_observer_runtime(strategy):
    driver = ScriptedModelDriver([
        ScriptedAction(type="tool_call", tool_name="search", arguments={"q": "same"}),
        ScriptedAction(type="tool_call", tool_name="search", arguments={"q": "same"}),
        ScriptedAction(type="tool_call", tool_name="search", arguments={"q": "same"}),
        ScriptedAction(type="final_answer", content="done"),
    ])
    core = Phase5AgentCore(driver, strategy=strategy)
    core.initialize("task", [{"name": "search"}])
    core.set_shadow_observer(strategy.create_observer())
    core.set_evidence_ledger(EvidenceLedger(run_id="closure"))
    for _ in range(3):
        core.next_model_action()
        core.receive_tool_result("search", {"status": "success"})
    return core


def test_a4_is_causal_runtime_ablation_and_direct_failure_parity():
    a3 = _run_observer_runtime(OdysFullStrategy())
    a4 = _run_observer_runtime(OdysMinusObservableProgress())

    assert a3._shadow_observer.get_records()[-1].signal.value == "STALLED"
    assert a4._shadow_observer.get_records()[-1].signal.value == "STALLED"
    assert a3.get_recovery_decisions(), "A3 must act on the OP signal"
    assert not a4.get_recovery_decisions(), "A4 must not act on the OP signal"

    # Same direct public failure remains authoritative in both arms.
    for strategy in (OdysFullStrategy(), OdysMinusObservableProgress()):
        core = Phase5AgentCore(
            ScriptedModelDriver([ScriptedAction(type="tool_call", tool_name="x", arguments={})]),
            strategy=strategy,
        )
        core.initialize("task", [{"name": "x"}])
        core.set_shadow_observer(strategy.create_observer())
        core.set_evidence_ledger(EvidenceLedger(run_id="direct"))
        core.next_model_action()
        core.receive_tool_result("x", {"status": "error"})
        assert core.get_recovery_decisions()


def test_toolmaze_execution_unit_identity_is_exact_and_non_overwriting():
    adapter = ToolMazeAdapter()
    descriptors = adapter.enumerate_tasks()
    assert len({d.task_id for d in descriptors}) == len(descriptors)
    assert adapter._find_task("C1_task_082") is None
    assert adapter._find_task("C1_task_082_P0")["perturbation_mode"] == "P0"
    assert adapter._find_task("C1_task_082_P1")["perturbation_mode"] == "P1"


def test_toolmaze_p1_native_public_error_is_observable():
    """The frozen native P1 result exposes a public failure signal."""
    adapter = ToolMazeAdapter()
    raw = adapter._find_task("C1_task_015_P1")
    assert raw is not None
    native_failure = next(
        step for step in raw["execution_trace"] if step.get("status") == "error"
    )
    public_result = dict(native_failure["output"])
    public_result["status"] = native_failure["status"]

    observer = ShadowProgressObserver()
    record = observer.observe(
        task_id="C1_task_015_P1",
        step=native_failure["step"],
        action_identity=f"{native_failure['tool_name']}:native-p1",
        tool_result=public_result,
    )
    assert public_result["error"]
    assert record.signal.value == "ANOMALY"

    feedback = PublicEvidenceCompletionValidator().validate(
        candidate_id="candidate-p1-native",
        evidence_refs=[],
        runtime_evidence={
            "candidate_answer": "done",
            "public_tool_results": [
                {
                    "tool_name": native_failure["tool_name"],
                    "tool_call_id": "native-p1-call",
                    **public_result,
                }
            ],
        },
    )
    assert feedback.failure_type == "UNRESOLVED_PUBLIC_FAILURE"


def test_validator_strict_dto_and_public_lineage():
    validator = PublicEvidenceCompletionValidator()
    base = {
        "candidate_answer": "done",
        "public_tool_results": [
            {"tool_name": "A", "tool_call_id": "a1", "status": "error"},
            {"tool_name": "B", "tool_call_id": "b1", "status": "success"},
        ],
    }
    feedback = validator.validate(candidate_id="c1", evidence_refs=[], runtime_evidence=base)
    assert feedback.failure_type == "UNRESOLVED_PUBLIC_FAILURE"
    with pytest.raises(ValueError):
        validator.validate(candidate_id="c2", evidence_refs=[], runtime_evidence={**base, "random_unknown_field": 1})


def test_frozen_pilot_preflight_resolves_all_units():
    from lhas.phase5.real_pilot_runner import RealPilotRunner
    from lhas.phase5.model_driver import ScriptedModelDriver

    runner = RealPilotRunner(model_driver_factory=lambda: ScriptedModelDriver([]))
    report = runner._preflight["report"]
    assert report["expected_execution_units"] == 20
    assert report["resolved_execution_units"] == 20
    assert report["mode_mismatches"] == 0
    assert report["file_mismatches"] == 0
