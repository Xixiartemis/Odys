from sqlalchemy import select, update as sa_update
from lhas.domain.models import json_dumps, json_loads
from lhas.persistence.database import Database
from lhas.persistence.orm import GoalRow, PlanRow, PlanStepRow
from lhas.planning.models import Goal, Plan, PlanStep, StepPrecondition


class PlanVersionConflict(RuntimeError):
    code = "REPLAN_VERSION_CONFLICT"

class GoalRepository:
    def __init__(self, db): self.db=db
    def create(self, g):
        with self.db.session() as s:
            if s.get(GoalRow, g.id) is None: s.add(GoalRow(id=g.id,project_id=g.project_id,objective=g.objective,constraints=json_dumps(g.constraints),success_criteria=json_dumps(g.success_criteria),allowed_capabilities=json_dumps(g.allowed_capabilities),requires_human_approval=g.requires_human_approval,metadata_json=json_dumps(g.metadata),created_at=g.created_at))
        return g
    def get(self, i):
        with self.db.session() as s:
            r=s.get(GoalRow,i)
            return Goal(id=r.id,project_id=r.project_id,objective=r.objective,constraints=json_loads(r.constraints) or [],success_criteria=json_loads(r.success_criteria) or [],allowed_capabilities=json_loads(r.allowed_capabilities) or [],requires_human_approval=bool(r.requires_human_approval),metadata=json_loads(r.metadata_json) or {},created_at=r.created_at) if r else None

def _step_inputs(step):
    inputs = dict(step.inputs)
    inputs["_agent_platform"] = {
        "suggested_role": step.suggested_role,
        "required_capabilities": step.required_capabilities,
        "optional_skill_refs": step.optional_skill_refs,
    }
    return inputs


def _step_row_kwargs(step, plan_id, position):
    """Build PlanStepRow kwargs including Phase 3 authority fields."""
    return dict(
        id=step.id, plan_id=plan_id, position=position,
        title=step.title, objective=step.objective, capability=step.capability,
        depends_on=json_dumps(step.depends_on), inputs=json_dumps(_step_inputs(step)),
        expected_output=step.expected_output, success_criteria=json_dumps(step.success_criteria),
        status=step.status.value, task_id=step.task_id, output=json_dumps(step.output),
        execution_context=json_dumps(step.execution_context),
        semantic_fingerprint=step.semantic_fingerprint,
        preconditions=json_dumps([pc.model_dump(mode="json") for pc in step.preconditions]),
        expected_effects=json_dumps(step.expected_effects),
        evidence=json_dumps(step.evidence),
        risk_class=step.risk_class,
        budget=json_dumps(step.budget),
        checkpoint_policy=step.checkpoint_policy,
        recovery_policy=step.recovery_policy,
    )


def _update_step_row(row, step, plan_id, position):
    """Update an existing PlanStepRow with Phase 3 authority fields."""
    row.position = position
    row.title = step.title
    row.objective = step.objective
    row.capability = step.capability
    row.depends_on = json_dumps(step.depends_on)
    row.inputs = json_dumps(_step_inputs(step))
    row.expected_output = step.expected_output
    row.success_criteria = json_dumps(step.success_criteria)
    row.status = step.status.value
    row.task_id = step.task_id
    row.output = json_dumps(step.output)
    row.execution_context = json_dumps(step.execution_context)
    row.semantic_fingerprint = step.semantic_fingerprint
    row.preconditions = json_dumps([pc.model_dump(mode="json") for pc in step.preconditions])
    row.expected_effects = json_dumps(step.expected_effects)
    row.evidence = json_dumps(step.evidence)
    row.risk_class = step.risk_class
    row.budget = json_dumps(step.budget)
    row.checkpoint_policy = step.checkpoint_policy
    row.recovery_policy = step.recovery_policy


