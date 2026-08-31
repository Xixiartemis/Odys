"""Production macro-replan consumer for the existing Plan/TaskGraph."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lhas.domain.enums import EventType
from lhas.persistence.event_store import EventStore
from lhas.persistence.planning_repositories import PlanRepository
from lhas.planning.models import Goal, Plan, PlanStatus, PlanStepStatus


@dataclass(frozen=True)
class ReplanResult:
    accepted: bool
    plan: Plan
    signal_count: int
    invalidated_step_ids: tuple[str, ...] = ()


class MacroReplanService:
    """Consume durable signals and revise the canonical graph in place.

    The planner proposes graph content; this service owns acceptance and
    preserves completed work. Completion authority remains outside the
    planner and still belongs to the outer validator.
    """

    def __init__(self, db, planner):
        self.db = db
        self.planner = planner
        self.plans = PlanRepository(db)
        self.events = EventStore(db)

    async def consume(self, *, goal: Goal, plan: Plan, signals: list[Any], context: dict[str, Any] | None = None) -> ReplanResult:
        if not signals:
            return ReplanResult(False, plan, 0)
        if plan.status in {PlanStatus.COMPLETED}:
            self.events.append(EventType.REPLAN_REJECTED, payload={"plan_id": plan.id, "reason": "plan already completed"})
            return ReplanResult(False, plan, len(signals))
        planner_context = dict(context or {})
        planner_context.update({
            "replan_signals": [item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item) for item in signals[-20:]],
            "current_plan": plan.model_dump(mode="json"),
            "completed_nodes": [step.id for step in plan.steps if step.status is PlanStepStatus.COMPLETED],
        })
        proposal = await self.planner.create_plan(goal=goal, capabilities=context.get("capabilities", []) if context else [], context=planner_context)
        if not proposal.steps:
            self.events.append(EventType.REPLAN_REJECTED, payload={"plan_id": plan.id, "reason": "planner returned empty graph"})
            return ReplanResult(False, plan, len(signals))
        old_by_capability = {}
        for step in plan.steps:
            if step.status is PlanStepStatus.COMPLETED:
                old_by_capability.setdefault(step.capability, step)
        proposed_capabilities = {step.capability for step in proposal.steps}
        invalidated = []
        for old in plan.steps:
            if old.status not in {PlanStepStatus.COMPLETED, PlanStepStatus.STALE} and old.capability not in proposed_capabilities:
                old.status = PlanStepStatus.STALE
                invalidated.append(old.id)
        preserved_ids = set()
        id_remap = {}
        for step in proposal.steps:
            completed = old_by_capability.get(step.capability)
            if completed is not None:
                id_remap[step.id] = completed.id
                step.id = completed.id
                step.status = PlanStepStatus.COMPLETED
                step.output = completed.output
                step.task_id = completed.task_id
                step.execution_context = completed.execution_context
                preserved_ids.add(completed.id)
        for step in proposal.steps:
            step.depends_on = [id_remap.get(item, item) for item in step.depends_on]
        # Retain completed and stale nodes in the canonical graph for audit;
        # only the revised pending graph is executable.
        retained = [item for item in plan.steps if item.status in {PlanStepStatus.COMPLETED, PlanStepStatus.STALE}]
        plan.steps = retained + [item for item in proposal.steps if item.id not in preserved_ids]
        plan.version = f"{plan.version}-r{plan.replan_count + 1}"
        plan.replan_count += 1
        plan.status = PlanStatus.RUNNING
        plan.invalidated_step_ids.extend(item for item in invalidated if item not in plan.invalidated_step_ids)
        plan.metadata.update({"last_replan_reason": signals[-1].reason, "last_replan_signal_ids": [item.id for item in signals[-20:]], "replan_count": plan.replan_count})
        self.plans.update(plan)
        self.events.append(EventType.REPLAN_ACCEPTED, payload={"plan_id": plan.id, "new_version": plan.version,
            "signal_count": len(signals), "invalidated_step_ids": invalidated,
            "preserved_completed_node_ids": sorted(preserved_ids)})
        return ReplanResult(True, plan, len(signals), tuple(invalidated))
