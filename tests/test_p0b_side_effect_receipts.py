"""P0-B receipt-backed side-effect semantics.

These tests exercise the runtime receipt service directly and through the
native dispatcher.  They intentionally use deterministic local fakes; no
provider or benchmark runner is involved.
"""

from __future__ import annotations

import pytest

from lhas.agent.models import AgentBudget, AgentRequest, AgentRole
from lhas.capability_registry import CapabilityDefinition, CapabilityRegistry, CapabilityRuntimeContext, RuntimePlatform
from lhas.domain.enums import EventType
from lhas.execution_control import ExecutionControlError, ExecutionControlToken
from lhas.native.models import ExecutionSnapshot, ProviderToolCall
from lhas.native.tools import NativeToolDispatcher
from lhas.persistence.database import Database
from lhas.persistence.event_store import EventStore
from lhas.planning.models import CapabilitySpec
from lhas.side_effects import (
    DeterministicIdempotentExternalFake,
    EffectClass,
    ReplayDecision,
    ReceiptStatus,
    SideEffectReceiptManager,
)
from lhas.tools.protocol import ToolRequest, ToolResult, ToolResultStatus
from lhas.tools.contract import ToolContract
from lhas.tools.registry import ToolRegistry
from lhas.workspace import CommandPolicy, StagedWorkspace, register_staged_workspace_tools


def _begin(manager: SideEffectReceiptManager, *, effect_class: EffectClass, **extra):
    return manager.begin(
        operation_id=extra.pop("operation_id", "operation-1"),
        task_id=extra.pop("task_id", "task-1"),
        run_id=extra.pop("run_id", "run-1"),
        attempt_id=extra.pop("attempt_id", "attempt-1"),
        step_id=extra.pop("step_id", "step-1"),
        tool_call_id=extra.pop("tool_call_id", "tool-call-1"),
        tool_name=extra.pop("tool_name", "workspace.edit"),
        effect_class=effect_class,
        target=extra.pop("target", {"path": "out.txt"}),
        request=extra.pop("request", {"new_text": "updated"}),
        **extra,
    )


def test_receipt_reopen_after_commit_before_observation_is_not_replayed(tmp_path):
    db_path = tmp_path / "receipt.sqlite"
    db = Database(db_path)
    db.init_db()
    manager = SideEffectReceiptManager(db)
    receipt = _begin(
        manager,
        effect_class=EffectClass.LOCAL_DURABLE,
        workspace_before_digest="a" * 64,
    )
    manager.mark_dispatch_started(receipt.receipt_id)
    manager.mark_committed(
        receipt.receipt_id,
        workspace_after_digest="b" * 64,
        result={"bytes_written": 7},
    )
    # This is the crash window: commit is durable, observation is absent.
    db.close()

    reopened = Database(db_path)
    reopened.init_db()
    reopened_manager = SideEffectReceiptManager(reopened)
    persisted = reopened_manager.receipts.get(receipt.receipt_id)
    assert persisted is not None
    assert persisted.status is ReceiptStatus.COMMITTED
    assert persisted.run_id == "run-1"
    assert persisted.attempt_id == "attempt-1"
    assert persisted.step_id == "step-1"
    assert persisted.tool_call_id == "tool-call-1"
    assert reopened_manager.replay_decision(receipt.receipt_id) is ReplayDecision.REUSE_COMMITTED
    event_types = [event.event_type for event in EventStore(reopened).list_for_attempt("attempt-1")]
    assert EventType.SIDE_EFFECT_COMMITTED in event_types
    assert EventType.SIDE_EFFECT_OBSERVATION_RECEIVED not in event_types
    reopened.close()


def test_local_dispatch_without_commit_reconciles_as_no_effect(db):
    manager = SideEffectReceiptManager(db)
    receipt = _begin(
        manager,
        effect_class=EffectClass.LOCAL_REVERSIBLE,
        workspace_before_digest="a" * 64,
    )
    manager.mark_dispatch_started(receipt.receipt_id)
    reconciled = manager.reconcile(receipt.receipt_id, observed_digest="a" * 64)
    assert reconciled.status is ReceiptStatus.RECONCILED
    assert reconciled.workspace_after_digest is None
    assert manager.replay_decision(receipt.receipt_id) is ReplayDecision.SAFE_TO_RETRY


