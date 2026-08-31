from typing import Any
from lhas.domain.enums import EventType, ExecutionStatus
from lhas import HARNESS_VERSION
from lhas.domain.models import Task, new_id
from lhas.executors.protocol import ExecutionRequest, ExecutionResult
from lhas.persistence.database import Database
from lhas.persistence.event_store import EventStore
from lhas.persistence.repositories import TaskRepository, RunRepository, AttemptRepository
from lhas.persistence.planning_repositories import GoalRepository, PlanRepository
from lhas.orchestrator_v2 import RecoveringOrchestrator
from lhas.planning.models import Goal, Plan, PlanStatus, PlanStepStatus
from lhas.planning.scheduler import TaskGraphScheduler, build_step_dependency_context
from lhas.planning.planner import Planner
from lhas.tools.registry import ToolRegistry
from lhas.tools.protocol import ToolRequest, ToolResultStatus

class _ToolExecutor:
    name = "ToolRegistryExecutor"
    def __init__(self, registry, step, db, context): self.registry, self.step, self.db, self.context = registry, step, db, context
    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        event = EventStore(self.db)
        try: tool = self.registry.resolve(self.step.capability)
        except KeyError as exc: return ExecutionResult(status=ExecutionStatus.FAILURE, error_type="UNKNOWN_CAPABILITY", error_message=str(exc))
        tr = ToolRequest(tool_call_id=new_id(), task_id=request.task_id, run_id=request.run_id, attempt_id=request.attempt_id,
                         capability=self.step.capability, arguments=self.step.inputs, context={**self.context, **request.context}, metadata=request.metadata)
        safe_request={"tool_call_id":tr.tool_call_id,"capability":tr.capability}
        event.append(EventType.TOOL_CALL_STARTED, task_id=request.task_id, run_id=request.run_id, attempt_id=request.attempt_id, payload={"request":safe_request})
        try:
            result = await tool.execute(tr)
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
    def __init__(self, executor, plan, step): self.executor, self.plan, self.step = executor, plan, step
    async def execute(self, request):
        completed=[item.id for item in self.plan.steps if item.status == PlanStepStatus.COMPLETED]
        pending=[item.id for item in self.plan.steps if item.id != self.step.id and item.status in {PlanStepStatus.PENDING,PlanStepStatus.READY,PlanStepStatus.RUNNING}]
        context={**request.context,"taskgraph":{"plan_id":self.plan.id,"active_node":self.step.id,"completed_nodes":completed,"pending_nodes":pending,"depends_on":list(self.step.depends_on)}}
        return await self.executor.execute(request.model_copy(update={"context":context}))
    async def resume(self, request): return await self.execute(request)
    async def cancel(self, run_id): return await self.executor.cancel(run_id)
    async def status(self, run_id): return await self.executor.status(run_id)

