"""Planner boundary and deterministic platform planner."""

from __future__ import annotations

from typing import Any, Protocol

from lhas.planning.models import CapabilitySpec, Goal, Plan, PlanMode, PlanStep


class ModelPlanner(Protocol):
    async def create_plan(self, *, goal: Goal, capabilities: list[CapabilitySpec], context: dict[str, Any] | None = None) -> Plan: ...


class ScriptedPlatformPlanner:
    """Three-step schema-valid planner for the network-free vertical slice."""

    CAPABILITIES = ("platform.prepare", "platform.delegate", "platform.finalize")

    async def create_plan(self, *, goal: Goal, capabilities: list[CapabilitySpec], context=None) -> Plan:
        available={item.name for item in capabilities}
        missing=set(self.CAPABILITIES)-available
        if missing:
            raise ValueError(f"platform planner capabilities missing: {sorted(missing)}")
        steps=[]
        previous=None
        definitions=[
            ("Prepare bounded execution context","platform.prepare","WORKER",["coding/bug-fix"]),
            ("Delegate focused evidence research","platform.delegate","WORKER",["coding/code-review"]),
            ("Assemble validated platform result","platform.finalize","REVIEWER",["coding/code-review"]),
        ]
        for title,capability,role,skills in definitions:
            step=PlanStep(
                title=title,
                objective=f"{title} for: {goal.objective}",
                capability=capability,
                depends_on=[previous] if previous else [],
                expected_output="bounded structured result",
                success_criteria=list(goal.success_criteria) or ["deterministic step output is non-empty"],
                suggested_role=role,
                required_capabilities=[capability],
                optional_skill_refs=skills,
                inputs={"goal":goal.objective},
            )
            steps.append(step); previous=step.id
        return Plan(goal_id=goal.id,mode=PlanMode.SIMPLE_DEPENDENCY,status="READY",steps=steps,version="P-1.0")
