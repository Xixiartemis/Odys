"""Offline Evaluator — owns hidden task state and official judge.

``ToolMazeOfflineEvaluator`` is the *evaluation plane*.  It runs
**only after** the runtime phase has terminated (i.e., after
``ToolMazeRuntimeBackend.finalize()``).

It holds:
  • The full raw task JSON (including hidden fields like
    ``expected_result``, ``execution_trace``, ``perturbation_point``,
    ``valid_paths``, ``alternative_tools``).
  • The official ``JudgeSystem`` from ``evaluation/core/judge.py``.
  • The official ``MetricsCalculator`` from ``evaluation/core/metrics.py``.

Design contract
───────────────
* The evaluator **never** participates in the runtime phase.
* It receives only the *actual agent trace* produced by the backend.
* It uses the hidden fields solely to run the official judge and
  compute metrics.
* It returns a ``NativeResult`` (from ``.types``) that is immutable
  once produced.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

from .types import NativeResult


# ── ToolMaze repo path resolution ────────────────────────────────────

_TOOLMAZE_REPO = Path(__file__).resolve().parents[2] / ".." / "experiments" / "phase5" / "benchmarks" / "toolmaze"


class ToolMazeOfflineEvaluator:
    """Offline evaluation engine using official ToolMaze judge + metrics.

    Lifecycle::

        evaluator = ToolMazeOfflineEvaluator(repo_dir=...)
        # Register tasks (full JSON with hidden fields)
        evaluator.register_task(raw_task_json)
        # After runtime ends:
        result = evaluator.evaluate(task_id, runtime_artifact)
        # Aggregate:
        report = evaluator.generate_report()
    """

    def __init__(
        self,
        *,
        repo_dir: Optional[Path] = None,
    ):
        self._repo_dir = Path(repo_dir) if repo_dir else _TOOLMAZE_REPO

        # ── Official judge and metrics (lazy-loaded) ──
        self._judge: Any = None
        self._metrics: Any = None

        # ── Hidden task state ──
        self._tasks: dict[str, dict[str, Any]] = {}

        # ── Per-task evaluation results ──
        self._results: dict[str, NativeResult] = {}

        # ── Ensure official modules are importable ──
        self._repo_str = str(self._repo_dir)

    # ── Task registration ─────────────────────────────────────────────

    def register_task(self, task_json: dict[str, Any]) -> None:
        """Register a raw task JSON (with hidden fields) for evaluation.

        This must be called *before* ``evaluate()`` for each task.
        The evaluator stores the full task including hidden fields.
        """
        task_id = task_json.get("task_id", "")
        self._tasks[task_id] = task_json

    def register_tasks(self, tasks: list[dict[str, Any]]) -> None:
        """Register multiple tasks at once."""
        for task in tasks:
            self.register_task(task)

    # ── Official modules lazy loading ─────────────────────────────────

    def _ensure_judge(self) -> Any:
        """Lazy-load the official JudgeSystem."""
        if self._judge is not None:
            return self._judge

        if self._repo_str not in sys.path:
            sys.path.insert(0, self._repo_str)

        try:
            from evaluation.core.judge import JudgeSystem
            self._judge = JudgeSystem()
            return self._judge
        finally:
            if self._repo_str in sys.path:
                sys.path.remove(self._repo_str)

    def _ensure_metrics(self) -> Any:
        """Lazy-load the official MetricsCalculator."""
        if self._metrics is not None:
            return self._metrics

        if self._repo_str not in sys.path:
            sys.path.insert(0, self._repo_str)

        try:
            from evaluation.core.metrics import MetricsCalculator
            self._metrics = MetricsCalculator()
            return self._metrics
        finally:
            if self._repo_str in sys.path:
                sys.path.remove(self._repo_str)

    # ── Evaluation ────────────────────────────────────────────────────

    def evaluate(
        self,
        task_id: str,
        runtime_artifact: dict[str, Any],
    ) -> NativeResult:
        """Run official judge on the actual agent trace.

        This method runs **only after runtime termination**.  It
        accesses hidden fields from the registered task JSON to
        compute the official ToolMaze metrics.

        Parameters
        ----------
        task_id : str
            The task identifier.
        runtime_artifact : dict
            The sealed runtime artifact from
            ``ToolMazeRuntimeBackend.finalize()``.

        Returns
        -------
        NativeResult
            Official benchmark-native scoring result.
        """
        task_json = self._tasks.get(task_id)
        if task_json is None:
            return NativeResult(
                tsr=0.0,
                prr=0.0,
                rc=1.0,
                native_metrics={"error": f"task not registered: {task_id}"},
            )

        mode = task_json.get("perturbation_mode", "P0")
        complexity = task_json.get("complexity", "C1")

        # ── Build the trace dict expected by the judge ──
        trace = self._build_judge_trace(runtime_artifact)

        # ── Run the official judge ──
        try:
            judge = self._ensure_judge()
            judge_result = judge.judge(task_json, trace)
        except Exception as e:
            judge_result = {
                "pass": False,
                "failure_reason": f"judge error: {e}",
                "trace_check": {},
            }

        passed = judge_result.get("pass", False)

        # ── Compute native metrics ──
        tsr = 1.0 if passed else 0.0
        prr = self._compute_prr(task_json, judge_result, mode)
        rc = self._compute_rc(task_json, judge_result, mode, passed)

        result = NativeResult(
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

        self._results[task_id] = result

        # ── Feed to official MetricsCalculator ──
        try:
            metrics = self._ensure_metrics()
            inference_data = self._build_inference_data(runtime_artifact)
            metrics.add_result(task_json, inference_data, judge_result)
        except Exception:
            pass  # Metrics accumulation is best-effort

        return result

    # ── Trace conversion ──────────────────────────────────────────────

    def _build_judge_trace(self, runtime_artifact: dict[str, Any]) -> dict[str, Any]:
        """Convert the runtime artifact to the judge's expected format.

        The judge expects ``{"messages": [...]}`` with tool_call and
        tool result messages.
        """
        tool_calls = runtime_artifact.get("tool_calls", [])
        messages: list[dict[str, Any]] = []

        for entry in tool_calls:
            step = entry.get("step", 0)
            tool_name = entry.get("tool_name", "")
            arguments = entry.get("arguments", {})
            output = entry.get("output", {})
            perturbation_status = entry.get("perturbation_status", "clean")

            messages.append({
                "role": "assistant",
                "type": "tool_call",
                "tool_call": {
                    "id": f"call_{step}",
                    "name": tool_name,
                    "arguments": arguments,
                },
            })
            messages.append({
                "role": "tool",
                "call_id": f"call_{step}",
                "name": tool_name,
                "content": output,
                "metadata": {
                    "perturbation_status": perturbation_status,
                },
            })

        return {"messages": messages}

    def _build_inference_data(self, runtime_artifact: dict[str, Any]) -> dict[str, Any]:
        """Build inference data dict compatible with MetricsCalculator."""
        tool_calls = runtime_artifact.get("tool_calls", [])
        messages: list[dict[str, Any]] = []

        for entry in tool_calls:
            step = entry.get("step", 0)
            tool_name = entry.get("tool_name", "")
            arguments = entry.get("arguments", {})
            output = entry.get("output", {})
            perturbation_status = entry.get("perturbation_status", "clean")

            messages.append({
                "role": "assistant",
                "type": "tool_call",
                "tool_call": {
                    "id": f"call_{step}",
                    "name": tool_name,
                    "arguments": arguments,
                },
            })
            messages.append({
                "role": "tool",
                "call_id": f"call_{step}",
                "name": tool_name,
                "content": output,
                "metadata": {
                    "perturbation_status": perturbation_status,
                },
            })

        return {
            "messages": messages,
            "tokens": {"total_tokens": 0},
        }

    # ── Metric computation ────────────────────────────────────────────

    def _compute_prr(
        self,
        task: dict[str, Any],
        judge_result: dict[str, Any],
        mode: str,
    ) -> Optional[float]:
        """Compute PRR for this task (single-task, 0.0 or 1.0 or None)."""
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

    # ── Aggregation ───────────────────────────────────────────────────

    def get_result(self, task_id: str) -> Optional[NativeResult]:
        """Get the evaluation result for a specific task."""
        return self._results.get(task_id)

    def get_all_results(self) -> dict[str, NativeResult]:
        """Get all evaluation results."""
        return dict(self._results)

    def generate_report(self) -> dict[str, Any]:
        """Generate a full metrics report from all evaluated tasks.

        Uses the official MetricsCalculator's ``generate_report()``
        method.
        """
        try:
            metrics = self._ensure_metrics()
            return metrics.generate_report()
        except Exception as e:
            return {
                "error": f"report generation failed: {e}",
                "results_count": len(self._results),
                "results": {
                    tid: r.model_dump()
                    for tid, r in self._results.items()
                },
            }
