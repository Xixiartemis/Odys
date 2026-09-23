"""Runtime Backend — wraps the official ExecutionEngine.

``ToolMazeRuntimeBackend`` is the *execution plane*.  It holds the full
raw task JSON (including hidden fields) because the official
``ExecutionEngine`` needs ``execution_trace``, ``perturbation_point``,
and ``valid_paths`` to inject perturbations.

However, the backend **never** exposes those hidden fields to the
runtime plane.  Its public API is:

  • ``envelope`` — a frozen ``ToolMazeRuntimeEnvelope`` that control
    arms receive.  Built from YAML tool skeletons, not from
    ``execution_trace``.

  • ``execute(driver, max_rounds)`` — runs a full agent loop through
    the official ``ExecutionEngine`` with the given ``ModelDriver``.
    Returns the sealed runtime artifact for offline evaluation.

Design contract
───────────────
* The backend owns the full task JSON — **never** the envelope.
* The envelope owns the tool skeletons — **never** the backend.
* The agent trace is sealed after ``finalize()`` and handed to the
  ``ToolMazeOfflineEvaluator``.
* ``_PerturbationEngine`` has been removed — perturbation injection
  is handled entirely by the official ``ExecutionEngine``.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .runtime_envelope import (
    ToolMazeRuntimeEnvelope,
    ToolSkeleton,
    build_envelope_from_task,
)
from .types import BudgetConfig


# ── ToolMaze repo path resolution ────────────────────────────────────

_TOOLMAZE_REPO = Path(__file__).resolve().parents[2] / ".." / "experiments" / "phase5" / "benchmarks" / "toolmaze"


class ToolMazeRuntimeBackend:
    """Execution engine wrapper that enforces the sanitize boundary.

    Lifecycle::

        backend = ToolMazeRuntimeBackend(raw_task, repo_dir=...)
        envelope = backend.envelope                  # frozen, oracle-free
        artifact = backend.execute(model_driver)      # full agent loop
        ...
        trace    = backend.finalize()                # sealed for offline eval
    """

    def __init__(
        self,
        task_json: dict[str, Any],
        *,
        repo_dir: Optional[Path] = None,
        budget: Optional[BudgetConfig] = None,
    ):
        """Initialize the runtime backend.

        Parameters
        ----------
        task_json : dict
            The **full** raw task JSON.  Hidden fields are retained
            internally for the official ExecutionEngine but never
            forwarded to the envelope.
        repo_dir : Path, optional
            Path to the ToolMaze repo root.  Defaults to the standard
            location under ``experiments/phase5/benchmarks/toolmaze/``.
        budget : BudgetConfig, optional
            Override budget for the envelope.
        """
        self._task_json = task_json
        self._repo_dir = Path(repo_dir) if repo_dir else _TOOLMAZE_REPO

        # ── Load tool skeletons from YAML definitions ──
        self._tool_skeletons = self._load_tool_skeletons()
        skeleton_list = list(self._tool_skeletons.values())

        # ── Build the frozen envelope ──
        self._envelope = build_envelope_from_task(
            task_json,
            tool_skeletons=skeleton_list,
            budget=budget,
        )

        # ── Execution state ──
        self._trace_logger: Any = None
        self._token_usage: Optional[dict[str, int]] = None
        self._finalized = False

    # ── Public properties ─────────────────────────────────────────────

    @property
    def envelope(self) -> ToolMazeRuntimeEnvelope:
        """The frozen, oracle-free runtime envelope."""
        return self._envelope

    @property
    def task_id(self) -> str:
        return self._envelope.task_id

    @property
    def is_finalized(self) -> bool:
        return self._finalized

    # ── Tool skeleton loading ─────────────────────────────────────────

    def _load_tool_skeletons(self) -> dict[str, ToolSkeleton]:
        """Load tool definitions from official YAML files via ToolLoader.

        This is the *only* sanctioned source of tool metadata for the
        runtime plane.  We never extract tools from ``execution_trace``.
        """
        repo_str = str(self._repo_dir)
        if repo_str not in sys.path:
            sys.path.insert(0, repo_str)

        try:
            from tools.loader import ToolLoader

            definitions_dir = self._repo_dir / "tools" / "definitions"
            loader = ToolLoader(str(definitions_dir))

            skeletons: dict[str, ToolSkeleton] = {}
            for tool_name, tool_def in loader.tools_by_name.items():
                # Extract function_call spec if present
                paradigms = tool_def.get("paradigms", {})
                fc = paradigms.get("function_call", {})
                spec = fc.get("spec", {})

                skeleton = ToolSkeleton(
                    name=tool_name,
                    description=tool_def.get("description", ""),
                    category=tool_def.get("category", ""),
                    domain=tool_def.get("domain", ""),
                    parameters_schema=spec.get("parameters", {}),
                    substitutes=tool_def.get("substitutes", []),
                    paradigm_spec=spec if spec else None,
                )
                skeletons[tool_name] = skeleton

            return skeletons

        finally:
            if repo_str in sys.path:
                sys.path.remove(repo_str)

    # ── Execution via official ExecutionEngine ────────────────────────

    def execute(
        self,
        model_driver: Any,
        *,
        max_rounds: int = 15,
    ) -> dict[str, Any]:
        """Run a full agent loop through the official ExecutionEngine.

        Creates an ``OdysToolMazeAgentAdapter`` wrapping the given
        ``ModelDriver``, instantiates the official ``ExecutionEngine``,
        and calls ``engine.run(max_rounds)``.  The TraceLogger output
        is stored for later finalization.

        Parameters
        ----------
        model_driver : ModelDriver
            The model backend that produces actions.
        max_rounds : int
            Maximum reasoning rounds (default 15).

        Returns
        -------
        dict
            The sealed runtime artifact (same as ``finalize()`` output).
        """
        if self._finalized:
            raise RuntimeError("Backend is finalized — cannot execute again.")

        # Import the adapter
        from .agent_adapter import OdysToolMazeAgentAdapter

        # Create the adapter wrapping the model driver
        agent_adapter = OdysToolMazeAgentAdapter(model_driver)

        # Resolve tools_dir for the ExecutionEngine
        tools_dir = str(self._repo_dir / "tools")

        repo_str = str(self._repo_dir)
        if repo_str not in sys.path:
            sys.path.insert(0, repo_str)

        try:
            from evaluation.core.sandbox import ExecutionEngine

            # Instantiate the official ExecutionEngine
            engine = ExecutionEngine(
                task_json=self._task_json,
                agent=agent_adapter,
                tools_dir=tools_dir,
            )

            # Run the full agent loop
            trace_logger, token_usage = engine.run(max_rounds=max_rounds)

            self._trace_logger = trace_logger
            self._token_usage = token_usage

        finally:
            if repo_str in sys.path:
                sys.path.remove(repo_str)

        return self.finalize()

    # ── Trace finalization ────────────────────────────────────────────

    def finalize(self) -> dict[str, Any]:
        """Seal the agent trace for offline evaluation.

        Returns the full runtime artifact including the accumulated
        trace from the TraceLogger.  After this call, ``execute`` is
        disabled.

        The returned artifact is handed to
        ``ToolMazeOfflineEvaluator.evaluate()``.
        """
        self._finalized = True

        if self._trace_logger is not None:
            # Return the TraceLogger's native output format
            return self._trace_logger.to_dict(
                token_usage=self._token_usage,
            )

        # Fallback if execute() was never called
        return {
            "task_id": self.task_id,
            "finalized_at": datetime.now(timezone.utc).isoformat(),
            "tool_calls": [],
            "total_tool_calls": 0,
        }
