from lhas.domain.enums import EventType, TaskStatus
from lhas.domain.models import Attempt, Run, Task
from lhas.native.delegation import (
    ChildExecutionState,
    ChildOutcome,
    DelegationLifecycleRepository,
    DeliveryState,
    DurableDeliveryService,
)
from lhas.persistence.event_store import EventStore
from lhas.persistence.platform_repositories import DelegationRepository
from lhas.persistence.repositories import AttemptRepository, RunRepository, TaskRepository
from lhas.platform_models import Delegation


def _case(db, project):
    parent = TaskRepository(db).create(Task(project_id=project.id, title="parent", objective="parent", status=TaskStatus.RUNNING))
    parent_run = RunRepository(db).create(Run(task_id=parent.id, status="RUNNING"))
    parent_attempt = AttemptRepository(db).create(Attempt(run_id=parent_run.id, attempt_number=1, status="RUNNING"))
    child = TaskRepository(db).create(Task(project_id=project.id, title="child", objective="child"))
    child_run = RunRepository(db).create(Run(task_id=child.id, status="RUNNING"))
    delegation = Delegation(
        parent_agent_id="parent-agent",
        parent_task_id=parent.id,
        parent_run_id=parent_run.id,
        child_agent_id="child-agent",
        child_task_id=child.id,
        child_run_id=child_run.id,
        spawn_depth=1,
    )
    DelegationRepository(db).create(delegation)
    lifecycle = DelegationLifecycleRepository(db).create(
        delegation_id=delegation.id,
        parent_attempt_id=parent_attempt.id,
        execution_owner="child-agent",
        conversation_owner="parent-agent",
        delivery_owner="parent-agent",
    )
    return parent, parent_run, parent_attempt, child, child_run, delegation, lifecycle


def test_parent_crash_with_running_child_preserves_relation(db, project):
    case = _case(db, project)
    service = DurableDeliveryService(db)
    service.validate_lineage(case[5].id)
    service.record_started(case[5].id)
    fresh = DurableDeliveryService(db).repo.get(case[5].id)
    assert fresh.execution_state is ChildExecutionState.RUNNING
    assert fresh.parent_attempt_id == case[2].id
    assert DelegationRepository(db).get(case[5].id).child_run_id == case[4].id


def test_child_crash_is_durable_parent_outcome(db, project):
    case = _case(db, project)
    service = DurableDeliveryService(db)
    service.record_started(case[5].id)
    service.record_outcome(case[5].id, ChildOutcome(status=ChildExecutionState.CRASHED, failure_type="PROCESS_CRASH", child_run_id=case[4].id, retryable=True), validator_result=False)
    item = service.repo.get(case[5].id)
    assert item.execution_state is ChildExecutionState.CRASHED
    assert item.delivery_state is DeliveryState.DELIVERY_PENDING
    assert item.outcome["failure_type"] == "PROCESS_CRASH" and item.outcome["retryable"] is True


def test_child_completed_then_delivery_crash_resumes_once(db, project):
    case = _case(db, project)
    first = DurableDeliveryService(db)
    first.record_started(case[5].id)
    first.record_outcome(case[5].id, ChildOutcome(status=ChildExecutionState.COMPLETED, child_run_id=case[4].id, verification={"passed": True}), validator_result=True)
    fresh = DurableDeliveryService(db)
    delivered = fresh.resume_pending_deliveries()
    assert len(delivered) == 1 and delivered[0].delivery_state is DeliveryState.DELIVERED
    assert fresh.resume_pending_deliveries() == []


def test_delivery_ack_crash_has_one_logical_delivery(db, project):
    case = _case(db, project)
    service = DurableDeliveryService(db)
    service.record_started(case[5].id)
    service.record_outcome(case[5].id, ChildOutcome(status=ChildExecutionState.COMPLETED, child_run_id=case[4].id), validator_result=True)
    first = service.deliver(case[5].id)
    second = DurableDeliveryService(db).deliver(case[5].id)
    assert first.delivery_token == second.delivery_token
    events = [event for event in EventStore(db).list_for_run(case[1].id) if event.event_type is EventType.DELEGATION_DELIVERED]
    assert len(events) == 1
    consumed = service.consume_for_parent_attempt(case[2].id)
    duplicate = service.consume_for_parent_attempt(case[2].id)
    assert len(consumed) == 1 and duplicate == []


