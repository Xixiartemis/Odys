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

  • ``execute(driver, strategy, max_rounds)`` — runs a full agent loop
    through the official ``ExecutionEngine`` with the given
    ``ModelDriver`` and ``PolicyStrategy``.
    Returns a result dict with:
      - official trace (from TraceLogger)
      - recovery decisions (from agent_adapter.get_recovery_decisions())
      - evidence events (from EvidenceLedger)
      - budget accounting (model calls tracked by BudgetedModelDriver)

Design contract
───────────────
* The backend owns the full task JSON — **never** the envelope.
* The envelope owns the tool skeletons — **never** the backend.
* The agent trace is sealed after ``finalize()`` and handed to the
  ``ToolMazeOfflineEvaluator``.
* ``_PerturbationEngine`` has been removed — perturbation injection
  is handled entirely by the official ``ExecutionEngine``.
* Strategy integration: when a ``PolicyStrategy`` is passed, the backend
  wires the strategy's observer + evidence into the adapter, and after
  each tool result consults the strategy for recovery decisions.
* Budget enforcement: model calls are tracked via a
  ``BudgetedModelDriver`` wrapper around the injected ModelDriver;
  exceeding the budget raises ``BudgetExhausted``.
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
from .types import BudgetConfig, PolicyExecutionError


# ── ToolMaze repo path resolution ────────────────────────────────────

_TOOLMAZE_REPO = Path(__file__).resolve().parents[2] / ".." / "experiments" / "phase5" / "benchmarks" / "toolmaze"


