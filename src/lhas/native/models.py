"""Provider-neutral state contracts for the Odys-owned agent loop."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lhas.domain.models import new_id, utcnow


class NativePhase(str, Enum):
    CONTINUE = "CONTINUE"
    WAITING_TOOL = "WAITING_TOOL"
    WAITING_CHILD = "WAITING_CHILD"
    CANDIDATE_COMPLETE = "CANDIDATE_COMPLETE"
    RECOVERING = "RECOVERING"
    REPLANNING = "REPLANNING"
    FAILED = "FAILED"
    ACCEPTED_COMPLETE = "ACCEPTED_COMPLETE"


class RuntimeTarget(BaseModel):
    """Secret-free, immutable identity of one executable provider route.

    ``model_id`` alone is deliberately insufficient: two providers can expose
    the same model while having different endpoint and credential routes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=128)
    endpoint_identity: str = Field(min_length=1, max_length=256)
    credential_route_id: str = Field(min_length=1, max_length=128)
    route_type: str = Field(min_length=1, max_length=64)

    @field_validator("endpoint_identity", mode="before")
    @classmethod
    def normalize_endpoint_identity(cls, value: Any) -> str:
        value = str(value)
        if "://" in value:
            parsed = urlparse(value)
            value = parsed.hostname or "unknown-endpoint"
        return value.split("@", 1)[-1][:256]

    @property
    def composite_id(self) -> str:
        return "|".join((self.provider_id, self.model_id, self.endpoint_identity, self.credential_route_id, self.route_type))

    def safe_projection(self) -> dict[str, str]:
        return self.model_dump(mode="json")

    def display(self) -> str:
        return f"{self.model_id} @ {self.provider_id} ({self.endpoint_identity})"


class ProviderFailureCategory(str, Enum):
    TRANSIENT_RATE_LIMIT = "TRANSIENT_RATE_LIMIT"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    BILLING_OR_CREDIT_EXHAUSTED = "BILLING_OR_CREDIT_EXHAUSTED"
    AUTH_INVALID = "AUTH_INVALID"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    MALFORMED_PROVIDER_RESPONSE = "MALFORMED_PROVIDER_RESPONSE"
    UNKNOWN_PROVIDER_FAILURE = "UNKNOWN_PROVIDER_FAILURE"


class ProviderHealthState(str, Enum):
    HEALTHY = "HEALTHY"
    TRANSIENTLY_UNAVAILABLE = "TRANSIENTLY_UNAVAILABLE"
    QUOTA_BLOCKED = "QUOTA_BLOCKED"
    AUTH_BLOCKED = "AUTH_BLOCKED"
    UNKNOWN = "UNKNOWN"


class TargetSwitchState(str, Enum):
    REQUESTED = "REQUESTED"
    PENDING = "PENDING"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"


class TargetSwitch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    execution_id: str
    state: TargetSwitchState
    previous_target: RuntimeTarget
    requested_target: RuntimeTarget
    effective_target: RuntimeTarget
    failure_reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class InvocationState(str, Enum):
    REQUESTED = "REQUESTED"
    STARTED = "STARTED"
    FINISHED = "FINISHED"
    RECONCILED = "RECONCILED"


class SideEffectClass(str, Enum):
    READ_ONLY = "READ_ONLY"
    WORKSPACE_MUTATION = "WORKSPACE_MUTATION"
    DELEGATION = "DELEGATION"
    EXTERNAL = "EXTERNAL"


class ReconciliationDecision(str, Enum):
    SAFE_TO_RETRY = "SAFE_TO_RETRY"
    DO_NOT_RETRY = "DO_NOT_RETRY"
    RECONCILE_FIRST = "RECONCILE_FIRST"
    UNKNOWN = "UNKNOWN"


class CandidateStatus(str, Enum):
    CANDIDATE_COMPLETION = "CANDIDATE_COMPLETION"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class ProviderToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ProviderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(default="", max_length=40_000)
    tool_calls: list[ProviderToolCall] = Field(default_factory=list)
    completion_claim: bool = False
    usage: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tool_calls")
    @classmethod
    def bound_tool_calls(cls, value: list[ProviderToolCall]) -> list[ProviderToolCall]:
        if len(value) > 64:
            raise ValueError("provider response exceeds per-turn tool-call bound")
        return value


class ModelContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[dict[str, Any]] = Field(default_factory=list)
    sections: dict[str, Any] = Field(default_factory=dict)
    chars_used: int = Field(ge=0)
    budget_chars: int = Field(ge=1)
    truncated_sections: list[str] = Field(default_factory=list)


