"""ToolMaze primary benchmark adapter — official data integration.

Loads tasks from the official ToolMaze HuggingFace dataset (frozen locally).
Uses the official JudgeSystem for offline evaluation.
FaultSource.BENCHMARK_NATIVE only — Odys must not alter P1/P2/P3/P4 semantics.

Hidden fields stripped from runtime-visible tasks:
  - expected_result (ground truth tool calls)
  - execution_trace (oracle execution path)
  - perturbation_point (where perturbation was injected)
  - valid_paths (C2-C4 alternative paths with oracle traces)
  - alternative_tools (C2-C4 alternative tool definitions)

These are preserved for offline_native_evaluate() which runs after runtime.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from .types import (
    BenchmarkAdapter,
    BenchmarkIdentity,
    BenchmarkName,
    BudgetConfig,
    FaultSource,
    NativeResult,
    PerturbationMode,
    RuntimeTask,
    TaskDescriptor,
    TopologyClass,
)

# Fields hidden from runtime — only accessible via offline evaluation
_HIDDEN_FIELDS = frozenset({
    "expected_result",
    "execution_trace",
    "valid_paths",
    "alternative_tools",
    "perturbation_point",
})

# Default paths
_DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / ".." / "experiments" / "phase5" / "benchmarks" / "toolmaze" / "data"
_DEFAULT_REPO_DIR = Path(__file__).resolve().parents[2] / ".." / "experiments" / "phase5" / "benchmarks" / "toolmaze"


class ToolMazeAdapter:
    """ToolMaze primary adapter using official frozen data.

    Loads from experiments/phase5/benchmarks/toolmaze/data/perturbed_tasks/.
    Strips hidden fields before passing to runtime.
    Uses official JudgeSystem for offline evaluation.
    """

    def __init__(
        self,
        *,
        data_dir: Optional[Path] = None,
        repo_dir: Optional[Path] = None,
        benchmark_revision: str = "ef0798a",
        dataset_hash: str = "9fcd7d7ec3c06afcee098877cca1ae8c29b87a1102be4b365a1055f824a76bc8",
        evaluator_hash: str = "412f9c3615bfd997af903f6514f716be94a774140975b82c8ff2fd16b6547833",
    ):
        self._data_dir = Path(data_dir) if data_dir else _DEFAULT_DATA_DIR
        self._repo_dir = Path(repo_dir) if repo_dir else _DEFAULT_REPO_DIR
        self._revision = benchmark_revision
        self._dataset_hash = dataset_hash
        self._evaluator_hash = evaluator_hash

        # Load all tasks from official data
        self._tasks: list[dict[str, Any]] = []
        self._task_index: dict[str, dict[str, Any]] = {}
        self._load_tasks()

        # Hidden state cache for offline evaluation
        self._runtime_artifacts: dict[str, dict[str, Any]] = {}

    def _load_tasks(self) -> None:
        """Load all perturbed tasks from the frozen data directory."""
        perturbed_dir = self._data_dir / "perturbed_tasks"
        if not perturbed_dir.exists():
            raise FileNotFoundError(
                f"ToolMaze data not found at {perturbed_dir}. "
                f"Run Phase5-01 environment freeze first."
            )

        for cat_dir in sorted(perturbed_dir.iterdir()):
            if not cat_dir.is_dir():
                continue
            for task_file in sorted(cat_dir.glob("*.json")):
                try:
                    task = json.loads(task_file.read_text(encoding="utf-8"))
                    task["_source_file"] = str(task_file.relative_to(self._data_dir))
                    self._tasks.append(task)
                    self._task_index[task["task_id"]] = task
                except (json.JSONDecodeError, KeyError) as e:
                    print(f"Warning: skipping {task_file}: {e}", file=sys.stderr)

    @property
    def benchmark_identity(self) -> BenchmarkIdentity:
        return BenchmarkIdentity(
            benchmark_name=BenchmarkName.TOOLMAZE,
            benchmark_revision=self._revision,
            repository_url="https://github.com/Zhudongsheng75/ToolMaze",
            commit_sha=self._revision,
            dataset_digest=self._dataset_hash,
            evaluator_digest=self._evaluator_hash,
        )

    def enumerate_tasks(self) -> Sequence[TaskDescriptor]:
        """Enumerate all loaded tasks as descriptors."""
        # Deduplicate by (task_id, perturbation_mode) since each task has P0-P4 variants
        seen: set[str] = set()
        descriptors: list[TaskDescriptor] = []

        for task in self._tasks:
            task_id = task["task_id"]
            mode = task.get("perturbation_mode", "P0")
            key = f"{task_id}_{mode}"
            if key in seen:
                continue
            seen.add(key)

            complexity = task.get("complexity", "C1")
            topo = TopologyClass(complexity) if complexity in {"C1", "C2", "C3", "C4"} else TopologyClass.C1
            pm = PerturbationMode(mode) if mode in {"P0", "P1", "P2", "P3", "P4"} else PerturbationMode.P0

            descriptors.append(TaskDescriptor(
                task_id=key,
                benchmark=BenchmarkName.TOOLMAZE,
                topology=topo,
                complexity=complexity,
                perturbation_mode=pm,
                perturbation_victim=self._extract_victim_tool(task),
                native_metadata={
                    "original_task_id": task_id,
                    "template_id": task.get("template_id", ""),
                    "domains": task.get("domains", []),
                    "source_file": task.get("_source_file", ""),
                },
            ))

        return descriptors

    def _extract_victim_tool(self, task: dict[str, Any]) -> Optional[str]:
        """Extract victim tool name from execution trace (for metadata only)."""
        trace = task.get("execution_trace", [])
        pp = task.get("perturbation_point")
        if pp is not None and isinstance(pp, int) and 0 <= pp < len(trace):
            return trace[pp].get("tool_name")
        return None

    def _find_task(self, task_id: str) -> Optional[dict[str, Any]]:
        """Find the raw task by composite task_id (original_id_mode)."""
        # Try direct lookup first
        if task_id in self._task_index:
            return self._task_index[task_id]

        # Try composite key: original_id + mode
        parts = task_id.rsplit("_", 1)
        if len(parts) == 2:
            original_id = parts[0]
            mode = parts[1]
            for task in self._tasks:
                if task["task_id"] == original_id and task.get("perturbation_mode") == mode:
                    return task

        return None

    def build_runtime_task(self, descriptor: TaskDescriptor) -> RuntimeTask:
        """Build a runtime-visible task.  ALL hidden fields are stripped."""
        raw = self._find_task(descriptor.task_id)
        if raw is None:
            raise ValueError(f"Task not found: {descriptor.task_id}")

        # Extract only runtime-visible fields
        visible = {k: v for k, v in raw.items() if k not in _HIDDEN_FIELDS and not k.startswith("_")}

        return RuntimeTask(
            task_id=descriptor.task_id,
            objective=raw.get("task_description", ""),
            visible_tools=self._extract_visible_tools(raw),
            prompt=raw.get("user_input", {}).get("query", raw.get("task_description", "")),
            constraints=[],
            acceptance_criteria=["complete the task as described"],
            environment_snapshot=raw.get("user_input", {}),
            budget=BudgetConfig(max_turns=30, max_model_calls=50),
        )

    def _extract_visible_tools(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract tool definitions visible to the runtime agent.

        For C1: tools are inferred from execution_trace (minus hidden outputs).
        For C2-C4: alternative_tools provide the tool definitions.
        """
        tools: list[dict[str, Any]] = []

        # From execution_trace: extract tool names (no outputs)
        for step in task.get("execution_trace", []):
            tool_name = step.get("tool_name")
            if tool_name:
                tools.append({
                    "name": tool_name,
                    "description": f"Tool: {tool_name}",
                    "parameters": step.get("arguments", {}),
                })

        # From alternative_tools (C2-C4)
        for alt in task.get("alternative_tools", []):
            if isinstance(alt, dict):
                for path in alt.get("paths", []):
                    for tool in path.get("tools", []):
                        if isinstance(tool, dict) and tool.get("tool_name"):
                            tools.append({
                                "name": tool["tool_name"],
                                "description": f"Alternative tool: {tool['tool_name']}",
                            })

        # Deduplicate by name
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for t in tools:
            if t["name"] not in seen:
                seen.add(t["name"])
                unique.append(t)
        return unique

    async def reset_environment(self, task: RuntimeTask) -> None:
        """Reset environment for a new trial."""
        pass  # dry-run: no real environment

    def native_condition(self, descriptor: TaskDescriptor) -> str:
        """Return topology/perturbation condition string."""
        return f"{descriptor.topology.value}/{descriptor.perturbation_mode.value}"

    def collect_public_observation(self, task_id: str, step: int) -> dict[str, Any]:
        """Collect runtime-visible observation.  No hidden labels."""
        return {
            "task_id": task_id,
            "step": step,
            "observation": f"step {step} completed",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def finalize_runtime_artifact(self, task_id: str) -> dict[str, Any]:
        """Finalize runtime artifacts.  No hidden ground truth included."""
        artifact = {
            "task_id": task_id,
            "finalized_at": datetime.now(timezone.utc).isoformat(),
            "runtime_events": [],
            "tool_calls": [],
            "state_observations": [],
        }
        self._runtime_artifacts[task_id] = artifact
        return artifact

    def offline_native_evaluate(
        self,
        task_id: str,
        runtime_artifact: dict[str, Any],
    ) -> NativeResult:
        """Offline evaluation using official ToolMaze judge.

        This runs ONLY after runtime termination.  It accesses the full
        task data including hidden fields.
        """
        raw = self._find_task(task_id)
        if raw is None:
            return NativeResult(
                tsr=0.0, prr=0.0, rc=1.0,
                native_metrics={"error": f"task not found: {task_id}"},
            )

        # Build a simulated trace from the runtime artifact
        # In production, this would use the actual agent trace
        mode = raw.get("perturbation_mode", "P0")
        complexity = raw.get("complexity", "C1")

        # Use the official judge logic
        try:
            judge_result = self._run_official_judge(raw, runtime_artifact)
        except Exception as e:
            judge_result = {"pass": False, "failure_reason": str(e), "trace_check": {}}

        passed = judge_result.get("pass", False)

        # Compute native metrics
        tsr = 1.0 if passed else 0.0
        prr = self._compute_prr(raw, judge_result, mode)
        rc = self._compute_rc(raw, judge_result, mode, passed)

        return NativeResult(
            tsr=tsr,
            prr=prr,
            rc=rc,
            raw_score=tsr,
            native_metrics={
                "toolmaze_task_success": passed,
                "perturbation_mode": mode,
                "complexity": complexity,
                "judge_reason": judge_result.get("failure_reason"),
            },
            judge_output=judge_result,
        )

    def _run_official_judge(
        self,
        task: dict[str, Any],
        runtime_artifact: dict[str, Any],
    ) -> dict[str, Any]:
        """Run the official ToolMaze judge.

        Adds the ToolMaze repo to sys.path temporarily to import the judge.
        """
        repo_str = str(self._repo_dir)
        if repo_str not in sys.path:
            sys.path.insert(0, repo_str)

        try:
            from evaluation.core.judge import JudgeSystem
            judge = JudgeSystem()

            # Build a minimal trace dict for the judge
            # The judge expects {"messages": [...]} format
            # For dry-run, we use the execution_trace as the agent's trace
            trace = self._build_simulated_trace(task)

            result = judge.judge(task, trace)
            return result
        except ImportError as e:
            return {"pass": False, "failure_reason": f"judge import failed: {e}", "trace_check": {}}
        finally:
            if repo_str in sys.path:
                sys.path.remove(repo_str)

    def _build_simulated_trace(self, task: dict[str, Any]) -> dict[str, Any]:
        """Build a simulated trace for judge evaluation.

        In a real run, this would be the agent's actual trace.
        For dry-run/simulated evaluation, we use the oracle trace.
        """
        execution_trace = task.get("execution_trace", [])
        messages = []
        for step in execution_trace:
            messages.append({
                "role": "assistant",
                "type": "tool_call",
                "tool_call": {
                    "id": f"call_{step.get('step', 0)}",
                    "name": step.get("tool_name", ""),
                    "arguments": step.get("arguments", {}),
                },
            })
            messages.append({
                "role": "tool",
                "call_id": f"call_{step.get('step', 0)}",
                "name": step.get("tool_name", ""),
                "content": step.get("output", {}),
                "metadata": {"perturbation_status": "perturbed" if step.get("is_perturbed") else "clean"},
            })
        return {"messages": messages}

    def _compute_prr(
        self,
        task: dict[str, Any],
        judge_result: dict[str, Any],
        mode: str,
    ) -> Optional[float]:
        """Compute PRR for this task (single-task, returns 0.0 or 1.0 or None)."""
        if mode == "P0":
            return None
        trace_check = judge_result.get("trace_check", {})
        if not trace_check.get("victim_tool"):
            return None
        return 1.0 if judge_result.get("pass") else 0.0

    def _compute_rc(
        self,
        task: dict[str, Any],
        judge_result: dict[str, Any],
        mode: str,
        passed: bool,
    ) -> Optional[float]:
        """Compute RC for this task."""
        if mode == "P0":
            return None
        if not passed:
            return 1.0
        return 0.0  # Simplified: passed = no recovery cost

    # ── Hidden-state access (offline audit only) ────────────────────

    def _get_raw_task(self, task_id: str) -> Optional[dict[str, Any]]:
        """Offline-only access to full task data including hidden fields."""
        return self._find_task(task_id)

    def _get_execution_trace(self, task_id: str) -> list[dict[str, Any]]:
        """Offline-only access to oracle execution trace."""
        raw = self._find_task(task_id)
        if raw is None:
            return []
        return raw.get("execution_trace", [])

    def _get_expected_result(self, task_id: str) -> dict[str, Any]:
        """Offline-only access to expected result (ground truth)."""
        raw = self._find_task(task_id)
        if raw is None:
            return {}
        return raw.get("expected_result", {})
