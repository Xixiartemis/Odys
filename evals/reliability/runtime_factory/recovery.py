"""Official Odys recovery bridge for the Phase 4 execution adapter.

This module is the one explicit bridge between the official benchmark runtime
and the existing P3.3 repair authority.  It does not implement a second
repair policy: scope selection, attempt creation, lineage persistence, and
durable repair events remain owned by :class:`PlanExecutionService`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from evals.reliability.run_phase4 import (
    ATTEMPT_LOCAL_BUDGET_FAILURES,
    ExecutionOutcome,
    ROOT_API_BUDGET_FAILURE,
)


class OfficialRecoveryContractError(RuntimeError):
    """Raised when the official recovery context cannot be proven."""


def _event_timestamp_utc(value: Any) -> str:
    """Serialize DB event timestamps as UTC without local-time reinterpretation.

    SQLite/SQLAlchemy may return a timezone-naive value after reopening a
    database even though the producer wrote an aware UTC timestamp.  A naive
    persisted value from this event store is therefore treated as UTC, not as
    the host's local timezone.
    """
    from datetime import datetime, timezone

    if not isinstance(value, datetime):
        raise OfficialRecoveryContractError("EVENT_TIMESTAMP_INVALID")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class _KernelTaskExecutor:
    """Adapt the existing native kernel to the planning executor protocol."""

    name = "OfficialOdysKernelRecoveryExecutor"

    def __init__(self, kernel: Any, provider: Any = None):
        self.kernel = kernel
        self.provider = provider

    async def execute(self, request: Any) -> Any:
        from lhas.agent.models import AgentBudget, AgentRequest, AgentRole, AgentStatus
        from lhas.domain.enums import ExecutionStatus
        from lhas.executors.protocol import ExecutionResult

        task = request.task if isinstance(request.task, Mapping) else {}
        context = dict(request.context or {})
        context.setdefault(
            "acceptance_criteria",
            list(task.get("acceptance_criteria", [])),
        )
        context.setdefault("taskgraph", {})
        budget = AgentBudget(
            max_turns=int(task.get("max_turns", 20)),
            max_tool_calls=int(task.get("max_model_calls", 20)),
        )
        metadata = {
            "task_id": request.task_id,
            "run_id": request.run_id,
            "attempt_id": request.attempt_id,
        }
        binder = getattr(self.provider, "bind_execution_context", None)
        if callable(binder):
            binder(
                run_id=request.run_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                phase="repair",
            )
        agent_request = AgentRequest(
            agent_id=f"odys-repair-{request.run_id}",
            role=AgentRole.WORKER,
            objective=str(task.get("objective", "")),
            context=context,
            messages=[],
            allowed_capabilities=set(
                task.get("required_capabilities", [])
                or context.get("allowed_capabilities", [])
            ),
            budget=budget,
            metadata=metadata,
        )
        result = await self.kernel.run(agent_request)
        completed = result.status is AgentStatus.COMPLETED
        return ExecutionResult(
            status=ExecutionStatus.SUCCESS if completed else ExecutionStatus.FAILURE,
            output=result.final_output,
            error_type=result.error_type,
            error_message=result.error_message,
            usage=dict(result.usage or {}),
            artifacts=dict(result.artifacts or {}),
            raw={
                "safe_trace": list(result.safe_trace or []),
                "completion_claim": bool(result.completion_claim),
                "status": result.status.value,
                "usage": dict(result.usage or {}),
                "turn_count": result.turn_count,
                "tool_call_count": result.tool_call_count,
            },
        )

    async def resume(self, request: Any) -> Any:
        return await self.execute(request)

    async def cancel(self, _run_id: str) -> None:
        return None

    async def status(self, run_id: str) -> dict[str, str]:
        return {"run_id": run_id}


class OfficialOdysRecoveryCoordinator:
    """Own one persisted P3.3 repair context for one official run."""

    def __init__(
        self,
        *,
        db: Any,
        kernel: Any,
        registry: Any,
        capability_registry: Any,
        tool_contract: Any,
    ) -> None:
        from lhas.planning.service import PlanExecutionService
        from lhas.planning.planner import DeterministicPlanner
        from lhas.planning.verification import WorkflowVerifier

        self.db = db
        self.kernel = kernel
        self.registry = registry
        self._contexts: dict[str, dict[str, Any]] = {}
        executor = _KernelTaskExecutor(
            kernel,
            provider=getattr(kernel, "provider", None),
        )
        self.service = PlanExecutionService(
            db,
            # This is the existing production macro planner used by the
            # durable workflow runtime.  Local repair normally resumes the
            # persisted plan without invoking it; escalation/replan paths
            # receive the real planner rather than a placeholder guard.
            DeterministicPlanner(),
            registry,
            agent_executor_factory=lambda _step: executor,
            capability_registry=capability_registry,
            tool_contract=tool_contract,
            workflow_verifier=WorkflowVerifier(db),
        )

    def prepare(
        self,
        *,
        task: Mapping[str, Any],
        config: Mapping[str, Any],
        run_id: str,
        attempt_id: str,
    ) -> None:
        """Persist the single-step plan that will receive external rejection."""
        from lhas.domain.enums import EventType
        from lhas.domain.models import Project
        from lhas.persistence.event_store import EventStore
        from lhas.persistence.planning_repositories import GoalRepository, PlanRepository
        from lhas.persistence.repositories import ProjectRepository
        from lhas.planning.models import Goal, Plan, PlanMode, PlanStatus, PlanStep, PlanStepStatus

        if run_id in self._contexts:
            return
        project = ProjectRepository(self.db).get_by_name("benchmark")
        if project is None:
            project = ProjectRepository(self.db).create(
                Project(name="benchmark", type="benchmark")
            )
        task_id = str(task.get("task_id", "unknown"))
        objective = str(task.get("objective", task_id))
        goal = Goal(
            project_id=project.id,
            objective=f"Official recovery for {task_id}: {objective}",
            allowed_capabilities=list(task.get("required_capabilities", [])),
            metadata={
                "benchmark_run_id": run_id,
                "benchmark_config": config.get("config_id"),
                # DeterministicPlanner is the existing macro authority.  The
                # frozen task supplies the allowed strategy; the planner may
                # only propose within that allow-list during escalation.
                "plan_steps": list(task.get("required_capabilities", [])),
            },
        )
        GoalRepository(self.db).create(goal)

        available = set(self.registry.list_capabilities())
        candidates = [
            str(capability)
            for capability in task.get("required_capabilities", [])
            if str(capability) in available
        ]
        if not candidates:
            raise OfficialRecoveryContractError(
                f"RECOVERY_CAPABILITY_UNAVAILABLE:{task_id}"
            )

        step_id = f"{run_id}::recovery-step"
        step = PlanStep(
            id=step_id,
            title=str(task.get("title", task_id)),
            objective=objective,
            capability=candidates[0],
            required_capabilities=list(task.get("required_capabilities", [])),
            task_id=task_id,
            status=PlanStepStatus.CLASSIFIED_FAILURE,
            success_criteria=list(task.get("acceptance_criteria", [])),
            expected_effects=dict(task.get("expected_observable_effects", {})),
            budget={"max_repair_attempts": 1},
            evidence={"original_failure_attempt_id": attempt_id},
        )
        plan = Plan(
            id=f"{run_id}::recovery-plan",
            goal_id=goal.id,
            mode=PlanMode.SIMPLE_DEPENDENCY,
            status=PlanStatus.FAILED,
            steps=[step],
            metadata={"official_benchmark_recovery": True},
        )
        PlanRepository(self.db).create(plan)
        EventStore(self.db).append(
            EventType.PLAN_CREATED,
            payload={"plan_id": plan.id, "official_benchmark_recovery": True},
        )
        self._contexts[run_id] = {
            "plan_id": plan.id,
            "goal": goal,
            "step_id": step_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
        }

    async def recover_after_validation(
        self,
        request: Any,
        outcome: ExecutionOutcome,
        validation: Any,
    ) -> ExecutionOutcome:
        """Persist provenance, invoke canonical repair, and return observations."""
        from lhas.domain.enums import EventType, FailureClass, FailureType
        from lhas.persistence.event_store import EventStore
        from lhas.persistence.planning_repositories import PlanRepository
        from lhas.planning.models import (
            PlanStepStatus,
            RepairScope,
            RepairScopeHint,
            StepFailureProvenance,
            compute_repair_scope,
        )

        context = self._contexts.pop(request.run_id, None)
        if context is None:
            raise OfficialRecoveryContractError("RECOVERY_CONTEXT_MISSING")
        plans = PlanRepository(self.db)
        plan = plans.get(context["plan_id"])
        if plan is None:
            raise OfficialRecoveryContractError("RECOVERY_PLAN_MISSING")
        step = next((item for item in plan.steps if item.id == context["step_id"]), None)
        if step is None:
            raise OfficialRecoveryContractError("RECOVERY_STEP_MISSING")

        failure_type_value = str(
            outcome.failure_type or FailureType.VERIFICATION_REJECTED.value
        )
        budget_failure_type = str(
            outcome.budget_failure_type or failure_type_value
        ).upper()
        if budget_failure_type in ATTEMPT_LOCAL_BUDGET_FAILURES:
            # The native kernel reports the exact attempt-local budget
            # boundary; the existing P3 policy classifies it as a normal
            # executable tool failure so local recovery can proceed.
            failure_type = FailureType.TOOL_ERROR
        elif budget_failure_type == ROOT_API_BUDGET_FAILURE:
            failure_type = FailureType.BUDGET_EXHAUSTED
        else:
            try:
                failure_type = FailureType(failure_type_value)
            except ValueError:
                failure_type = FailureType.VERIFICATION_REJECTED
        if failure_type in {
            FailureType.QUOTA_EXHAUSTED,
            FailureType.BILLING_OR_CREDIT_EXHAUSTED,
            FailureType.AUTH_INVALID,
            FailureType.PROVIDER_UNAVAILABLE,
            FailureType.PROVIDER_TIMEOUT,
            FailureType.MALFORMED_PROVIDER_RESPONSE,
            FailureType.UNKNOWN_PROVIDER_FAILURE,
            FailureType.BUDGET_EXHAUSTED,
            FailureType.NETWORK_ERROR,
        }:
            failure_class = FailureClass.EXECUTION
        elif failure_type in {
            FailureType.WRONG_ASSUMPTION,
            FailureType.STALE_CONTEXT,
            FailureType.CONTEXT_CONFLICT,
            FailureType.MISSING_CONTEXT,
            FailureType.CONTEXT_OVERLOAD,
        }:
            failure_class = FailureClass.REASONING
        else:
            failure_class = FailureClass.DATA
        scope, _affected_ids = compute_repair_scope(
            step,
            plan,
            failure_class=failure_class,
            error_type=failure_type.value,
        )

        provenance = StepFailureProvenance(
            step_id=step.id,
            plan_id=plan.id,
            task_id=context["task_id"],
            failure_class=failure_class,
            failure_type=failure_type,
            failure_evidence={
                "validator_execution_status": getattr(
                    validation, "validator_execution_status", "SUCCESS"
                ),
                "acceptance_status": getattr(
                    validation, "acceptance_status", "REJECTED"
                ),
                "agent_claimed_complete": bool(outcome.claimed_complete),
                "budget_failure_type": budget_failure_type,
            },
            attempt_id=context["attempt_id"],
            run_id=request.run_id,
            repair_scope_hint=RepairScopeHint(scope.value),
        )
        step.status = PlanStepStatus.CLASSIFIED_FAILURE
        step.evidence["failure_provenance"] = provenance.model_dump(mode="json")
        step.evidence["original_failure_attempt_id"] = context["attempt_id"]
        plans.update(plan)
        events = EventStore(self.db)
        before_ids = {
            event.id for event in events.list_all() if event.id is not None
        }
        events.append(
            EventType.STEP_FAILURE_PROVENANCE,
            payload={
                "plan_id": plan.id,
                "step_id": step.id,
                "task_id": context["task_id"],
                "run_id": request.run_id,
                "attempt_id": context["attempt_id"],
                "failure_class": failure_class.value,
                "failure_type": failure_type.value,
                "budget_failure_type": budget_failure_type,
                "repair_scope_hint": RepairScopeHint(scope.value).value,
            },
        )

        repaired_plan = await self.service.repair_after_failure(
            plan.id,
            step.id,
            context["goal"],
            context={
                "benchmark_task_id": context["task_id"],
                "benchmark_run_id": request.run_id,
                "official_benchmark_recovery": True,
                "bounded_recovery": True,
                "repair_scope": scope.value,
            },
        )
        repaired_step = next(
            item for item in repaired_plan.steps if item.id == step.id
        )
        original_attempt_id = repaired_step.evidence.get(
            "original_failure_attempt_id", context["attempt_id"]
        )
        repair_attempt_id = repaired_step.evidence.get("repair_attempt_id")
        if scope != RepairScope.MACRO_REPLAN and (
            not repair_attempt_id or repair_attempt_id == original_attempt_id
        ):
            raise OfficialRecoveryContractError("REPAIR_ATTEMPT_LINEAGE_MISSING")

        trace = self._project_events(
            events,
            before_ids=before_ids,
            plan_id=plan.id,
            task_id=context["task_id"],
            original_attempt_id=str(original_attempt_id),
            repair_attempt_id=str(repair_attempt_id or original_attempt_id),
        )
        required = {"StepFailureProvenance"}
        if scope != RepairScope.MACRO_REPLAN:
            required.update({"REPAIR_STARTED", "REPAIR_COMPLETED"})
        if not required.issubset({event["event_type"] for event in trace}):
            raise OfficialRecoveryContractError("RECOVERY_TRACE_INCOMPLETE")

        state: dict[str, Any] = {}
        step_record = (
            repaired_step.execution_context.get("steps", {}).get(repaired_step.id, {})
            if isinstance(repaired_step.execution_context, Mapping)
            else {}
        )
        if isinstance(step_record, Mapping):
            for key in ("output", "artifacts"):
                value = step_record.get(key)
                if isinstance(value, Mapping):
                    state.update(dict(value))
        # ``repair_scope`` is a declared observable effect for the CWR
        # tasks.  It is the canonical scope decision produced by the
        # planning authority, not a fabricated success flag.
        state["repair_scope"] = scope.value.lower()
        verified = repaired_step.status is PlanStepStatus.VERIFIED
        is_macro_replan = scope == RepairScope.MACRO_REPLAN
        return ExecutionOutcome(
            claimed_complete=verified,
            observed_state=state,
            failure_type=None if verified else "VERIFICATION_REJECTED",
            recovery_required=True,
            recovery_attempted=True,
            recovery_success=verified,
            repair_scope=scope.value,
            repair_attempts=0 if is_macro_replan else 1,
            # The runner adds the initial attempt count to this recovery
            # segment. A local/subgraph repair contributes exactly one new
            # attempt; a macro replan contributes no provider attempt here.
            attempt_count=0 if is_macro_replan else 1,
            execution_trace=trace,
            original_failure_attempt_id=str(original_attempt_id),
            repair_attempt_id=(
                str(repair_attempt_id) if repair_attempt_id else None
            ),
            recovery_trace_authoritative=True,
            recovery_action=scope.value,
            replan_count=int(repaired_plan.replan_count),
            budget_failure_type=(
                budget_failure_type
                if budget_failure_type in ATTEMPT_LOCAL_BUDGET_FAILURES
                else None
            ),
        )

    @staticmethod
    def _project_events(
        events: Any,
        *,
        before_ids: set[int | None],
        plan_id: str,
        task_id: str,
        original_attempt_id: str,
        repair_attempt_id: str,
    ) -> list[dict[str, Any]]:
        mapped = {
            "STEP_FAILURE_PROVENANCE": "StepFailureProvenance",
            "REPAIR_STARTED": "REPAIR_STARTED",
            "REPAIR_COMPLETED": "REPAIR_COMPLETED",
            "REPLAN_ACCEPTED": "REPLAN_ACCEPTED",
            "REPLAN_REJECTED": "REPLAN_REJECTED",
        }
        output: list[dict[str, Any]] = []
        for event in events.list_all():
            if event.id in before_ids or event.event_type.value not in mapped:
                continue
            payload = dict(event.payload or {})
            if payload.get("plan_id") != plan_id:
                continue
            event_type = mapped[event.event_type.value]
            event_attempt_id = str(
                payload.get("repair_attempt_id")
                or payload.get("attempt_id")
                or (
                    original_attempt_id
                    if event_type == "StepFailureProvenance"
                    else repair_attempt_id or original_attempt_id
                )
            )
            output.append(
                {
                    "timestamp": _event_timestamp_utc(event.timestamp),
                    "event_type": event_type,
                    "task_id": task_id,
                    "step_id": str(payload.get("step_id") or "root"),
                    "attempt_id": event_attempt_id,
                    "status": event_type.casefold(),
                    "metadata": {
                        "plan_id": plan_id,
                        **{
                            str(key): value
                            for key, value in payload.items()
                            if key not in {"plan_id", "step_id", "attempt_id", "repair_attempt_id"}
                        },
                    },
                }
            )
        return output


__all__ = [
    "OfficialOdysRecoveryCoordinator",
    "OfficialRecoveryContractError",
]
