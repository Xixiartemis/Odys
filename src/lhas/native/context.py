"""Deterministic bounded context assembly for NativeAgentKernel."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

from lhas.agent.context import ContextAssembler, ContextPriority, ContextSource
from lhas.agent.models import AgentRequest
from lhas.native.models import ExecutionSnapshot, ModelContext, ReplanSignal, ValidationFailure
from lhas.planning.execution_contract import contract_from_context
from lhas.recovery_control import RecoveryContextProjector


_SYSTEM = (
    "You are executing inside the Odys Native Harness. Use only listed tools. "
    "Tool failures and validator rejection are observations, not permission to "
    "repeat side effects blindly. A final answer is only a completion candidate; "
    "Odys independently validates it. Do not expose hidden reasoning or secrets."
)


class NativeContextAssembler:
    def __init__(self, assembler: ContextAssembler | None = None):
        self.assembler = assembler or ContextAssembler()
        self.recovery_projector = RecoveryContextProjector()

    def build(
        self,
        request: AgentRequest,
        snapshot: ExecutionSnapshot,
        *,
        validation_failures: list[ValidationFailure] | None = None,
        replan_signals: list[ReplanSignal] | None = None,
    ) -> ModelContext:
        runtime = request.context if isinstance(request.context, dict) else {}
        canonical_graph = runtime.get("taskgraph") if isinstance(runtime.get("taskgraph"), dict) else {}
        active_contract = contract_from_context(runtime)
        graph = {
            "goal_id": runtime.get("goal_id") or runtime.get("runtime", {}).get("goal_id"),
            "plan_id": canonical_graph.get("plan_id") or runtime.get("plan_id"),
            "active_node": snapshot.taskgraph_position,
            "completed_nodes": snapshot.completed_nodes,
            "pending_nodes": snapshot.pending_nodes,
        }
        if active_contract is not None:
            graph["active_step_contract"] = active_contract
        execution = {
            "phase": snapshot.phase.value,
            "attempt_id": snapshot.attempt_id,
            "model_turn_count": snapshot.model_turn_count,
            "tool_call_count": snapshot.tool_call_count,
            "workspace_identity": snapshot.workspace_identity,
            "workspace_mutation_version": snapshot.workspace_mutation_version,
            # Keep the prompt projection compact while the durable snapshot
            # retains its bounded forensic history for replay/audit.
            "recent_tool_outcomes": snapshot.recent_tool_outcomes[-8:],
            "repeated_failure_state": snapshot.repeated_failure_state,
            "verification_state": snapshot.verification_state,
            "current_failure": snapshot.current_failure,
            "delegation_dependencies": snapshot.delegation_dependencies,
        }
        repair_progress = snapshot.current_failure.get("repair_convergence")
        if isinstance(repair_progress, dict):
            execution["repair_progress"] = dict(repair_progress)
        if runtime.get("recovery_control_plane_v2"):
            repair_context = runtime.get("repair_context", {})
            if not isinstance(repair_context, Mapping):
                repair_context = {}
            recent = snapshot.recent_tool_outcomes
            last_observation = recent[-1] if recent else None
            action_fingerprints = [
                item.get("args_sha256")
                for item in recent
                if isinstance(item, Mapping) and item.get("args_sha256")
            ][-16:]
            execution["recovery_context_projection"] = self.recovery_projector.project(
                goal=request.objective,
                acceptance_contract=runtime.get("acceptance_criteria", []),
                current_state=(
                    last_observation.get("safe_summary", last_observation)
                    if isinstance(last_observation, Mapping)
                    else {}
                ),
                failure_provenance=repair_context.get("failure_provenance", {}),
                progress=repair_progress or {},
                attempted_actions=action_fingerprints,
                last_useful_observation=last_observation,
                current_mismatch=repair_context.get("mismatch"),
                budget=runtime.get("recovery_budget", {}),
            )
            # The durable snapshot remains complete; the model receives the
            # semantic projection plus only the latest bounded observations.
            execution["recent_tool_outcomes"] = recent[-3:]
        sources = [
            ContextSource("goal", request.objective, ContextPriority.REQUIRED, 20_000),
            ContextSource("acceptance", runtime.get("acceptance_criteria", []), ContextPriority.REQUIRED, 8_000),
            # Keep the accepted PlanStep explicit and high priority.  The
            # model must receive the exact capability and inputs selected by
            # the durable planner, not rediscover a strategy from the goal.
            ContextSource("active_step_contract", active_contract or {}, ContextPriority.REQUIRED, 12_000),
            ContextSource("taskgraph", graph, ContextPriority.HIGH, 8_000),
            ContextSource("execution_state", execution, ContextPriority.HIGH, 18_000),
            ContextSource("repair_context", runtime.get("repair_context", {}), ContextPriority.HIGH, 12_000),
            ContextSource("validation_failures", [item.model_dump(mode="json") for item in (validation_failures or [])][-5:], ContextPriority.HIGH, 8_000),
            ContextSource("replan_signals", [item.model_dump(mode="json") for item in (replan_signals or [])][-10:], ContextPriority.HIGH, 8_000),
            ContextSource("selected_memory", runtime.get("selected_memory", runtime.get("memory", [])), ContextPriority.NORMAL, 6_000),
            ContextSource("selected_knowledge", runtime.get("selected_knowledge", runtime.get("knowledge", [])), ContextPriority.NORMAL, 8_000),
            ContextSource("skill_instructions", runtime.get("skill_instructions", []), ContextPriority.NORMAL, 8_000),
            ContextSource("conversation", request.messages[-20:], ContextPriority.LOW, 8_000),
        ]
        assembled = self.assembler.assemble(sources, budget_chars=request.budget.max_context_chars)
        user_payload = json.dumps(assembled.sections, ensure_ascii=False, sort_keys=True, default=str)
        return ModelContext(
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user_payload},
            ],
            sections=assembled.sections,
            chars_used=assembled.chars_used,
            budget_chars=assembled.budget_chars,
            truncated_sections=list(assembled.truncated_sections),
        )
