"""Provider-free closure checks for the plan-to-tool recovery boundary."""

from __future__ import annotations

import asyncio
import hashlib

from lhas.agent.models import AgentBudget, AgentRequest, AgentRole
from lhas.native.models import ExecutionSnapshot, ProviderToolCall
from lhas.native.tools import NativeToolDispatcher, _safe_tool_arguments
from lhas.planning.models import PlanStep, compute_step_semantic_fingerprint
from lhas.recovery_control import RecoveryController, RecoveryDecision, ProgressStatus
from lhas.tools.registry import ToolRegistry
from lhas.workspace import (
    CommandPolicy,
    LocalReadOnlyWorkspace,
    StagedWorkspace,
    register_staged_workspace_tools,
    register_workspace_tools,
)
from evals.reliability.run_phase4 import ExecutionOutcome, ExternalObservableValidator, _validator_observation_view


def _request(contract: dict) -> AgentRequest:
    return AgentRequest(
        agent_id="preflight-agent",
        role=AgentRole.WORKER,
        objective="execute the accepted step",
        context={"active_step_contract": contract},
        allowed_capabilities={contract["capability"]},
        budget=AgentBudget(max_turns=2, max_tool_calls=2),
        metadata={"task_id": "task", "run_id": "run", "attempt_id": "attempt"},
    )


def _snapshot() -> ExecutionSnapshot:
    return ExecutionSnapshot(
        task_id="task",
        run_id="run",
        attempt_id="attempt",
        goal="execute the accepted step",
        taskgraph_position="accepted-step",
    )


def test_plan_owned_inputs_reject_hallucinated_values_before_tool_execution(db, tmp_path):
    target = tmp_path / "state.txt"
    target.write_text("before\n", encoding="utf-8")
    registry = ToolRegistry()
    register_workspace_tools(registry, LocalReadOnlyWorkspace(tmp_path), CommandPolicy())
    dispatcher = NativeToolDispatcher(
        db=db,
        registry=registry,
        allowed_capabilities={"workspace.edit_lines"},
        allowed_side_effect_capabilities={"workspace.edit_lines"},
    )
    contract = {
        "plan_id": "plan",
        "plan_version": "P-0.1",
        "step_id": "accepted-step",
        "capability": "workspace.edit_lines",
        "inputs": {"path": "state.txt", "old_string": "before\n", "new_string": "after\n"},
        "success_criteria": [],
        "expected_effects": {},
    }

    result = asyncio.run(
        dispatcher.dispatch(
            ProviderToolCall(
                id="call-mismatch",
                name="workspace.edit_lines",
                arguments={"path": "state.txt", "old_string": "wrong\n", "new_string": "after\n"},
            ),
            _request(contract),
            _snapshot(),
        )
    )

    assert result["status"] == "FAILURE"
    assert result["error_type"] == "PLAN_STEP_ARGUMENTS_MISMATCH"
    assert target.read_text(encoding="utf-8") == "before\n"


def test_edit_lines_persists_before_after_digests_and_observes_mutation(db, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "state.txt").write_text("before\nsecond\n", encoding="utf-8")
    workspace = StagedWorkspace.create(source, tmp_path / "stage")
    target = workspace.root / "state.txt"
    registry = ToolRegistry()
    register_staged_workspace_tools(registry, workspace, CommandPolicy())
    dispatcher = NativeToolDispatcher(
        db=db,
        registry=registry,
        allowed_capabilities={"workspace.edit_lines"},
        allowed_side_effect_capabilities={"workspace.edit_lines"},
    )
    expected_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    contract = {
        "step_id": "accepted-step",
        "capability": "workspace.edit_lines",
        "inputs": {
            "path": "state.txt",
            "start_line": 1,
            "end_line": 1,
            "new_lines": ["after"],
            "expected_sha256": expected_sha,
        },
    }
    result = asyncio.run(
        dispatcher.dispatch(
            ProviderToolCall(
                id="call-edit",
                name="workspace.edit_lines",
                arguments={
                    "path": "state.txt",
                    "start_line": 1,
                    "end_line": 1,
                    "new_lines": ["after"],
                    "expected_sha256": expected_sha,
                },
            ),
            _request(contract),
            _snapshot(),
        )
    )

    assert result["status"] == "SUCCESS"
    assert result["observed_mutation"] is True
    assert result["bounded_output"]["before_sha256"] != result["bounded_output"]["after_sha256"]