def test_duplicate_child_completion_event_is_idempotent(db, project):
    case = _case(db, project)
    service = DurableDeliveryService(db)
    service.record_started(case[5].id)
    outcome = ChildOutcome(status=ChildExecutionState.COMPLETED, child_run_id=case[4].id, summary="same")
    first = service.record_outcome(case[5].id, outcome, validator_result=True)
    second = service.record_outcome(case[5].id, outcome, validator_result=True)
    assert first.outcome == second.outcome
    events = [event for event in EventStore(db).list_all() if event.event_type is EventType.CHILD_OUTCOME_RECORDED]
    assert len(events) == 1


def test_timeout_preserves_partial_artifact_and_mutation(db, project):
    case = _case(db, project)
    service = DurableDeliveryService(db)
    service.record_started(case[5].id)
    service.record_outcome(case[5].id, ChildOutcome(
        status=ChildExecutionState.TIMEOUT,
        failure_type="BUDGET_EXHAUSTED",
        artifact_refs=["workspace_patch:abc"],
        workspace_mutation_present=True,
        verification={"passed": False},
        retryable=True,
        child_run_id=case[4].id,
    ), validator_result=False)
    item = service.repo.get(case[5].id)
    assert item.outcome["workspace_mutation_present"] is True
    assert item.outcome["artifact_refs"] == ["workspace_patch:abc"]
    assert item.execution_state is ChildExecutionState.TIMEOUT


def test_reviewer_rejection_cannot_complete_parent(db, project):
    case = _case(db, project)
    service = DurableDeliveryService(db)
    service.record_started(case[5].id)
    service.record_outcome(case[5].id, ChildOutcome(status=ChildExecutionState.VALIDATION_REJECTED, child_run_id=case[4].id, verification={"passed": False}), validator_result=False)
    service.deliver(case[5].id)
    assert TaskRepository(db).get(case[0].id).status is TaskStatus.RUNNING
    assert service.repo.get(case[5].id).validator_result is False


def test_nested_child_provenance_keeps_distinct_owners(db, project):
    parent_case = _case(db, project)
    child_task = parent_case[3]
    child_run = parent_case[4]
    child_attempt = AttemptRepository(db).create(Attempt(run_id=child_run.id, attempt_number=1, status="RUNNING"))
    grandchild = TaskRepository(db).create(Task(project_id=project.id, title="grandchild", objective="grandchild"))
    nested = Delegation(parent_agent_id="child-agent", parent_task_id=child_task.id, parent_run_id=child_run.id, child_agent_id="grandchild-agent", child_task_id=grandchild.id, spawn_depth=2)
    DelegationRepository(db).create(nested)
    item = DelegationLifecycleRepository(db).create(delegation_id=nested.id, parent_attempt_id=child_attempt.id, execution_owner="grandchild-agent", conversation_owner="child-agent", delivery_owner="child-agent")
    DurableDeliveryService(db).validate_lineage(nested.id)
    assert item.execution_owner != item.conversation_owner
    assert item.delivery_owner == "child-agent"


def test_missing_parent_fails_closed(db, project):
    child = TaskRepository(db).create(Task(project_id=project.id, title="child", objective="child"))
    delegation = Delegation(parent_agent_id="missing", parent_task_id="missing-task", parent_run_id="missing-run", child_agent_id="child", child_task_id=child.id, spawn_depth=1)
    DelegationRepository(db).create(delegation)
    DelegationLifecycleRepository(db).create(delegation_id=delegation.id, parent_attempt_id="missing-attempt", execution_owner="child", conversation_owner="missing", delivery_owner="missing")
    import pytest
    with pytest.raises(ValueError, match="MISSING_DELEGATION_OWNER"):
        DurableDeliveryService(db).validate_lineage(delegation.id)


def test_cyclic_lineage_fails_closed(db, project):
    parent = TaskRepository(db).create(Task(project_id=project.id, title="same", objective="same"))
    run = RunRepository(db).create(Run(task_id=parent.id, status="RUNNING"))
    attempt = AttemptRepository(db).create(Attempt(run_id=run.id, attempt_number=1, status="RUNNING"))
    delegation = Delegation(parent_agent_id="a", parent_task_id=parent.id, parent_run_id=run.id, child_agent_id="b", child_task_id=parent.id, spawn_depth=1)
    DelegationRepository(db).create(delegation)
    DelegationLifecycleRepository(db).create(delegation_id=delegation.id, parent_attempt_id=attempt.id, execution_owner="b", conversation_owner="a", delivery_owner="a")
    import pytest
    with pytest.raises(ValueError, match="CYCLIC_DELEGATION_LINEAGE"):
        DurableDeliveryService(db).validate_lineage(delegation.id)
