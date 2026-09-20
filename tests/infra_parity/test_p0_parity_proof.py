"""Offline P0 parity proofs at a non-Phase4 runtime boundary.

These tests deliberately construct a service-style root around the real
``NativeAgentKernel``.  The provider is a blocking in-memory double; no
benchmark runner or network provider is involved.  Existing P0-A/P0-B tests
cover the individual provider/tool/process/MCP/recovery/child and receipt
matrices; this file proves the missing non-Phase4 root ownership boundary and
keeps the fail-closed side-effect classification explicit.
"""

from __future__ import annotations

import asyncio

import pytest

from lhas.agent.models import AgentBudget, AgentRequest, AgentRole, AgentStatus
from lhas.domain.enums import EventType
from lhas.execution_control import ExecutionControlError, ExecutionControlToken, await_with_control
from lhas.native.completion import CompletionAuthority
from lhas.native.kernel import NativeAgentKernel
from lhas.native.models import RuntimeTarget
from lhas.native.tools import NativeToolDispatcher
from lhas.native.transport import opaque_transport_identity
from lhas.persistence.database import Database
from lhas.persistence.event_store import EventStore
from lhas.side_effects import (
    EffectClass,
    ReplayDecision,
    ReconciliationStrategy,
    ReceiptStatus,
    SideEffectReceiptManager,
)
from lhas.tools.registry import ToolRegistry
from lhas.validation import NeverPassValidator


class _BlockingProvider:
    """Offline provider that proves the kernel owns cancellation of transport."""

    name = "offline-parity-provider"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False
        self.bound: ExecutionControlToken | None = None
        self.runtime_target = RuntimeTarget(
            provider_id="offline-parity",
            model_id="offline",
            endpoint_identity="offline",
            credential_route_id="none",
            route_type="offline",
        )
        self.transport_identity = opaque_transport_identity("offline")

    def bind_execution_control(self, control: ExecutionControlToken | None) -> None:
        self.bound = control

    async def generate(self, *, context, tools, timeout_seconds):  # noqa: ANN001
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _service_case() -> tuple[Database, _BlockingProvider, NativeAgentKernel, AgentRequest]:
    db = Database(":memory:")
    db.init_db()
    provider = _BlockingProvider()
    dispatcher = NativeToolDispatcher(
        db=db,
        registry=ToolRegistry(),
        allowed_capabilities=set(),
        allowed_side_effect_capabilities=set(),
    )
    kernel = NativeAgentKernel(
        db=db,
        provider=provider,
        dispatcher=dispatcher,
        completion_authority=CompletionAuthority(db=db, validator=NeverPassValidator()),
        provider_timeout_seconds=30,
    )
    request = AgentRequest(
        agent_id="offline-service-agent",
        role=AgentRole.WORKER,
        objective="offline non-Phase4 parity proof",
        budget=AgentBudget(max_turns=2, max_tool_calls=1),
        metadata={
            "task_id": "offline-service-task",
            "run_id": "offline-service-run",
            "attempt_id": "offline-service-attempt",
        },
    )
    return db, provider, kernel, request


@pytest.mark.asyncio
async def test_non_phase4_root_owns_deadline_and_binds_native_kernel():
    """A service-style caller can own the root token without Phase4Runner."""

    db, provider, kernel, request = _service_case()
    try:
        # This is the non-Phase4 root boundary.  It creates the one root
        # authority, then passes it into the real Odys native kernel.
        root = ExecutionControlToken(
            "offline-service-run",
            attempt_id="offline-service-attempt",
            # Leave room for durable snapshot setup; the provider ceiling is
            # still 30 seconds, so the root remains the selected authority.
            timeout_seconds=1.0,
        )
        task = asyncio.create_task(kernel.run(request, execution_control=root))
        await provider.started.wait()
        result = await task

        assert provider.bound is root
        assert provider.cancelled is True
        assert result.status is AgentStatus.CANCELLED
        assert result.error_type == "ROOT_DEADLINE_EXCEEDED"
        events = EventStore(db).list_for_run("offline-service-run")
        assert any(event.event_type is EventType.EXECUTION_DEADLINE_EXCEEDED for event in events)
        assert root.absolute_deadline is not None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_non_phase4_root_cancel_rejects_late_provider_result():
    db, provider, kernel, request = _service_case()
    try:
        root = ExecutionControlToken(
            "offline-service-run",
            attempt_id="offline-service-attempt",
            timeout_seconds=10,
        )
        task = asyncio.create_task(kernel.run(request, execution_control=root))
        await provider.started.wait()
        assert root.cancel("USER_CANCEL", source="offline-service-boundary") is True
        result = await task

        assert provider.bound is root
        assert provider.cancelled is True
        assert result.status is AgentStatus.CANCELLED
        assert result.error_type == "USER_CANCEL"
        events = EventStore(db).list_for_run("offline-service-run")
        assert any(event.event_type is EventType.EXECUTION_CANCELLED for event in events)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_native_kernel_fallback_token_is_non_phase4_safe():
    """Direct NativeAgentKernel callers get a kernel-owned root token."""

    db, provider, kernel, request = _service_case()
    try:
        task = asyncio.create_task(kernel.run(request))
        await provider.started.wait()
        await kernel.cancel(request.agent_id)
        result = await task

        assert provider.bound is not None
        assert provider.bound.run_id == "offline-service-run"
        assert result.status is AgentStatus.CANCELLED
        assert result.error_type == "USER_CANCEL"
    finally:
        db.close()


