"""Phase5 Canary Runner — runs 1 task × 6 arms through live provider.

Usage:
    python -m lhas.phase5.canary_runner

Requires:
    ODYS_AGENT_API_KEY
    ODYS_AGENT_BASE_URL
    ODYS_AGENT_MODEL (optional, defaults to mimo-v2.5)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback
from pathlib import Path

# Ensure ToolMaze is on path
_REPO = Path(__file__).resolve().parents[3] / "experiments" / "phase5" / "benchmarks" / "toolmaze"
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from lhas.phase5.agent_core import Phase5AgentCore
from lhas.phase5.agent_adapter import create_toolmaze_agent_adapter
from lhas.phase5.control_arms import create_policy, ControlArm
from lhas.phase5.live_driver import LiveModelDriver
from lhas.phase5.model_driver import BudgetedModelDriver, BudgetExhausted
from lhas.phase5.trace_parser import count_tool_calls, build_derived_view
from lhas.phase5.types import BudgetConfig, PolicyExecutionError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _classify_exception(exc: Exception) -> dict:
    """Extract full exception chain for infrastructure diagnostics."""
    chain = []
    current = exc
    while current is not None:
        chain.append({
            "type": type(current).__name__,
            "message": str(current)[:500],
        })
        current = current.__cause__
    return {
        "exception_type": type(exc).__name__,
        "exception_message": str(exc)[:500],
        "cause_type": type(exc.__cause__).__name__ if exc.__cause__ else None,
        "cause_message": str(exc.__cause__)[:500] if exc.__cause__ else None,
        "full_chain": chain,
    }


def run_canary(experiment_id: str = "phase5-real-canary-002") -> dict:
    """Run the Phase5 canary: 1 task × 6 arms."""
    # Load canary manifest
    manifest_path = Path("experiments/phase5/manifests/phase5-real-canary-001.json")
    manifest = json.loads(manifest_path.read_text())

    task_info = manifest["task"]
    task_id = task_info["task_id"]
    arms = manifest["arms"]
    budget_cfg = manifest.get("root_budget", {})
    gen_cfg = manifest.get("generation_config", {})

    logger.info("=== PHASE5 CANARY: %s ===", experiment_id)
    logger.info("Task: %s (%s/%s)", task_id, task_info.get("complexity"), task_info.get("perturbation_mode"))

    # Load the actual task JSON
    task_file = _REPO / "data" / task_info["file_path"]
    if not task_file.exists():
        raise FileNotFoundError(f"Task file not found: {task_file}")
    task_json = json.loads(task_file.read_text(encoding="utf-8"))

    # Load tool definitions from ToolMaze
    from tools.loader import ToolLoader
    tool_loader = ToolLoader(str(_REPO / "tools" / "definitions"))

    tool_names = set()
    for step in task_json.get("execution_trace", []):
        if "tool_name" in step:
            tool_names.add(step["tool_name"])
    tool_definitions = []
    for tn in sorted(tool_names):
        tool = tool_loader.get_tool_by_name(tn)
        if tool:
            tool_definitions.append(tool)

    # Run each arm
    results = []
    for arm_name in arms:
        arm = ControlArm(arm_name)
        logger.info("\n--- ARM: %s ---", arm_name)

        driver = LiveModelDriver(
            model_id=gen_cfg.get("model_id", "mimo-v2.5"),
            temperature=gen_cfg.get("temperature", 0.0),
            max_output_tokens=gen_cfg.get("max_output_tokens", 4096),
            thinking_enabled=gen_cfg.get("thinking_enabled", True),
        )
        max_calls = budget_cfg.get("max_model_calls", 50)
        budgeted = BudgetedModelDriver(driver, max_model_calls=max_calls)
        from lhas.phase5.control_arms import _STRATEGY_MAP
        strategy = _STRATEGY_MAP[arm]()
        core = Phase5AgentCore(budgeted, strategy=strategy)
        adapter = create_toolmaze_agent_adapter(core)

        from evaluation.core.sandbox import ExecutionEngine
        engine = ExecutionEngine(
            task_json=task_json,
            agent=adapter,
            tools_dir=str(_REPO / "tools"),
        )

        trial_id = f"{experiment_id}_{task_id}_{arm_name}"
        start_time = time.time()
        trace_dict = {}
        derived_view = {}
        termination_reason = "completed"
        error_diagnostics = None

        try:
            trace_logger, token_usage = engine.run(max_rounds=budget_cfg.get("max_turns", 15))
            trace_dict = trace_logger.to_dict() if hasattr(trace_logger, 'to_dict') else {}
            derived_view = build_derived_view(trace_dict)
        except BudgetExhausted as exc:
            termination_reason = f"budget_exhausted"
            error_diagnostics = _classify_exception(exc)
            token_usage = driver.get_token_usage().to_dict()
        except PolicyExecutionError as exc:
            termination_reason = f"policy_error"
            error_diagnostics = _classify_exception(exc)
            token_usage = driver.get_token_usage().to_dict()
        except Exception as exc:
            termination_reason = f"infrastructure_error"
            error_diagnostics = _classify_exception(exc)
            token_usage = driver.get_token_usage().to_dict()

        wall_time = time.time() - start_time

        trial_result = {
            "trial_id": trial_id,
            "experiment_id": experiment_id,
            "task_id": task_id,
            "arm": arm_name,
            "termination_reason": termination_reason,
            "model_calls_used": budgeted.calls_used,
            "input_tokens": driver.get_token_usage().input_tokens,
            "output_tokens": driver.get_token_usage().output_tokens,
            "wall_time_seconds": round(wall_time, 1),
            "recovery_decisions": core.get_recovery_decisions(),
            "tool_call_count": derived_view.get("tool_call_count", 0),
            "round_count": derived_view.get("round_count", 0),
            "has_final_answer": derived_view.get("has_final_answer", False),
            "error_diagnostics": error_diagnostics,
        }
        results.append(trial_result)
        logger.info("Result: %s", json.dumps(trial_result, indent=2, default=str, ensure_ascii=False)[:500])

    # Write canary report
    report = {
        "experiment_id": experiment_id,
        "experiment_role": "INFRA_CANARY",
        "task_id": task_id,
        "trials_expected": 6,
        "trials_completed": len(results),
        "trials": results,
    }

    out_dir = Path(manifest.get("output_dir", f"experiments/phase5/runs/{experiment_id}")).parent / experiment_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # Write official trace per trial
    for r in results:
        trial_dir = out_dir / r["trial_id"]
        trial_dir.mkdir(parents=True, exist_ok=True)
        (trial_dir / "trial_manifest.json").write_text(json.dumps(r, indent=2, default=str, ensure_ascii=False))

    report_path = out_dir / "canary_report.json"
    report_path.write_text(json.dumps(report, indent=2, default=str, ensure_ascii=False))
    logger.info("\n=== CANARY COMPLETE ===")
    logger.info("Report: %s", report_path)

    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", default="phase5-real-canary-002", help="Experiment ID")
    args = parser.parse_args()
    run_canary(experiment_id=args.id)