class ToolMazeRuntimeBackend:
    """Execution engine wrapper that enforces the sanitize boundary.

    Lifecycle::

        backend = ToolMazeRuntimeBackend(raw_task, repo_dir=...)
        envelope = backend.envelope                  # frozen, oracle-free
        artifact = backend.execute(model_driver, strategy=strategy)
        ...
        trace    = backend.finalize()                # sealed for offline eval

    When ``strategy`` is provided, the backend becomes the single
    integration point connecting:
      - Official ExecutionEngine (ToolMaze environment)
      - OdysToolMazeAgentAdapter (BaseAgent bridge)
      - PolicyStrategy (A0-A5 recovery policy)
      - Research substrate (EvidenceLedger, ShadowObserver)
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

        # ── Budget ──
        self._root_budget: Optional[BudgetConfig] = budget or self._envelope.budget

        # ── Strategy / substrate state ──
        self._evidence_events: list[dict[str, Any]] = []

        # ── Stored observer reference (created once per execute()) ──
        self._observer: Any = None

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
        strategy: Any = None,
        max_rounds: int = 15,
    ) -> dict[str, Any]:
        """Run a full agent loop through the official ExecutionEngine.

        Creates an ``OdysToolMazeAgentAdapter`` wrapping the given
        ``ModelDriver`` and optional ``PolicyStrategy``, instantiates
        the official ``ExecutionEngine``, and calls
        ``engine.run(max_rounds)``.  The TraceLogger output is stored
        for later finalization.

        When a ``strategy`` is provided, the adapter is wired with:
          - The strategy's shadow observer (via ``create_observer()``)
          - The substrate ``EvidenceLedger`` (for evidence collection)
          - Recovery decision recording (consulted after each tool result)

        Budget enforcement: the model driver is wrapped in a
        ``BudgetedModelDriver`` that counts calls and raises
        ``BudgetExhausted`` when the limit is reached.

        Parameters
        ----------
        model_driver : ModelDriver
            The model backend that produces actions.
        strategy : PolicyStrategy, optional
            The arm-specific recovery policy (A0-A5).  When provided,
            the backend becomes the unified execution + strategy path.
        max_rounds : int
            Maximum reasoning rounds (default 15).

        Returns
        -------
        dict
            The sealed runtime artifact enriched with:
            - ``recovery_decisions``: list of strategy decisions
            - ``evidence_events``: substrate evidence records
            - ``budget_accounting``: model calls used / remaining
            - ``strategy_config``: strategy configuration snapshot

        Raises
        ------
        BudgetExhausted
            If the root budget is exhausted before completion.
        PolicyExecutionError
            If strategy.configure() fails.
        RuntimeError
            If the backend is already finalized.
        """
        if self._finalized:
            raise RuntimeError("Backend is finalized — cannot execute again.")

        # Reset state for this execution
        self._evidence_events = []
        self._observer = None

        # ── Wrap model driver with BudgetedModelDriver ──────────────
        from .model_driver import BudgetedModelDriver, BudgetExhausted

        max_calls = (
            self._root_budget.max_model_calls
            if self._root_budget is not None
            else 50  # fallback default
        )
        budgeted_driver = BudgetedModelDriver(model_driver, max_model_calls=max_calls)

        # Create the benchmark-neutral agent core
        from .agent_core import Phase5AgentCore
        core = Phase5AgentCore(budgeted_driver, strategy=strategy)

        # ── Wire strategy + substrate handles ─────────────────────────
        strategy_config: dict[str, Any] = {}

        if strategy is not None:
            try:
                from .types import RuntimeTask, GenerationConfig
                rt = RuntimeTask(
                    task_id=self._envelope.task_id,
                    objective=self._envelope.objective,
                    visible_tools=[
                        {"name": s.name, "description": s.description}
                        for s in self._tool_skeletons.values()
                    ],
                    prompt=self._envelope.prompt,
                    budget=self._root_budget or BudgetConfig(max_turns=30, max_model_calls=50),
                )
                gen_cfg = GenerationConfig(
                    model_id=getattr(model_driver, '_model_id', 'unknown'),
                    provider=getattr(model_driver, '_provider', 'unknown'),
                )
                strategy_config = strategy.configure(task=rt, generation_config=gen_cfg)
            except PolicyExecutionError:
                raise
            except Exception as exc:
                raise PolicyExecutionError(
                    f"strategy.configure() failed: {exc}"
                ) from exc

            self._observer = strategy.create_observer()
            if self._observer is not None:
                core.set_shadow_observer(self._observer)

            try:
                from .substrate.evidence import EvidenceLedger
                ledger = EvidenceLedger(run_id=f"trial-{self.task_id}")
                core.set_evidence_ledger(ledger)
            except ImportError:
                ledger = None

        # Create the live ToolMaze adapter from the core
        from .agent_adapter import create_toolmaze_agent_adapter
        agent_adapter = create_toolmaze_agent_adapter(core)

        # ── Execute through official ExecutionEngine ──────────────────
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

            # Run the full agent loop — BudgetExhausted is raised by
            # BudgetedModelDriver.next_action() if the budget is hit
            try:
                trace_logger, token_usage = engine.run(max_rounds=max_rounds)
                self._trace_logger = trace_logger
                self._token_usage = token_usage
            except BudgetExhausted:
                # Capture partial state on budget exhaustion
                self._trace_logger = getattr(engine, '_trace_logger', None)
                self._token_usage = getattr(model_driver, 'get_token_usage', lambda: None)()

        finally:
            if repo_str in sys.path:
                sys.path.remove(repo_str)

        # ── Collect strategy recovery decisions from the core ─────────
        recovery_decisions = core.get_recovery_decisions()

        # ── Collect evidence from the stored observer ─────────────────
        # FIX: reuse stored self._observer, not strategy.create_observer() again
        if self._observer is not None:
            try:
                records = self._observer.get_records()
                self._evidence_events = [
                    {
                        "step": r.step,
                        "signal": r.signal.value,
                        "reason": r.signal_reason,
                        "confidence": r.confidence,
                    }
                    for r in records
                ]
            except Exception:
                pass

        # ── Build enriched result ─────────────────────────────────────
        artifact = self.finalize()

        # Enrich with strategy + budget information
        artifact["recovery_decisions"] = recovery_decisions
        artifact["evidence_events"] = self._evidence_events
        artifact["budget_accounting"] = {
            "model_calls_used": budgeted_driver.calls_used,
            "model_calls_limit": max_calls,
            "budget_remaining": budgeted_driver.calls_remaining,
            "budget_exhausted": budgeted_driver.calls_remaining <= 0,
        }
        artifact["strategy_config"] = strategy_config

        return artifact

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


# ── Re-export BudgetExhausted for backward compatibility ─────────────
from .model_driver import BudgetExhausted  # noqa: E402, F811
