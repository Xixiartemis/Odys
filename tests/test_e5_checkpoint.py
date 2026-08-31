import asyncio, hashlib, json
from pathlib import Path
import pytest
from lhas import HARNESS_VERSION
from lhas.checkpoint import (Checkpoint, CheckpointCorrupt, CheckpointRepository, CheckpointService,
                             ContextReconstructionService, WorkingState, WorkingStateProjector)
from lhas.context_builder import ContextBudget, ContextBuilder, ContextBudgetExceeded
from lhas.domain.enums import EventType
from lhas.domain.enums import ExecutionStatus
from lhas.domain.models import Task
from lhas.executors.protocol import ExecutionResult
from lhas.orchestrator_v2 import RecoveringOrchestrator
from lhas.persistence.phaseb_repos import ContextSnapshotRepository
from lhas.persistence.database import Database
from lhas.persistence.event_store import EventStore
from lhas.validation import AlwaysPassValidator

def task(): return Task(id="task-e5", project_id="p", title="repair", objective="Inspect and repair the calculator", constraints=[], acceptance_criteria=[])
def db():
    d=Database(":memory:"); d.init_db(); return d
def observation(db, run, capability, payload): return EventStore(db).append(EventType.INNER_AGENT_TOOL_OBSERVATION, run_id=run, payload={"capability":capability, **payload})

def test_event_cursor_and_checkpoint_integrity():
    d=db(); run="run"; [observation(d,run,"workspace.read",{"path":f"src/{i}.py","sha256":"x"}) for i in range(3)]
    checkpoint=CheckpointService(d).create_checkpoint(task(),run,"a1",1)
    assert checkpoint.event_cursor == 3 and CheckpointRepository(d).latest_for_run(run).state_sha256 == checkpoint.state_sha256
    assert EventStore(d).list_for_run_after(run, checkpoint.event_cursor)[0].event_type == EventType.CHECKPOINT_CREATED

def test_checkpoint_corruption_is_rejected():
    d=db(); checkpoint=CheckpointService(d).create_checkpoint(task(),"run","a",1)
    from lhas.persistence.orm import CheckpointRow
    with d.session() as s: s.get(CheckpointRow, checkpoint.id).working_state_json=json.dumps({"task_id":"tampered","run_id":"run"})
    try: CheckpointRepository(d).get(checkpoint.id); assert False
    except CheckpointCorrupt as exc: assert str(exc) == "CHECKPOINT_CORRUPT"

def test_cp3_reconstruction_is_bounded_and_deterministic():
    d=db(); run="canonical-e5"; store=EventStore(d)
    for i in range(100): observation(d,run,"workspace.read",{"path":f"src/{i}.py","sha256":"x","truncated":False})
    checkpoint=CheckpointService(d).create_checkpoint(task(),run,"a1",1)
    for i in range(20): observation(d,run,"cli.exec",{"exit_code":1 if i==19 else 0,"timed_out":False,"duration_ms":1,"stdout_truncated":False,"stderr_truncated":False,"command_name":"pytest"})
    budget=ContextBudget(max_total_bytes=4096,max_working_state_bytes=1024,max_recent_history_bytes=1024,max_recent_events=20)
    service=ContextReconstructionService(d,builder=ContextBuilder(policy="CP-3"),budget=budget)
    first,metrics=service.reconstruct(task(),run,"a2",2)
    second,metrics2=service.reconstruct(task(),run,"a2",2)
    assert metrics["raw_history_event_count"] == 120 and metrics["checkpoint_event_cursor"] == checkpoint.event_cursor and metrics["delta_event_count"] == 20
    assert metrics["selected_recent_events"] <= 20 and metrics["dropped_recent_events"] == 0
    assert len(first.raw_text.encode()) <= 4096 and first.context_sha256 == second.context_sha256
    assert "working_state" in first.sections and "recent_history" in first.sections and metrics["event_replay_reduction_ratio"] > 0.8
    assert first.sections["goal"] == second.sections["goal"]

def test_restart_safe_checkpoint_reconstruction(tmp_path):
    path=tmp_path / "e5.sqlite"; d=Database(path); d.init_db(); observation(d,"run","workspace.search",{"match_count":1,"matched_paths":["README.md"],"truncated":False}); CheckpointService(d).create_checkpoint(task(),"run","a",1); d.close()
    reopened=Database(path); reopened.init_db(); result,metrics=ContextReconstructionService(reopened,budget=ContextBudget()).reconstruct(task(),"run","a2",2)
    assert metrics["checkpoint_used"] is True and result.sections["working_state"]