class ExecutionSnapshot(BaseModel):
    """Latest bounded, reconstructable state for one durable Attempt."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    task_id: str
    run_id: str
    attempt_id: str
    goal: str = Field(max_length=20_000)
    phase: NativePhase = NativePhase.CONTINUE
    version: int = Field(default=1, ge=1)
    model_turn_count: int = Field(default=0, ge=0)
    tool_call_count: int = Field(default=0, ge=0)
    taskgraph_position: str | None = None
    completed_nodes: list[str] = Field(default_factory=list)
    pending_nodes: list[str] = Field(default_factory=list)
    workspace_identity: dict[str, Any] = Field(default_factory=dict)
    workspace_mutation_version: int = Field(default=0, ge=0)
    recent_tool_outcomes: list[dict[str, Any]] = Field(default_factory=list)
    repeated_failure_state: dict[str, Any] = Field(default_factory=dict)
    verification_state: dict[str, Any] = Field(default_factory=dict)
    current_failure: dict[str, Any] = Field(default_factory=dict)
    recovery_lineage: list[str] = Field(default_factory=list)
    checkpoint_lineage: list[str] = Field(default_factory=list)
    delegation_dependencies: dict[str, Any] = Field(default_factory=dict)
    completion_candidate_id: str | None = None
    consumed_delivery_tokens: list[str] = Field(default_factory=list)
    configured_target: RuntimeTarget | None = None
    effective_target: RuntimeTarget | None = None
    actual_provider_target: RuntimeTarget | None = None
    fallback_reason: str | None = Field(default=None, max_length=512)
    target_event_id: str | None = None
    model_turn_ordinal: int = Field(default=0, ge=0)
    updated_at: datetime = Field(default_factory=utcnow)

    @field_validator("completed_nodes", "pending_nodes", "recovery_lineage", "checkpoint_lineage")
    @classmethod
    def bound_string_lists(cls, value: list[str]) -> list[str]:
        return [str(item)[:128] for item in value[-100:]]

    @field_validator("recent_tool_outcomes")
    @classmethod
    def bound_outcomes(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return value[-40:]

    @field_validator("consumed_delivery_tokens")
    @classmethod
    def bound_delivery_tokens(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(str(item)[:64] for item in value))[-256:]


class ToolInvocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    task_id: str
    run_id: str
    attempt_id: str
    ordinal: int = Field(ge=1)
    capability: str
    args_fingerprint: str = Field(min_length=64, max_length=64)
    side_effect_class: SideEffectClass
    state: InvocationState = InvocationState.REQUESTED
    observed_mutation: bool = False
    result_status: str | None = None
    error_type: str | None = None
    result_summary: dict[str, Any] = Field(default_factory=dict)
    reconciliation: ReconciliationDecision | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class CompletionCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    task_id: str
    run_id: str
    attempt_id: str
    source: str = Field(max_length=48)
    status: CandidateStatus = CandidateStatus.CANDIDATE_COMPLETION
    claim_sha256: str = Field(min_length=64, max_length=64)
    summary: str = Field(default="", max_length=8_000)
    validation: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ValidationFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    candidate_id: str
    attempt_id: str
    category: str = Field(max_length=128)
    failed_criterion: str = Field(default="", max_length=2_000)
    safe_evidence: str = Field(default="", max_length=8_000)
    recommended_recovery: str = Field(default="REPAIR_AND_REVALIDATE", max_length=128)
    created_at: datetime = Field(default_factory=utcnow)


class ReplanSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    task_id: str
    run_id: str
    attempt_id: str
    reason: str = Field(max_length=128)
    scope: str = Field(default="ATTEMPT", max_length=32)
    failed_node_id: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class NativeFaultPoint(str, Enum):
    AFTER_MODEL_TURN_PERSISTED = "AFTER_MODEL_TURN_PERSISTED"
    AFTER_TOOL_REQUESTED = "AFTER_TOOL_REQUESTED"
    AFTER_TOOL_STARTED = "AFTER_TOOL_STARTED"
    AFTER_TOOL_EXECUTED = "AFTER_TOOL_EXECUTED"
    AFTER_TOOL_OBSERVED = "AFTER_TOOL_OBSERVED"
    AFTER_CANDIDATE_PERSISTED = "AFTER_CANDIDATE_PERSISTED"
    AFTER_CANDIDATE_VALIDATED = "AFTER_CANDIDATE_VALIDATED"


class NoOpNativeFaultInjector:
    def hit(self, point: NativeFaultPoint, **context: Any) -> None:
        return None
