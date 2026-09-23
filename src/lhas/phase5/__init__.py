"""Phase 5 — Falsification Harness for Odys.

Consumes existing public benchmark tasks, perturbations, evaluators,
and native metrics.  Never invents new primary fault semantics.

Three-plane architecture:
  - runtime_envelope:  ToolMazeRuntimeEnvelope (what control arms see)
  - runtime_backend:   ToolMazeRuntimeBackend  (execution + perturbation)
  - offline_evaluator: ToolMazeOfflineEvaluator (judge + metrics)
"""

__all__: list[str] = [
    "runtime_envelope",
    "runtime_backend",
    "offline_evaluator",
]
