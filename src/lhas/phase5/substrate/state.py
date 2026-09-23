"""Verified Task State and Control State.

VerifiedTaskState contains ONLY trusted, independently verified facts.
ControlState represents runtime control-plane state.

Invariant: UNVERIFIED_AGENT_CLAIM_CANNOT_MUTATE_VERIFIED_STATE
Invariant: CONTROL_STATE_CANNOT_BE_USED_AS_VERIFIED_ENVIRONMENT_STATE
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


def _new_id() -> str:
    import uuid
    return uuid.uuid4().hex


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _state_digest(facts: dict[str, Any], artifacts: dict[str, Any]) -> str:
    """Deterministic digest of verified state."""
    payload = _canonical_json({"facts": facts, "artifacts": artifacts})
    return hashlib.sha256(payload).hexdigest()


class VerifiedFact(BaseModel):
    """A single independently verified fact.

    Must be backed by validator-accepted evidence.
    Model claims are NEVER verified facts.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: str = Field(default_factory=_new_id)
    key: str
    value: Any
    evidence_id: str  # which EvidenceEvent established this fact
    commit_id: str    # which StateCommit promoted it
    verified_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def as_dict_entry(self) -> tuple[str, Any]:
        return self.key, self.value


class VerifiedTaskState(BaseModel):
    """Materialized view of all verified facts and artifacts.

    Only promoted through StateCommitter after validator ACCEPT.
    Agent claims cannot directly mutate this state.
    """
    model_config = ConfigDict(extra="forbid")

    task_id: str
    run_id: str
    goal: str
    state_version: int = 0
    verified_facts: dict[str, VerifiedFact] = Field(default_factory=dict)
    verified_artifact_ids: list[str] = Field(default_factory=list)
    status: str = "ACTIVE"  # ACTIVE | COMPLETED | FAILED | ESCALATED
    last_commit_id: Optional[str] = None
    state_digest: str = ""

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        if not self.state_digest:
            self.state_digest = self._compute_digest()

    def _compute_digest(self) -> str:
        facts = {k: f.value for k, f in sorted(self.verified_facts.items())}
        arts = sorted(self.verified_artifact_ids)
        return _state_digest(facts, arts)

    def fact_value(self, key: str, default: Any = None) -> Any:
        f = self.verified_facts.get(key)
        return f.value if f else default

    def with_commit(
        self,
        *,
        commit_id: str,
        feedback_id: str,
        new_facts: list[VerifiedFact],
        new_artifact_ids: list[str],
        new_status: Optional[str] = None,
    ) -> "VerifiedTaskState":
        """Produce a new immutable state version."""
        updated_facts = dict(self.verified_facts)
        for f in new_facts:
            updated_facts[f.key] = f
        updated_arts = list(set(self.verified_artifact_ids + new_artifact_ids))
        new_version = self.state_version + 1
        result = VerifiedTaskState(
            task_id=self.task_id,
            run_id=self.run_id,
            goal=self.goal,
            state_version=new_version,
            verified_facts=updated_facts,
            verified_artifact_ids=updated_arts,
            status=new_status or self.status,
            last_commit_id=commit_id,
        )
        return result

    def rebuild_digest(self) -> str:
        """Recompute digest from current facts — for parity testing."""
        return self._compute_digest()


class ControlState(BaseModel):
    """Runtime/control-plane state.  NOT environment truth.

    Transient, not durable.  Used by recovery policy for budget tracking.
    """
    model_config = ConfigDict(extra="forbid")

    task_id: str
    run_id: str
    attempt_id: str
    turn_index: int = 0
    strategy_epoch: int = 0

    remaining_model_calls: int = 50
    remaining_tokens: Optional[int] = None
    remaining_repair_budget: int = 3
    remaining_replan_budget: int = 1

    last_progress_evidence_id: Optional[str] = None
    pending_validation_candidate_id: Optional[str] = None
    execution_status: str = "RUNNING"  # RUNNING | BLOCKED | COMPLETED | FAILED

    def decrement_call(self) -> None:
        self.remaining_model_calls = max(0, self.remaining_model_calls - 1)
        self.turn_index += 1

    def decrement_repair(self) -> bool:
        if self.remaining_repair_budget <= 0:
            return False
        self.remaining_repair_budget -= 1
        return True

    def decrement_replan(self) -> bool:
        if self.remaining_replan_budget <= 0:
            return False
        self.remaining_replan_budget -= 1
        return True


class StateCommit(BaseModel):
    """Immutable record of a verified state transition.

    Only created by StateCommitter after validator ACCEPT + SUCCESS.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    commit_id: str = Field(default_factory=_new_id)
    feedback_id: str
    previous_state_version: int
    next_state_version: int
    accepted_evidence_ids: list[str] = Field(default_factory=list)
    accepted_artifact_ids: list[str] = Field(default_factory=list)
    resulting_state_digest: str = ""
    committed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def validate_version_increment(self) -> bool:
        return self.next_state_version == self.previous_state_version + 1
