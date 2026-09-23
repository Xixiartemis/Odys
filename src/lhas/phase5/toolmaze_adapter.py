"""ToolMaze primary benchmark adapter — official data integration.

Loads tasks from the official ToolMaze HuggingFace dataset (frozen locally).
FaultSource.BENCHMARK_NATIVE only — Odys must not alter P1/P2/P3/P4 semantics.

**Three-plane architecture** (Phase 5):

  1. ``ToolMazeRuntimeEnvelope`` (runtime_envelope.py) — what control arms
     see: task_id, objective, tool skeletons from YAML, prompt, env snapshot,
     budget.  Never contains hidden fields.

  2. ``ToolMazeRuntimeBackend`` (runtime_backend.py) — wraps the official
     ExecutionEngine.  Holds the full task JSON for perturbation injection
     but never exposes hidden fields through its public API.

  3. ``ToolMazeOfflineEvaluator`` (offline_evaluator.py) — runs after
     runtime termination.  Owns hidden task state, official JudgeSystem,
     and MetricsCalculator.

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

    **Three-plane architecture**: Tools are loaded from official YAML
    definitions (``tools/definitions/*.yaml``) via ``ToolLoader``, NOT
    from ``execution_trace``.  Offline evaluation delegates to
    ``ToolMazeOfflineEvaluator`` which owns the hidden task state and
    the official ``JudgeSystem`` + ``MetricsCalculator``.
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

        Tools are loaded from official YAML definitions via ToolLoader.
        This replaces the previous oracle-leaking implementation that
        read tool names from ``execution_trace`` and
        ``alternative_tools`` (hidden fields).
        """
        import sys as _sys
        repo_str = str(self._repo_dir)
        if repo_str not in _sys.path:
            _sys.path.insert(0, repo_str)
        try:
            from tools.loader import ToolLoader

            definitions_dir = self._repo_dir / "tools" / "definitions"
            loader = ToolLoader(str(definitions_dir))

            # Collect all tool names that appear in this task's
            # execution_trace (name only — no outputs or arguments).
            trace_tool_names: set[str] = set()
            for step in task.get("execution_trace", []):
                tn = step.get("tool_name")
                if tn:
                    trace_tool_names.add(tn)

            # Build skeletons from YAML definitions
            tools: list[dict[str, Any]] = []
            for tool_name in sorted(trace_tool_names):
                tool_def = loader.get_tool_by_name(tool_name)
                if tool_def:
                    paradigms = tool_def.get("paradigms", {})
                    fc = paradigms.get("function_call", {})
                    spec = fc.get("spec", {})
                    tools.append({
                        "name": tool_name,
                        "description": tool_def.get("description", ""),
                        "parameters": spec.get("parameters", {}),
                        "category": tool_def.get("category", ""),
                        "domain": tool_def.get("domain", ""),
                    })
                else:
                    # Fallback for tools not in YAML definitions
                    tools.append({
                        "name": tool_name,
                        "description": f"Tool: {tool_name}",
                        "parameters": {},
                    })

            return tools
        finally:
            if repo_str in _sys.path:
                _sys.path.remove(repo_str)

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

        This runs ONLY after runtime termination.  It delegates to
        ``ToolMazeOfflineEvaluator`` which owns the hidden task state
        and the official ``JudgeSystem`` + ``MetricsCalculator``.
        """
        from .offline_evaluator import ToolMazeOfflineEvaluator

        raw = self._find_task(task_id)
        if raw is None:
            return NativeResult(
                tsr=0.0, prr=0.0, rc=1.0,
                native_metrics={"error": f"task not found: {task_id}"},
            )

        evaluator = ToolMazeOfflineEvaluator(repo_dir=self._repo_dir)
        evaluator.register_task(raw)
        return evaluator.evaluate(task_id, runtime_artifact)

    # ── Three-plane architecture notes ────────────────────────────────
    #
    # Hidden-state accessors (_get_raw_task, _get_execution_trace,
    # _get_expected_result), metric computation helpers (_compute_prr,
    # _compute_rc), and judge delegation (_run_official_judge) have been
    # REMOVED.  These responsibilities now live in:
    #
    #   • ToolMazeRuntimeEnvelope  — runtime-visible data plane
    #   • ToolMazeRuntimeBackend   — execution + perturbation plane
    #   • ToolMazeOfflineEvaluator — evaluation + metrics plane
