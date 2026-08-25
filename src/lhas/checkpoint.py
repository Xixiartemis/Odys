from __future__ import annotations
import hashlib, json
from datetime import datetime, timezone
from typing import Any
from pydantic import BaseModel, ConfigDict, Field
from lhas.domain.models import new_id, json_dumps, json_loads, Task
from lhas.domain.enums import EventType
from lhas.persistence.database import Database
from lhas.persistence.event_store import EventStore
from lhas.persistence.orm import CheckpointRow
from sqlalchemy import select
from lhas.context_builder import ContextBudget, ContextBuilder, ContextSnapshot

def _canonical(value): return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
def _sha(value): return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()

class WorkingState(BaseModel):
    model_config=ConfigDict(extra="forbid")
    schema_version: str = "working-state-v1"
    task_id: str
    run_id: str
    attempts_completed: int = 0
    last_attempt_number: int = 0
    last_attempt_status: str | None = None
    last_failure_type: str | None = None
    last_recovery_action: str | None = None
    files_inspected: list[str] = Field(default_factory=list)
    files_modified: list[str] = Field(default_factory=list)
    last_cli_exit_code: int | None = None
    candidate_patch_summary: dict[str, Any] = Field(default_factory=dict)
    last_completion_claim: bool | None = None
    tool_call_count: int = 0
    event_cursor: int = 0
    truncated: bool = False

    def bounded(self):
        data=self.model_dump()
        for key,limit in (("files_inspected",100),("files_modified",100)):
            values=list(dict.fromkeys(data[key])); data[key]=values[-limit:]; data["truncated"] = data["truncated"] or len(values)>limit
        if len(_canonical(data.get("candidate_patch_summary", {})).encode()) > 8192:
            data["candidate_patch_summary"]={"truncated":True}; data["truncated"]=True
        if data.get("last_completion_claim") is not None:
            claim=str(data["last_completion_claim"])
            if len(claim.encode())>4096: data["last_completion_claim"]=claim.encode()[:4096].decode("utf-8","ignore"); data["truncated"]=True
        return WorkingState(**data)

class Checkpoint(BaseModel):
    model_config=ConfigDict(extra="forbid")
    id: str = Field(default_factory=new_id)
    schema_version: str = "checkpoint-v1"
    task_id: str
    run_id: str
    attempt_id: str
    attempt_number: int
    event_cursor: int
    working_state: WorkingState
    state_sha256: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    def with_hash(self):
        self.state_sha256=_sha(self.working_state.model_dump(mode="json")); return self

class CheckpointCorrupt(Exception): pass
class CheckpointRepository:
    def __init__(self, db: Database): self.db=db
    def create(self, checkpoint: Checkpoint):
        checkpoint=checkpoint.with_hash()
        with self.db.session() as session:
            session.add(CheckpointRow(id=checkpoint.id,schema_version=checkpoint.schema_version,task_id=checkpoint.task_id,run_id=checkpoint.run_id,attempt_id=checkpoint.attempt_id,attempt_number=checkpoint.attempt_number,event_cursor=checkpoint.event_cursor,working_state_json=_canonical(checkpoint.working_state.model_dump(mode="json")),state_sha256=checkpoint.state_sha256,created_at=checkpoint.created_at))
        return checkpoint
    def _row(self,row):
        state=WorkingState(**(json_loads(row.working_state_json) or {}))
        if _sha(state.model_dump(mode="json")) != row.state_sha256: raise CheckpointCorrupt("CHECKPOINT_CORRUPT")
        return Checkpoint(id=row.id,schema_version=row.schema_version,task_id=row.task_id,run_id=row.run_id,attempt_id=row.attempt_id,attempt_number=row.attempt_number,event_cursor=row.event_cursor,working_state=state,state_sha256=row.state_sha256,created_at=row.created_at)
    def get(self, checkpoint_id):
        with self.db.session() as session: row=session.get(CheckpointRow, checkpoint_id)
        return self._row(row) if row else None
    def latest_for_run(self, run_id):
        with self.db.session() as session: row=session.execute(select(CheckpointRow).where(CheckpointRow.run_id==run_id).order_by(CheckpointRow.event_cursor.desc(), CheckpointRow.created_at.desc())).scalars().first()
        return self._row(row) if row else None
    def list_for_run(self, run_id):
        with self.db.session() as session: rows=session.execute(select(CheckpointRow).where(CheckpointRow.run_id==run_id).order_by(CheckpointRow.event_cursor.asc())).scalars().all()
        return [self._row(row) for row in rows]