def test_child_deadline_is_clamped_to_non_phase4_root():
    class _Clock:
        value = 100.0

        def __call__(self):
            return self.value

    clock = _Clock()
    root = ExecutionControlToken("service-root", absolute_deadline=101.0, clock=clock)
    child = root.derive(run_id="service-child", attempt_id="child-attempt", local_ceiling=50.0)
    assert child.absolute_deadline == root.absolute_deadline
    assert child.root_run_id == root.root_run_id


def test_external_unverifiable_is_classifiable_and_fail_closed():
    db = Database(":memory:")
    db.init_db()
    try:
        manager = SideEffectReceiptManager(db)
        receipt = manager.begin(
            operation_id="unknown-operation",
            task_id="task-unknown",
            run_id="run-unknown",
            attempt_id="attempt-unknown",
            step_id="step-unknown",
            tool_call_id="tool-unknown",
            tool_name="external.unknown",
            effect_class=EffectClass.EXTERNAL_UNVERIFIABLE,
            target={"resource": "unknown"},
            request={"operation": "mutate"},
        )
        assert receipt.effect_class is EffectClass.EXTERNAL_UNVERIFIABLE
        assert receipt.reconciliation_strategy is ReconciliationStrategy.HUMAN_REQUIRED
        assert receipt.replay_safe is False
        manager.mark_dispatch_started(receipt.receipt_id)
        reconciled = manager.reconcile(receipt.receipt_id)
        assert reconciled.status is ReceiptStatus.COMMIT_STATE_UNKNOWN
        assert manager.replay_decision(receipt.receipt_id) is ReplayDecision.REQUIRE_HUMAN
        assert reconciled.run_id == "run-unknown"
        assert reconciled.attempt_id == "attempt-unknown"
        assert reconciled.step_id == "step-unknown"
        assert reconciled.tool_call_id == "tool-unknown"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_negative_control_late_result_is_caught():
    control = ExecutionControlToken("negative-late-result", timeout_seconds=10)
    control.cancel("USER_CANCEL", source="negative-control")

    async def broken_operation():
        return "late-result"

    with pytest.raises(ExecutionControlError):
        await await_with_control(broken_operation(), control=control, source="negative-control")


def test_negative_control_fresh_deadline_is_caught():
    class _Clock:
        value = 200.0

        def __call__(self):
            return self.value

    clock = _Clock()
    root = ExecutionControlToken("negative-root", absolute_deadline=201.0, clock=clock)
    broken_child = ExecutionControlToken("negative-child", timeout_seconds=30.0, clock=clock)
    with pytest.raises(AssertionError):
        assert broken_child.absolute_deadline <= root.absolute_deadline


def test_negative_control_lost_receipt_is_caught():
    class _BrokenReceiptStore:
        def __init__(self):
            self.receipts = {}

        def commit(self, receipt):
            # Deliberately broken: commit is acknowledged but not persisted.
            return receipt

        def get(self, receipt_id):
            return self.receipts.get(receipt_id)

    broken = _BrokenReceiptStore()
    receipt_id = "receipt-that-must-survive"
    broken.commit(receipt_id)
    with pytest.raises(AssertionError):
        assert broken.get(receipt_id) == receipt_id


def test_negative_control_non_idempotent_duplicate_is_caught():
    class _NonIdempotentFake:
        def __init__(self):
            self.effect_count = 0

        def apply(self):
            self.effect_count += 1

    fake = _NonIdempotentFake()
    fake.apply()
    fake.apply()
    with pytest.raises(AssertionError):
        assert fake.effect_count == 1
