"""Runtime Envelope — what control arms see.

This is the *only* data plane visible to any control arm during a live
trial.  It contains:

  • task_id         — stable identifier
  • objective       — natural-language task description
  • prompt          — the user query / instruction
  • visible_tools   — tool *skeletons* loaded from official YAML
                       definitions (name, description, parameters schema).
                       NO execution_trace, NO oracle outputs.
  • environment_snapshot — immutable snapshot of user_input context
  • budget          — BudgetConfig (turns, model calls, tokens, deadline)

The envelope is a frozen Pydantic model.  Once constructed it cannot be
mutated, and it can never contain any of the hidden fields:

  - execution_trace
  - expected_result
  - valid_paths
  - perturbation_point
  - alternative_tools
  - oracle_solution

These fields live exclusively in the offline evaluator or the raw task
JSON, both of which are unreachable from the runtime plane.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .types import BudgetConfig, PerturbationMode, TopologyClass


# ── Hidden-field guard ────────────────────────────────────────────────

_HIDDEN_FIELD_NAMES = frozenset({
    "execution_trace",
    "expected_result",
    "valid_paths",
    "perturbation_point",
    "alternative_tools",
    "oracle_solution",
    "oracle_path",
    "hidden_perturbation",
    "ground_truth",
    "ground_truth_labels",
    "judge_result",
    "target_milestones",
    "hidden_test_result",
})


# ── Tool skeleton (what the agent sees) ───────────────────────────────

class ToolSkeleton(BaseModel):
    """A tool definition visible to the runtime agent.

    Extracted from official YAML definitions — NOT from execution_trace.
    Contains only the declarative spec: name, description, parameter
    schema.  Never contains example outputs, oracle traces, or
    perturbation metadata.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str = ""
    category: str = ""
    domain: str = ""
    parameters_schema: dict[str, Any] = Field(default_factory=dict)
    substitutes: list[str] = Field(default_factory=list)

    # MCP / function-call paradigm info (read-only, declarative)
    paradigm_spec: Optional[dict[str, Any]] = None


# ── Runtime Envelope ──────────────────────────────────────────────────

class ToolMazeRuntimeEnvelope(BaseModel):
    """Immutable, oracle-free task envelope visible to all control arms.

    Constructed by the ``ToolMazeRuntimeBackend`` from the raw task JSON
    and official YAML tool definitions.  After construction the envelope
    is frozen — no mutation, no hidden-field injection.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    objective: str
    prompt: str
    visible_tools: list[ToolSkeleton] = Field(default_factory=list)
    environment_snapshot: dict[str, Any] = Field(default_factory=dict)
    budget: BudgetConfig = Field(
        default_factory=lambda: BudgetConfig(max_turns=30, max_model_calls=50)
    )

    # ── Lightweight metadata (safe for runtime) ──
    perturbation_mode: PerturbationMode = PerturbationMode.P0
    complexity: TopologyClass = TopologyClass.C1
    template_id: str = ""
    domains: list[str] = Field(default_factory=list)

    # ── Runtime guards ────────────────────────────────────────────────

    @model_validator(mode="after")
    def _no_hidden_leakage(self) -> "ToolMazeRuntimeEnvelope":
        """Ensure no hidden field names leaked into the envelope."""
        # Check all top-level field names
        actual_fields = set(self.model_fields.keys())
        violations = actual_fields & _HIDDEN_FIELD_NAMES
        if violations:
            raise ValueError(
                f"RuntimeEnvelope contains hidden fields: {violations}. "
                f"These must never appear in the runtime plane."
            )
        return self

    @model_validator(mode="after")
    def _no_oracle_in_tools(self) -> "ToolMazeRuntimeEnvelope":
        """Verify tool skeletons don't contain oracle data."""
        for tool in self.visible_tools:
            if tool.substitutes and any(
                s in _HIDDEN_FIELD_NAMES for s in tool.substitutes
            ):
                raise ValueError(
                    f"Tool '{tool.name}' has a substitute that looks like "
                    f"a hidden field name."
                )
        return self


# ── Factory helper ────────────────────────────────────────────────────

def build_envelope_from_task(
    task: dict[str, Any],
    *,
    tool_skeletons: list[ToolSkeleton],
    budget: Optional[BudgetConfig] = None,
) -> ToolMazeRuntimeEnvelope:
    """Build a ``ToolMazeRuntimeEnvelope`` from raw task JSON + skeletons.

    This is the **only** sanctioned way to construct an envelope.  It
    extracts the runtime-visible fields from the raw task and pairs them
    with pre-loaded tool skeletons (from YAML definitions).

    Parameters
    ----------
    task : dict
        The full raw task JSON (may contain hidden fields — they are
        ignored, not forwarded).
    tool_skeletons : list[ToolSkeleton]
        Tool definitions loaded from official YAML via ToolLoader.
    budget : BudgetConfig, optional
        Override budget.  Defaults to 30 turns / 50 model calls.
    """
    user_input = task.get("user_input", {})

    complexity_str = task.get("complexity", "C1")
    topo = (
        TopologyClass(complexity_str)
        if complexity_str in {"C1", "C2", "C3", "C4"}
        else TopologyClass.C1
    )
    mode_str = task.get("perturbation_mode", "P0")
    pm = (
        PerturbationMode(mode_str)
        if mode_str in {"P0", "P1", "P2", "P3", "P4"}
        else PerturbationMode.P0
    )

    return ToolMazeRuntimeEnvelope(
        task_id=task.get("task_id", ""),
        objective=task.get("task_description", ""),
        prompt=user_input.get("query", task.get("task_description", "")),
        visible_tools=tool_skeletons,
        environment_snapshot=user_input,
        budget=budget or BudgetConfig(max_turns=30, max_model_calls=50),
        perturbation_mode=pm,
        complexity=topo,
        template_id=task.get("template_id", ""),
        domains=task.get("domains", []),
    )
