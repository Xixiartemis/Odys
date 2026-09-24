"""Mock MetricsCalculator for synthetic fixture tests."""

from __future__ import annotations
from typing import Any


class MetricsCalculator:
    """Mock metrics calculator."""

    def __init__(self) -> None:
        self._results: list[dict[str, Any]] = []

    def add_result(
        self,
        task: dict[str, Any],
        inference_data: dict[str, Any],
        judge_result: dict[str, Any],
    ) -> None:
        self._results.append({
            "task_id": task.get("task_id"),
            "passed": judge_result.get("pass", False),
        })

    def generate_report(self) -> dict[str, Any]:
        total = len(self._results)
        passed = sum(1 for r in self._results if r["passed"])
        return {
            "total_tasks": total,
            "passed_tasks": passed,
            "tsr": passed / total if total > 0 else 0.0,
            "results": self._results,
        }
