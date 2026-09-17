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


def repair_thresholds_from_task(task: Mapping[str, Any]) -> dict[str, int]:
    """Project one task-owned convergence policy for every recovery layer.

    The controller and the per-attempt progress tracker must never resolve
    independent defaults.  The task projection is the only experiment-local
    authority; both consumers receive this same bounded mapping.
    """
    return {
        "max_no_progress": int(task.get("repair_max_no_progress", 3)),
        "max_repeated_action": int(task.get("repair_max_repeated_action", 2)),
        "max_repeated_state": int(task.get("repair_max_repeated_state", 2)),
    }


def _tool_invocation_evidence(events: Any, attempt_id: str) -> list[dict[str, Any]]:
    """Join durable native request/observation events for one attempt.

    Only dispatcher-produced bounded projections are returned.  Raw model
    messages and raw tool arguments are never read from provider state here;
    the request projection has already removed arbitrary argument values.
    """
    requested: dict[str, dict[str, Any]] = {}
    observed: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events.list_for_attempt(attempt_id):
        payload = dict(event.payload or {})
        invocation_id = payload.get("invocation_id")
        if not invocation_id:
            continue
        invocation_id = str(invocation_id)
        if event.event_type.value == "NATIVE_TOOL_REQUESTED":
            requested[invocation_id] = payload
            if invocation_id not in order:
                order.append(invocation_id)
        elif event.event_type.value == "NATIVE_TOOL_OBSERVED":
            observed[invocation_id] = payload
            if invocation_id not in order:
                order.append(invocation_id)

    return [
        {
            "invocation_id": invocation_id,
            "attempt_id": attempt_id,
            "ordinal": requested.get(invocation_id, {}).get(
                "ordinal", observed.get(invocation_id, {}).get("ordinal")
            ),
            "capability": requested.get(invocation_id, {}).get(
                "capability", observed.get(invocation_id, {}).get("capability")
            ),
            "arguments_sanitized": requested.get(invocation_id, {}).get(
                "arguments_sanitized", {}
            ),
            "args_sha256": requested.get(invocation_id, {}).get("args_sha256"),
            "result_status": observed.get(invocation_id, {}).get("status"),
            "error_type": observed.get(invocation_id, {}).get("error_type"),
            "result_summary": (
                observed.get(invocation_id, {}).get("summary")
                if isinstance(observed.get(invocation_id, {}).get("summary"), Mapping)
                else {}
            ),
            "bounded_output": observed.get(invocation_id, {}).get(
                "bounded_output", {}
            ),
            "observed_mutation": bool(
                observed.get(invocation_id, {}).get("observed_mutation", False)
            ),
        }
        for invocation_id in order
    ]


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
        from lhas.repair_progress import RepairProgressTracker

        task = request.task if isinstance(request.task, Mapping) else {}
        context = dict(request.context or {})
        progress_config = context.pop("_repair_progress_config", None)
        recovery_controller = context.pop("_recovery_controller", None)
        progress_tracker = (
            RepairProgressTracker.from_config(progress_config)
            if isinstance(progress_config, Mapping)
            else None
        )
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
        if progress_tracker is not None:
            # In-process only: never place the tracker in prompt context or
            # durable plan JSON.  The static config is the only persisted
            # repair input; tracker state belongs to this native Attempt.
            metadata["_repair_progress_tracker"] = progress_tracker
        if recovery_controller is not None:
            # In-process only: the controller owns durable signal emission;
            # its state is never serialized into prompt or plan JSON.
            metadata["_recovery_controller"] = recovery_controller
            context["recovery_control_plane_v2"] = True
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
                or (
                    context.get("repair_context", {}).get(
                        "required_capabilities", []
                    )
                    if isinstance(context.get("repair_context"), Mapping)
                    else []
                )
            ),
            budget=budget,
            metadata=metadata,
        )
        control = getattr(request, "execution_control", None)
        if control is None and isinstance(getattr(request, "context", None), Mapping):
            control = request.context.get("_execution_control")
        if control is not None:
            control.check()
        agent_request.execution_control = control
        result = await self.kernel.run(agent_request, execution_control=control)
        completed = result.status is AgentStatus.COMPLETED
        artifacts = dict(result.artifacts or {})
        if progress_tracker is not None:
            if result.status is AgentStatus.COMPLETED:
                progress_tracker.stop("VERIFIED")
            elif progress_tracker.repair_stop_reason is None:
                error_type = str(result.error_type or "")
                progress_tracker.stop(
                    "MODEL_FAILURE"
                    if error_type.startswith("PROVIDER")
                    or "MODEL" in error_type
                    else "TOOL_FAILURE"
                )
            artifacts["repair_convergence"] = progress_tracker.snapshot()
        return ExecutionResult(
            status=ExecutionStatus.SUCCESS if completed else ExecutionStatus.FAILURE,
            output=result.final_output,
            error_type=result.error_type,
            error_message=result.error_message,
            usage=dict(result.usage or {}),
            artifacts=artifacts,
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
        experiment_macro_replan_enabled: bool = False,
        escalation_trigger_policy: str = "NO_PROGRESS_AWARE",
        root_budget_authority: Any = None,
        effect_policy: Any = None,
    ) -> None:
        from lhas.planning.service import PlanExecutionService
        from lhas.planning.planner import DeterministicPlanner
        from lhas.planning.verification import WorkflowVerifier

        self.db = db
        self.kernel = kernel
        self.registry = registry
        self._contexts: dict[str, dict[str, Any]] = {}
        # One controller is created at the run boundary and reused by the
        # initial native attempt and the canonical recovery path.  Keeping the
        # object here is intentionally in-process only; durable escalation
        # truth remains in ReplanSignalRepository/EventStore.
        self._controllers: dict[str, Any] = {}
        self.experiment_macro_replan_enabled = bool(
            experiment_macro_replan_enabled
        )
        self.escalation_trigger_policy = str(
            escalation_trigger_policy or "NO_PROGRESS_AWARE"
        ).upper()
        self.root_budget_authority = root_budget_authority
        self.effect_policy = effect_policy
        self.execution_control = None
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
            execution_control=self.execution_control,
            effect_policy=self.effect_policy,
        )

    def bind_execution_control(self, control: Any) -> None:
        """Bind the parent root authority to the existing repair service."""
        self.execution_control = control
        self.service.execution_control = control

    def controller_for(self, run_id: str) -> Any | None:
        """Return the run-scoped controller for the initial native attempt."""
        return self._controllers.get(str(run_id))

    def discard_controller(self, run_id: str) -> None:
        """Release a run-scoped controller after the run is terminal.

        Recovery consumes the controller itself when validation rejects a
        candidate.  Accepted initial executions and infrastructure failures
        bypass that path, so the runtime boundary must also be able to
        release the in-process reference without touching durable evidence.
        """
        self._controllers.pop(str(run_id), None)

    def _reserve_replan(self) -> bool:
        authority = self.root_budget_authority
        reserve = getattr(authority, "reserve_replan", None)
        if callable(reserve):
            accepted = bool(reserve())
            if self.effect_policy is not None:
                record = getattr(self.effect_policy, "record_replan_reservation", None)
                if callable(record):
                    snapshot = authority.snapshot() if hasattr(authority, "snapshot") else {}
                    record(accepted=accepted, snapshot=dict(snapshot))
            return accepted
        # The non-opt-in official path never calls this guard.  Returning
        # false here is fail-closed if an experiment forgets to bind the
        # single root budget authority.
        return False

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
        from lhas.recovery_control import RecoveryController
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
        goal_metadata = {
            "benchmark_run_id": run_id,
            "benchmark_config": config.get("config_id"),
            # DeterministicPlanner is the existing macro authority.  The
            # frozen task supplies the allowed strategy; the planner may
            # only propose within that allow-list during escalation.
            "plan_steps": list(
                task.get(
                    "experiment_initial_plan_steps",
                    task.get("required_capabilities", []),
                )
            ),
        }
        if self.experiment_macro_replan_enabled:
            alternate = task.get("experiment_replan_plan_steps", [])
            if alternate:
                goal_metadata["replan_plan_steps"] = list(alternate)
            step_inputs = task.get("experiment_step_inputs", {})
            if isinstance(step_inputs, Mapping):
                goal_metadata["step_inputs"] = {
                    str(key): dict(value)
                    for key, value in step_inputs.items()
                    if isinstance(value, Mapping)
                }
        goal = Goal(
            project_id=project.id,
            objective=f"Official recovery for {task_id}: {objective}",
            allowed_capabilities=list(task.get("required_capabilities", [])),
            metadata=goal_metadata,
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
        repair_thresholds = repair_thresholds_from_task(task)
        self._controllers[run_id] = RecoveryController(
            db=self.db,
            task_id=task_id,
            run_id=run_id,
            attempt_id=attempt_id,
            step_id=step_id,
            expected_effects=dict(step.expected_effects),
            **repair_thresholds,
            escalation_policy=str(
                config.get("escalation_trigger_policy", self.escalation_trigger_policy)
            ),
        )
        official_benchmark_recovery = not self.experiment_macro_replan_enabled
        plan = Plan(
            id=f"{run_id}::recovery-plan",
            goal_id=goal.id,
            mode=PlanMode.SIMPLE_DEPENDENCY,
            status=PlanStatus.FAILED,
            steps=[step],
            metadata={
                "benchmark_run_id": run_id,
                "official_benchmark_recovery": official_benchmark_recovery,
                "experiment_macro_replan_enabled": self.experiment_macro_replan_enabled,
                "escalation_trigger_policy": self.escalation_trigger_policy,
            },
        )
        PlanRepository(self.db).create(plan)
        EventStore(self.db).append(
            EventType.PLAN_CREATED,
            payload={
                "plan_id": plan.id,
                "official_benchmark_recovery": official_benchmark_recovery,
                "experiment_macro_replan_enabled": self.experiment_macro_replan_enabled,
            },
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

        control = getattr(request, "execution_control", None)
        if control is not None:
            control.check()
        context = self._contexts.pop(request.run_id, None)
        recovery_controller = self._controllers.pop(request.run_id, None)
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

        if (
            self.experiment_macro_replan_enabled
            and str(
                request.config.get(
                    "escalation_trigger_policy",
                    self.escalation_trigger_policy,
                )
            ).upper()
            == "NO_PROGRESS_AWARE"
        ):
            # The initial kernel may have emitted a typed signal before
            # external validation rejected the candidate.  Consume the
            # durable same-run signal at this recovery boundary so the
            # canonical planning service receives MACRO_REPLAN rather than
            # silently falling back to LOCAL.
            from lhas.native.persistence import ReplanSignalRepository
            from lhas.recovery_control import TYPED_ESCALATION_REASONS

            typed_signals = [
                item
                for item in ReplanSignalRepository(self.db).list_for_run(
                    request.run_id
                )
                if item.reason in TYPED_ESCALATION_REASONS
            ]
            if typed_signals:
                signal = typed_signals[-1]
                scope = RepairScope.MACRO_REPLAN
                step.evidence["recovery_control_escalation"] = {
                    "reason": signal.reason,
                    "evidence": {
                        "source": "DURABLE_RUN_REPLAN_SIGNAL",
                        "signal_id": signal.id,
                        **dict(signal.evidence or {}),
                    },
                }

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
        if control is not None:
            control.check()
        events = EventStore(self.db)
        before_ids = {
            event.id for event in events.list_all() if event.id is not None
        }
        repair_thresholds = repair_thresholds_from_task(request.task)
        if recovery_controller is None:
            # Compatibility for callers that enter recovery without first
            # passing through prepare().  The official runtime path always
            # creates and reuses the controller above.
            from lhas.recovery_control import RecoveryController

            recovery_controller = RecoveryController(
                db=self.db,
                task_id=context["task_id"],
                run_id=request.run_id,
                attempt_id=context["attempt_id"],
                step_id=step.id,
                expected_effects=dict(step.expected_effects),
                **repair_thresholds,
                escalation_policy=str(
                    request.config.get(
                        "escalation_trigger_policy",
                        self.escalation_trigger_policy,
                    )
                ),
            )
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

        if control is not None:
            control.check()

        if self.effect_policy is not None:
            bind_phase = getattr(self.effect_policy, "bind_provider_phase", None)
            if callable(bind_phase):
                bind_phase("recovery")

        pre_replan_count = int(plan.replan_count)
        repaired_plan = await self.service.repair_after_failure(
            plan.id,
            step.id,
            context["goal"],
            context={
                "benchmark_task_id": context["task_id"],
                "benchmark_run_id": request.run_id,
                # The frozen official path remains bounded and cannot enter
                # macro replan. Experiment 02 opts in explicitly and uses the
                # same planning/recovery service with a different execution-
                # local policy value.
                "official_benchmark_recovery": not self.experiment_macro_replan_enabled,
                "experiment_macro_replan_enabled": self.experiment_macro_replan_enabled,
                "recovery_control_plane_v2": self.experiment_macro_replan_enabled,
                "escalation_trigger_policy": str(
                    request.config.get(
                        "escalation_trigger_policy",
                        self.escalation_trigger_policy,
                    )
                ),
                "_replan_budget_guard": (
                    self._reserve_replan
                    if self.experiment_macro_replan_enabled
                    else None
                ),
                "bounded_recovery": True,
                "repair_scope": scope.value,
                "timeout_seconds": float(
                    request.task.get("timeout_seconds", 60.0)
                ),
                "repair_context": {
                    "failure_provenance": provenance.model_dump(mode="json"),
                    "observed_state": dict(outcome.observed_state),
                    "expected_observable_effects": dict(
                        step.expected_effects
                    ),
                    "required_capabilities": list(
                        step.required_capabilities
                    ),
                    "repair_scope": scope.value,
                    "instruction": (
                        "Repair the rejected external state using the listed "
                        "capability before claiming completion."
                    ),
                },
                # This is a generic, serializable policy input.  The tracker
                # itself is created inside the repair Attempt and is never
                # persisted as an object or treated as completion authority.
                "_repair_progress_config": {
                    "expected_effects": dict(step.expected_effects),
                    "initial_state_digest": outcome.pre_repair_state_digest,
                    # The same task projection is the single authority for
                    # both RecoveryController and RepairProgressTracker.
                    **repair_thresholds,
                },
                "_recovery_controller": recovery_controller,
                "_execution_control": getattr(request, "execution_control", None),
            },
        )

        # Macro replan changes the durable graph, but the generic planning
        # service intentionally returns after accepting that graph.  The
        # official recovery boundary must then execute the newly proposed
        # strategy through that same service, controller, root ledger, and
        # validator path.  Otherwise the bridge would project the old stale
        # step as if a repair attempt had happened and lose real lineage.
        post_replan_step_ids: set[str] = set()
        accepted_replan = False
        if (
            scope == RepairScope.MACRO_REPLAN
            and int(repaired_plan.replan_count) > pre_replan_count
        ):
            # The planner's accepted result is not enough by itself: the
            # effect authority may transition only after the corresponding
            # durable event has been written for this plan and run boundary.
            accepted_replan = any(
                event.id not in before_ids
                and event.event_type.value == "REPLAN_ACCEPTED"
                and str((event.payload or {}).get("plan_id") or "")
                == str(repaired_plan.id)
                for event in events.list_all()
            )
            if not accepted_replan:
                raise OfficialRecoveryContractError(
                    "REPLAN_ACCEPTANCE_NOT_DURABLE"
                )
            # This is intentionally before post-replan execute_goal().  The
            # service hook normally performs the same transition immediately
            # after MacroReplanService persists acceptance; the guarded call
            # here closes the runtime boundary without a late duplicate.
            mark_replan = getattr(
                self.effect_policy, "mark_replan_accepted", None
            )
            if callable(mark_replan) and getattr(
                self.effect_policy, "phase", None
            ) != "post_replan":
                mark_replan()
            post_replan_step_ids = {
                item.id
                for item in repaired_plan.steps
                if item.id != step.id
                and item.status
                not in {
                    PlanStepStatus.VERIFIED,
                    PlanStepStatus.COMPLETED,
                    PlanStepStatus.STALE,
                }
            }
            if not post_replan_step_ids:
                raise OfficialRecoveryContractError("POST_REPLAN_STEP_MISSING")
            repaired_plan = await self.service.execute_goal(
                context["goal"],
                context={
                    "benchmark_task_id": context["task_id"],
                    "benchmark_run_id": request.run_id,
                    "official_benchmark_recovery": False,
                    "experiment_macro_replan_enabled": True,
                    "recovery_control_plane_v2": True,
                    "escalation_trigger_policy": str(
                        request.config.get(
                            "escalation_trigger_policy",
                            self.escalation_trigger_policy,
                        )
                    ),
                    "bounded_recovery": True,
                    "timeout_seconds": float(
                        request.task.get("timeout_seconds", 60.0)
                    ),
                    "repair_context": {
                        "failure_provenance": provenance.model_dump(mode="json"),
                        "observed_state": dict(outcome.observed_state),
                        "expected_observable_effects": dict(
                            step.expected_effects
                        ),
                        "required_capabilities": list(
                            step.required_capabilities
                        ),
                        "repair_scope": scope.value,
                        "instruction": (
                            "Execute the accepted alternate recovery strategy "
                            "before claiming completion."
                        ),
                    },
                    "_repair_progress_config": {
                        "expected_effects": dict(step.expected_effects),
                        "initial_state_digest": outcome.pre_repair_state_digest,
                        **repair_thresholds,
                    },
                    "_recovery_controller": recovery_controller,
                    "_execution_control": getattr(
                        request, "execution_control", None
                    ),
                },
                experiment_id=str(request.task.get("benchmark_version", ""))
                or None,
                resume_plan_id=repaired_plan.id,
                repair_step_ids=post_replan_step_ids,
            )

        # Project bounded recovery-control telemetry from durable events and
        # controller decisions. The policy is execution-local and never
        # becomes validator truth or benchmark input.
        if self.effect_policy is not None:
            record_detection = getattr(self.effect_policy, "record_detection", None)
            if callable(record_detection):
                for detection in recovery_controller.detections:
                    record_detection(
                        run_id=request.run_id,
                        reason=str(detection.get("reason")),
                        escalation_policy=recovery_controller.escalation_policy,
                    )
            after_events = [
                event for event in events.list_all() if event.id not in before_ids
            ]
            signal_events = [
                event
                for event in after_events
                if event.event_type.value == "REPLAN_SIGNAL_CREATED"
            ]
            signal_reasons = [
                str((event.payload or {}).get("reason"))
                for event in signal_events
            ]
            signal_run_ids = [
                str((event.payload or {}).get("run_id") or request.run_id)
                for event in signal_events
            ]
            record_signal = getattr(self.effect_policy, "record_signal", None)
            if callable(record_signal):
                for reason, signal_run_id in zip(signal_reasons, signal_run_ids):
                    record_signal(
                        run_id=signal_run_id,
                        reason=reason,
                        escalation_policy=recovery_controller.escalation_policy,
                    )
            record_result = getattr(self.effect_policy, "record_replan_result", None)
            if callable(record_result):
                for event in after_events:
                    if event.event_type.value not in {"REPLAN_ACCEPTED", "REPLAN_REJECTED"}:
                        continue
                    payload = dict(event.payload or {})
                    if event.event_type.value == "REPLAN_ACCEPTED":
                        accepted_replan = True
                    record_result(
                        run_id=request.run_id,
                        plan_id=str(payload.get("plan_id") or plan.id),
                        accepted=event.event_type.value == "REPLAN_ACCEPTED",
                        error_type=payload.get("error_type"),
                        signal_count=payload.get("signal_count"),
                        signal_reasons=signal_reasons,
                        signal_run_ids=signal_run_ids,
                    )
        if scope == RepairScope.MACRO_REPLAN:
            repaired_step = next(
                (
                    item
                    for item in repaired_plan.steps
                    if item.id in post_replan_step_ids
                ),
                None,
            )
            if repaired_step is None:
                raise OfficialRecoveryContractError("POST_REPLAN_STEP_MISSING")
            # The new strategy is the repair attempt.  Carry the original
            # failure provenance onto that new node before recording lineage.
            repaired_step.evidence.setdefault(
                "failure_provenance",
                dict(step.evidence.get("failure_provenance", {})),
            )
        else:
            repaired_step = next(
                item for item in repaired_plan.steps if item.id == step.id
            )
        if control is not None:
            control.check()
        original_attempt_id = repaired_step.evidence.get(
            "original_failure_attempt_id", context["attempt_id"]
        )
        repair_attempt_id = repaired_step.evidence.get("repair_attempt_id")
        if scope == RepairScope.MACRO_REPLAN and not repair_attempt_id:
            from lhas.persistence.repositories import AttemptRepository, RunRepository

            if repaired_step.task_id:
                repair_runs = RunRepository(self.db).list_for_task(
                    repaired_step.task_id
                )
                if repair_runs:
                    repair_attempts = AttemptRepository(self.db).list_for_run(
                        repair_runs[-1].id
                    )
                    if repair_attempts:
                        repair_attempt_id = repair_attempts[-1].id
            if not repair_attempt_id or repair_attempt_id == original_attempt_id:
                raise OfficialRecoveryContractError("REPAIR_ATTEMPT_LINEAGE_MISSING")
            lineage = {
                "plan_id": repaired_plan.id,
                "step_id": repaired_step.id,
                "original_failure_attempt_id": str(original_attempt_id),
                "repair_attempt_id": str(repair_attempt_id),
                "repair_number": 1,
                "repair_scope": scope.value,
            }
            repaired_step.evidence["original_failure_attempt_id"] = str(
                original_attempt_id
            )
            repaired_step.evidence["repair_attempt_id"] = str(repair_attempt_id)
            repaired_step.evidence["repair_scope"] = scope.value
            repaired_step.evidence["repair_lineage"] = [lineage]
            PlanRepository(self.db).update(repaired_plan)
            events.append(
                EventType.REPAIR_COMPLETED,
                payload={**lineage, "outcome": "ATTEMPT_CREATED"},
            )
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
        convergence = state.get("repair_convergence")
        if not isinstance(convergence, Mapping):
            convergence = {}
        else:
            convergence = dict(convergence)
        if repaired_step.status is PlanStepStatus.VERIFIED:
            convergence["repair_stop_reason"] = "VERIFIED"
        state["repair_convergence"] = convergence
        repair_tool_calls = 0
        invocations = getattr(self.kernel.dispatcher, "invocations", None)
        if invocations is not None and repair_attempt_id:
            repair_tool_calls = len(invocations.list_for_attempt(repair_attempt_id))
        tool_evidence = (
            _tool_invocation_evidence(events, str(repair_attempt_id))
            if repair_attempt_id
            else []
        )
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
            tool_calls=repair_tool_calls,
            tool_invocation_evidence=tool_evidence,
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
            "NATIVE_TOOL_REQUESTED": "TOOL_CALL_REQUESTED",
            "NATIVE_TOOL_OBSERVED": "TOOL_CALL_OBSERVED",
            "MODEL_RESPONSE_RECEIVED": "PROVIDER_RESPONSE_SUCCESS",
            "MODEL_RESPONSE_PARSED": "MODEL_OUTPUT_PARSE_SUCCEEDED",
            "MODEL_RESPONSE_REJECTED": "MODEL_OUTPUT_PARSE_FAILED",
        }
        output: list[dict[str, Any]] = []
        for event in events.list_all():
            event_value = event.event_type.value
            if event.id in before_ids or event_value not in mapped:
                continue
            payload = dict(event.payload or {})
            event_type = mapped[event_value]
            if event_type in {
                "TOOL_CALL_REQUESTED",
                "TOOL_CALL_OBSERVED",
                "PROVIDER_RESPONSE_SUCCESS",
                "MODEL_OUTPUT_PARSE_SUCCEEDED",
                "MODEL_OUTPUT_PARSE_FAILED",
            }:
                if str(event.attempt_id or "") != str(repair_attempt_id):
                    continue
                if event_type == "MODEL_OUTPUT_PARSE_FAILED" and payload.get(
                    "failure_stage"
                ) != "MODEL_OUTPUT_PARSE":
                    continue
            elif payload.get("plan_id") != plan_id:
                continue
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
