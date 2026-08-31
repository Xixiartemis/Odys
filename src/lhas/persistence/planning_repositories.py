from sqlalchemy import select
from lhas.domain.models import json_dumps, json_loads
from lhas.persistence.database import Database
from lhas.persistence.orm import GoalRow, PlanRow, PlanStepRow
from lhas.planning.models import Goal, Plan, PlanStep

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


class PlanRepository:
    def __init__(self, db): self.db=db
    def create(self,p):
        with self.db.session() as s:
            s.add(PlanRow(id=p.id,goal_id=p.goal_id,version=p.version,mode=p.mode.value,status=p.status.value,created_at=p.created_at,metadata_json=json_dumps({**p.metadata, "replan_count": p.replan_count}),invalidated_step_ids=json_dumps(p.invalidated_step_ids)))
            for i,x in enumerate(p.steps): s.add(PlanStepRow(id=x.id,plan_id=p.id,position=i,title=x.title,objective=x.objective,capability=x.capability,depends_on=json_dumps(x.depends_on),inputs=json_dumps(_step_inputs(x)),expected_output=x.expected_output,success_criteria=json_dumps(x.success_criteria),status=x.status.value,task_id=x.task_id,output=json_dumps(x.output),execution_context=json_dumps(x.execution_context)))
        return p
    def update(self,p):
        with self.db.session() as s:
            r=s.get(PlanRow,p.id); r.status=p.status.value; r.version=p.version; r.metadata_json=json_dumps({**p.metadata, "replan_count": p.replan_count}); r.invalidated_step_ids=json_dumps(p.invalidated_step_ids)
            for x in p.steps:
                q=s.get(PlanStepRow,x.id)
                if q is None:
                    q=PlanStepRow(id=x.id,plan_id=p.id,position=p.steps.index(x),title=x.title,objective=x.objective,capability=x.capability,depends_on=json_dumps(x.depends_on),inputs=json_dumps(_step_inputs(x)),expected_output=x.expected_output,success_criteria=json_dumps(x.success_criteria),status=x.status.value,task_id=x.task_id,output=json_dumps(x.output),execution_context=json_dumps(x.execution_context)); s.add(q)
                else:
                    q.position=p.steps.index(x); q.title=x.title; q.objective=x.objective; q.capability=x.capability; q.depends_on=json_dumps(x.depends_on); q.inputs=json_dumps(_step_inputs(x)); q.expected_output=x.expected_output; q.success_criteria=json_dumps(x.success_criteria); q.status=x.status.value; q.task_id=x.task_id; q.output=json_dumps(x.output); q.execution_context=json_dumps(x.execution_context)
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
                steps.append(PlanStep(id=x.id,title=x.title,objective=x.objective,capability=x.capability,depends_on=json_loads(x.depends_on) or [],inputs=inputs,expected_output=x.expected_output or "",success_criteria=json_loads(x.success_criteria) or [],status=x.status,task_id=x.task_id,output=json_loads(x.output),execution_context=json_loads(x.execution_context) or {},suggested_role=agent_fields.get("suggested_role","WORKER"),required_capabilities=agent_fields.get("required_capabilities",[]),optional_skill_refs=agent_fields.get("optional_skill_refs",[])))
            metadata=json_loads(r.metadata_json) or {}
            return Plan(id=r.id,goal_id=r.goal_id,version=r.version,mode=r.mode,status=r.status,steps=steps,metadata=metadata,invalidated_step_ids=json_loads(r.invalidated_step_ids) or [],replan_count=int(metadata.get("replan_count", 0)),created_at=r.created_at)
