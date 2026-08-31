"""Domain-neutral planning contracts for Phase D."""

from lhas.planning.models import (
    CapabilitySpec,
    Goal,
    Plan,
    PlanMode,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
)

__all__ = [
    "CapabilitySpec", "Goal", "Plan", "PlanMode", "PlanStatus",
    "PlanStep", "PlanStepStatus",
    "PlanExecutionService", "TaskGraphScheduler", "build_step_dependency_context", "MacroReplanService", "ReplanResult",
]

def __getattr__(name):
    # Lazy imports preserve the E1 public API without introducing the
    # tools.protocol <-> planning.service initialization cycle.
    if name == "PlanExecutionService":
        from lhas.planning.service import PlanExecutionService
        return PlanExecutionService
    if name in {"TaskGraphScheduler", "build_step_dependency_context"}:
        from lhas.planning.scheduler import TaskGraphScheduler, build_step_dependency_context
        return {"TaskGraphScheduler": TaskGraphScheduler, "build_step_dependency_context": build_step_dependency_context}[name]
    if name in {"MacroReplanService", "ReplanResult"}:
        from lhas.planning.replan import MacroReplanService, ReplanResult
        return {"MacroReplanService": MacroReplanService, "ReplanResult": ReplanResult}[name]
    raise AttributeError(name)
