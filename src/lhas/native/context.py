"""Deterministic bounded context assembly for NativeAgentKernel."""

from __future__ import annotations

import json
from typing import Any

from lhas.agent.context import ContextAssembler, ContextPriority, ContextSource
from lhas.agent.models import AgentRequest
from lhas.native.models import ExecutionSnapshot, ModelContext, ReplanSignal, ValidationFailure


_SYSTEM = (
    "You are executing inside the Odys Native Harness. Use only listed tools. "
    "Tool failures and validator rejection are observations, not permission to "
    "repeat side effects blindly. A final answer is only a completion candidate; "
    "Odys independently validates it. Do not expose hidden reasoning or secrets."
)


class NativeContextAssembler:
    def __init__(self, assembler: ContextAssembler | None = None):
        self.assembler = assembler or ContextAssembler()

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
        graph = {
            "goal_id": runtime.get("goal_id") or runtime.get("runtime", {}).get("goal_id"),
            "plan_id": canonical_graph.get("plan_id") or runtime.get("plan_id"),
            "active_node": snapshot.taskgraph_position,
            "completed_nodes": snapshot.completed_nodes,
            "pending_nodes": snapshot.pending_nodes,
        }
        execution = {
            "phase": snapshot.phase.value,
            "attempt_id": snapshot.attempt_id,
            "model_turn_count": snapshot.model_turn_count,
            "tool_call_count": snapshot.tool_call_count,
            "workspace_identity": snapshot.workspace_identity,
            "workspace_mutation_version": snapshot.workspace_mutation_version,
            "recent_tool_outcomes": snapshot.recent_tool_outcomes[-20:],
            "repeated_failure_state": snapshot.repeated_failure_state,
            "verification_state": snapshot.verification_state,
            "current_failure": snapshot.current_failure,
            "delegation_dependencies": snapshot.delegation_dependencies,
        }
        sources = [
            ContextSource("goal", request.objective, ContextPriority.REQUIRED, 20_000),
            ContextSource("acceptance", runtime.get("acceptance_criteria", []), ContextPriority.REQUIRED, 8_000),
            ContextSource("taskgraph", graph, ContextPriority.HIGH, 8_000),
            ContextSource("execution_state", execution, ContextPriority.HIGH, 18_000),
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