class WorkingStateProjector:
    def project(self, previous: WorkingState | None, events):
        state=previous.model_copy(deep=True) if previous else WorkingState(task_id="",run_id="")
        for event in events:
            payload=event.payload or {}; state.event_cursor=max(state.event_cursor,int(event.id or 0))
            if event.event_type == EventType.INNER_AGENT_TOOL_OBSERVATION:
                state.tool_call_count += 1; capability=payload.get("capability")
                if capability == "workspace.read" and payload.get("path"): state.files_inspected.append(payload["path"])
                if capability == "workspace.search": state.files_inspected.extend(payload.get("matched_paths", []))
                if capability == "workspace.edit" and payload.get("path"): state.files_modified.append(payload["path"])
                if capability == "workspace.diff": state.candidate_patch_summary={k:payload[k] for k in ("changed_files","files_changed","lines_added","lines_removed","truncated") if k in payload}
                if capability == "cli.exec": state.last_cli_exit_code=payload.get("exit_code")
            elif event.event_type in {EventType.INNER_AGENT_COMPLETED, EventType.INNER_AGENT_FAILED}:
                state.last_attempt_status="SUCCESS" if event.event_type == EventType.INNER_AGENT_COMPLETED else "FAILURE"; state.attempts_completed += 1
                state.last_attempt_number=max(state.last_attempt_number,int(payload.get("attempt_number",state.last_attempt_number)))
                state.last_completion_claim=bool(payload.get("completion_claim", state.last_completion_claim or False)); state.tool_call_count=max(state.tool_call_count,int(payload.get("tool_call_count",state.tool_call_count)))
            elif event.event_type == EventType.FAILURE_CLASSIFIED: state.last_failure_type=payload.get("failure_type")
            elif event.event_type == EventType.RECOVERY_DECIDED: state.last_recovery_action=payload.get("action_type")
        return state.bounded()


# Recent history is an executor-facing projection, not an event dump.  Keep
# this allowlist deliberately small: event payloads can contain provider/tool
# output, and unknown event types must never inherit a new field implicitly.
_SAFE_EVENT_FIELDS = {
    EventType.INNER_AGENT_TOOL_OBSERVATION: {
        "capability", "status", "error_type", "path", "sha256", "truncated",
        "matched_paths", "match_count", "exit_code", "timed_out", "duration_ms",
        "stdout_truncated", "stderr_truncated", "command_name",
    },
    EventType.ATTEMPT_STARTED: {"attempt_number"},
    EventType.ATTEMPT_FAILED: {"attempt_number", "reason", "error_type"},
    EventType.ATTEMPT_TIMED_OUT: {"attempt_number", "reason", "error_type"},
    EventType.ATTEMPT_CRASHED: {"attempt_number", "reason", "error_type"},
    EventType.ATTEMPT_COMPLETED: {"attempt_number"},
    EventType.FAILURE_CLASSIFIED: {"failure_type", "failure_class", "suggested_recovery"},
    EventType.RECOVERY_DECIDED: {"action", "attempt_to"},
    EventType.RECOVERY_STARTED: {"action", "next_attempt"},
    EventType.VALIDATION_FAILED: {"passed"},
    EventType.VALIDATION_PASSED: {"passed"},
}


def _bounded_safe_value(key: str, value: Any) -> Any:
    """Return only bounded scalar/path summaries suitable for recent history."""
    if key == "matched_paths" and isinstance(value, list):
        return [str(item)[:512] for item in value if isinstance(item, (str, int))][:100]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value if not isinstance(value, str) else value[:512]
    return None


def safe_event_projection(event) -> dict[str, Any]:
    """Project one event without exposing arbitrary payload fields."""
    allowed = _SAFE_EVENT_FIELDS.get(event.event_type, set())
    projected = {"event_type": event.event_type.value}
    if event.id is not None:
        projected["event_id"] = event.id
    payload = event.payload if isinstance(event.payload, dict) else {}
    for key in allowed:
        if key not in payload:
            continue
        value = _bounded_safe_value(key, payload[key])
        if value is not None:
            projected[key] = value
    return projected