def test_working_state_projection_uses_safe_summaries_only():
    d=db(); observation(d,"run","workspace.read",{"path":"src/a.py","sha256":"abc","content":"SECRET_SHOULD_NOT_APPEAR"}); observation(d,"run","workspace.search",{"match_count":1,"matched_paths":["src/a.py"],"matched_text":"SECRET"}); observation(d,"run","cli.exec",{"exit_code":1,"stdout":"raw output","stderr":"raw error","command_name":"pytest"})
    state=WorkingStateProjector().project(WorkingState(task_id="t",run_id="run"),EventStore(d).list_for_run("run"))
    encoded=json.dumps(state.model_dump()); assert "SECRET" not in encoded and "raw output" not in encoded and state.last_cli_exit_code == 1

def test_context_budget_rejects_oversized_mandatory_goal():
    huge=task().model_copy(update={"objective":"x"*5000})
    try: ContextBuilder(policy="CP-3").build(task=huge,attempt_number=1,budget=ContextBudget(max_total_bytes=100)) ; assert False
    except ContextBudgetExceeded: pass

def test_harness_version_cp3(): assert HARNESS_VERSION == "HV-1.5"


def test_cp3_recent_history_projects_allowlisted_fields_only():
    d=db(); run="safe-history"; store=EventStore(d)
    CheckpointService(d).create_checkpoint(task(), run, "a1", 1)
    store.append(EventType.EXECUTOR_COMPLETED, run_id=run, payload={
        "content": "SECRET", "stdout": "RAW", "output": "RAW_MODEL_OUTPUT",
        "duration_ms": 1,
    })
    snapshot, _ = ContextReconstructionService(d, budget=ContextBudget()).reconstruct(
        task(), run, "a2", 2
    )
    assert "SECRET" not in snapshot.raw_text
    assert "RAW" not in snapshot.raw_text
    assert "RAW_MODEL_OUTPUT" not in snapshot.raw_text
    assert "EXECUTOR_COMPLETED" in snapshot.sections["recent_history"]


def test_cp3_reconstruction_failure_emits_bounded_event():
    class BrokenBuilder:
        def build(self, **_kwargs):
            raise RuntimeError("provider response SECRET")

    d=db(); run="failed-reconstruction"; CheckpointService(d).create_checkpoint(task(), run, "a1", 1)
    service=ContextReconstructionService(d, builder=BrokenBuilder())
    with pytest.raises(RuntimeError):
        service.reconstruct(task(), run, "a2", 2)
    events=EventStore(d).list_for_run(run)
    assert [e.event_type for e in events][-2:] == [EventType.CONTEXT_RECONSTRUCTION_STARTED, EventType.CONTEXT_RECONSTRUCTION_FAILED]
    assert events[-1].payload == {"error_type": "RuntimeError"}


def test_retry_uses_checkpoint_aware_cp3_context():
    class RecordingExecutor:
        name="RecordingExecutor"

        def __init__(self):
            self.requests=[]

        async def execute(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                return ExecutionResult(status=ExecutionStatus.FAILURE, error_type="FIRST_FAIL", error_message="first")
            return ExecutionResult(status=ExecutionStatus.SUCCESS, output="recovered")

    d=db(); t=task().model_copy(update={"max_attempts": 2});
    from lhas.persistence.repositories import TaskRepository
    TaskRepository(d).create(t)
    executor=RecordingExecutor()
    orch=RecoveringOrchestrator(
        d, executor_factory=lambda: executor, validator=AlwaysPassValidator(),
        context_policy_version="CP-2",
    )
    run=asyncio.run(orch.execute_task(t.id))
    assert run.status.value == "COMPLETED"
    assert len(executor.requests) == 2
    second=executor.requests[1].context
    assert second["policy"] == "CP-3"
    assert "working_state" in second and "recent_history" in second
    snapshots=ContextSnapshotRepository(d).list_for_attempt(orch.attempt_repo.list_for_run(run.id)[1].id)
    assert snapshots[0].policy == "CP-3"
    assert snapshots[0].metrics["checkpoint_used"] is True
    completed=[e for e in EventStore(d).list_for_run(run.id) if e.event_type is EventType.CONTEXT_RECONSTRUCTION_COMPLETED]
    assert completed and completed[-1].payload["checkpoint_used"] is True
    assert snapshots[0].metrics["delta_event_count"] < snapshots[0].metrics["raw_history_event_count"]
