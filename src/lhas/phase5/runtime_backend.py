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

  • ``run_tool(name, arguments)`` — executes a single tool call
    through the official ``ExecutionEngine`` (which may inject a
    perturbation).  Returns the (possibly perturbed) result.

  • ``agent_trace`` — the accumulated trace of all tool calls and
    results, available **only** after ``finalize()`` for the offline
    evaluator.

Design contract
───────────────
* The backend owns the full task JSON — **never** the envelope.
* The envelope owns the tool skeletons — **never** the backend.
* The agent trace is sealed after ``finalize()`` and handed to the
  ``ToolMazeOfflineEvaluator``.
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
        envelope = backend.envelope          # frozen, oracle-free
        result   = backend.run_tool(name, args)  # may be perturbed
        ...
        trace    = backend.finalize()        # sealed for offline eval
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
            internally for perturbation injection but never forwarded
            to the envelope.
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

        # ── Lazy-init ExecutionEngine (needs agent injection) ──
        self._engine: Any = None
        self._engine_initialized = False

        # ── Agent trace accumulator ──
        self._trace_entries: list[dict[str, Any]] = []
        self._tool_call_count = 0
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

    @property
    def tool_call_count(self) -> int:
        return self._tool_call_count

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

    # ── ExecutionEngine initialization ────────────────────────────────

    def _ensure_engine(self) -> Any:
        """Lazy-initialize the official ExecutionEngine.

        The engine is not created at __init__ time because it requires
        a concrete ``BaseAgent`` instance (which the control arm
        provides).  However, we can still prepare the engine without an
        agent for the *tool interception* path.

        For the three-plane architecture, the backend provides a
        ``run_tool`` API that bypasses the agent loop entirely — the
        control arm's agent decides *what* to call, and the backend
        decides *how* to execute it (with perturbation injection).
        """
        if self._engine is not None:
            return self._engine

        repo_str = str(self._repo_dir)
        if repo_str not in sys.path:
            sys.path.insert(0, repo_str)

        try:
            from toolmaze.core import ToolExecutor
            from tools.loader import ToolLoader

            tools_dir = self._repo_dir / "tools"
            plugins_dir = str(tools_dir / "plugins")
            definitions_dir = str(tools_dir / "definitions")

            loader = ToolLoader(definitions_dir)
            executor = ToolExecutor(
                plugins_dir=plugins_dir,
                definitions_dir=definitions_dir,
                loader=loader,
            )

            self._engine = _PerturbationEngine(
                task_json=self._task_json,
                executor=executor,
                loader=loader,
                repo_dir=self._repo_dir,
            )
            self._engine_initialized = True
            return self._engine

        finally:
            if repo_str in sys.path:
                sys.path.remove(repo_str)

    # ── Tool execution API ────────────────────────────────────────────

    def run_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a single tool call with perturbation interception.

        This is the **only** way control arms interact with the
        benchmark environment.  The engine intercepts the call and
        may inject a perturbation according to the task's perturbation
        mode and point.

        Parameters
        ----------
        tool_name : str
            Name of the tool to call.
        arguments : dict
            Tool arguments.

        Returns
        -------
        dict
            Tool result (possibly perturbed).
        """
        if self._finalized:
            raise RuntimeError("Backend is finalized — no more tool calls.")

        engine = self._ensure_engine()
        result, perturbation_status = engine.execute_tool(tool_name, arguments)

        self._tool_call_count += 1
        self._trace_entries.append({
            "step": self._tool_call_count,
            "tool_name": tool_name,
            "arguments": arguments,
            "output": result,
            "perturbation_status": perturbation_status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        return result

    # ── Trace finalization ────────────────────────────────────────────

    def finalize(self) -> dict[str, Any]:
        """Seal the agent trace for offline evaluation.

        Returns the full runtime artifact including the accumulated
        trace.  After this call, ``run_tool`` is disabled.

        The returned artifact is handed to
        ``ToolMazeOfflineEvaluator.evaluate()``.
        """
        self._finalized = True
        return {
            "task_id": self.task_id,
            "finalized_at": datetime.now(timezone.utc).isoformat(),
            "tool_calls": list(self._trace_entries),
            "total_tool_calls": self._tool_call_count,
        }


# ── Internal perturbation engine ──────────────────────────────────────

class _PerturbationEngine:
    """Thin wrapper around ToolExecutor + perturbation map.

    Extracts the perturbation logic from the official
    ``ExecutionEngine.run()`` loop, exposing a single-call
    ``execute_tool(name, args)`` API instead of the full agent loop.
    """

    def __init__(
        self,
        *,
        task_json: dict[str, Any],
        executor: Any,
        loader: Any,
        repo_dir: Path,
    ):
        self._task_json = task_json
        self._executor = executor
        self._loader = loader

        # Extract perturbation metadata from task
        self._mode = task_json.get("perturbation_mode", "P0")
        self._complexity = task_json.get("complexity", "C1")
        self._perturbation_point = task_json.get("perturbation_point", 0)

        # Build perturbation map from execution_trace
        self._perturbation_map = self._build_perturbation_map(task_json)

        # C2/C3/C4 multi-path support
        if self._complexity in ("C2", "C3", "C4"):
            self._alternative_tools = self._get_alt_tools(task_json)
            self._c2_activated = False
            self._c2_path_maps = self._build_c2_path_maps(task_json)
        else:
            self._alternative_tools = set()
            self._c2_activated = False
            self._c2_path_maps = {}

    def _build_perturbation_map(self, task_json: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Build perturbation map from execution_trace."""
        pmap: dict[str, dict[str, Any]] = {}
        trace = task_json.get("execution_trace", [])
        for step in trace:
            if step.get("is_perturbed", False):
                tool_name = step.get("tool_name")
                if tool_name:
                    pmap[tool_name] = {
                        "step": step.get("step"),
                        "output": step.get("output", {}),
                        "status": step.get("status", "error"),
                    }
        return pmap

    def _get_alt_tools(self, task_json: dict[str, Any]) -> set:
        """Get alternative tools for C2/C3/C4."""
        valid_paths = task_json.get("valid_paths", [])
        if len(valid_paths) < 2:
            return set(task_json.get("alternative_tools", []))

        tool_sets = []
        for vp in valid_paths:
            tools = vp.get("tools", [])
            if isinstance(tools, list):
                tool_sets.append(set(t for t in tools if t))
        if len(tool_sets) < 2:
            return set()

        union_tools = set().union(*tool_sets)
        shared_tools = set.intersection(*tool_sets)
        return union_tools - shared_tools

    def _build_c2_path_maps(self, task_json: dict[str, Any]) -> dict[str, dict]:
        """Build per-path perturbation maps for C2/C3."""
        path_maps: dict[str, dict] = {}
        for vp in task_json.get("valid_paths", []):
            perturb_info: dict[str, dict[str, Any]] = {}
            alt_tool_name = None
            for step in vp.get("execution_trace", []):
                if step.get("is_perturbed", False):
                    tool_name = step.get("tool_name", "")
                    if tool_name in self._alternative_tools:
                        alt_tool_name = tool_name
                    perturb_info[tool_name] = {
                        "output": step.get("output", {}),
                        "status": step.get("status", "error"),
                    }
            if alt_tool_name:
                path_maps[alt_tool_name] = perturb_info
        return path_maps

    def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        """Execute a single tool with perturbation interception.

        Returns (result, perturbation_status).
        """
        # C2 first-touch activation
        if self._complexity in ("C2", "C3") and not self._c2_activated:
            if tool_name in self._c2_path_maps:
                self._c2_activated = True
                self._perturbation_map = self._c2_path_maps[tool_name]

        # Check if this tool should be perturbed
        should_perturb = (
            tool_name in self._perturbation_map
            and self._should_perturb(tool_name)
        )

        if should_perturb:
            # Inject the perturbed output
            perturb_data = self._perturbation_map[tool_name]
            return perturb_data["output"], "perturbed"

        # Execute normally via the ToolExecutor
        try:
            context = type("Ctx", (), {
                "user_input": {},
                "history": [],
                "record": lambda *a, **kw: None,
                "find_tool_output": lambda *a, **kw: None,
            })()
            result = self._executor.execute(tool_name, arguments, context)
            return result, "clean"
        except Exception as e:
            return {"error": str(e)}, "error"

    def _should_perturb(self, tool_name: str) -> bool:
        """Determine if a tool call should be perturbed.

        For P0: never perturb.
        For P1/P3 (transient): perturb on first call only.
        For P2/P4 (permanent): perturb on every call.
        """
        if self._mode == "P0":
            return False
        if self._mode in ("P1", "P3"):
            # Transient: perturb only once
            # We track via the perturbation map entry
            data = self._perturbation_map.get(tool_name)
            if data and not data.get("_consumed", False):
                data["_consumed"] = True
                return True
            return False
        # P2/P4: always perturb
        return True
