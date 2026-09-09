from typing import Any
from lhas.domain.enums import EventType, ExecutionStatus
from lhas import HARNESS_VERSION
from lhas.domain.models import Task, new_id
from lhas.executors.protocol import ExecutionRequest, ExecutionResult
from lhas.persistence.database import Database
from lhas.persistence.event_store import EventStore
from lhas.persistence.repositories import TaskRepository, RunRepository, AttemptRepository
from lhas.persistence.planning_repositories import GoalRepository, PlanRepository
from lhas.native.persistence import ReplanSignalRepository
from lhas.native.models import ReplanSignal
from lhas.orchestrator_v2 import RecoveringOrchestrator
from lhas.persistence.phaseb_repos import FailureReportRepository
from lhas.planning.models import (
    Goal, Plan, PlanStatus, PlanStepStatus, StepFailureProvenance,
    RepairScope, RepairScopeHint, compute_repair_scope_hint, compute_repair_scope, invalidate_affected_subgraph,
    evaluate_step_eligibility, transition_step,
)
from lhas.planning.scheduler import TaskGraphScheduler, build_step_dependency_context
from lhas.planning.planner import Planner
from lhas.planning.replan import MacroReplanService
from lhas.planning.replan_policy import ReplanTriggerPolicy
from lhas.tools.registry import ToolRegistry
from lhas.tools.protocol import ToolRequest, ToolResultStatus

from lhas.tools.invocation import invoke_via_contract
from lhas.tools.contract import ToolContract

class _ToolExecutor:
    name = "ToolRegistryExecutor"
    def __init__(self, registry, step, db, context, tool_contract):
        if tool_contract is None:
            raise ValueError("_ToolExecutor requires a valid ToolContract; direct Tool.execute is forbidden")
        self.registry, self.step, self.db, self.context, self.tool_contract = registry, step, db, context, tool_contract
    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        event = EventStore(self.db)
        try: tool = self.registry.resolve(self.step.capability)
        except KeyError as exc: return ExecutionResult(status=ExecutionStatus.FAILURE, error_type="UNKNOWN_CAPABILITY", error_message=str(exc))
        tr = ToolRequest(tool_call_id=new_id(), task_id=request.task_id, run_id=request.run_id, attempt_id=request.attempt_id,
                         capability=self.step.capability, tool_name=self.step.capability, arguments=self.step.inputs, context={**self.context, **request.context}, metadata=request.metadata)
        safe_request={"tool_call_id":tr.tool_call_id,"capability":tr.capability}
        event.append(EventType.TOOL_CALL_STARTED, task_id=request.task_id, run_id=request.run_id, attempt_id=request.attempt_id, payload={"request":safe_request})
        try:
            result = await invoke_via_contract(self.tool_contract, tr)
            typ = EventType.TOOL_CALL_COMPLETED if result.status == ToolResultStatus.SUCCESS else EventType.TOOL_CALL_FAILED
            safe_result={"status":result.status.value,"error_type":result.error_type,"artifact_keys":sorted(result.artifacts)[:20]}
            event.append(typ, task_id=request.task_id, run_id=request.run_id, attempt_id=request.attempt_id, payload={"request":safe_request,"result":safe_result})
            status = ExecutionStatus.SUCCESS if result.status == ToolResultStatus.SUCCESS else ExecutionStatus.FAILURE
            import json
            output = result.output if isinstance(result.output, str) else json.dumps(result.output, ensure_ascii=False)
            return ExecutionResult(status=status, output=output, artifacts=result.artifacts, usage=result.usage, raw=result.model_dump(mode="json"), error_type=result.error_type, error_message=result.error_message)
        except Exception as exc:
            event.append(EventType.TOOL_CALL_FAILED, task_id=request.task_id, run_id=request.run_id, attempt_id=request.attempt_id, payload={"request":safe_request,"error_type":type(exc).__name__})
            return ExecutionResult(status=ExecutionStatus.FAILURE, error_type=type(exc).__name__, error_message=str(exc))
    async def resume(self, request): return await self.execute(request)
    async def cancel(self, run_id): return None
    async def status(self, run_id): return {"run_id": run_id}


class _TaskGraphAgentExecutor:
    """Expose the canonical Plan/PlanStep projection to an injected executor."""
    name = "TaskGraphAgentExecutor"
    def __init__(self, executor, plan, step, db=None):
        self.executor, self.plan, self.step, self.db = executor, plan, step, db
        self.bound_plan_id = plan.id
        self.bound_plan_version = str(plan.version)
        self.bound_step_id = step.id
    async def execute(self, request):
        # INVARIANT 3 — STALE PLAN AUTHORITY: reject if plan version changed
        if self.db is not None:
            current = PlanRepository(self.db).get(self.bound_plan_id)
            if current is None or str(current.version) != self.bound_plan_version or not any(item.id == self.bound_step_id for item in current.steps):
                EventStore(self.db).append(
                    EventType.PLAN_STALE_REJECTED,
                    payload={
                        "plan_id": self.bound_plan_id,
                        "bound_version": self.bound_plan_version,
                        "step_id": self.bound_step_id,
                        "current_version": str(current.version) if current else None,
                    },
                )
                return ExecutionResult(status=ExecutionStatus.FAILURE, error_type="STALE_PLAN", error_message="plan version is no longer authoritative")
        completed=[item.id for item in self.plan.steps if item.status in {PlanStepStatus.COMPLETED, PlanStepStatus.VERIFIED}]
        pending=[item.id for item in self.plan.steps if item.id != self.step.id and item.status in {PlanStepStatus.PENDING,PlanStepStatus.READY,PlanStepStatus.RUNNING}]
        context={**request.context,"taskgraph":{"plan_id":self.plan.id,"active_node":self.step.id,"completed_nodes":completed,"pending_nodes":pending,"depends_on":list(self.step.depends_on)}}
        return await self.executor.execute(request.model_copy(update={"context":context}))
    async def resume(self, request): return await self.execute(request)
    async def cancel(self, run_id): return await self.executor.cancel(run_id)
    async def status(self, run_id): return await self.executor.status(run_id)

