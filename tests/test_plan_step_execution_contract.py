"""Provider-free proof of the durable PlanStep -> native agent contract."""

from __future__ import annotations

import asyncio

from lhas.agent.models import AgentResult, AgentRole, AgentStatus, AgentRequest
from lhas.domain.enums import ExecutionStatus
from lhas.executors.protocol import ExecutionRequest, ExecutionResult
from lhas.native.context import NativeContextAssembler
from lhas.native.models import ExecutionSnapshot
from lhas.planning.models import Plan, PlanStep, PlanStatus
from lhas.planning.service import _TaskGraphAgentExecutor
from evals.reliability.runtime_factory.recovery import _KernelTaskExecutor


def _step() -> PlanStep:
    return PlanStep(
        id="accepted-step",
        title="apply alternate route",
        objective="Apply the accepted alternate route.",
        capability="workspace.edit_lines",
        inputs={
            "path": "state.json",
            "old_string": '"route":"local"',
            "new_string": '"route":"alternate"',
        },
        success_criteria=["route is alternate"],
        expected_effects={"route": "alternate"},
    )


def test_taskgraph_projection_is_bounded_and_narrows_task_capabilities():
    captured = []

    class Executor:
        async def execute(self, request):
            captured.append(request)
            return ExecutionResult(status=ExecutionStatus.SUCCESS, output="ok")

        async def cancel(self, run_id):
            return None

        async def status(self, run_id):
            return {}

    step = _step()
    plan = Plan(
        id="accepted-plan",
        goal_id="goal",
        status=PlanStatus.READY,
        steps=[step],
    )
    request = ExecutionRequest(
        task_id="task",
        run_id="run",
        attempt_id="attempt",
        attempt_number=1,
        task={
            "required_capabilities": ["workspace.edit", "workspace.edit_lines", "workspace.list"],
            "acceptance_criteria": ["task-wide fallback must not win"],
        },
        context={"allowed_capabilities": ["workspace.edit", "workspace.list"]},
        metadata={},
    )

    result = asyncio.run(_TaskGraphAgentExecutor(Executor(), plan, step).execute(request))

    assert result.status is ExecutionStatus.SUCCESS
    projected = captured[0].context["active_step_contract"]
    assert projected == {
        "plan_id": "accepted-plan",
        "plan_version": "P-0.1",
        "step_id": "accepted-step",
        "objective": "Apply the accepted alternate route.",
        "capability": "workspace.edit_lines",
        "inputs": step.inputs,
        "success_criteria": ["route is alternate"],
        "expected_effects": {"route": "alternate"},
    }
    assert captured[0].context["allowed_capabilities"] == ["workspace.edit_lines"]
    assert captured[0].context["taskgraph"]["active_step_contract"] == projected
    assert captured[0].context["execution_contract_telemetry"] == {
        "accepted_plan_id": "accepted-plan",
        "accepted_plan_version": "P-0.1",
        "accepted_plan_step_id": "accepted-step",
        "accepted_plan_step_capability": "workspace.edit_lines",
        "accepted_plan_step_inputs_sha256": "aef36c2a8c07da5d9eb8416807160ca1427a9a3c64a60c7571755fba0e99bc1a",
        "active_execution_plan_id": "accepted-plan",
        "active_execution_plan_version": "P-0.1",
        "active_execution_step_id": "accepted-step",
        "active_execution_capability": "workspace.edit_lines",
        "active_execution_inputs_sha256": "aef36c2a8c07da5d9eb8416807160ca1427a9a3c64a60c7571755fba0e99bc1a",
    }


def test_kernel_active_contract_blocks_unrelated_task_capability():
    captured = []

    class Kernel:
        async def run(self, request, *, execution_control=None):
            captured.append(request)
            return AgentResult(status=AgentStatus.COMPLETED, final_output="done")

    request = ExecutionRequest(
        task_id="task",
        run_id="run",
        attempt_id="attempt",
        attempt_number=1,
        task={
            "objective": "broad task objective",
            "required_capabilities": ["workspace.edit_lines", "workspace.list"],
            "max_turns": 1,
            "max_model_calls": 1,
        },
        context={
            "active_step_contract": {
                "step_id": "accepted-step",
                "objective": "Apply the accepted alternate route.",
                "capability": "workspace.edit_lines",
                "inputs": {"path": "state.json"},
                "success_criteria": ["route is alternate"],
                "expected_effects": {"route": "alternate"},
            },
            "allowed_capabilities": ["workspace.edit", "workspace.list"],
        },
        metadata={},
    )

    result = asyncio.run(_KernelTaskExecutor(Kernel()).execute(request))

    assert result.status is ExecutionStatus.SUCCESS
    assert captured[0].allowed_capabilities == {"workspace.edit_lines"}
    assert captured[0].objective == "Apply the accepted alternate route."
    assert result.artifacts["execution_contract_telemetry"] == {
        "accepted_plan_id": "",
        "accepted_plan_version": "",
        "accepted_plan_step_id": "accepted-step",
        "accepted_plan_step_capability": "workspace.edit_lines",
        "accepted_plan_step_inputs_sha256": "6fd3710af5dbc771bc5052cd6228e45844880b3abf1c3ffb1ac970ba00bc4f54",
        "active_execution_plan_id": "",
        "active_execution_plan_version": "",
        "active_execution_step_id": "accepted-step",
        "active_execution_capability": "workspace.edit_lines",
        "active_execution_inputs_sha256": "6fd3710af5dbc771bc5052cd6228e45844880b3abf1c3ffb1ac970ba00bc4f54",
    }


def test_native_context_exposes_exact_active_step_inputs_to_model():
    request = AgentRequest(
        agent_id="agent",
        role=AgentRole.WORKER,
        objective="broad objective",
        context={
            "taskgraph": {"plan_id": "plan", "active_node": "accepted-step"},
            "active_step_contract": {
                "step_id": "accepted-step",
                "objective": "Apply the accepted alternate route.",
                "capability": "workspace.edit_lines",
                "inputs": {"path": "state.json", "new_string": "alternate"},
                "success_criteria": ["route is alternate"],
                "expected_effects": {"route": "alternate"},
            },
            "acceptance_criteria": ["route is alternate"],
        },
    )
    snapshot = ExecutionSnapshot(
        task_id="task",
        run_id="run",
        attempt_id="attempt",
        goal="broad objective",
        taskgraph_position="accepted-step",
    )

    model_context = NativeContextAssembler().build(request, snapshot)

    assert model_context.sections["active_step_contract"]["capability"] == "workspace.edit_lines"
    assert model_context.sections["active_step_contract"]["inputs"] == {
        "path": "state.json",
        "new_string": "alternate",
    }
    assert model_context.sections["taskgraph"]["active_step_contract"] == model_context.sections["active_step_contract"]
