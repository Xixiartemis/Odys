"""Phase5 Canary Runner — runs 1 task × 6 arms through live provider.

Persists all required artifacts per trial:
- official_trace.json (verbatim from TraceLogger)
- derived_runtime_view.json (from trace_parser)
- recovery_decisions.json
- progress_shadow.json
- budget_ledger.json
- provider_usage.json
- native_judgement.json (from official JudgeSystem)
- native_metrics.json (from official MetricsCalculator)
- validity.json
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3] / "experiments" / "phase5" / "benchmarks" / "toolmaze"
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from lhas.phase5.agent_core import Phase5AgentCore
from lhas.phase5.agent_adapter import create_toolmaze_agent_adapter
from lhas.phase5.control_arms import ControlArm, _STRATEGY_MAP
from lhas.phase5.live_driver import LiveModelDriver, ProviderExecutionError
from lhas.phase5.model_driver import BudgetedModelDriver, BudgetExhausted
from lhas.phase5.trace_parser import count_tool_calls, build_derived_view
from lhas.phase5.types import BudgetConfig, PolicyExecutionError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _classify_exception(exc: Exception) -> dict:
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
                "trace_check": judgement.get("trace_check"),
            },
            "metrics": {
                "tsr": report.get("tsr"),
                "prr": report.get("prr"),
                "rc": report.get("rc"),
                "hit_count": report.get("hit_count"),
                "resolved_hit_count": report.get("resolved_hit_count"),
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


def _write_trial_artifacts(trial_dir: Path, *, official_trace: dict, derived_view: dict,
                           recovery_decisions: list, provider_usage: dict,
                           grader_result: dict, termination_reason: str,
                           error_diagnostics: dict | None):
    """Write all required trial artifacts."""
    trial_dir.mkdir(parents=True, exist_ok=True)

    # Official trace — verbatim, unmodified
    (trial_dir / "official_trace.json").write_text(
        json.dumps(official_trace, indent=2, default=str, ensure_ascii=False)
    )

    # Derived view — separate from official trace
    (trial_dir / "derived_runtime_view.json").write_text(
        json.dumps(derived_view, indent=2, default=str, ensure_ascii=False)
    )

    # Recovery decisions
    (trial_dir / "recovery_decisions.json").write_text(
        json.dumps(recovery_decisions, indent=2, default=str, ensure_ascii=False)
    )

    # Progress shadow (empty if no observer)
    (trial_dir / "progress_shadow.json").write_text("[]")

    # Budget ledger
    (trial_dir / "budget_ledger.json").write_text(
        json.dumps({"model_calls_used": provider_usage.get("model_calls_used", 0),
                     "max_model_calls": provider_usage.get("max_model_calls", 0),
                     "input_tokens": provider_usage.get("input_tokens", 0),
                     "output_tokens": provider_usage.get("output_tokens", 0)}, indent=2)
    )

    # Provider usage
    (trial_dir / "provider_usage.json").write_text(
        json.dumps(provider_usage, indent=2, default=str)
    )

    # Native judgement + metrics from official grader
    (trial_dir / "native_judgement.json").write_text(
        json.dumps(grader_result.get("judgement") or {}, indent=2, default=str)
    )
    (trial_dir / "native_metrics.json").write_text(
        json.dumps(grader_result.get("metrics") or {}, indent=2, default=str)
    )

    # Validity
    grader_error = grader_result.get("error")
    if termination_reason == "completed" and grader_error is None:
        validity = "VALID"
    elif termination_reason == "completed" and grader_error is not None:
        validity = "INVALID_INFRA"
    else:
        validity = "INVALID_INFRA"

    (trial_dir / "validity.json").write_text(
        json.dumps({
            "validity": validity,
            "termination_reason": termination_reason,
            "grader_error": grader_error,
            "error_diagnostics": error_diagnostics,
        }, indent=2, default=str, ensure_ascii=False)
    )


def run_canary(experiment_id: str = "phase5-real-canary-003") -> dict:
    """Run the Phase5 canary: 1 task × 6 arms."""
    manifest_path = Path("experiments/phase5/manifests/phase5-real-canary-001.json")
    manifest = json.loads(manifest_path.read_text())

    task_info = manifest["task"]
    task_id = task_info["task_id"]
    arms = manifest["arms"]
    budget_cfg = manifest.get("root_budget", {})
    gen_cfg = manifest.get("generation_config", {})

    logger.info("=== PHASE5 CANARY: %s ===", experiment_id)
    logger.info("Task: %s (%s/%s)", task_id, task_info.get("complexity"), task_info.get("perturbation_mode"))

    task_file = _REPO / "data" / task_info["file_path"]
    task_json = json.loads(task_file.read_text(encoding="utf-8"))

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

    results = []
    for arm_name in arms:
        arm = ControlArm(arm_name)
        logger.info("\n--- ARM: %s ---", arm_name)

        driver = LiveModelDriver(
            model_id=gen_cfg.get("model_id", "mimo-v2.5"),
            temperature=gen_cfg.get("temperature", 0.0),
            max_output_tokens=gen_cfg.get("max_output_tokens", 4096),
            thinking_enabled=gen_cfg.get("thinking_enabled", True),
            max_retries=3,
        )
        max_calls = budget_cfg.get("max_model_calls", 50)
        budgeted = BudgetedModelDriver(driver, max_model_calls=max_calls)
        strategy = _STRATEGY_MAP[arm]()
        core = Phase5AgentCore(budgeted, strategy=strategy)
        adapter = create_toolmaze_agent_adapter(core)

        from evaluation.core.sandbox import ExecutionEngine
        engine = ExecutionEngine(
            task_json=task_json, agent=adapter, tools_dir=str(_REPO / "tools"),
        )

        trial_id = f"{experiment_id}_{task_id}_{arm_name}"
        trial_dir = Path(f"experiments/phase5/runs/{experiment_id}/{trial_id}")
        start_time = time.time()

        trace_dict = {}
        termination_reason = "completed"
        error_diagnostics = None

        try:
            trace_logger, token_usage = engine.run(max_rounds=budget_cfg.get("max_turns", 15))
            trace_dict = trace_logger.to_dict() if hasattr(trace_logger, 'to_dict') else {}
        except BudgetExhausted as exc:
            termination_reason = "budget_exhausted"
            error_diagnostics = _classify_exception(exc)
        except ProviderExecutionError as exc:
            termination_reason = "provider_transport_failure"
            error_diagnostics = _classify_exception(exc)
        except PolicyExecutionError as exc:
            termination_reason = "policy_error"
            error_diagnostics = _classify_exception(exc)
        except Exception as exc:
            termination_reason = "infrastructure_error"
            error_diagnostics = _classify_exception(exc)

        wall_time = time.time() - start_time
        derived_view = build_derived_view(trace_dict)

        # Run official offline grader
        grader_result = _run_offline_grader(task_json, trace_dict)

        provider_usage = {
            "model_calls_used": budgeted.calls_used,
            "max_model_calls": max_calls,
            "input_tokens": driver.get_token_usage().input_tokens,
            "output_tokens": driver.get_token_usage().output_tokens,
            "provider_request_count": driver.provider_request_count,
            "wall_time_seconds": round(wall_time, 1),
        }

        _write_trial_artifacts(
            trial_dir,
            official_trace=trace_dict,
            derived_view=derived_view,
            recovery_decisions=core.get_recovery_decisions(),
            provider_usage=provider_usage,
            grader_result=grader_result,
            termination_reason=termination_reason,
            error_diagnostics=error_diagnostics,
        )

        validity_file = trial_dir / "validity.json"
        validity_data = json.loads(validity_file.read_text())

        trial_result = {
            "trial_id": trial_id,
            "experiment_id": experiment_id,
            "task_id": task_id,
            "arm": arm_name,
            "termination_reason": termination_reason,
            "validity": validity_data["validity"],
            "tool_call_count": derived_view.get("tool_call_count", 0),
            "round_count": derived_view.get("round_count", 0),
            "has_final_answer": derived_view.get("has_final_answer", False),
            "grader_pass": grader_result.get("judgement", {}).get("pass") if grader_result.get("judgement") else None,
            "tsr": grader_result.get("metrics", {}).get("tsr") if grader_result.get("metrics") else None,
            "error_diagnostics": error_diagnostics,
        }
        results.append(trial_result)
        logger.info("Result: %s", json.dumps(trial_result, indent=2, default=str, ensure_ascii=False)[:400])

    # Write canary report
    report = {
        "experiment_id": experiment_id,
        "experiment_role": "INFRA_CANARY",
        "task_id": task_id,
        "trials_expected": 6,
        "trials_completed": len(results),
        "trials_valid": sum(1 for r in results if r["validity"] == "VALID"),
        "trials_invalid_infra": sum(1 for r in results if r["validity"] == "INVALID_INFRA"),
        "trials": results,
    }

    out_dir = Path(f"experiments/phase5/runs/{experiment_id}")
    (out_dir / "canary_report.json").write_text(json.dumps(report, indent=2, default=str, ensure_ascii=False))
    logger.info("\n=== CANARY COMPLETE ===")
    logger.info("Valid: %d/%d", report["trials_valid"], report["trials_expected"])
    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", default="phase5-real-canary-003")
    args = parser.parse_args()
    run_canary(experiment_id=args.id)