class PlanExecutionService:
    def __init__(self, db: Database, planner: Planner, registry: ToolRegistry, agent_executor_factory=None, tool_contract=None, capability_registry=None, workflow_verifier=None):
        self.db, self.planner, self.registry, self.agent_executor_factory = db, planner, registry, agent_executor_factory
        # P3.1 verification seam: explicit verifier only, default=None (fail-closed)
        # No auto-verify, no implicit accept-all, no compatibility flag
        self.workflow_verifier = workflow_verifier
        # If no explicit tool_contract provided, build one from default_capabilities()
        # (NOT from ToolRegistry — that would be reverse synthesis)
        if tool_contract is None and capability_registry is None:
            from lhas.tools.invocation import build_contract_for_registry
            self.capability_registry, self.tool_contract = build_contract_for_registry(registry)
        else:
            self.tool_contract = tool_contract
            self.capability_registry = capability_registry or getattr(tool_contract, "capability_registry", None)
    def _step_executor(self, plan, step, context):
        if self.agent_executor_factory is not None:
            return _TaskGraphAgentExecutor(self.agent_executor_factory(step),plan,step,self.db)
        return _ToolExecutor(self.registry,step,self.db,context,self.tool_contract)
    def _emit(self, typ, payload): EventStore(self.db).append(typ, payload=payload)
    @property
    def _evidence_provenance(self) -> str:
        """Evidence provenance for execution results produced by this service.

        TOOL_CONTRACT_EVIDENCE: execution went through ToolContract (trusted).
        AGENT_CLAIM: execution was by agent executor (untrusted for verification).
        """
        return "AGENT_CLAIM" if self.agent_executor_factory is not None else "TOOL_CONTRACT_EVIDENCE"
    def _planner_capabilities(self):
        """Return tool specs for plan creation.

        When a capability_registry is provided, only tools with explicit
        CapabilityDefinitions are planner-visible (strict authority).
        Without a semantic registry there is no planner-visible capability.
        """
        if self.capability_registry is not None:
            from lhas.capability_registry import CapabilityRuntimeContext
            ctx = CapabilityRuntimeContext(platform="windows", available_tools=set(self.registry.list_capabilities()))
            available = {d.id for d in self.capability_registry.list_available(ctx)}
            return [spec for spec in self.registry.specs() if spec.name in available]
        return []
    def _resolve_capability_spec(self, capability_name: str):
        """Resolve the CapabilitySpec for a step, preferring CapabilityDefinition authority."""
        return self.registry.resolve(capability_name).capability
    def _create_step_failure_provenance(self, step, plan, run_id: str) -> StepFailureProvenance | None:
        """Create durable failure provenance linking step to failure classification."""
        attempts = AttemptRepository(self.db).list_for_run(run_id)
        if not attempts:
            return None
        failure_repo = FailureReportRepository(self.db)
        reports = [
            report
            for attempt in attempts
            for report in failure_repo.list_for_attempt(attempt.id)
        ]
        if not reports:
            return None
        # Use the latest failure report
        report = reports[-1]
        # Determine if any downstream steps depend on this step
        by_id = {s.id: s for s in plan.steps}
        has_downstream_deps = any(
            step.id in s.depends_on
            for s in plan.steps
            if s.id != step.id
        )
        hint = compute_repair_scope_hint(
            failure_class=report.failure_class,
            failure_type=report.failure_type,
            has_downstream_deps=has_downstream_deps,
        )
        provenance = StepFailureProvenance(
            step_id=step.id,
            plan_id=plan.id,
            failure_class=report.failure_class,
            failure_type=report.failure_type,
            failure_evidence={
                "summary": report.summary,
                "evidence": report.evidence,
                "confidence": report.confidence,
                "suggested_recovery": report.suggested_recovery,
            },
            attempt_id=report.attempt_id,
            run_id=run_id,
            repair_scope_hint=hint,
        )
        step.evidence["failure_provenance"] = provenance.model_dump(mode="json")
        self._emit(EventType.STEP_FAILURE_PROVENANCE, {
            "plan_id": plan.id,
            "step_id": step.id,
            "run_id": run_id,
            "attempt_id": report.attempt_id,
            "failure_class": report.failure_class.value,
            "failure_type": report.failure_type.value,
            "repair_scope_hint": hint.value,
        })
        return provenance

    def _create_verification_failure_provenance(self, step, plan, vresult) -> None:
        """Create failure provenance for verification rejection.

        Verification rejection is a real failure source — the step executed
        successfully but didn't pass verification criteria.
        Persists real validation_id, run_id, attempt_id for durable lineage.
        """
        from lhas.domain.enums import FailureClass, FailureType
        # Resolve the attempt/run that produced the execution
        attempt_id = None
        run_id = None
        runs = RunRepository(self.db).list_for_task(step.task_id) if step.task_id else []
        if runs:
            run_id = runs[-1].id
            attempts = AttemptRepository(self.db).list_for_run(run_id)
            if attempts:
                attempt_id = attempts[-1].id

        # Extract real validation_id if available
        validation_id = None
        if hasattr(vresult, 'validation') and vresult.validation:
            validation_id = vresult.validation.id

        provenance = StepFailureProvenance(
            step_id=step.id,
            plan_id=plan.id,
            failure_class=FailureClass.DATA,  # verification failure = data didn't meet criteria
            failure_type=FailureType.TOOL_ERROR,  # closest existing enum
            failure_evidence={
                "verification_failure": True,
                "validation_id": validation_id,
                "verification_reason": vresult.reason if hasattr(vresult, 'reason') else str(vresult),
                "checks": [
                    {"name": c.name, "passed": c.passed, "detail": c.detail}
                    for c in (vresult.validation.checks if hasattr(vresult, 'validation') and vresult.validation else [])
                ],
            },
            attempt_id=attempt_id,
            run_id=run_id,
            repair_scope_hint=RepairScopeHint.LOCAL,  # default for validation failures
        )
        step.evidence["failure_provenance"] = provenance.model_dump(mode="json")
        self._emit(EventType.STEP_FAILURE_PROVENANCE, {
            "plan_id": plan.id,
            "step_id": step.id,
            "attempt_id": attempt_id,
            "run_id": run_id,
            "validation_id": validation_id,
            "failure_class": FailureClass.DATA.value,
            "failure_type": "VERIFICATION_REJECTED",
            "repair_scope_hint": RepairScopeHint.LOCAL.value,
        })

    def _record_step_replan_signal(self, step, run_id: str) -> None:
        """Turn a durable step failure into the canonical replan input."""
        attempts = AttemptRepository(self.db).list_for_run(run_id)
        if not attempts:
            return
        signal_repo = ReplanSignalRepository(self.db)
        if any(signal_repo.list_for_attempt(attempt.id) for attempt in attempts):
            return
        trigger = ReplanTriggerPolicy(self.db).evaluate(step=step, run_id=run_id)
        if trigger is None:
            return
        run = RunRepository(self.db).get(run_id)
        provenance_ref = step.evidence.get("failure_provenance")
        signal = ReplanSignal(
            task_id=run.task_id if run is not None else attempts[-1].run_id,
            run_id=run_id,
            attempt_id=attempts[-1].id,
            reason=trigger.reason,
            scope="TASKGRAPH_NODE",
            failed_node_id=step.id,
            evidence={**trigger.evidence, "objective": step.objective,
                       "failure_provenance": provenance_ref},
        )
        signal_repo.create(signal)
        self._emit(EventType.REPLAN_SIGNAL_CREATED, {"signal_id": signal.id, "reason": signal.reason, "failed_node_id": step.id})
    async def _maybe_replan(self, goal, plan, run_id: str, context: dict[str, Any]) -> bool:
        attempts = AttemptRepository(self.db).list_for_run(run_id)
        signals = []
        repo = ReplanSignalRepository(self.db)
        for attempt in attempts:
            signals.extend(repo.list_for_attempt(attempt.id))
        consumed = set(plan.metadata.get("consumed_replan_signal_ids", []))
        signals = [signal for signal in signals if signal.id not in consumed]
        if not signals:
            return False
        result = await MacroReplanService(self.db, self.planner).consume(
            goal=goal, plan=plan, signals=signals, context={**context, "capabilities": self._planner_capabilities()}
        )
        return result.accepted
    async def execute_goal(self, goal: Goal, *, context: dict[str, Any] | None = None, experiment_id: str | None = None, approved_step_ids: set[str] | None = None, resume_plan_id: str | None = None, repair_step_ids: set[str] | None = None) -> Plan:
        self._emit(EventType.GOAL_CREATED, {"goal": goal.model_dump(mode="json")})
        GoalRepository(self.db).create(goal)
        plans = PlanRepository(self.db)
        if resume_plan_id:
            plan = plans.get(resume_plan_id)
            if plan is None or plan.goal_id != goal.id: raise KeyError(f"plan not found for goal: {resume_plan_id}")
            self._emit(EventType.HUMAN_APPROVAL_GRANTED, {"plan_id": plan.id, "approved_step_ids": sorted(approved_step_ids or set())})
        else:
            plan = await self.planner.create_plan(goal=goal, capabilities=self._planner_capabilities(), context=context or {})
            plans.create(plan)
            self._emit(EventType.PLAN_CREATED, {"plan": plan.model_dump(mode="json")})
        if plan.mode.value != "LINEAR":
            if plan.mode.value == "SIMPLE_DEPENDENCY":
                return await self._execute_dependency_plan(goal, plan, context=context or {}, experiment_id=experiment_id, approved_step_ids=approved_step_ids or set(), repair_step_ids=repair_step_ids)
            raise NotImplementedError(f"unsupported plan mode: {plan.mode.value}")
        self._emit(EventType.PLAN_STARTED, {"plan_id": plan.id})
        task_repo = TaskRepository(self.db)
        execution_context = {"runtime": {**dict(context or {}), "goal_id": goal.id}, "steps": {}}
        events = EventStore(self.db)
        while True:
            plan = plans.get(plan.id) or plan
            restart_authoritative_schedule = False
            for step in list(plan.steps):
                if step.status in {PlanStepStatus.COMPLETED, PlanStepStatus.VERIFIED, PlanStepStatus.STALE, PlanStepStatus.BLOCKED, PlanStepStatus.CLASSIFIED_FAILURE, PlanStepStatus.FAILED, PlanStepStatus.PRECONDITION_FAILED, PlanStepStatus.WAITING_FOR_VERIFICATION}:
                    if step.status in {PlanStepStatus.COMPLETED, PlanStepStatus.VERIFIED}:
                        execution_context["steps"][step.id] = step.execution_context.get("steps", {}).get(step.id, {"capability": step.capability, "output": step.output, "artifacts": {}, "usage": {}})
                    continue
                # CLAIMED_COMPLETE on reload: route through verifier seam
                if step.status == PlanStepStatus.CLAIMED_COMPLETE:
                    execution_context["steps"][step.id] = step.execution_context.get("steps", {}).get(step.id, {"capability": step.capability, "output": step.output, "artifacts": {}, "usage": {}})
                    if self.workflow_verifier is not None:
                        vresult = self.workflow_verifier.verify(step, plan, events)
                        if vresult.accepted:
                            transition_step(step, PlanStepStatus.VERIFIED, "deferred_verification_accepted", events, plan_id=plan.id)
                        else:
                            transition_step(step, PlanStepStatus.CLASSIFIED_FAILURE, "deferred_verification_rejected", events, plan_id=plan.id)
                    else:
                        transition_step(step, PlanStepStatus.WAITING_FOR_VERIFICATION, "deferred_no_verifier", events, plan_id=plan.id)
                    plans.update(plan)
                    continue
                # BLOCKER B: dispatch-time eligibility check (dependency + precondition)
                by_id = {s.id: s for s in plan.steps}
                eligible, reason = evaluate_step_eligibility(step, by_id)
                if not eligible:
                    if "not_verified" in reason or "not_all_deps" in reason:
                        transition_step(step, PlanStepStatus.BLOCKED, reason, events, plan_id=plan.id)
                    elif "precondition" in reason.lower():
                        transition_step(step, PlanStepStatus.PRECONDITION_FAILED, reason, events, plan_id=plan.id)
                    elif "stale" in reason.lower():
                        transition_step(step, PlanStepStatus.STALE, reason, events, plan_id=plan.id)
                    else:
                        transition_step(step, PlanStepStatus.BLOCKED, reason, events, plan_id=plan.id)
                    plans.update(plan)
                    continue
                spec = self.registry.resolve(step.capability).capability
                if step.id not in (approved_step_ids or set()) and (spec.requires_human_approval or (goal.requires_human_approval and spec.side_effect)):
                    transition_step(step, PlanStepStatus.WAITING_FOR_HUMAN_APPROVAL, "human_approval_required", events, plan_id=plan.id)
                    plan.status = PlanStatus.WAITING_FOR_HUMAN_APPROVAL
                    self._emit(EventType.HUMAN_APPROVAL_REQUIRED, {"plan_id": plan.id, "step_id": step.id, "capability": step.capability})
                    plans.update(plan); return plan
                step.execution_context = dict(execution_context)
                task = Task(project_id=goal.project_id, title=step.title, objective=step.objective, constraints=goal.constraints, acceptance_criteria=step.success_criteria, max_attempts=2)
                task_repo.create(task); step.task_id = task.id
                transition_step(step, PlanStepStatus.RUNNING, "dispatch", events, plan_id=plan.id)
                self._emit(EventType.PLAN_STEP_STARTED, {"plan_id": plan.id, "step_id": step.id, "task_id": task.id})
                plans.update(plan)
                orch = RecoveringOrchestrator(self.db, executor_factory=lambda s=step,p=plan: self._step_executor(p,s,execution_context), executor_type="TaskGraphAgentExecutor" if self.agent_executor_factory else "ToolRegistryExecutor", provider="native-kernel" if self.agent_executor_factory else "tool-registry", model="provider-adapter" if self.agent_executor_factory else "deterministic", harness_version=HARNESS_VERSION, dataset_version="PLANNING-V0.1", experiment_id=experiment_id)
                run = await orch.execute_task(task.id)
                if run.status.value != "COMPLETED":
                    transition_step(step, PlanStepStatus.FAILED, "run_failed", events, plan_id=plan.id)
                    self._emit(EventType.PLAN_STEP_FAILED, {"plan_id": plan.id, "step_id": step.id, "run_id": run.id})
                    self._create_step_failure_provenance(step, plan, run.id)
                    self._record_step_replan_signal(step, run.id)
                    if await self._maybe_replan(goal, plan, run.id, context or {}):
                        restart_authoritative_schedule = True
                        break
                    plan.status = PlanStatus.FAILED; plans.update(plan); self._emit(EventType.PLAN_FAILED, {"plan_id": plan.id}); return plan
                if await self._maybe_replan(goal, plan, run.id, context or {}):
                    restart_authoritative_schedule = True
                    break
                import json
                payload = json.loads(run.result or "{}")
                step.output = payload.get("output")
                if isinstance(step.output, str):
                    try: step.output = json.loads(step.output)
                    except json.JSONDecodeError: pass
                attempts = AttemptRepository(self.db).list_for_run(run.id)
                raw = json.loads(attempts[-1].executor_result or "{}") if attempts and attempts[-1].executor_result else {}
                record = {"capability": step.capability, "output": step.output, "artifacts": raw.get("artifacts", {}), "usage": raw.get("usage", {}), "provenance": self._evidence_provenance}
                execution_context["steps"][step.id] = record
                execution_context[step.capability] = record
                step.execution_context = dict(execution_context)

                # P3.1: run success → CLAIMED_COMPLETE
                transition_step(step, PlanStepStatus.CLAIMED_COMPLETE, "run_completed", events, plan_id=plan.id, extra_payload={"run_id": run.id})
                self._emit(EventType.PLAN_STEP_COMPLETED, {"plan_id": plan.id, "step_id": step.id, "run_id": run.id, "output": step.output})

                # Verification seam: explicit verifier only, default fail-closed
                if self.workflow_verifier is not None:
                    vresult = self.workflow_verifier.verify(step, plan, events)
                    if vresult.accepted:
                        transition_step(step, PlanStepStatus.VERIFIED, "verification_accepted", events, plan_id=plan.id)
                    else:
                        transition_step(step, PlanStepStatus.CLASSIFIED_FAILURE, "verification_rejected", events, plan_id=plan.id)
                        # Create failure provenance for verification rejection
                        self._create_verification_failure_provenance(step, plan, vresult)
                else:
                    # No verifier configured → WAITING_FOR_VERIFICATION (fail-closed)
                    transition_step(step, PlanStepStatus.WAITING_FOR_VERIFICATION, "no_verifier_configured", events, plan_id=plan.id)

                plans.update(plan)
            if restart_authoritative_schedule:
                continue
            plan = plans.get(plan.id) or plan
            # Plan is complete when all non-stale steps are VERIFIED
            # (CLAIMED_COMPLETE and legacy COMPLETED do NOT complete a plan)
            if all(step.status in {PlanStepStatus.VERIFIED, PlanStepStatus.STALE} for step in plan.steps) and any(step.status == PlanStepStatus.VERIFIED for step in plan.steps):
                plan.status = PlanStatus.COMPLETED; plans.update(plan); self._emit(EventType.PLAN_COMPLETED, {"plan_id": plan.id}); return plan
            # Check if any step is waiting for verification (not a failure)
            if any(s.status == PlanStepStatus.WAITING_FOR_VERIFICATION for s in plan.steps):
                plan.status = PlanStatus.WAITING_FOR_VERIFICATION; plans.update(plan); return plan
            plan.status = PlanStatus.FAILED; plans.update(plan); self._emit(EventType.PLAN_FAILED, {"plan_id": plan.id}); return plan

    async def resume_after_approval(self, plan_id: str, goal: Goal, step_id: str, *, context: dict[str, Any] | None = None, experiment_id: str | None = None) -> Plan:
        """Resume by explicitly granting one previously gated capability."""
        return await self.execute_goal(goal, context=context, experiment_id=experiment_id, approved_step_ids={step_id}, resume_plan_id=plan_id)

    # -----------------------------------------------------------------------
    # Phase 3.3 — Repair execution
    # -----------------------------------------------------------------------

    async def repair_after_failure(self, plan_id: str, failed_step_id: str, goal: Goal, *, context: dict[str, Any] | None = None, experiment_id: str | None = None) -> Plan:
        """Repair a plan after a step failure, preserving VERIFIED work.

        Given a failed step_id:
        1. Load the plan
        2. Get the failure provenance from step.evidence
        3. Compute repair scope (failed step + transitively dependent steps)
        4. Invalidate affected steps to PENDING
        5. Execute repair via execute_goal with repair_step_ids
        6. Return the repaired plan

        Respects repair budget: if a step has been repaired max_repair_attempts
        times (from step.budget, default 3), stops retrying.
        """
        plans = PlanRepository(self.db)
        plan = plans.get(plan_id)
        if plan is None:
            raise KeyError(f"plan not found: {plan_id}")
        by_id = {s.id: s for s in plan.steps}
        failed_step = by_id.get(failed_step_id)
        if failed_step is None:
            raise KeyError(f"step not found: {failed_step_id}")

        # Compute repair scope using the canonical authority
        provenance = failed_step.evidence.get("failure_provenance", {})
        scope, affected_ids = compute_repair_scope(
            failed_step, plan,
            failure_class=provenance.get("failure_class"),
            error_type=provenance.get("failure_type"),
        )

        # MACRO_REPLAN: invoke canonical macro replan path, NOT local repair
        if scope == RepairScope.MACRO_REPLAN:
            self._record_step_replan_signal(failed_step, provenance.get("run_id", ""))
            if await self._maybe_replan(goal, plan, provenance.get("run_id", ""), context):
                plan = plans.get(plan_id) or plan
            return plan

        # For LOCAL: only repair the failed step
        # For AFFECTED_SUBGRAPH: repair failed step + dependents
        repair_scope = affected_ids if affected_ids else {failed_step_id}

        # Check repair budget
        repair_count = failed_step.evidence.get("repair_attempt_count", 0)
        max_repair_attempts = failed_step.budget.get("max_repair_attempts", 3)
        if repair_count >= max_repair_attempts:
            self._emit(EventType.REPAIR_COMPLETED, {
                "plan_id": plan.id,
                "repair_step_ids": sorted(repair_scope),
                "outcome": "BUDGET_EXHAUSTED",
                "repair_attempt_count": repair_count,
            })
            return plan

        # Record original attempt ID for repair lineage before incrementing
        original_attempt_id = failed_step.evidence.get("original_failure_attempt_id")
        if original_attempt_id is None:
            # First repair — capture the original failure attempt
            runs = RunRepository(self.db).list_for_task(failed_step.task_id) if failed_step.task_id else []
            if runs:
                attempts = AttemptRepository(self.db).list_for_run(runs[-1].id)
                if attempts:
                    original_attempt_id = attempts[-1].id

        # Increment repair attempt count and chain provenance for each affected step
        for step in plan.steps:
            if step.id in repair_scope:
                step.evidence["repair_attempt_count"] = step.evidence.get("repair_attempt_count", 0) + 1
                step.evidence["repair_parent_step_id"] = failed_step_id
                if original_attempt_id:
                    step.evidence["original_failure_attempt_id"] = original_attempt_id
        plans.update(plan)

        # Execute repair
        repaired_plan = await self.execute_goal(
            goal, context=context, experiment_id=experiment_id,
            resume_plan_id=plan_id, repair_step_ids=repair_scope,
        )

        # Capture repair attempt lineage after execution
        for step in repaired_plan.steps:
            if step.id in repair_scope and step.task_id:
                new_runs = RunRepository(self.db).list_for_task(step.task_id)
                if new_runs:
                    new_attempts = AttemptRepository(self.db).list_for_run(new_runs[-1].id)
                    if new_attempts:
                        repair_attempt_id = new_attempts[-1].id
                        step.evidence["repair_attempt_id"] = repair_attempt_id
                        # Emit durable lineage event
                        self._emit(EventType.REPAIR_COMPLETED, {
                            "plan_id": plan_id,
                            "step_id": step.id,
                            "original_failure_attempt_id": step.evidence.get("original_failure_attempt_id"),
                            "repair_attempt_id": repair_attempt_id,
                            "repair_number": step.evidence.get("repair_attempt_count", 0),
                            "scope": scope.value,
                        })
        plans = PlanRepository(self.db)
        plan = plans.get(plan_id) or repaired_plan
        plans.update(plan)
        return repaired_plan

    async def _execute_dependency_plan(self, goal, plan, *, context, experiment_id, approved_step_ids, repair_step_ids=None):
        plans=PlanRepository(self.db); tasks=TaskRepository(self.db); scheduler=TaskGraphScheduler()
        events=EventStore(self.db)
        execution_context={"runtime":{**context,"goal_id":goal.id},"steps":{}}
        # Phase 3.3 — Repair mode: invalidate repair targets to PENDING
        if repair_step_ids:
            self._emit(EventType.REPAIR_STARTED, {"plan_id": plan.id, "repair_step_ids": sorted(repair_step_ids)})
            _TERMINAL_REPAIRABLE = {PlanStepStatus.FAILED, PlanStepStatus.CLASSIFIED_FAILURE, PlanStepStatus.BLOCKED, PlanStepStatus.STALE, PlanStepStatus.PRECONDITION_FAILED}
            # Reset repair targets to PENDING
            for s in plan.steps:
                if s.id in repair_step_ids and s.status in _TERMINAL_REPAIRABLE:
                    transition_step(s, PlanStepStatus.PENDING, "repair_invalidation", events, plan_id=plan.id)
                    self._emit(EventType.REPAIR_STEP_INVALIDATED, {"plan_id": plan.id, "step_id": s.id})
            # Also reset BLOCKED dependents of repaired steps
            # (they were blocked because of the failure, now the failure is being repaired)
            for s in plan.steps:
                if s.status == PlanStepStatus.BLOCKED and any(dep in repair_step_ids for dep in s.depends_on):
                    transition_step(s, PlanStepStatus.PENDING, "repair_unblock_dependent", events, plan_id=plan.id)
            plans.update(plan)
        for s in plan.steps:
            if s.id in approved_step_ids and s.status == PlanStepStatus.WAITING_FOR_HUMAN_APPROVAL:
                transition_step(s, PlanStepStatus.PENDING, "human_approval_granted", events, plan_id=plan.id)
            if s.status in {PlanStepStatus.COMPLETED, PlanStepStatus.VERIFIED}:
                execution_context["steps"][s.id]=s.execution_context.get("steps",{}).get(s.id,{"capability":s.capability,"output":s.output,"artifacts":{}, "usage":{}})
            # CLAIMED_COMPLETE on reload: route through verifier seam
            if s.status == PlanStepStatus.CLAIMED_COMPLETE:
                execution_context["steps"][s.id]=s.execution_context.get("steps",{}).get(s.id,{"capability":s.capability,"output":s.output,"artifacts":{}, "usage":{}})
                if self.workflow_verifier is not None:
                    vresult = self.workflow_verifier.verify(s, plan, events)
                    if vresult.accepted:
                        transition_step(s, PlanStepStatus.VERIFIED, "deferred_verification_accepted", events, plan_id=plan.id)
                    else:
                        transition_step(s, PlanStepStatus.CLASSIFIED_FAILURE, "deferred_verification_rejected", events, plan_id=plan.id)
                else:
                    transition_step(s, PlanStepStatus.WAITING_FOR_VERIFICATION, "deferred_no_verifier", events, plan_id=plan.id)
        plans.update(plan)
        while True:
            plan = plans.get(plan.id) or plan
            schedule=scheduler.calculate(plan)
            restart_authoritative_schedule = False
            for step in schedule.blocked_steps:
                transition_step(step, PlanStepStatus.BLOCKED, "dependency_failed", events, plan_id=plan.id)
                blockers=[d for d in step.depends_on if next(x for x in plan.steps if x.id==d).status in {PlanStepStatus.FAILED,PlanStepStatus.BLOCKED,PlanStepStatus.CLASSIFIED_FAILURE}]
                self._emit(EventType.PLAN_STEP_BLOCKED,{"plan_id":plan.id,"step_id":step.id,"blocked_by_step_ids":blockers})
            if schedule.blocked_steps: plans.update(plan)
            for step in list(schedule.ready_steps):
                self._emit(EventType.PLAN_STEP_READY,{"plan_id":plan.id,"step_id":step.id})
                spec=self.registry.resolve(step.capability).capability
                if step.id not in approved_step_ids and (spec.requires_human_approval or (goal.requires_human_approval and spec.side_effect)):
                    transition_step(step, PlanStepStatus.WAITING_FOR_HUMAN_APPROVAL, "human_approval_required", events, plan_id=plan.id)
                    plan.status=PlanStatus.WAITING_FOR_HUMAN_APPROVAL
                    self._emit(EventType.HUMAN_APPROVAL_REQUIRED,{"plan_id":plan.id,"step_id":step.id,"capability":step.capability}); plans.update(plan); continue

                # BLOCKER B: dispatch-time precondition re-evaluation
                # Reload authoritative plan from DB and re-check eligibility
                # with current execution context BEFORE any side effects.
                current_plan = plans.get(plan.id) or plan
                by_id = {s.id: s for s in current_plan.steps}
                step_in_plan = by_id.get(step.id)
                if step_in_plan is not None:
                    dispatch_context = {"runtime": {**context, "goal_id": goal.id}, "steps": execution_context.get("steps", {})}
                    eligible, reason = evaluate_step_eligibility(step_in_plan, by_id, execution_context=dispatch_context, event_store=events, plan_id=plan.id)
                    if not eligible:
                        if "precondition" in reason:
                            transition_step(step, PlanStepStatus.PRECONDITION_FAILED, "dispatch_precondition_failed", events, plan_id=plan.id)
                        # No side effects — skip this step
                        plans.update(plan)
                        continue

                transition_step(step, PlanStepStatus.RUNNING, "dispatch", events, plan_id=plan.id)
                step.execution_context=build_step_dependency_context(plan,step,execution_context)
                task=Task(project_id=goal.project_id,title=step.title,objective=step.objective,constraints=goal.constraints,acceptance_criteria=step.success_criteria,max_attempts=2); tasks.create(task); step.task_id=task.id
                self._emit(EventType.PLAN_STEP_STARTED,{"plan_id":plan.id,"step_id":step.id,"task_id":task.id})
                plans.update(plan)
                orch=RecoveringOrchestrator(self.db,executor_factory=lambda s=step,p=plan: self._step_executor(p,s,step.execution_context),executor_type="TaskGraphAgentExecutor" if self.agent_executor_factory else "ToolRegistryExecutor",provider="native-kernel" if self.agent_executor_factory else "tool-registry",model="provider-adapter" if self.agent_executor_factory else "deterministic",harness_version=HARNESS_VERSION,dataset_version="PLANNING-V0.1",experiment_id=experiment_id)
                run=await orch.execute_task(task.id)
                if run.status.value != "COMPLETED":
                    transition_step(step, PlanStepStatus.FAILED, "run_failed", events, plan_id=plan.id)
                    self._emit(EventType.PLAN_STEP_FAILED,{"plan_id":plan.id,"step_id":step.id,"run_id":run.id})
                    self._create_step_failure_provenance(step, plan, run.id)
                    # P3.3: compute repair scope using the durable provenance
                    provenance = step.evidence.get("failure_provenance", {})
                    scope, affected_ids = compute_repair_scope(
                        step, plan,
                        failure_class=provenance.get("failure_class"),
                        error_type=provenance.get("failure_type"),
                    )
                    if scope == RepairScope.AFFECTED_SUBGRAPH:
                        # Exclude the failed step itself — it's already FAILED
                        dependent_ids = affected_ids - {step.id}
                        invalidated = invalidate_affected_subgraph(plan, dependent_ids, events)
                        plan.invalidated_step_ids.extend(sid for sid in invalidated if sid not in plan.invalidated_step_ids)
                        self._emit(EventType.PLAN_STEP_BLOCKED, {"plan_id":plan.id,"step_id":step.id,"repair_scope":"AFFECTED_SUBGRAPH","invalidated_step_ids":sorted(invalidated)})
                        plans.update(plan); continue
                    elif scope == RepairScope.MACRO_REPLAN:
                        self._record_step_replan_signal(step, run.id)
                        if await self._maybe_replan(goal, plan, run.id, context):
                            restart_authoritative_schedule = True; break
                        plans.update(plan); continue
                    else:
                        # LOCAL scope: bounded local repair before replan
                        # Check repair budget
                        repair_count = step.evidence.get("repair_attempt_count", 0)
                        max_repair = step.budget.get("max_repair_attempts", 3)
                        if repair_count < max_repair:
                            # Budget allows — reset step to PENDING for re-execution
                            step.evidence["repair_attempt_count"] = repair_count + 1
                            # Capture original failure attempt for lineage
                            if "original_failure_attempt_id" not in step.evidence:
                                attempts = AttemptRepository(self.db).list_for_run(run.id)
                                if attempts:
                                    step.evidence["original_failure_attempt_id"] = attempts[-1].id
                            transition_step(step, PlanStepStatus.PENDING, "local_repair", events, plan_id=plan.id)
                            plans.update(plan)
                            continue
                        else:
                            # Budget exhausted — escalate to replan
                            self._record_step_replan_signal(step, run.id)
                            if await self._maybe_replan(goal, plan, run.id, context):
                                restart_authoritative_schedule = True; break
                            plans.update(plan); continue
                if await self._maybe_replan(goal, plan, run.id, context):
                    restart_authoritative_schedule = True
                    break
                import json
                payload=json.loads(run.result or "{}"); step.output=payload.get("output")
                if isinstance(step.output,str):
                    try: step.output=json.loads(step.output)
                    except json.JSONDecodeError: pass
                attempts=AttemptRepository(self.db).list_for_run(run.id); raw=json.loads(attempts[-1].executor_result or "{}") if attempts and attempts[-1].executor_result else {}
                rec={"capability":step.capability,"output":step.output,"artifacts":raw.get("artifacts",{}),"usage":raw.get("usage",{}),"provenance":self._evidence_provenance}; execution_context["steps"][step.id]=rec
                persisted_context=build_step_dependency_context(plan,step,execution_context); persisted_context["steps"][step.id]=rec; step.execution_context=persisted_context

                # P3.1: run success → CLAIMED_COMPLETE
                transition_step(step, PlanStepStatus.CLAIMED_COMPLETE, "run_completed", events, plan_id=plan.id, extra_payload={"run_id": run.id})
                self._emit(EventType.PLAN_STEP_COMPLETED,{"plan_id":plan.id,"step_id":step.id,"run_id":run.id})

                # Verification seam: explicit verifier only, default fail-closed
                if self.workflow_verifier is not None:
                    vresult = self.workflow_verifier.verify(step, plan, events)
                    if vresult.accepted:
                        transition_step(step, PlanStepStatus.VERIFIED, "verification_accepted", events, plan_id=plan.id)
                    else:
                        transition_step(step, PlanStepStatus.CLASSIFIED_FAILURE, "verification_rejected", events, plan_id=plan.id)
                        # Create failure provenance for verification rejection
                        self._create_verification_failure_provenance(step, plan, vresult)
                else:
                    transition_step(step, PlanStepStatus.WAITING_FOR_VERIFICATION, "no_verifier_configured", events, plan_id=plan.id)

                plans.update(plan)
            if restart_authoritative_schedule:
                continue
            schedule=scheduler.calculate(plan)
            if not schedule.ready_steps:
                # No steps can be dispatched — check if we can terminate
                if any(s.status==PlanStepStatus.WAITING_FOR_HUMAN_APPROVAL for s in plan.steps):
                    plan.status=PlanStatus.WAITING_FOR_HUMAN_APPROVAL; plans.update(plan); return plan
                if any(s.status==PlanStepStatus.WAITING_FOR_VERIFICATION for s in plan.steps):
                    plan.status=PlanStatus.WAITING_FOR_VERIFICATION; plans.update(plan); return plan
                if schedule.blocked_steps or schedule.pending_steps:
                    # Transition blocked steps to BLOCKED before returning
                    for step in schedule.blocked_steps:
                        transition_step(step, PlanStepStatus.BLOCKED, "dependency_failed", events, plan_id=plan.id)
                    plan.status=PlanStatus.FAILED; plans.update(plan); self._emit(EventType.PLAN_FAILED,{"plan_id":plan.id}); return plan
            if schedule.blocked_steps:
                continue
            if schedule.ready_steps:
                continue
            if any(s.status==PlanStepStatus.WAITING_FOR_HUMAN_APPROVAL for s in plan.steps): plan.status=PlanStatus.WAITING_FOR_HUMAN_APPROVAL; plans.update(plan); return plan
            # Plan is complete when all non-stale steps are VERIFIED
            if all(s.status in {PlanStepStatus.VERIFIED, PlanStepStatus.STALE} for s in plan.steps) and any(s.status == PlanStepStatus.VERIFIED for s in plan.steps):
                plan.status=PlanStatus.COMPLETED; plans.update(plan); self._emit(EventType.PLAN_COMPLETED,{"plan_id":plan.id})
                if repair_step_ids: self._emit(EventType.REPAIR_COMPLETED, {"plan_id": plan.id, "repair_step_ids": sorted(repair_step_ids), "outcome": "SUCCESS"})
                return plan
            if not schedule.ready_steps and not schedule.pending_steps:
                plan.status=PlanStatus.FAILED; plans.update(plan); self._emit(EventType.PLAN_FAILED,{"plan_id":plan.id})
                if repair_step_ids: self._emit(EventType.REPAIR_COMPLETED, {"plan_id": plan.id, "repair_step_ids": sorted(repair_step_ids), "outcome": "FAILED"})
                return plan