def test_local_reopen_reconciles_changed_workspace_as_committed(db):
    manager = SideEffectReceiptManager(db)
    receipt = _begin(
        manager,
        effect_class=EffectClass.LOCAL_DURABLE,
        workspace_before_digest="a" * 64,
    )
    manager.mark_dispatch_started(receipt.receipt_id)
    reconciled = manager.reconcile(receipt.receipt_id, observed_digest="b" * 64)
    assert reconciled.status is ReceiptStatus.COMMITTED
    assert reconciled.workspace_before_digest == "a" * 64
    assert reconciled.workspace_after_digest == "b" * 64
    assert manager.replay_decision(receipt.receipt_id) is ReplayDecision.REUSE_COMMITTED


def test_idempotent_external_fake_has_one_effect_for_repeated_requests(db):
    fake = DeterministicIdempotentExternalFake()
    first = fake.apply(idempotency_key="stable-key", target="ticket/7", payload={"state": "open"})
    second = fake.apply(idempotency_key="stable-key", target="ticket/7", payload={"state": "open"})
    assert first == second
    assert first["operation_id"] == second["operation_id"]
    assert fake.request_count == 2
    assert fake.effect_count == 1

    manager = SideEffectReceiptManager(db)
    for ordinal in (1, 2):
        receipt = _begin(
            manager,
            effect_class=EffectClass.EXTERNAL_IDEMPOTENT,
            operation_id=f"operation-{ordinal}",
            tool_name="external.update",
            idempotency_key="stable-key",
        )
        manager.mark_dispatch_started(receipt.receipt_id)
        manager.mark_committed(receipt.receipt_id, result=first, external_resource_id=first["resource_id"])
    assert manager.replay_decision(receipt.receipt_id) is ReplayDecision.REUSE_COMMITTED


def test_external_receipt_lookup_reconciles_existing_operation(db):
    fake = DeterministicIdempotentExternalFake()
    result = fake.apply(idempotency_key="receipt-key", target="ticket/9", payload={"state": "closed"})
    manager = SideEffectReceiptManager(db)
    receipt = _begin(
        manager,
        effect_class=EffectClass.EXTERNAL_RECEIPT,
        tool_name="external.update",
        idempotency_key="receipt-key",
    )
    manager.mark_dispatch_started(receipt.receipt_id)
    reconciled = manager.reconcile(
        receipt.receipt_id,
        lookup=lambda current: fake.lookup(current.idempotency_key or ""),
    )
    assert reconciled.status is ReceiptStatus.COMMITTED
    assert reconciled.external_resource_id == result["resource_id"]
    assert manager.replay_decision(receipt.receipt_id) is ReplayDecision.REUSE_COMMITTED


def test_unverifiable_external_commit_state_is_unknown_and_fail_closed(db):
    manager = SideEffectReceiptManager(db)
    receipt = _begin(manager, effect_class=EffectClass.EXTERNAL_UNVERIFIABLE, tool_name="external.charge")
    manager.mark_dispatch_started(receipt.receipt_id)
    reconciled = manager.reconcile(receipt.receipt_id)
    assert reconciled.status is ReceiptStatus.COMMIT_STATE_UNKNOWN
    assert reconciled.replay_safe is False
    assert manager.replay_decision(receipt.receipt_id) is ReplayDecision.REQUIRE_HUMAN


