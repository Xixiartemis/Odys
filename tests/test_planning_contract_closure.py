"""Tests for planning/platform contract closure (ODYS-PHASE2-NATIVE-RUNTIME-PLANNING-CONTRACT-CLOSURE-03).

Validates that PlanExecutionService routes plan-step capability execution through
ToolContract, platform capabilities traverse the contract boundary, and
CapabilitySpec-only tools are not planner-visible.
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from lhas.capability_registry import (
    CapabilityDefinition,
    CapabilityRegistry,
    CapabilityRuntimeContext,
    RuntimePlatform,
    default_capabilities,
)
from lhas.domain.enums import EventType, ExecutionStatus
from lhas.domain.models import Project, new_id
from lhas.persistence.database import Database
from lhas.persistence.event_store import EventStore
from lhas.persistence.repositories import ProjectRepository
from lhas.planning.models import CapabilitySpec, Goal, PlanMode, PlanStatus, PlanStep, Plan
from lhas.planning.service import PlanExecutionService, _ToolExecutor
from lhas.tools.contract import ToolContract, ToolErrorCode
from lhas.tools.fakes import FakeTool
from lhas.tools.invocation import build_contract_for_registry, invoke_via_contract
from lhas.tools.protocol import ToolRequest, ToolResult, ToolResultStatus
from lhas.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _InstrumentedTool(FakeTool):
    """FakeTool that records how many times execute() was called."""
    def __init__(self, capability, handler=None):
        super().__init__(capability, handler)
        self.execute_count = 0

    async def execute(self, request: ToolRequest) -> ToolResult:
        self.execute_count += 1
        return await super().execute(request)


class _FailingOutputTool(FakeTool):
    """Tool that returns SUCCESS but with output that fails output schema validation."""
    async def execute(self, request: ToolRequest) -> ToolResult:
        return ToolResult(status=ToolResultStatus.SUCCESS, output={"bad_field": 42})


class _EvidenceTrackingTool(FakeTool):
    """Tool that records the request identity for evidence verification."""
    def __init__(self, capability, handler=None):
        super().__init__(capability, handler)
        self.last_request = None

    async def execute(self, request: ToolRequest) -> ToolResult:
        self.last_request = request
        return await super().execute(request)


class _DeterministicPlanner:
    """Minimal planner that returns a single-step plan for the given capability."""
    async def create_plan(self, *, goal: Goal, capabilities: list[CapabilitySpec], context=None) -> Plan:
        cap_name = goal.allowed_capabilities[0] if goal.allowed_capabilities else capabilities[0].name
        step = PlanStep(
            title=f"Execute {cap_name}",
            objective=goal.objective,
            capability=cap_name,
            depends_on=[],
            expected_output="bounded result",
            success_criteria=list(goal.success_criteria) or ["deterministic step output is non-empty"],
            inputs={"goal": goal.objective},
        )
        return Plan(goal_id=goal.id, mode=PlanMode.LINEAR, status="READY", steps=[step], version="P-1.0")


class _MultiStepPlanner:
    """Planner that returns a plan with steps for all allowed capabilities."""
    async def create_plan(self, *, goal: Goal, capabilities: list[CapabilitySpec], context=None) -> Plan:
        steps = []
        previous = None
        for cap in goal.allowed_capabilities:
            step = PlanStep(
                title=f"Execute {cap}",
                objective=f"{cap} for: {goal.objective}",
                capability=cap,
                depends_on=[previous] if previous else [],
                expected_output="bounded result",
                success_criteria=list(goal.success_criteria) or ["deterministic step output is non-empty"],
                inputs={"goal": goal.objective},
            )
            steps.append(step)
            previous = step.id
        return Plan(goal_id=goal.id, mode=PlanMode.SIMPLE_DEPENDENCY, status="READY", steps=steps, version="P-1.0")


def _make_db(tmp_path):
    db = Database(tmp_path / "test.db")
    db.init_db()
    return db


def _make_project(db):
    return ProjectRepository(db).create(Project(name="contract-test"))


def _explicit_definition(capability_id: str) -> CapabilityDefinition:
    return CapabilityDefinition(
        id=capability_id,
        name=capability_id,
        description=f"Explicit capability {capability_id}",
        category="test",
        version="v1",
        input_schema={"type": "object", "additionalProperties": True},
        output_schema={"type": "object"},
        platforms=(RuntimePlatform.WINDOWS, RuntimePlatform.LINUX, RuntimePlatform.MACOS),
        permissions=("test.execute",),
        risk_level="LOW",
        workspace_scope="SOURCE_WORKSPACE",
        timeout_seconds=30.0,
        retryable=True,
        preferred_tool=capability_id,
        source="explicit-test",
        evidence_type="DETERMINISTIC_TOOL_RESULT",
    )


# ---------------------------------------------------------------------------
# Test 1-3: platform.prepare/delegate/finalize through PlanExecutionService traverses ToolContract
# ---------------------------------------------------------------------------

def test_platform_prepare_through_contract(tmp_path):
    """Test 1: platform.prepare through PlanExecutionService traverses ToolContract."""
    db = _make_db(tmp_path)
    project = _make_project(db)

    worker = _InstrumentedTool(CapabilitySpec(name="platform.prepare", description="prepare"))
    reg = ToolRegistry()
    reg.register(worker)

    cap_reg, contract = build_contract_for_registry(reg)
    invoked = []

    original_invoke = contract.invoke

    async def tracking_invoke(request, ctx):
        invoked.append(request.capability_id)
        return await original_invoke(request, ctx)

    contract.invoke = tracking_invoke

    goal = Goal(
        project_id=project.id, objective="test prepare",
        allowed_capabilities=["platform.prepare"],
        metadata={"plan_steps": ["platform.prepare"]},
    )
    svc = PlanExecutionService(db, _DeterministicPlanner(), reg, tool_contract=contract, capability_registry=cap_reg)
    plan = asyncio.run(svc.execute_goal(goal))

    assert plan.status == PlanStatus.COMPLETED
    assert "platform.prepare" in invoked


def test_platform_delegate_through_contract(tmp_path):
    """Test 2: platform.delegate through PlanExecutionService traverses ToolContract."""
    db = _make_db(tmp_path)
    project = _make_project(db)

    delegate_tool = _InstrumentedTool(CapabilitySpec(name="platform.delegate", description="delegate"))
    reg = ToolRegistry()
    reg.register(delegate_tool)

    cap_reg, contract = build_contract_for_registry(reg)
    invoked = []

    original_invoke = contract.invoke

    async def tracking_invoke(request, ctx):
        invoked.append(request.capability_id)
        return await original_invoke(request, ctx)

    contract.invoke = tracking_invoke

    goal = Goal(
        project_id=project.id, objective="test delegate",
        allowed_capabilities=["platform.delegate"],
        metadata={"plan_steps": ["platform.delegate"]},
    )
    svc = PlanExecutionService(db, _DeterministicPlanner(), reg, tool_contract=contract, capability_registry=cap_reg)
    plan = asyncio.run(svc.execute_goal(goal))

    assert plan.status == PlanStatus.COMPLETED
    assert "platform.delegate" in invoked


def test_platform_finalize_through_contract(tmp_path):
    """Test 3: platform.finalize through PlanExecutionService traverses ToolContract."""
    db = _make_db(tmp_path)
    project = _make_project(db)

    finalize_tool = _InstrumentedTool(CapabilitySpec(name="platform.finalize", description="finalize"))
    reg = ToolRegistry()
    reg.register(finalize_tool)

    cap_reg, contract = build_contract_for_registry(reg)
    invoked = []

    original_invoke = contract.invoke

    async def tracking_invoke(request, ctx):
        invoked.append(request.capability_id)
        return await original_invoke(request, ctx)

    contract.invoke = tracking_invoke

    goal = Goal(
        project_id=project.id, objective="test finalize",
        allowed_capabilities=["platform.finalize"],
        metadata={"plan_steps": ["platform.finalize"]},
    )
    svc = PlanExecutionService(db, _DeterministicPlanner(), reg, tool_contract=contract, capability_registry=cap_reg)
    plan = asyncio.run(svc.execute_goal(goal))

    assert plan.status == PlanStatus.COMPLETED
    assert "platform.finalize" in invoked


# ---------------------------------------------------------------------------
# Test 4: Invalid platform arguments → concrete backend execute count = 0
# ---------------------------------------------------------------------------

def test_invalid_arguments_no_backend_execute(tmp_path):
    """Test 4: Invalid platform arguments: concrete backend execute count = 0."""
    db = _make_db(tmp_path)
    project = _make_project(db)

    tool = _InstrumentedTool(CapabilitySpec(name="platform.prepare", description="prepare"))
    reg = ToolRegistry()
    reg.register(tool)

    cap_reg, contract = build_contract_for_registry(reg)

    # The platform.prepare CapabilityDefinition requires {"goal": <string>}.
    # Sending {"invalid": 123} should be rejected by contract validation
    # and the tool's execute() should never be called.
    goal = Goal(
        project_id=project.id, objective="test invalid args",
        allowed_capabilities=["platform.prepare"],
        metadata={"plan_steps": ["platform.prepare"]},
    )

    # Use a planner that sets bad inputs
    class _BadInputPlanner:
        async def create_plan(self, *, goal, capabilities, context=None):
            step = PlanStep(
                title="Bad input step", objective=goal.objective,
                capability="platform.prepare", depends_on=[],
                expected_output="bounded result",
                success_criteria=["ok"],
                inputs={"invalid": 123},  # Bad: goal field missing
            )
            return Plan(goal_id=goal.id, mode=PlanMode.LINEAR, status="READY", steps=[step], version="P-1.0")

    svc = PlanExecutionService(db, _BadInputPlanner(), reg, tool_contract=contract, capability_registry=cap_reg)
    plan = asyncio.run(svc.execute_goal(goal))

    assert plan.status == PlanStatus.FAILED
    assert tool.execute_count == 0


# ---------------------------------------------------------------------------
# Test 5: Output schema mismatch → plan-step execution observes FAILURE
# ---------------------------------------------------------------------------

def test_output_schema_mismatch_observes_failure(tmp_path):
    """Test 5: Output schema mismatch: plan-step execution observes FAILURE."""
    db = _make_db(tmp_path)
    project = _make_project(db)

    # platform.prepare's output_schema is {"type": "object"} which is permissive.
    # Use workspace.read which has a strict output schema.
    from lhas.capability_registry import _WORKSPACE_READ_OUTPUT
    tool = _FailingOutputTool(CapabilitySpec(name="workspace.read", description="read"))
    reg = ToolRegistry()
    reg.register(tool)

    cap_reg, contract = build_contract_for_registry(reg)

    goal = Goal(
        project_id=project.id, objective="test output mismatch",
        allowed_capabilities=["workspace.read"],
        metadata={"plan_steps": ["workspace.read"]},
    )
    svc = PlanExecutionService(db, _DeterministicPlanner(), reg, tool_contract=contract, capability_registry=cap_reg)
    plan = asyncio.run(svc.execute_goal(goal))

    assert plan.status == PlanStatus.FAILED


# ---------------------------------------------------------------------------
# Test 6: Evidence identity survives planning execution
# ---------------------------------------------------------------------------

def test_evidence_identity_survives_planning_execution(tmp_path):
    """Test 6: Evidence identity survives planning execution."""
    db = _make_db(tmp_path)
    project = _make_project(db)

    tool = _EvidenceTrackingTool(CapabilitySpec(name="platform.prepare", description="prepare"))
    reg = ToolRegistry()
    reg.register(tool)

    cap_reg, contract = build_contract_for_registry(reg)

    goal = Goal(
        project_id=project.id, objective="test evidence",
        allowed_capabilities=["platform.prepare"],
        metadata={"plan_steps": ["platform.prepare"]},
    )
    svc = PlanExecutionService(db, _DeterministicPlanner(), reg, tool_contract=contract, capability_registry=cap_reg)
    plan = asyncio.run(svc.execute_goal(goal))

    assert plan.status == PlanStatus.COMPLETED
    assert tool.last_request is not None
    # The tool should have received the request through the contract
    # The contract adds evidence with the correct identity
    assert tool.last_request.capability_id == "platform.prepare"
    assert tool.last_request.tool_name == "platform.prepare"


# ---------------------------------------------------------------------------
# Test 7: COMMAND_NOT_ALLOWED remains preserved where applicable
# ---------------------------------------------------------------------------

def test_command_not_allowed_preserved(tmp_path):
    """Test 7: COMMAND_NOT_ALLOWED remains preserved where applicable."""
    db = _make_db(tmp_path)
    project = _make_project(db)

    # Create a tool that simulates a COMMAND_NOT_ALLOWED error (sync handler)
    def command_blocked(request):
        return ToolResult(
            status=ToolResultStatus.FAILURE,
            error_type=ToolErrorCode.COMMAND_NOT_ALLOWED.value,
            error_message="command not permitted by policy",
        )

    tool = _InstrumentedTool(CapabilitySpec(name="platform.prepare", description="prepare"), command_blocked)
    reg = ToolRegistry()
    reg.register(tool)

    cap_reg, contract = build_contract_for_registry(reg)

    goal = Goal(
        project_id=project.id, objective="test command blocked",
        allowed_capabilities=["platform.prepare"],
        metadata={"plan_steps": ["platform.prepare"]},
    )
    svc = PlanExecutionService(db, _DeterministicPlanner(), reg, tool_contract=contract, capability_registry=cap_reg)
    plan = asyncio.run(svc.execute_goal(goal))

    assert plan.status == PlanStatus.FAILED

    # Check that the event store has a TOOL_CALL_FAILED event with the error type
    events = [e for e in EventStore(db).list_all() if e.event_type == EventType.TOOL_CALL_FAILED]
    assert len(events) >= 1
    payload = events[-1].payload
    assert payload.get("result", {}).get("error_type") == ToolErrorCode.COMMAND_NOT_ALLOWED.value


# ---------------------------------------------------------------------------
# Test 8: CapabilitySpec-only Tool is NOT planner/model visible
# ---------------------------------------------------------------------------

def test_capability_spec_only_tool_not_planner_visible(tmp_path):
    """Test 8: CapabilitySpec-only Tool is not semantic or model visible."""
    db = _make_db(tmp_path)
    project = _make_project(db)

    platform_tool = _InstrumentedTool(CapabilitySpec(name="platform.prepare", description="prepare"))
    secret_tool = _InstrumentedTool(CapabilitySpec(name="secret.backend", description="internal"))

    reg = ToolRegistry()
    reg.register(platform_tool)
    reg.register(secret_tool)

    cap_reg, contract = build_contract_for_registry(reg)

    # A backend CapabilitySpec must never create a semantic definition.
    assert "secret.backend" not in {definition.id for definition in cap_reg.list_all()}

    # The contract must reject the undeclared semantic capability before the
    # concrete backend is reached.
    rejected = asyncio.run(invoke_via_contract(contract, ToolRequest(
        tool_call_id="spec-only", task_id="t", run_id="r", attempt_id="a",
        capability_id="secret.backend", tool_name="secret.backend", arguments={},
    )))
    assert rejected.error_type == ToolErrorCode.CAPABILITY_UNAVAILABLE.value
    assert secret_tool.execute_count == 0

    # Model-facing exposure is also fail-closed for the undeclared backend.
    from lhas.native.tools import NativeToolDispatcher
    dispatcher = NativeToolDispatcher(
        db=db, registry=reg, allowed_capabilities={"secret.backend"},
        allowed_side_effect_capabilities=set(), capability_registry=cap_reg,
        tool_contract=contract,
    )
    assert dispatcher.tool_schemas() == []

    # The planner sees capabilities from _planner_capabilities()
    # secret.backend should NOT be visible because it has no CapabilityDefinition
    planner_caps = []

    class _CapturingPlanner:
        async def create_plan(self, *, goal, capabilities, context=None):
            planner_caps.extend(capabilities)
            step = PlanStep(
                title="test", objective=goal.objective,
                capability=capabilities[0].name, depends_on=[],
                expected_output="result", success_criteria=["ok"],
                inputs={"goal": goal.objective},
            )
            return Plan(goal_id=goal.id, mode=PlanMode.LINEAR, status="READY", steps=[step], version="P-1.0")

    goal = Goal(
        project_id=project.id, objective="test visibility",
        allowed_capabilities=["platform.prepare"],
    )
    svc = PlanExecutionService(
        db, _CapturingPlanner(), reg,
        tool_contract=contract, capability_registry=cap_reg,
    )
    plan = asyncio.run(svc.execute_goal(goal))

    # secret.backend should NOT appear in the capabilities passed to the planner
    cap_names = [c.name for c in planner_caps]
    assert "platform.prepare" in cap_names
    assert "secret.backend" not in cap_names


# ---------------------------------------------------------------------------
# Test 9: Explicit CapabilityDefinition projection IS planner visible
# ---------------------------------------------------------------------------

def test_explicit_definition_projection_visible(tmp_path):
    """Test 9: Explicit CapabilityDefinition projection IS planner visible."""
    db = _make_db(tmp_path)
    project = _make_project(db)

    explicit_tool = _InstrumentedTool(CapabilitySpec(name="explicit.backend", description="backend"))
    reg = ToolRegistry()
    reg.register(explicit_tool)

    cap_reg, contract = build_contract_for_registry(reg, definitions=[_explicit_definition("explicit.backend")])

    planner_caps = []

    class _CapturingPlanner:
        async def create_plan(self, *, goal, capabilities, context=None):
            planner_caps.extend(capabilities)
            step = PlanStep(
                title="test", objective=goal.objective,
                capability=capabilities[0].name, depends_on=[],
                expected_output="result", success_criteria=["ok"],
                inputs={"goal": goal.objective},
            )
            return Plan(goal_id=goal.id, mode=PlanMode.LINEAR, status="READY", steps=[step], version="P-1.0")

    svc = PlanExecutionService(
        db, _CapturingPlanner(), reg,
        tool_contract=contract, capability_registry=cap_reg,
    )
    goal = Goal(
        project_id=project.id, objective="test visibility",
        allowed_capabilities=["explicit.backend"],
    )
    plan = asyncio.run(svc.execute_goal(goal))

    cap_names = [c.name for c in planner_caps]
    assert "explicit.backend" in cap_names

    from lhas.native.tools import NativeToolDispatcher
    dispatcher = NativeToolDispatcher(
        db=db, registry=reg, allowed_capabilities={"explicit.backend"},
        allowed_side_effect_capabilities=set(), capability_registry=cap_reg,
        tool_contract=contract,
    )
    assert [item["function"]["name"] for item in dispatcher.tool_schemas()] == ["explicit.backend"]


# ---------------------------------------------------------------------------
# Test 10: No _ToolExecutor direct Tool.execute bypass remains
# ---------------------------------------------------------------------------

def test_no_direct_tool_execute_bypass(tmp_path):
    """Test 10: No _ToolExecutor direct Tool.execute bypass remains when contract is provided."""
    db = _make_db(tmp_path)
    project = _make_project(db)

    tool = _InstrumentedTool(CapabilitySpec(name="platform.prepare", description="prepare"))
    reg = ToolRegistry()
    reg.register(tool)

    cap_reg, contract = build_contract_for_registry(reg)

    # Track whether contract.invoke is called (not tool.execute directly)
    contract_invoked = []
    original_invoke = contract.invoke

    async def tracking_invoke(request, ctx):
        contract_invoked.append(True)
        return await original_invoke(request, ctx)

    contract.invoke = tracking_invoke

    goal = Goal(
        project_id=project.id, objective="test no bypass",
        allowed_capabilities=["platform.prepare"],
        metadata={"plan_steps": ["platform.prepare"]},
    )
    svc = PlanExecutionService(db, _DeterministicPlanner(), reg, tool_contract=contract, capability_registry=cap_reg)
    plan = asyncio.run(svc.execute_goal(goal))

    assert plan.status == PlanStatus.COMPLETED
    # The contract was invoked (not a direct bypass)
    assert len(contract_invoked) >= 1
    # The tool was also invoked (but via contract)
    assert tool.execute_count >= 1


# ---------------------------------------------------------------------------
# Test 11: NativeToolDispatcher remains unchanged and still uses ToolContract
# ---------------------------------------------------------------------------

def test_native_tool_dispatcher_uses_contract(tmp_path):
    """Test 11: NativeToolDispatcher remains unchanged and still uses ToolContract."""
    from lhas.native.tools import NativeToolDispatcher

    tool = _InstrumentedTool(CapabilitySpec(name="platform.prepare", description="prepare"))
    reg = ToolRegistry()
    reg.register(tool)

    cap_reg, contract = build_contract_for_registry(reg)
    db = _make_db(tmp_path)

    dispatcher = NativeToolDispatcher(
        db=db,
        registry=reg,
        allowed_capabilities={"platform.prepare"},
        allowed_side_effect_capabilities=set(),
        capability_registry=cap_reg,
        tool_contract=contract,
    )

    # NativeToolDispatcher should use ToolContract internally
    assert dispatcher.tool_contract is contract
    assert dispatcher.capability_registry is cap_reg


# ---------------------------------------------------------------------------
# Test 12: Tool SUCCESS alone does not bypass existing completion authority
# ---------------------------------------------------------------------------

def test_tool_success_does_not_bypass_completion_authority(tmp_path):
    """Test 12: Tool SUCCESS alone does not bypass existing completion authority."""
    db = _make_db(tmp_path)
    project = _make_project(db)

    tool = _InstrumentedTool(CapabilitySpec(name="platform.prepare", description="prepare"))
    reg = ToolRegistry()
    reg.register(tool)

    cap_reg, contract = build_contract_for_registry(reg)

    goal = Goal(
        project_id=project.id, objective="test completion authority",
        allowed_capabilities=["platform.prepare"],
        metadata={"plan_steps": ["platform.prepare"]},
    )
    svc = PlanExecutionService(db, _DeterministicPlanner(), reg, tool_contract=contract, capability_registry=cap_reg)
    plan = asyncio.run(svc.execute_goal(goal))

    # Plan should complete via the planning service's completion logic,
    # not just because the tool returned SUCCESS
    assert plan.status == PlanStatus.COMPLETED
    assert plan.steps[0].status.value == "COMPLETED"

    # Verify planning events were emitted (not bypassed)
    event_types = [e.event_type for e in EventStore(db).list_all()]
    assert EventType.PLAN_STEP_STARTED in event_types
    assert EventType.PLAN_STEP_COMPLETED in event_types
    assert EventType.PLAN_COMPLETED in event_types
    assert EventType.TOOL_CALL_STARTED in event_types
    assert EventType.TOOL_CALL_COMPLETED in event_types
