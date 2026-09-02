"""SQLAlchemy 2.0 ORM mapping — single source for the SQLite schema.

JSON-ish columns are stored as TEXT (json.dumps / json.loads at the boundary).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from lhas.persistence.database import Base


def _json_col() -> Mapped[str | None]:
    return mapped_column(Text, nullable=True)


class ProjectRow(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String(32), default="generic", nullable=False)
    root_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TaskRow(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    constraints: Mapped[str | None] = _json_col()  # JSON list[str]
    acceptance_criteria: Mapped[str | None] = _json_col()  # JSON list[str]
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

class GoalRow(Base):
    __tablename__ = "goals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(32), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    constraints: Mapped[str | None] = _json_col()
    success_criteria: Mapped[str | None] = _json_col()
    allowed_capabilities: Mapped[str | None] = _json_col()
    requires_human_approval: Mapped[bool] = mapped_column(Integer, nullable=False, default=False)
    metadata_json: Mapped[str | None] = _json_col()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

class PlanRow(Base):
    __tablename__ = "plans"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    goal_id: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(48), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_json: Mapped[str | None] = _json_col()
    invalidated_step_ids: Mapped[str | None] = _json_col()

class PlanStepRow(Base):
    __tablename__ = "plan_steps"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    plan_id: Mapped[str] = mapped_column(String(32), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    capability: Mapped[str] = mapped_column(String(128), nullable=False)
    depends_on: Mapped[str | None] = _json_col()
    inputs: Mapped[str | None] = _json_col()
    expected_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    success_criteria: Mapped[str | None] = _json_col()
    status: Mapped[str] = mapped_column(String(48), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    output: Mapped[str | None] = _json_col()
    execution_context: Mapped[str | None] = _json_col()
    semantic_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)


class RunRow(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    experiment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    executor_type: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    harness_version: Mapped[str] = mapped_column(String(32), nullable=False)
    context_policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[str | None] = _json_col()  # JSON payload
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AttemptRow(Base):
    __tablename__ = "attempts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    context_snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executor_result: Mapped[str | None] = _json_col()  # JSON ExecutionResult
    usage: Mapped[str | None] = _json_col()  # JSON dict
    failure_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EventRow(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[str | None] = _json_col()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ContextSnapshotRow(Base):
    """Per-attempt context snapshot (docs/05 — every attempt saves a full
    snapshot for replay / A-B / token analysis / failure analysis)."""

    __tablename__ = "context_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(32), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    policy: Mapped[str] = mapped_column(String(16), nullable=False)
    sections: Mapped[str | None] = _json_col()
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metrics: Mapped[str | None] = _json_col()
    context_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

class CheckpointRow(Base):
    __tablename__ = "checkpoints"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    task_id: Mapped[str] = mapped_column(String(32), nullable=False)
    run_id: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    event_cursor: Mapped[int] = mapped_column(Integer, nullable=False)
    working_state_json: Mapped[str] = _json_col()
    state_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkspaceSessionRow(Base):
    """Durable workspace identity/location binding for an outer Run."""

    __tablename__ = "workspace_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    task_id: Mapped[str] = mapped_column(String(32), nullable=False)
    session_root: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ValidationResultRow(Base):
    """Validation outcome per attempt (docs/06)."""

    __tablename__ = "validation_results"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(32), nullable=False)
    passed: Mapped[bool] = mapped_column(Integer, nullable=False)  # SQLite bool
    level: Mapped[str] = mapped_column(String(32), nullable=False)
    checks: Mapped[str | None] = _json_col()
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    stdout: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FailureReportRow(Base):
    """Failure classification per failed attempt (docs/07)."""

    __tablename__ = "failure_reports"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(32), nullable=False)
    failure_type: Mapped[str] = mapped_column(String(64), nullable=False)
    failure_class: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    suggested_recovery: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RecoveryActionRow(Base):
    """Recovery decision per failed attempt (docs/08 — full log required)."""

    __tablename__ = "recovery_actions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(32), nullable=False)
    action_type: Mapped[str] = mapped_column(String(48), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_policy: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_from: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    added_context: Mapped[str | None] = _json_col()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_json: Mapped[str | None] = _json_col()
    invalidated_step_ids: Mapped[str | None] = _json_col()


class ConversationSessionRow(Base):
    __tablename__ = "conversation_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    parent_session_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    metadata_json: Mapped[str | None] = _json_col()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SessionMessageRow(Base):
    __tablename__ = "session_messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    safe_tool_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = _json_col()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DelegationRow(Base):
    __tablename__ = "delegations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    parent_agent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_task_id: Mapped[str] = mapped_column(String(32), nullable=False)
    parent_run_id: Mapped[str] = mapped_column(String(32), nullable=False)
    child_agent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    child_task_id: Mapped[str] = mapped_column(String(32), nullable=False)
    child_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    spawn_depth: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    context_json: Mapped[str | None] = _json_col()
    result_json: Mapped[str | None] = _json_col()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NativeExecutionSnapshotRow(Base):
    __tablename__ = "native_execution_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    attempt_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(48), nullable=False)
    payload_json: Mapped[str | None] = _json_col()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NativeToolInvocationRow(Base):
    __tablename__ = "native_tool_invocations"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    attempt_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    capability: Mapped[str] = mapped_column(String(128), nullable=False)
    args_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    side_effect_class: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_mutation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    result_summary_json: Mapped[str | None] = _json_col()
    reconciliation: Mapped[str | None] = mapped_column(String(32), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CompletionCandidateRow(Base):
    __tablename__ = "completion_candidates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    attempt_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    claim_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_json: Mapped[str | None] = _json_col()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NativeValidationFailureRow(Base):
    __tablename__ = "native_validation_failures"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    attempt_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(128), nullable=False)
    failed_criterion: Mapped[str | None] = mapped_column(Text, nullable=True)
    safe_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommended_recovery: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReplanSignalRow(Base):
    __tablename__ = "replan_signals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    attempt_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    failed_node_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    evidence_json: Mapped[str | None] = _json_col()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NativeRuntimeTargetRow(Base):
    __tablename__ = "native_runtime_targets"

    execution_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    owner_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    configured_json: Mapped[str] = _json_col()
    effective_json: Mapped[str] = _json_col()
    pending_json: Mapped[str | None] = _json_col()
    fallback_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    switch_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProviderHealthRow(Base):
    __tablename__ = "provider_route_health"

    target_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    target_json: Mapped[str] = _json_col()
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    failure_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DelegationLifecycleRow(Base):
    __tablename__ = "delegation_lifecycle"

    delegation_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    parent_attempt_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    execution_owner: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_owner: Mapped[str] = mapped_column(String(128), nullable=False)
    delivery_owner: Mapped[str] = mapped_column(String(128), nullable=False)
    dispatch_state: Mapped[str] = mapped_column(String(32), nullable=False)
    execution_state: Mapped[str] = mapped_column(String(32), nullable=False)
    delivery_state: Mapped[str] = mapped_column(String(32), nullable=False)
    delivery_token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    outcome_json: Mapped[str | None] = _json_col()
    artifact_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    validator_result: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_of_delegation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