class PlanRepository:
    def __init__(self, db): self.db=db
    def create(self,p):
        with self.db.session() as s:
            s.add(PlanRow(id=p.id,goal_id=p.goal_id,version=p.version,mode=p.mode.value,status=p.status.value,created_at=p.created_at,metadata_json=json_dumps({**p.metadata, "replan_count": p.replan_count}),invalidated_step_ids=json_dumps(p.invalidated_step_ids)))
            for i,x in enumerate(p.steps): s.add(PlanStepRow(**_step_row_kwargs(x, p.id, i)))
        return p
    def update(self,p):
        with self.db.session() as s:
            r=s.get(PlanRow,p.id); r.status=p.status.value; r.version=p.version; r.metadata_json=json_dumps({**p.metadata, "replan_count": p.replan_count}); r.invalidated_step_ids=json_dumps(p.invalidated_step_ids)
            for x in p.steps:
                q=s.get(PlanStepRow,x.id)
                if q is None:
                    q=PlanStepRow(**_step_row_kwargs(x, p.id, p.steps.index(x))); s.add(q)
                else:
                    _update_step_row(q, x, p.id, p.steps.index(x))
        return p
    def update_if_version(self, p, *, expected_version: str):
        """Commit an authoritative replan only if its base is still current."""
        with self.db.session() as s:
            result = s.execute(
                sa_update(PlanRow)
                .where(PlanRow.id == p.id, PlanRow.version == expected_version)
                .values(
                    status=p.status.value,
                    version=p.version,
                    metadata_json=json_dumps({**p.metadata, "replan_count": p.replan_count}),
                    invalidated_step_ids=json_dumps(p.invalidated_step_ids),
                )
            )
            if result.rowcount != 1:
                raise PlanVersionConflict("REPLAN_VERSION_CONFLICT")
            for x in p.steps:
                q=s.get(PlanStepRow,x.id)
                if q is None:
                    q=PlanStepRow(**_step_row_kwargs(x, p.id, p.steps.index(x))); s.add(q)
                else:
                    _update_step_row(q, x, p.id, p.steps.index(x))
        return p
    def get(self, plan_id):
        with self.db.session() as s:
            r=s.get(PlanRow, plan_id)
            if not r: return None
            rows=s.execute(select(PlanStepRow).where(PlanStepRow.plan_id==plan_id).order_by(PlanStepRow.position)).scalars().all()
            steps=[]
            for x in rows:
                inputs=json_loads(x.inputs) or {}
                agent_fields=inputs.pop("_agent_platform", {})
                # Deserialize Phase 3 preconditions
                raw_pcs = json_loads(x.preconditions) or []
                preconditions = [StepPrecondition(**pc) for pc in raw_pcs]
                steps.append(PlanStep(
                    id=x.id,title=x.title,objective=x.objective,capability=x.capability,
                    depends_on=json_loads(x.depends_on) or [],inputs=inputs,
                    expected_output=x.expected_output or "",
                    success_criteria=json_loads(x.success_criteria) or [],
                    status=x.status,task_id=x.task_id,output=json_loads(x.output),
                    execution_context=json_loads(x.execution_context) or {},
                    suggested_role=agent_fields.get("suggested_role","WORKER"),
                    required_capabilities=agent_fields.get("required_capabilities",[]),
                    optional_skill_refs=agent_fields.get("optional_skill_refs",[]),
                    semantic_fingerprint=x.semantic_fingerprint,
                    preconditions=preconditions,
                    expected_effects=json_loads(x.expected_effects) or {},
                    evidence=json_loads(x.evidence) or {},
                    risk_class=x.risk_class or "LOW",
                    budget=json_loads(x.budget) or {},
                    checkpoint_policy=x.checkpoint_policy or "ON_FAILURE",
                    recovery_policy=x.recovery_policy or "RETRY_WITH_FAILURE_CONTEXT",
                ))
            metadata=json_loads(r.metadata_json) or {}
            return Plan(id=r.id,goal_id=r.goal_id,version=r.version,mode=r.mode,status=r.status,steps=steps,metadata=metadata,invalidated_step_ids=json_loads(r.invalidated_step_ids) or [],replan_count=int(metadata.get("replan_count", 0)),created_at=r.created_at)
