"""TrialExecutor — shared execution path for canary and pilot.

Every trial (canary or pilot) MUST go through this executor.
It ensures:
- strategy.configure() is called
- create_observer() is called (canonical observer lifecycle)
- EvidenceLedger is wired
- A2 validator is wired
- A5 recovery budget gating is wired
- Official ExecutionEngine is the sole execution engine
- All artifacts are persisted

This is the SINGLE canonical execution path.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .agent_core import Phase5AgentCore
from .agent_adapter import create_toolmaze_agent_adapter, ToolMazeDependencyUnavailable
from .control_arms import ControlArm, _STRATEGY_MAP
from .live_driver import ProviderExecutionError
from .model_driver import BudgetedModelDriver, BudgetExhausted, ModelDriver
from .trace_parser import build_derived_view
from .types import BudgetConfig, PolicyExecutionError

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parents[3] / "experiments" / "phase5" / "benchmarks" / "toolmaze"


class TrialResult:
    """Immutable result of a single trial execution."""

    def __init__(self, *, trial_id: str, task_id: str, arm: str):
        self.trial_id = trial_id
        self.task_id = task_id
        self.arm = arm
        self.termination_reason: str = "pending"
        self.validity: str = "pending"
        self.official_trace: Dict[str, Any] = {}
        self.derived_view: Dict[str, Any] = {}
        self.recovery_decisions: list = []
        self.provider_usage: Dict[str, Any] = {}
        self.grader_result: Dict[str, Any] = {}
        self.error_diagnostics: Optional[Dict[str, Any]] = None
        self.strategy_config: Dict[str, Any] = {}
        self.wall_time: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "task_id": self.task_id,
            "arm": self.arm,
            "termination_reason": self.termination_reason,
            "validity": self.validity,
            "tool_call_count": self.derived_view.get("tool_call_count", 0),
            "round_count": self.derived_view.get("round_count", 0),
            "has_final_answer": self.derived_view.get("has_final_answer", False),
            "grader_pass": self.grader_result.get("judgement", {}).get("pass") if self.grader_result.get("judgement") else None,
            "tsr": self.grader_result.get("metrics", {}).get("tsr") if self.grader_result.get("metrics") else None,
            "provider_usage": self.provider_usage,
            "recovery_decisions": self.recovery_decisions,
            "strategy_config": self.strategy_config,
            "error_diagnostics": self.error_diagnostics,
            "wall_time_seconds": round(self.wall_time, 1),
        }


def _classify_exception(exc: Exception) -> Dict[str, Any]:
    chain = []
    current = exc
    while current is not None:
        chain.append({"type": type(current).__name__, "message": str(current)[:500]})
        current = current.__cause__
    return {
        "exception_type": type(exc).__name__,
        "exception_message": str(exc)[:500],
        "cause_type": type(exc.__cause__).__name__ if exc.__cause__ else None,
        "cause_message": str(exc.__cause__)[:500] if exc.__cause__ else None,
        "full_chain": chain,
    }


def execute_trial(
    *,
    arm: ControlArm,
    task_json: Dict[str, Any],
    tool_definitions: list,
    model_driver: ModelDriver,
    budget: BudgetConfig,
    experiment_id: str,
    task_id: str,
    max_rounds: int = 15,
) -> TrialResult:
    """Execute a single trial through the canonical path.

    This is the ONLY function that runs a live trial.
    Both canary and pilot MUST call this function.
    """
    trial_id = f"{experiment_id}_{task_id}_{arm.value}"
    result = TrialResult(trial_id=trial_id, task_id=task_id, arm=arm.value)

    # 1. Create strategy
    strategy_cls = _STRATEGY_MAP[arm]
    strategy = strategy_cls()

    # 2. Canonical configure() — A3/A4/A5 differ here
    from .types import RuntimeTask, GenerationConfig
    rt = RuntimeTask(
        task_id=task_id,
        objective=task_json.get("task_description", ""),
        visible_tools=[{"name": td.get("name", ""), "description": td.get("description", "")} for td in tool_definitions],
        prompt=task_json.get("task_description", ""),
        budget=budget,
    )
    gen_cfg = GenerationConfig(
        model_id=getattr(model_driver, '_model_id', 'unknown'),
        provider=getattr(model_driver, '_provider', 'unknown'),
    )
    result.strategy_config = strategy.configure(task=rt, generation_config=gen_cfg)

    # 3. Create observer — canonical lifecycle (A3/A4 create observer, A0/A1/A2 don't)
    observer = strategy.create_observer()

    # 4. Create core with strategy
    core = Phase5AgentCore(model_driver, strategy=strategy)

    # 5. Wire observer (canonical)
    if observer is not None:
        core.set_shadow_observer(observer)

    # 6. Wire evidence ledger (canonical)
    try:
        from .substrate.evidence import EvidenceLedger
        ledger = EvidenceLedger(run_id=f"trial-{trial_id}")
        core.set_evidence_ledger(ledger)
    except ImportError:
        ledger = None

    # 7. Create ToolMaze adapter
    try:
        adapter = create_toolmaze_agent_adapter(core)
    except ToolMazeDependencyUnavailable as exc:
        result.termination_reason = "toolmaze_unavailable"
        result.validity = "INVALID_INFRA"
        result.error_diagnostics = _classify_exception(exc)
        return result

    # 8. Run through official ExecutionEngine
    from evaluation.core.sandbox import ExecutionEngine
    engine = ExecutionEngine(
        task_json=task_json,
        agent=adapter,
        tools_dir=str(_REPO / "tools"),
    )

    start_time = time.time()
    try:
        trace_logger, token_usage = engine.run(max_rounds=max_rounds)
        result.official_trace = trace_logger.to_dict() if hasattr(trace_logger, 'to_dict') else {}
        result.termination_reason = "completed"
    except BudgetExhausted as exc:
        result.termination_reason = "budget_exhausted"
        result.error_diagnostics = _classify_exception(exc)
    except ProviderExecutionError as exc:
        result.termination_reason = "provider_transport_failure"
        result.error_diagnostics = _classify_exception(exc)
    except PolicyExecutionError as exc:
        result.termination_reason = "policy_error"
        result.error_diagnostics = _classify_exception(exc)
    except Exception as exc:
        result.termination_reason = "infrastructure_error"
        result.error_diagnostics = _classify_exception(exc)

    result.wall_time = time.time() - start_time
    result.derived_view = build_derived_view(result.official_trace)
    result.recovery_decisions = core.get_recovery_decisions()

    # 9. Provider usage
    result.provider_usage = {
        "model_calls_used": model_driver.calls_used if hasattr(model_driver, 'calls_used') else 0,
        "input_tokens": model_driver.get_token_usage().input_tokens,
        "output_tokens": model_driver.get_token_usage().output_tokens,
        "wall_time_seconds": round(result.wall_time, 1),
    }

    # 10. Run official offline grader
    result.grader_result = _run_offline_grader(task_json, result.official_trace)

    # 11. Classify validity
    grader_error = result.grader_result.get("error")
    if result.termination_reason == "completed" and grader_error is None:
        result.validity = "VALID"
    else:
        result.validity = "INVALID_INFRA"

    return result


def _run_offline_grader(task_json: dict, official_trace: dict) -> dict:
    """Run official ToolMaze JudgeSystem + MetricsCalculator."""
    try:
        from evaluation.core.judge import JudgeSystem
        from evaluation.core.metrics import MetricsCalculator

        judge = JudgeSystem()
        judgement = judge.judge(task_json, official_trace)

        calc = MetricsCalculator()
        calc.add_result(task_json, official_trace, judgement)
        report = calc.generate_report()

        return {
            "judgement": {
                "pass": judgement.get("pass", False),
                "failure_reason": judgement.get("failure_reason", ""),
            },
            "metrics": {
                "tsr": report.get("tsr"),
                "prr": report.get("prr"),
                "rc": report.get("rc"),
            },
            "grader_source": "FROZEN_TOOLMAZE",
            "error": None,
        }
    except Exception as exc:
        return {
            "judgement": None,
            "metrics": None,
            "grader_source": "FROZEN_TOOLMAZE",
            "error": _classify_exception(exc),
        }