def test_cancel_after_commit_preserves_receipt_and_blocks_workflow_replay(db):
    manager = SideEffectReceiptManager(db)
    receipt = _begin(
        manager,
        effect_class=EffectClass.LOCAL_REVERSIBLE,
        workspace_before_digest="a" * 64,
    )
    manager.mark_dispatch_started(receipt.receipt_id)
    manager.mark_committed(receipt.receipt_id, workspace_after_digest="b" * 64)

    control = ExecutionControlToken("run-1", attempt_id="attempt-1", timeout_seconds=30)
    control.cancel("USER_CANCEL", source="test-after-commit")
    with pytest.raises(ExecutionControlError):
        control.check()

    persisted = manager.receipts.get(receipt.receipt_id)
    assert persisted is not None
    assert persisted.status is ReceiptStatus.COMMITTED
    assert manager.replay_decision(receipt.receipt_id) is ReplayDecision.REUSE_COMMITTED


def test_deadline_after_commit_preserves_receipt(db):
    manager = SideEffectReceiptManager(db)
    receipt = _begin(manager, effect_class=EffectClass.LOCAL_DURABLE)
    manager.mark_dispatch_started(receipt.receipt_id)
    manager.mark_committed(receipt.receipt_id)
    control = ExecutionControlToken("run-1", attempt_id="attempt-1", timeout_seconds=0)
    with pytest.raises(ExecutionControlError):
        control.check()
    assert manager.receipts.get(receipt.receipt_id).status is ReceiptStatus.COMMITTED


def test_cancel_before_dispatch_has_no_receipt_or_effect(db):
    control = ExecutionControlToken("run-1", attempt_id="attempt-1", timeout_seconds=30)
    control.cancel("USER_CANCEL", source="test-before-dispatch")
    with pytest.raises(ExecutionControlError):
        control.check()
    assert SideEffectReceiptManager(db).receipts.list_for_attempt("attempt-1") == []


def test_metadata_filters_sensitive_values(db):
    manager = SideEffectReceiptManager(db)
    receipt = _begin(
        manager,
        effect_class=EffectClass.LOCAL_DURABLE,
        sanitized_metadata={"api_key": "not-durable", "safe": "value"},
    )
    persisted = manager.receipts.get(receipt.receipt_id)
    assert persisted is not None
    assert persisted.sanitized_metadata == {"api_key": "[REDACTED]", "safe": "value"}


class _ReceiptTrackingTool:
    capability = CapabilitySpec(
        name="test.receipt_mutation",
        description="test receipt mutation",
        input_schema={"type": "object", "additionalProperties": False},
        side_effect=True,
    )

    async def execute(self, request: ToolRequest) -> ToolResult:
        return ToolResult(
            status=ToolResultStatus.SUCCESS,
            output={"before_sha256": "a" * 64, "after_sha256": "b" * 64, "bytes_written": 3},
        )


def _receipt_definition() -> CapabilityDefinition:
    return CapabilityDefinition(
        id="test.receipt_mutation",
        name="test.receipt_mutation",
        description="test receipt mutation",
        category="test",
        version="v1",
        input_schema={"type": "object", "additionalProperties": False},
        output_schema={"type": "object"},
        platforms=(RuntimePlatform.WINDOWS, RuntimePlatform.LINUX, RuntimePlatform.MACOS),
        permissions=("test.execute",),
        risk_level="MEDIUM",
        workspace_scope="SOURCE_WORKSPACE",
        timeout_seconds=30,
        retryable=False,
        preferred_tool="test.receipt_mutation",
        source="test",
        evidence_type="DETERMINISTIC_TOOL_RESULT",
        effect_class=EffectClass.LOCAL_DURABLE,
        receipt_support=True,
        reconciliation_support=True,
    )


