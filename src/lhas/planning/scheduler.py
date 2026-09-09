from dataclasses import dataclass, field
from lhas.planning.models import (
    Plan, PlanStep, PlanStepStatus,
    _TERMINAL_VERIFIED_STATUSES, _FAILED_OR_BLOCKED_STATUSES,
    evaluate_step_eligibility,
)

@dataclass(frozen=True)
class Schedule:
    ready_steps: list = field(default_factory=list)
    blocked_steps: list = field(default_factory=list)
    pending_steps: list = field(default_factory=list)
    waiting_steps: list = field(default_factory=list)

class TaskGraphScheduler:
    """Pure SIMPLE_DEPENDENCY scheduler; never executes tools or providers.

    Phase 3: delegates eligibility to the centralized evaluate_step_eligibility()
    to maintain single-authority semantics. Scheduler remains a pure calculator.
    """

    def calculate(self, plan: Plan) -> Schedule:
        by_id = {s.id: s for s in plan.steps}
        ready: list[PlanStep] = []
        blocked: list[PlanStep] = []
        pending: list[PlanStep] = []
        waiting: list[PlanStep] = []

        for step in plan.steps:
            # Terminal / done steps — skip (do not re-dispatch)
            if step.status in _TERMINAL_VERIFIED_STATUSES | _FAILED_OR_BLOCKED_STATUSES | {PlanStepStatus.STALE}:
                continue

            # Waiting for human approval
            if step.status == PlanStepStatus.WAITING_FOR_HUMAN_APPROVAL:
                waiting.append(step)
                continue

            # Already active (RUNNING, CLAIMED_COMPLETE, WAITING_FOR_VERIFICATION, etc.) — skip
            if step.status in {PlanStepStatus.RUNNING, PlanStepStatus.CLAIMED_COMPLETE, PlanStepStatus.READY, PlanStepStatus.WAITING_FOR_VERIFICATION}:
                continue

            # BLOCKER A: delegate to single eligibility authority
            eligible, reason = evaluate_step_eligibility(step, by_id)

            if eligible:
                ready.append(step)
            elif any(token in reason for token in ("failed", "blocked")):
                blocked.append(step)
            else:
                # not_verified, precondition_failed, missing_dependency, etc.
                pending.append(step)

        return Schedule(ready, blocked, pending, waiting)

def build_step_dependency_context(plan, step, execution_context):
    allowed=set(step.depends_on); by_id={s.id:s for s in plan.steps}
    changed=True
    while changed:
        changed=False
        for dep in list(allowed):
            for parent in by_id[dep].depends_on:
                if parent not in allowed: allowed.add(parent); changed=True
    return {"runtime":execution_context.get("runtime",{}),"steps":{i:execution_context["steps"][i] for i in allowed if i in execution_context.get("steps",{})}}