def test_safe_edit_projection_hashes_old_and_new_strings_without_raw_content():
    projection = _safe_tool_arguments(
        "workspace.edit_lines",
        {"path": "state.txt", "old_string": "secret-old", "new_string": "secret-new"},
    )
    assert projection["old_string_sha256"]
    assert projection["new_string_sha256"]
    assert "secret-old" not in str(projection)
    assert "secret-new" not in str(projection)


def test_changed_unknown_with_confirmed_mutation_is_not_no_progress():
    controller = RecoveryController(
        task_id="task",
        run_id="run",
        attempt_id="attempt",
        max_no_progress=1,
    )
    decision, progress = controller.observe(
        before_state={"route": "local"},
        after_state={"route": "alternate"},
        action={"capability": "workspace.edit_lines", "args_sha256": "a"},
        observation={"observed_mutation": True},
    )

    assert progress.status is ProgressStatus.CHANGED_UNKNOWN
    assert decision is RecoveryDecision.CONTINUE_LOCAL_REPAIR
    assert controller.no_progress_count == 0


def test_progress_evidence_distinguishes_action_observation_from_external_state():
    controller = RecoveryController(
        task_id="task",
        run_id="run",
        attempt_id="attempt",
        expected_effects={"route": "alternate"},
    )
    _, action_progress = controller.observe(
        before_state={"route": "local"},
        after_state={"route": "alternate"},
        action={"capability": "workspace.edit", "args_sha256": "a"},
        observation={"bounded_output": {"route": "alternate"}},
    )
    _, external_progress = controller.observe(
        before_state={"route": "local"},
        after_state={"route": "local"},
        action={"capability": "workspace.edit", "args_sha256": "b"},
        observation={"external_observed_state": {"route": "alternate"}},
    )

    assert action_progress.evidence["progress_source"] == "ACTION_OBSERVATION"
    assert action_progress.evidence["effect_progress_authoritative"] is False
    assert external_progress.evidence["progress_source"] == "EXTERNAL_OBSERVATION"
    assert external_progress.evidence["effect_progress_authoritative"] is True


def test_strategy_epoch_resets_convergence_but_preserves_signal_history():
    controller = RecoveryController(
        task_id="task",
        run_id="run",
        attempt_id="attempt",
    )
    old_epoch = controller.strategy_epoch
    signal = controller.emit_signal(
        "REPAIR_NO_PROGRESS",
        controller.budget_failure_progress(),
    )
    controller.no_progress_count = 2

    new_epoch = controller.begin_strategy_epoch("replanned-step", {"route": "alternate"})

    assert new_epoch == old_epoch + 1
    assert controller.step_id == "replanned-step"
    assert controller.no_progress_count == 0
    assert controller.signals[0] is signal
    assert controller.evaluator.expected_effects == {"route": "alternate"}


def test_fixture_observation_cannot_be_overridden_by_runtime_claim():
    task = {"expected_observable_effects": {"route": "alternate"}}
    outcome = ExecutionOutcome(
        observed_state={
            "fixture_observations": {"route": "local"},
            "route": "alternate",
        }
    )

    view = _validator_observation_view(outcome, task)
    result = ExternalObservableValidator().validate(task, None, outcome)

    assert view["route"] == "local"
    assert result.acceptance_status == "REJECTED"


def test_verified_work_fingerprint_includes_acceptance_and_authority_contract():
    base = PlanStep(
        id="step",
        title="apply route",
        objective="Apply the selected route.",
        capability="workspace.edit_lines",
        inputs={"path": "state.json"},
        success_criteria=["route is local"],
        expected_effects={"route": "local"},
    )
    changed = base.model_copy(
        update={
            "success_criteria": ["route is alternate"],
            "expected_effects": {"route": "alternate"},
        }
    )

    assert compute_step_semantic_fingerprint(base) != compute_step_semantic_fingerprint(
        changed
    )