class PlanExecutionService:
    def __init__(self, db: Database, planner: Planner, registry: ToolRegistry, agent_executor_factory=None): self.db, self.planner, self.registry, self.agent_executor_factory = db, planner, registry, agent_executor_factory
    def _step_executor(self, plan, step, context):
        if self.agent_executor_factory is not None:
            return _TaskGraphAgentExecutor(self.agent_executor_factory(step),plan,step)
        return _ToolExecutor(self.registry,step,self.db,context)
    def _emit(self, typ, payload): EventStore(self.db).append(typ, payload=payload)
    async def execute_goal(self, goal: Goal, *, context: dict[str, Any] | None = None, experiment_id: str | None = None, approved_step_ids: set[str] | None = None, resume_plan_id: str | None = None) -> Plan:
        self._emit(EventType.GOAL_CREATED, {"goal": goal.model_dump(mode="json")})
        GoalRepository(self.db).create(goal)
        plans = PlanRepository(self.db)
        if resume_plan_id:
            plan = plans.get(resume_plan_id)
            if plan is None or plan.goal_id != goal.id: raise KeyError(f"plan not found for goal: {resume_plan_id}")
            self._emit(EventType.HUMAN_APPROVAL_GRANTED, {"plan_id": plan.id, "approved_step_ids": sorted(approved_step_ids or set())})
        else:
            plan = await self.planner.create_plan(goal=goal, capabilities=self.registry.specs(), context=context or {})
            plans.create(plan)
            self._emit(EventType.PLAN_CREATED, {"plan": plan.model_dump(mode="json")})
        if plan.mode.value != "LINEAR":
            if plan.mode.value == "SIMPLE_DEPENDENCY":
                return await self._execute_dependency_plan(goal, plan, context=context or {}, experiment_id=experiment_id, approved_step_ids=approved_step_ids or set())
            raise NotImplementedError(f"unsupported plan mode: {plan.mode.value}")
        self._emit(EventType.PLAN_STARTED, {"plan_id": plan.id})
        task_repo = TaskRepository(self.db)
        execution_context = {"runtime": {**dict(context or {}), "goal_id": goal.id}, "steps": {}}
        for step in plan.steps:
            if step.status == PlanStepStatus.COMPLETED:
                execution_context["steps"][step.id] = step.execution_context.get("steps", {}).get(step.id, {"capability": step.capability, "output": step.output, "artifacts": {}, "usage": {}})
                continue
            spec = self.registry.resolve(step.capability).capability
            if step.id not in (approved_step_ids or set()) and (spec.requires_human_approval or (goal.requires_human_approval and spec.side_effect)):
                step.status = PlanStepStatus.WAITING_FOR_HUMAN_APPROVAL; plan.status = PlanStatus.WAITING_FOR_HUMAN_APPROVAL
                self._emit(EventType.HUMAN_APPROVAL_REQUIRED, {"plan_id": plan.id, "step_id": step.id, "capability": step.capability})
                plans.update(plan); return plan
            step.execution_context = dict(execution_context)
            task = Task(project_id=goal.project_id, title=step.title, objective=step.objective, constraints=goal.constraints, acceptance_criteria=step.success_criteria, max_attempts=2)
            task_repo.create(task); step.task_id = task.id; step.status = PlanStepStatus.RUNNING
            self._emit(EventType.PLAN_STEP_STARTED, {"plan_id": plan.id, "step_id": step.id, "task_id": task.id})
            orch = RecoveringOrchestrator(self.db, executor_factory=lambda s=step,p=plan: self._step_executor(p,s,execution_context), executor_type="TaskGraphAgentExecutor" if self.agent_executor_factory else "ToolRegistryExecutor", provider="native-kernel" if self.agent_executor_factory else "tool-registry", model="provider-adapter" if self.agent_executor_factory else "deterministic", harness_version=HARNESS_VERSION, dataset_version="PLANNING-V0.1", experiment_id=experiment_id)
            run = await orch.execute_task(task.id)
            if run.status.value != "COMPLETED":
                step.status = PlanStepStatus.FAILED; plan.status = PlanStatus.FAILED; plans.update(plan); self._emit(EventType.PLAN_STEP_FAILED, {"plan_id": plan.id, "step_id": step.id, "run_id": run.id}); self._emit(EventType.PLAN_FAILED, {"plan_id": plan.id}); return plan
            import json
            payload = json.loads(run.result or "{}")
            step.output = payload.get("output")
            if isinstance(step.output, str):
                try: step.output = json.loads(step.output)
                except json.JSONDecodeError: pass
            attempts = AttemptRepository(self.db).list_for_run(run.id)
            raw = json.loads(attempts[-1].executor_result or "{}") if attempts and attempts[-1].executor_result else {}
            record = {"capability": step.capability, "output": step.output, "artifacts": raw.get("artifacts", {}), "usage": raw.get("usage", {})}
            execution_context["steps"][step.id] = record
            execution_context[step.capability] = record
            step.execution_context = dict(execution_context)
            step.status = PlanStepStatus.COMPLETED; plans.update(plan); self._emit(EventType.PLAN_STEP_COMPLETED, {"plan_id": plan.id, "step_id": step.id, "run_id": run.id, "output": step.output})
        plan.status = PlanStatus.COMPLETED; plans.update(plan); self._emit(EventType.PLAN_COMPLETED, {"plan_id": plan.id}); return plan

    async def resume_after_approval(self, plan_id: str, goal: Goal, step_id: str, *, context: dict[str, Any] | None = None, experiment_id: str | None = None) -> Plan:
        """Resume by explicitly granting one previously gated capability."""
        return await self.execute_goal(goal, context=context, experiment_id=experiment_id, approved_step_ids={step_id}, resume_plan_id=plan_id)

    async def _execute_dependency_plan(self, goal, plan, *, context, experiment_id, approved_step_ids):
        plans=PlanRepository(self.db); tasks=TaskRepository(self.db); scheduler=TaskGraphScheduler()
        execution_context={"runtime":{**context,"goal_id":goal.id},"steps":{}}
        for s in plan.steps:
            if s.id in approved_step_ids and s.status == PlanStepStatus.WAITING_FOR_HUMAN_APPROVAL:
                s.status = PlanStepStatus.PENDING
            if s.status == PlanStepStatus.COMPLETED:
                execution_context["steps"][s.id]=s.execution_context.get("steps",{}).get(s.id,{"capability":s.capability,"output":s.output,"artifacts":{},"usage":{}})
        while True:
            schedule=scheduler.calculate(plan)
            for step in schedule.blocked_steps:
                step.status=PlanStepStatus.BLOCKED
                blockers=[d for d in step.depends_on if next(x for x in plan.steps if x.id==d).status in {PlanStepStatus.FAILED,PlanStepStatus.BLOCKED}]
                self._emit(EventType.PLAN_STEP_BLOCKED,{"plan_id":plan.id,"step_id":step.id,"blocked_by_step_ids":blockers})
            if schedule.blocked_steps: plans.update(plan)
            for step in schedule.ready_steps:
                self._emit(EventType.PLAN_STEP_READY,{"plan_id":plan.id,"step_id":step.id})
                spec=self.registry.resolve(step.capability).capability
                if step.id not in approved_step_ids and (spec.requires_human_approval or (goal.requires_human_approval and spec.side_effect)):
                    step.status=PlanStepStatus.WAITING_FOR_HUMAN_APPROVAL; plan.status=PlanStatus.WAITING_FOR_HUMAN_APPROVAL
                    self._emit(EventType.HUMAN_APPROVAL_REQUIRED,{"plan_id":plan.id,"step_id":step.id,"capability":step.capability}); plans.update(plan); continue
                step.status=PlanStepStatus.RUNNING; step.execution_context=build_step_dependency_context(plan,step,execution_context)
                task=Task(project_id=goal.project_id,title=step.title,objective=step.objective,constraints=goal.constraints,acceptance_criteria=step.success_criteria,max_attempts=2); tasks.create(task); step.task_id=task.id
                self._emit(EventType.PLAN_STEP_STARTED,{"plan_id":plan.id,"step_id":step.id,"task_id":task.id})
                orch=RecoveringOrchestrator(self.db,executor_factory=lambda s=step,p=plan: self._step_executor(p,s,step.execution_context),executor_type="TaskGraphAgentExecutor" if self.agent_executor_factory else "ToolRegistryExecutor",provider="native-kernel" if self.agent_executor_factory else "tool-registry",model="provider-adapter" if self.agent_executor_factory else "deterministic",harness_version=HARNESS_VERSION,dataset_version="PLANNING-V0.1",experiment_id=experiment_id)
                run=await orch.execute_task(task.id)
                if run.status.value != "COMPLETED":
                    step.status=PlanStepStatus.FAILED; self._emit(EventType.PLAN_STEP_FAILED,{"plan_id":plan.id,"step_id":step.id,"run_id":run.id}); plans.update(plan); continue
                import json
                payload=json.loads(run.result or "{}"); step.output=payload.get("output")
                if isinstance(step.output,str):
                    try: step.output=json.loads(step.output)
                    except json.JSONDecodeError: pass
                attempts=AttemptRepository(self.db).list_for_run(run.id); raw=json.loads(attempts[-1].executor_result or "{}") if attempts and attempts[-1].executor_result else {}
                rec={"capability":step.capability,"output":step.output,"artifacts":raw.get("artifacts",{}),"usage":raw.get("usage",{})}; execution_context["steps"][step.id]=rec
                persisted_context=build_step_dependency_context(plan,step,execution_context); persisted_context["steps"][step.id]=rec; step.execution_context=persisted_context
                step.status=PlanStepStatus.COMPLETED; self._emit(EventType.PLAN_STEP_COMPLETED,{"plan_id":plan.id,"step_id":step.id,"run_id":run.id}); plans.update(plan)
            schedule=scheduler.calculate(plan)
            if schedule.blocked_steps:
                continue
            if schedule.ready_steps:
                continue
            if any(s.status==PlanStepStatus.WAITING_FOR_HUMAN_APPROVAL for s in plan.steps): plan.status=PlanStatus.WAITING_FOR_HUMAN_APPROVAL; plans.update(plan); return plan
            if all(s.status==PlanStepStatus.COMPLETED for s in plan.steps): plan.status=PlanStatus.COMPLETED; plans.update(plan); self._emit(EventType.PLAN_COMPLETED,{"plan_id":plan.id}); return plan
            if not schedule.ready_steps and not schedule.pending_steps:
                plan.status=PlanStatus.FAILED; plans.update(plan); self._emit(EventType.PLAN_FAILED,{"plan_id":plan.id}); return plan
