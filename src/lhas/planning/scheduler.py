from dataclasses import dataclass
from lhas.planning.models import Plan, PlanStepStatus, _TERMINAL_VERIFIED_STATUSES, _FAILED_OR_BLOCKED_STATUSES, evaluate_step_eligibility

@dataclass(frozen=True)
class Schedule:
    ready_steps: list
    blocked_steps: list
    pending_steps: list
    waiting_steps: list

class TaskGraphScheduler:
    """Pure SIMPLE_DEPENDENCY scheduler; never executes tools or providers.

    Phase 3: delegates eligibility to the centralized evaluate_step_eligibility()
    to maintain single-authority semantics. Scheduler remains a pure calculator.
    """
    def calculate(self, plan: Plan) -> Schedule:
        by_id = {s.id: s for s in plan.steps}
        ready = []
        blocked = []
        pending = []
        waiting = []
        for step in plan.steps:
            if step.status == PlanStepStatus.WAITING_FOR_HUMAN_APPROVAL:
                waiting.append(step)
                continue
            if step.status in {
                PlanStepStatus.COMPLETED, PlanStepStatus.VERIFIED,
                PlanStepStatus.FAILED, PlanStepStatus.BLOCKED,
                PlanStepStatus.STALE, PlanStepStatus.CLASSIFIED_FAILURE,
            }:
                continue
            deps = [by_id[d] for d in step.depends_on]
            if any(d.status in _FAILED_OR_BLOCKED_STATUSES for d in deps):
                blocked.append(step)
            elif all(d.status in _TERMINAL_VERIFIED_STATUSES for d in deps):
                ready.append(step)
            else:
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
