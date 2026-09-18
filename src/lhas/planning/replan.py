"""Production macro-replan consumer for the existing Plan/TaskGraph."""

from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
from typing import Any

from lhas.domain.enums import EventType
from lhas.persistence.event_store import EventStore
from lhas.persistence.planning_repositories import PlanRepository, PlanVersionConflict
from lhas.planning.models import (
    Goal, Plan, PlanStatus, PlanStepStatus,
    _TERMINAL_VERIFIED_STATUSES,
    compute_step_semantic_fingerprint,
    transition_step,
)


@dataclass(frozen=True)
class ReplanResult:
    accepted: bool
    plan: Plan
    signal_count: int
    invalidated_step_ids: tuple[str, ...] = ()
    error_type: str | None = None
    old_strategy_fingerprint: str | None = None
    new_strategy_fingerprint: str | None = None
    affected_subgraph_ids: tuple[str, ...] = ()


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

    @staticmethod
    def _strategy_fingerprint(strategy: list[tuple[str, str]]) -> str:
        encoded = json.dumps(strategy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _affected_subgraph(plan: Plan, signals: list[Any]) -> set[str]:
        """Return failed nodes plus their transitive dependents.

        Verified nodes are deliberately never included as invalidation targets;
        this projection is used for audit evidence and scope checks, while the
        existing preservation logic remains the authority for their status.
        """
        affected = {
            str(signal.failed_node_id)
            for signal in signals
            if getattr(signal, "failed_node_id", None)
        }
        changed = True
        while changed:
            changed = False
            for step in plan.steps:
                if step.id in affected:
                    continue
                if any(dependency in affected for dependency in step.depends_on):
                    affected.add(step.id)
                    changed = True
        return affected

    async def consume(self, *, goal: Goal, plan: Plan, signals: list[Any], context: dict[str, Any] | None = None) -> ReplanResult:
        if not signals:
            return ReplanResult(False, plan, 0)
        caller_plan = plan
        authoritative = self.plans.get(plan.id)
        if authoritative is None:
            return ReplanResult(False, plan, len(signals), error_type="PLAN_NOT_FOUND")
        expected_plan_version = str(authoritative.version)
        plan = authoritative.model_copy(deep=True)
        if plan.status in {PlanStatus.COMPLETED}:
            self.events.append(EventType.REPLAN_REJECTED, payload={"plan_id": plan.id, "reason": "plan already completed"})
            return ReplanResult(False, plan, len(signals))
        affected_subgraph_ids = self._affected_subgraph(plan, signals)
        planner_context = dict(context or {})
        planner_context.update({
            "replan_signals": [item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item) for item in signals[-20:]],
            "current_plan": plan.model_dump(mode="json"),
            "completed_nodes": [step.id for step in plan.steps if step.status in _TERMINAL_VERIFIED_STATUSES],
        })
        proposal = await self.planner.create_plan(goal=goal, capabilities=context.get("capabilities", []) if context else [], context=planner_context)
        if not proposal.steps:
            self.events.append(EventType.REPLAN_REJECTED, payload={"plan_id": plan.id, "reason": "planner returned empty graph"})
            return ReplanResult(False, plan, len(signals))
        completed_strategy = [
            (step.capability, json.dumps(step.inputs, sort_keys=True, ensure_ascii=False))
            for step in plan.steps
            if step.status in _TERMINAL_VERIFIED_STATUSES
        ]
        current_strategy = [
            (step.capability, json.dumps(step.inputs, sort_keys=True, ensure_ascii=False))
            for step in plan.steps
            if step.status not in _TERMINAL_VERIFIED_STATUSES | {PlanStepStatus.STALE}
        ]
        proposed_strategy = [
            (step.capability, json.dumps(step.inputs, sort_keys=True, ensure_ascii=False))
            for step in proposal.steps
        ]
        for completed in completed_strategy:
            if completed in proposed_strategy:
                proposed_strategy.remove(completed)
        old_strategy_fingerprint = self._strategy_fingerprint(current_strategy)
        new_strategy_fingerprint = self._strategy_fingerprint(proposed_strategy)
        if current_strategy == proposed_strategy:
            self.events.append(EventType.REPLAN_REJECTED, payload={
                "plan_id": plan.id,
                "reason": "planner returned unchanged strategy",
                "error_type": "REPLAN_NO_CHANGE",
                "old_strategy_fingerprint": old_strategy_fingerprint,
                "new_strategy_fingerprint": new_strategy_fingerprint,
                "affected_subgraph_ids": sorted(affected_subgraph_ids),
            })
            return ReplanResult(
                False,
                plan,
                len(signals),
                error_type="REPLAN_NO_CHANGE",
                old_strategy_fingerprint=old_strategy_fingerprint,
                new_strategy_fingerprint=new_strategy_fingerprint,
                affected_subgraph_ids=tuple(sorted(affected_subgraph_ids)),
            )
        old_by_fingerprint = {}
        old_by_id = {step.id: step for step in plan.steps}
        for step in plan.steps:
            if step.status in _TERMINAL_VERIFIED_STATUSES:
                old_by_fingerprint.setdefault(compute_step_semantic_fingerprint(step, old_by_id), step)
        invalidated = []
        proposal_ids = {item.id for item in proposal.steps}
        # BLOCKER E: route STALE transitions through transition_step().  A
        # macro replan is scoped to the failed node and its descendants;
        # unrelated pending work remains executable and is not silently
        # converted into stale work.
        for old in plan.steps:
            if (
                old.id in affected_subgraph_ids
                and old.status not in _TERMINAL_VERIFIED_STATUSES | {PlanStepStatus.STALE}
            ):
                transition_step(old, PlanStepStatus.STALE, "replan_invalidation", self.events, plan_id=plan.id)
                invalidated.append(old.id)
        preserved_ids = set()
        id_remap = {}
        proposed_by_id = {step.id: step for step in proposal.steps}
        for step in proposal.steps:
            completed = old_by_fingerprint.get(compute_step_semantic_fingerprint(step, proposed_by_id))
            if completed is not None:
                id_remap[step.id] = completed.id
                step.id = completed.id
                # BLOCKER E: route VERIFIED preservation through transition_step()
                # Use transition_step() so the PENDING→VERIFIED transition is audited.
                transition_step(step, PlanStepStatus.VERIFIED, "replan_preserved_verified", self.events, plan_id=plan.id)
                step.output = completed.output
                step.task_id = completed.task_id
                step.execution_context = completed.execution_context
                preserved_ids.add(completed.id)
        # BLOCKER E: route STALE transitions for unpreserved completed steps
        for old in plan.steps:
            if (
                old.id in affected_subgraph_ids
                and old.status in _TERMINAL_VERIFIED_STATUSES
                and old.id not in preserved_ids
            ):
                transition_step(old, PlanStepStatus.STALE, "replan_unpreserved", self.events, plan_id=plan.id)
                invalidated.append(old.id)
        # Rows omitted by a revised proposal remain in the durable plan table;
        # retire unrelated pending rows explicitly so a reload cannot dispatch
        # superseded work.  This is not repair invalidation and is deliberately
        # excluded from invalidated_step_ids: only the failed subgraph is
        # eligible for recovery execution.
        for old in plan.steps:
            if (
                old.id not in proposal_ids
                and old.id not in affected_subgraph_ids
                and old.status not in _TERMINAL_VERIFIED_STATUSES | {PlanStepStatus.STALE}
            ):
                transition_step(old, PlanStepStatus.STALE, "replan_superseded_unaffected", self.events, plan_id=plan.id)
        for step in proposal.steps:
            step.depends_on = [id_remap.get(item, item) for item in step.depends_on]
        # Retain completed and stale nodes in the canonical graph for audit;
        # only the revised pending graph is executable.
        # Preserved steps (fingerprint-matched) come from the proposal with
        # VERIFIED status — exclude the old COMPLETED copy from retained.
        retained = [
            item
            for item in plan.steps
            if item.id not in preserved_ids
            and item.id not in proposal_ids
            and item.status in _TERMINAL_VERIFIED_STATUSES | {PlanStepStatus.STALE}
        ]
        plan.steps = retained + list(proposal.steps)
        by_id = {step.id: step for step in plan.steps}
        for step in plan.steps:
            step.semantic_fingerprint = compute_step_semantic_fingerprint(step, by_id)
        plan.version = f"{plan.version}-r{plan.replan_count + 1}"
        plan.replan_count += 1
        plan.status = PlanStatus.RUNNING
        plan.invalidated_step_ids.extend(item for item in invalidated if item not in plan.invalidated_step_ids)
        consumed = set(plan.metadata.get("consumed_replan_signal_ids", []))
        plan.metadata.update({
            "last_replan_reason": signals[-1].reason,
            "last_replan_signal_ids": [item.id for item in signals[-20:]],
            "consumed_replan_signal_ids": list(consumed | {item.id for item in signals})[-100:],
            "replan_count": plan.replan_count,
        })
        try:
            self.plans.update_if_version(plan, expected_version=expected_plan_version)
        except PlanVersionConflict:
            current = self.plans.get(plan.id) or plan
            self.events.append(EventType.REPLAN_REJECTED, payload={
                "plan_id": plan.id,
                "reason": "REPLAN_VERSION_CONFLICT",
                "expected_plan_version": expected_plan_version,
                "authoritative_plan_version": current.version,
            })
            return ReplanResult(False, current, len(signals), error_type="REPLAN_VERSION_CONFLICT")
        # Keep the historical in-memory API contract while the durable write
        # remains guarded by the authoritative version CAS above.
        for field in Plan.model_fields:
            setattr(caller_plan, field, getattr(plan, field))
        self.events.append(EventType.REPLAN_ACCEPTED, payload={"plan_id": plan.id, "new_version": plan.version,
            "signal_count": len(signals), "invalidated_step_ids": invalidated,
            "preserved_completed_node_ids": sorted(preserved_ids),
            "affected_subgraph_ids": sorted(affected_subgraph_ids),
            "old_strategy_fingerprint": old_strategy_fingerprint,
            "new_strategy_fingerprint": new_strategy_fingerprint,
        })
        return ReplanResult(
            True,
            plan,
            len(signals),
            tuple(invalidated),
            old_strategy_fingerprint=old_strategy_fingerprint,
            new_strategy_fingerprint=new_strategy_fingerprint,
            affected_subgraph_ids=tuple(sorted(affected_subgraph_ids)),
        )
