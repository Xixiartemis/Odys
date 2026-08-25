"""ContextBuilder (docs/05_CONTEXT_POLICY.md).

Context is NOT the full history. Each attempt gets the minimal, explainable
context needed to finish the current task. All context is built HERE — nothing
else may assemble context for an executor.

Context policies:
- CP-0: goal + current task
- CP-1: CP-0 + candidate profile + necessary evidence
- CP-2: CP-1 + failure evidence + previous attempt summary + relevant history

Every attempt's snapshot is persisted (context_snapshots) and referenced by
Attempt.context_snapshot_id — for replay, A/B comparison, token analysis and
failure analysis.
"""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from lhas.domain.models import Attempt, Task, new_id
from lhas.failure import FailureReport
from lhas.recovery import RecoveryAction


class ContextPolicy(str):
    CP_0 = "CP-0"
    CP_1 = "CP-1"
    CP_2 = "CP-2"
    CP_3 = "CP-3"

    def __ge__(self, other: "ContextPolicy") -> bool:
        order = {ContextPolicy.CP_0: 0, ContextPolicy.CP_1: 1, ContextPolicy.CP_2: 2, ContextPolicy.CP_3: 3}
        return order[self] >= order[other]


class ContextSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    task_id: str
    run_id: Optional[str] = None
    attempt_id: Optional[str] = None
    attempt_number: int
    policy: str = ContextPolicy.CP_2
    sections: dict[str, str] = Field(default_factory=dict)
    raw_text: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metrics: dict[str, Any] = Field(default_factory=dict)
    context_sha256: str = ""

@dataclass(frozen=True)
class ContextBudget:
    max_total_bytes: int = 32 * 1024
    max_working_state_bytes: int = 8 * 1024
    max_recent_history_bytes: int = 12 * 1024
    max_recent_events: int = 20

class ContextBudgetExceeded(ValueError): pass

def bounded_utf8(text: str, max_bytes: int) -> str:
    raw=text.encode("utf-8")
    if len(raw)<=max_bytes: return text
    return raw[:max_bytes].decode("utf-8", "ignore")


class ContextBuilder:
    """Assembles the attempt context. Phase B ships CP-0 and CP-2 policies
    (CP-1 profile data arrives with the Job benchmark in Phase C)."""

    def __init__(self, policy: str = ContextPolicy.CP_2, profile: Optional[dict[str, Any]] = None):
        self.policy = ContextPolicy(policy)
        self.profile = profile

    def build(
        self,
        *,
        task: Task,
        attempt_number: int,
        previous_attempts: Optional[list[Attempt]] = None,
        failure_report: Optional[FailureReport] = None,
        recovery_action: Optional[RecoveryAction] = None,
        run_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
        working_state: Optional[dict[str, Any]] = None,
        recent_history: Optional[list[dict[str, Any]]] = None,
        budget: Optional[ContextBudget] = None,
    ) -> ContextSnapshot:
        sections: dict[str, str] = {}

        # C0 — Goal
        sections["goal"] = task.objective

        # C2 — Current task
        task_text = (
            f"title: {task.title}\n"
            f"objective: {task.objective}\n"
            f"constraints: {json.dumps(task.constraints, ensure_ascii=False)}\n"
            f"acceptance_criteria: {json.dumps(task.acceptance_criteria, ensure_ascii=False)}"
        )
        sections["task"] = task_text

        # C1 — User / Profile (CP-1+; Phase C fills real profile data)
        if self.policy >= ContextPolicy.CP_1:
            sections["profile"] = json.dumps(self.profile, ensure_ascii=False) if self.profile else "(no candidate profile supplied)"

        # C4 — Previous attempts + failure evidence + recovery guidance (CP-2)
        if self.policy >= ContextPolicy.CP_2:
            prev = previous_attempts or []
            if prev:
                summaries = [
                    f"attempt {a.attempt_number}: status={a.status.value}"
                    + (f", error={a.error_message}" if a.error_message else "")
                    for a in prev
                ]
                sections["previous_attempts"] = "previous attempts:\n" + "\n".join(f"- {s}" for s in summaries)
            if failure_report is not None:
                sections["failure"] = (
                    f"failure_type: {failure_report.failure_type.value}\n"
                    f"failure_class: {failure_report.failure_class.value}\n"
                    f"evidence: {failure_report.evidence}\n"
                    f"summary: {failure_report.summary}"
                )
            if recovery_action is not None:
                guidance_parts = [
                    f"Recovery guidance (action={recovery_action.action_type.value}):",
                    f"reason: {recovery_action.reason}",
                ]
                if recovery_action.added_context:
                    guidance_parts.append(
                        "added_context: " + json.dumps(recovery_action.added_context, ensure_ascii=False)
                    )
                if failure_report is not None:
                    guidance_parts.append(f"failure_type: {failure_report.failure_type.value}")
                sections["recovery_guidance"] = "\n".join(guidance_parts)

        if self.policy >= ContextPolicy.CP_3:
            budget=budget or ContextBudget()
            state_text=json.dumps(working_state or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            history_text=json.dumps(recent_history or [], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            sections["working_state"]="working_state:\n" + bounded_utf8(state_text,budget.max_working_state_bytes)
            sections["recent_history"]="recent_history:\n" + bounded_utf8(history_text,budget.max_recent_history_bytes)

        raw_text = self._render_raw(sections)
        if budget is not None:
            mandatory="\n\n".join(sections[k] for k in ("goal","task") if k in sections)
            if len(mandatory.encode("utf-8")) > budget.max_total_bytes: raise ContextBudgetExceeded("CONTEXT_BUDGET_EXCEEDED")
            if len(raw_text.encode("utf-8")) > budget.max_total_bytes:
                for key in ("recent_history","working_state","previous_attempts","profile"):
                    if key in sections:
                        sections[key]=bounded_utf8(sections[key], max(0,budget.max_total_bytes-len("\n\n".join(v for k,v in sections.items() if k!=key).encode("utf-8"))))
                raw_text=self._render_raw(sections)
            if len(raw_text.encode("utf-8")) > budget.max_total_bytes: raise ContextBudgetExceeded("CONTEXT_BUDGET_EXCEEDED")
        context_sha=hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        return ContextSnapshot(
            task_id=task.id,
            run_id=run_id,
            attempt_id=attempt_id,
            attempt_number=attempt_number,
            policy=self.policy,
            sections=sections,
            raw_text=raw_text,
            context_sha256=context_sha,
        )

    def to_executor_context(self, snapshot: ContextSnapshot) -> dict[str, Any]:
        """What the executor actually sees for this attempt."""
        return {
            "policy": snapshot.policy,
            **snapshot.sections,
            "attempt_number": snapshot.attempt_number,
        }

    @staticmethod
    def _render_raw(sections: dict[str, str]) -> str:
        lines: list[str] = []
        for key in ["goal", "profile", "task", "previous_attempts", "failure", "recovery_guidance", "working_state", "recent_history"]:
            if key in sections:
                lines.append(sections[key])
        return "\n\n".join(lines)
