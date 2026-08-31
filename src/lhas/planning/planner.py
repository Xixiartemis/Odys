"""Constrained deterministic Planner protocol and baseline implementation."""

from __future__ import annotations

from typing import Any, Protocol

from lhas.planning.models import CapabilitySpec, Goal, Plan, PlanMode, PlanStep


class Planner(Protocol):
    async def create_plan(
        self, *, goal: Goal, capabilities: list[CapabilitySpec], context: dict[str, Any] | None = None
    ) -> Plan:
        ...


class DeterministicPlanner:
    """Build a linear plan from an allow-listed capability sequence.

    No Tool instance or provider is visible to this planner.  A caller may
    supply ``goal.metadata['plan_steps']`` for a domain-neutral explicit order;
    otherwise the goal allow-list order is used.
    """

    async def create_plan(self, *, goal: Goal, capabilities: list[CapabilitySpec], context=None) -> Plan:
        by_name = {spec.name: spec for spec in capabilities}
        context = context or {}
        replanning = bool(context.get("replan_signals"))
        requested = (goal.metadata.get("replan_plan_steps") if replanning else None) or goal.metadata.get("plan_steps") or goal.allowed_capabilities
        if not requested:
            requested = [spec.name for spec in capabilities]
        unknown = [name for name in requested if name not in by_name]
        if unknown:
            raise ValueError(f"planner requested unknown capability(s): {unknown}")
        if goal.allowed_capabilities:
            disallowed = [name for name in requested if name not in goal.allowed_capabilities]
            if disallowed:
                raise ValueError(f"planner capability violation: {disallowed}")
        steps: list[PlanStep] = []
        previous: str | None = None
        for index, name in enumerate(requested, start=1):
            spec = by_name[name]
            inputs = dict(goal.metadata.get("step_inputs", {}).get(name, {}))
            if name == "document.resume.read" and goal.metadata.get("resume_path"): inputs.setdefault("path", goal.metadata["resume_path"])
            if name == "web.search" and goal.metadata.get("query"): inputs.setdefault("query", goal.metadata["query"]); inputs.setdefault("max_results", 10)
            if name == "artifact.write" and goal.metadata.get("output_dir"): inputs.setdefault("output_dir", goal.metadata["output_dir"])
            step = PlanStep(
                title=f"Step {index}: {name}",
                objective=spec.description or f"Execute capability {name}",
                capability=name,
                depends_on=[previous] if previous else [],
                expected_output="structured tool result",
                success_criteria=list(goal.success_criteria),
                inputs=inputs,
            )
            steps.append(step)
            previous = step.id
        return Plan(goal_id=goal.id, mode=PlanMode.LINEAR, status="READY", steps=steps,
                    metadata={"planner": "DeterministicPlanner", "replan": replanning})