class CheckpointService:
    def __init__(self, db): self.db=db; self.events=EventStore(db); self.repo=CheckpointRepository(db); self.projector=WorkingStateProjector()
    def create_checkpoint(self, task, run_id, attempt_id, attempt_number):
        previous=self.repo.latest_for_run(run_id); cursor=previous.event_cursor if previous else 0; delta=self.events.list_for_run_after(run_id,cursor)
        state=self.projector.project(previous.working_state if previous else WorkingState(task_id=task.id,run_id=run_id),delta); cursor=self.events.latest_sequence(run_id); state.event_cursor=cursor
        checkpoint=self.repo.create(Checkpoint(task_id=task.id,run_id=run_id,attempt_id=attempt_id,attempt_number=attempt_number,event_cursor=cursor,working_state=state))
        self.events.append(EventType.CHECKPOINT_CREATED,task_id=task.id,run_id=run_id,attempt_id=attempt_id,payload={"checkpoint_id":checkpoint.id,"attempt_number":attempt_number,"event_cursor":cursor,"state_sha256":checkpoint.state_sha256})
        return checkpoint

class ContextReconstructionService:
    def __init__(self, db, builder=None, budget=None): self.db=db; self.repo=CheckpointRepository(db); self.events=EventStore(db); self.projector=WorkingStateProjector(); self.builder=builder or ContextBuilder(policy="CP-3"); self.budget=budget or ContextBudget()
    def reconstruct(self, task: Task, run_id, attempt_id, attempt_number, failure_report=None, recovery_action=None, previous_attempts=None):
        self.events.append(EventType.CONTEXT_RECONSTRUCTION_STARTED, task_id=task.id, run_id=run_id, attempt_id=attempt_id, payload={"checkpoint_used": False})
        try:
            checkpoint=self.repo.latest_for_run(run_id); cursor=checkpoint.event_cursor if checkpoint else 0; infrastructure={EventType.CHECKPOINT_CREATED,EventType.CONTEXT_RECONSTRUCTION_STARTED,EventType.CONTEXT_RECONSTRUCTION_COMPLETED,EventType.CONTEXT_RECONSTRUCTION_FAILED}; all_events=[e for e in self.events.list_for_run(run_id) if e.event_type not in infrastructure]; delta=[e for e in all_events if (e.id or 0)>cursor]; previous=checkpoint.working_state if checkpoint else WorkingState(task_id=task.id,run_id=run_id); state=self.projector.project(previous,delta)
            recent=[safe_event_projection(e) for e in delta[-self.budget.max_recent_events:]]
            snapshot=self.builder.build(task=task,attempt_number=attempt_number,run_id=run_id,attempt_id=attempt_id,previous_attempts=previous_attempts,failure_report=failure_report,recovery_action=recovery_action,working_state=state.model_dump(mode="json"),recent_history=recent,budget=self.budget)
            metrics={"checkpoint_used":checkpoint is not None,"source_checkpoint_id":checkpoint.id if checkpoint else None,"raw_history_event_count":len(all_events),"checkpoint_event_cursor":cursor,"delta_event_count":len(delta),"eligible_recent_events":len(delta),"selected_recent_events":len(recent),"dropped_recent_events":max(0,len(delta)-len(recent)),"history_input_bytes":len(json.dumps(recent,ensure_ascii=False).encode()),"context_output_bytes":len(snapshot.raw_text.encode()),"context_budget_bytes":self.budget.max_total_bytes,"event_replay_reduction_ratio":1-(len(delta)/len(all_events) if all_events else 0)}
            snapshot.metrics=metrics
            self.events.append(EventType.CONTEXT_RECONSTRUCTION_COMPLETED, task_id=task.id, run_id=run_id, attempt_id=attempt_id, payload={"checkpoint_id":checkpoint.id if checkpoint else None,"checkpoint_used":checkpoint is not None,"delta_event_count":len(delta),"selected_event_count":len(recent),"dropped_event_count":max(0,len(delta)-len(recent)),"context_output_bytes":len(snapshot.raw_text.encode()),"context_sha256":snapshot.context_sha256})
            return snapshot, metrics
        except Exception as exc:
            # Persist only a stable exception type; exception text may contain
            # provider responses, command output, or other sensitive material.
            self.events.append(EventType.CONTEXT_RECONSTRUCTION_FAILED, task_id=task.id, run_id=run_id, attempt_id=attempt_id, payload={"error_type":type(exc).__name__})
            raise