@pytest.mark.asyncio
async def test_native_dispatch_projects_runtime_receipt_without_owning_it(db):
    tools = ToolRegistry()
    tools.register(_ReceiptTrackingTool())
    cap_registry = CapabilityRegistry(tools, definitions=[_receipt_definition()])
    dispatcher = NativeToolDispatcher(
        db=db,
        registry=tools,
        capability_registry=cap_registry,
        allowed_capabilities={"test.receipt_mutation"},
        allowed_side_effect_capabilities={"test.receipt_mutation"},
    )
    request = AgentRequest(
        agent_id="agent-1",
        role=AgentRole.WORKER,
        objective="mutate",
        allowed_capabilities={"test.receipt_mutation"},
        budget=AgentBudget(max_turns=2, max_tool_calls=2),
        metadata={"task_id": "task-1", "run_id": "run-1", "attempt_id": "attempt-1"},
    )
    snapshot = ExecutionSnapshot(task_id="task-1", run_id="run-1", attempt_id="attempt-1", goal="mutate")
    observation = await dispatcher.dispatch(
        ProviderToolCall(id="call-1", name="test.receipt_mutation", arguments={}),
        request,
        snapshot,
    )
    assert observation["status"] == "SUCCESS"
    assert observation["side_effect_receipt"]["status"] == ReceiptStatus.OBSERVED.value
    receipts = dispatcher.receipts.receipts.list_for_attempt("attempt-1")
    assert len(receipts) == 1
    assert receipts[0].run_id == "run-1"
    assert receipts[0].step_id == "attempt:attempt-1"
    assert receipts[0].status is ReceiptStatus.OBSERVED


def test_tool_contract_exposes_effect_capability_facts(db):
    tools = ToolRegistry()
    tools.register(_ReceiptTrackingTool())
    definition = _receipt_definition()
    contract = ToolContract(CapabilityRegistry(tools, definitions=[definition]), tools)
    decision = contract.prepare(
        ToolRequest(
            tool_call_id="call-1",
            task_id="task-1",
            run_id="run-1",
            attempt_id="attempt-1",
            capability_id=definition.id,
            tool_name=definition.preferred_tool,
            arguments={},
        ),
        CapabilityRuntimeContext(platform="windows", available_tools={definition.preferred_tool}),
    )
    assert decision.valid is True
    assert decision.effect_class is EffectClass.LOCAL_DURABLE
    assert decision.receipt_support is True
    assert decision.reconciliation_support is True


class _CrashAfterToolExecution:
    def hit(self, point, **_context):
        from lhas.native.models import NativeFaultPoint

        if point is NativeFaultPoint.AFTER_TOOL_EXECUTED:
            raise RuntimeError("crash-after-tool-execution")


@pytest.mark.asyncio
async def test_real_staged_workspace_commit_survives_crash_before_native_observation(db, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "note.txt").write_text("before\n", encoding="utf-8")
    stage = StagedWorkspace.create(source, tmp_path / "stage")
    tools = ToolRegistry()
    register_staged_workspace_tools(tools, stage, CommandPolicy())
    dispatcher = NativeToolDispatcher(
        db=db,
        registry=tools,
        allowed_capabilities={"workspace.edit"},
        allowed_side_effect_capabilities={"workspace.edit"},
        fault_injector=_CrashAfterToolExecution(),
    )
    request = AgentRequest(
        agent_id="agent-1",
        role=AgentRole.WORKER,
        objective="edit",
        allowed_capabilities={"workspace.edit"},
        budget=AgentBudget(max_turns=2, max_tool_calls=2),
        metadata={"task_id": "task-1", "run_id": "run-1", "attempt_id": "attempt-1"},
    )
    snapshot = ExecutionSnapshot(task_id="task-1", run_id="run-1", attempt_id="attempt-1", goal="edit")
    with pytest.raises(RuntimeError, match="crash-after-tool-execution"):
        await dispatcher.dispatch(
            ProviderToolCall(
                id="edit-1",
                name="workspace.edit",
                arguments={"path": "note.txt", "old_text": "before", "new_text": "after"},
            ),
            request,
            snapshot,
        )
    receipt = dispatcher.receipts.receipts.list_for_attempt("attempt-1")[0]
    assert receipt.status is ReceiptStatus.COMMITTED
    assert receipt.workspace_before_digest and receipt.workspace_after_digest
    assert receipt.workspace_before_digest != receipt.workspace_after_digest
    assert (stage.root / "note.txt").read_text(encoding="utf-8") == "after\n"
    assert dispatcher.receipts.replay_decision(receipt.receipt_id) is ReplayDecision.REUSE_COMMITTED
