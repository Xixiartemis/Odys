import hashlib, json
from pathlib import Path
from lhas import HARNESS_VERSION
from lhas.checkpoint import (Checkpoint, CheckpointCorrupt, CheckpointRepository, CheckpointService,
                             ContextReconstructionService, WorkingState, WorkingStateProjector)
from lhas.context_builder import ContextBudget, ContextBuilder, ContextBudgetExceeded
from lhas.domain.enums import EventType
from lhas.domain.models import Task
from lhas.persistence.database import Database
from lhas.persistence.event_store import EventStore

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

def test_harness_version_cp3(): assert HARNESS_VERSION == "HV-1.0"
