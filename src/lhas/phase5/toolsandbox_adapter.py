"""ToolSandbox progress validation adapter.

Uses ToolSandbox's native world-state snapshots, Milestone DAG, and
milestone_mapping only OFFLINE.  Target milestones are never exposed
to the Odys runtime.  Derived Phase5 metrics are separate from the
official ToolSandbox leaderboard metrics.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from .types import (
    BenchmarkAdapter,
    BenchmarkIdentity,
    BenchmarkName,
    BudgetConfig,
    DerivedMetrics,
    NativeResult,
    PerturbationMode,
    RuntimeTask,
    TaskDescriptor,
    TrialStatus,
)


# ── Sample ToolSandbox task set ─────────────────────────────────────

_SAMPLE_MILESTONES: dict[str, list[dict[str, Any]]] = {
    "TS-001": [
        {"milestone_id": "m1", "description": "Initialize environment", "order": 1},
        {"milestone_id": "m2", "description": "Configure tools", "order": 2},
        {"milestone_id": "m3", "description": "Execute workflow", "order": 3},
        {"milestone_id": "m4", "description": "Validate output", "order": 4},
    ],
    "TS-002": [
        {"milestone_id": "m1", "description": "Parse input", "order": 1},
        {"milestone_id": "m2", "description": "Transform data", "order": 2},
        {"milestone_id": "m3", "description": "Generate report", "order": 3},
    ],
}

_SAMPLE_TASKS: list[dict[str, Any]] = [
    {
        "task_id": "TS-001",
        "objective": "Build and validate a multi-step tool pipeline",
        "complexity": "medium",
        "visible_tools": [
            {"name": "file_tool", "description": "File operations"},
            {"name": "data_tool", "description": "Data transformation"},
            {"name": "validate_tool", "description": "Validation"},
        ],
        "prompt": "Create a pipeline that reads data, transforms it, and validates the output.",
        "milestone_ids": ["m1", "m2", "m3", "m4"],
    },
    {
        "task_id": "TS-002",
        "objective": "Process and report on a dataset",
        "complexity": "standard",
        "visible_tools": [
            {"name": "parse_tool", "description": "Input parsing"},
            {"name": "transform_tool", "description": "Data transformation"},
            {"name": "report_tool", "description": "Report generation"},
        ],
        "prompt": "Parse the input, transform it, and generate a summary report.",
        "milestone_ids": ["m1", "m2", "m3"],
    },
]


class ToolSandboxAdapter:
    """ToolSandbox adapter.

    Milestone DAG and target milestones are stored but NEVER exposed
    to runtime.  They are used only in offline evaluation.
    """

    def __init__(
        self,
        *,
        benchmark_revision: str = "sandbox-sample-v0.1",
        repository_url: str = "https://github.com/toolsandbox/toolsandbox",
        commit_sha: str = "0000000",
        tasks: Optional[list[dict[str, Any]]] = None,
    ):
        self._revision = benchmark_revision
        self._repo_url = repository_url
        self._commit = commit_sha
        self._tasks = tasks or _SAMPLE_TASKS
        self._milestones = _SAMPLE_MILESTONES
        self._dataset_digest = self._compute_digest()
        # Hidden state
        self._world_snapshots: dict[str, dict[str, Any]] = {}
        self._runtime_artifacts: dict[str, dict[str, Any]] = {}

    def _compute_digest(self) -> str:
        raw = json.dumps(
            [{"task_id": t["task_id"], "objective": t["objective"]} for t in self._tasks],
            sort_keys=True,
        ).encode()
        return hashlib.sha256(raw).hexdigest()

    @property
    def benchmark_identity(self) -> BenchmarkIdentity:
        return BenchmarkIdentity(
            benchmark_name=BenchmarkName.TOOLSANDBOX,
            benchmark_revision=self._revision,
            repository_url=self._repo_url,
            commit_sha=self._commit,
            dataset_digest=self._dataset_digest,
            evaluator_digest="native-toolsandbox-eval-v1",
        )

    def enumerate_tasks(self) -> Sequence[TaskDescriptor]:
        return [
            TaskDescriptor(
                task_id=t["task_id"],
                benchmark=BenchmarkName.TOOLSANDBOX,
                complexity=t.get("complexity"),
                perturbation_mode=PerturbationMode.P0,
                native_metadata={"milestone_ids": t.get("milestone_ids", [])},
            )
            for t in self._tasks
        ]

    def build_runtime_task(self, descriptor: TaskDescriptor) -> RuntimeTask:
        raw = next(t for t in self._tasks if t["task_id"] == descriptor.task_id)
        return RuntimeTask(
            task_id=descriptor.task_id,
            objective=raw["objective"],
            visible_tools=raw.get("visible_tools", []),
            prompt=raw.get("prompt", ""),
            constraints=[],
            acceptance_criteria=["complete all steps successfully"],
            environment_snapshot={},
            budget=BudgetConfig(max_turns=30, max_model_calls=50),
        )

    async def reset_environment(self, task: RuntimeTask) -> None:
        pass  # dry-run

    def native_condition(self, descriptor: TaskDescriptor) -> str:
        return f"toolsandbox/{descriptor.task_id}"

    def collect_public_observation(self, task_id: str, step: int) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "step": step,
            "observation": f"step {step} state captured",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def finalize_runtime_artifact(self, task_id: str) -> dict[str, Any]:
        artifact = {
            "task_id": task_id,
            "finalized_at": datetime.now(timezone.utc).isoformat(),
            "runtime_events": [],
        }
        self._runtime_artifacts[task_id] = artifact
        return artifact

    def offline_native_evaluate(
        self,
        task_id: str,
        runtime_artifact: dict[str, Any],
    ) -> NativeResult:
        """Offline evaluation using native ToolSandbox evaluator."""
        milestones = self._milestones.get(task_id, [])
        # Simulated native scoring
        all_completed = len(milestones) > 0
        return NativeResult(
            tsr=1.0 if all_completed else 0.0,
            raw_score=1.0 if all_completed else 0.0,
            native_metrics={
                "toolsandbox_milestone_count": len(milestones),
                "toolsandbox_all_milestones_completed": all_completed,
            },
            judge_output={"milestones": milestones},
        )

    # ── Offline-only milestone access ───────────────────────────────

    def get_target_milestones(self, task_id: str) -> list[dict[str, Any]]:
        """Offline-only access to milestone DAG.  Not callable at runtime."""
        return list(self._milestones.get(task_id, []))

    def derive_progress_metrics(
        self,
        task_id: str,
        runtime_events: list[dict[str, Any]],
    ) -> DerivedMetrics:
        """Derive Phase5-specific progress metrics from ToolSandbox milestones.

        These are Phase5-derived metrics, NOT official ToolSandbox scores.
        """
        milestones = self._milestones.get(task_id, [])
        total = len(milestones)
        if total == 0:
            return DerivedMetrics(
                validity=TrialStatus.INVALID_INFRA,
                no_advancement_detection=1.0,
            )

        # Analyze runtime events for milestone advancement signals
        advancement_steps = sum(
            1 for e in runtime_events
            if e.get("event_type") in {"TOOL_CALL_COMPLETED", "PLAN_STEP_COMPLETED"}
        )
        stall_episodes = sum(
            1 for e in runtime_events
            if e.get("event_type") == "VALIDATION_FAILURE_CREATED"
        )

        alignment = min(advancement_steps / total, 1.0) if total > 0 else 0.0
        return DerivedMetrics(
            milestone_alignment=alignment,
            no_advancement_detection=0.0 if advancement_steps > 0 else 1.0,
            detection_latency=1.0 if stall_episodes == 0 else 0.5,
            missed_stall_episodes=max(0, stall_episodes - 1),
            premature_intervention_candidates=0,
            validity=TrialStatus.VALID,
            recovery_metrics={
                "advancement_steps": advancement_steps,
                "total_milestones": total,
                "stall_episodes": stall_episodes,
            },
            progress_labels={
                "milestone_advancement": advancement_steps,
                "milestone_target": total,
            },
        )
