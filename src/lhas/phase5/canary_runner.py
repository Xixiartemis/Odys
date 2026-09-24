"""Phase5 Canary Runner — uses the shared TrialExecutor.

Every trial goes through the canonical path:
strategy.configure() → create_observer() → EvidenceLedger → ExecutionEngine
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3] / "experiments" / "phase5" / "benchmarks" / "toolmaze"
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from lhas.phase5.control_arms import ControlArm
from lhas.phase5.live_driver import LiveModelDriver
from lhas.phase5.model_driver import BudgetedModelDriver
from lhas.phase5.trial_executor import execute_trial, TrialResult
from lhas.phase5.types import BudgetConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _persist_trial_artifacts(trial_dir: Path, result: TrialResult):
    """Persist all required artifacts for a trial."""
    trial_dir.mkdir(parents=True, exist_ok=True)

    (trial_dir / "official_trace.json").write_text(
        json.dumps(result.official_trace, indent=2, default=str, ensure_ascii=False))
    (trial_dir / "derived_runtime_view.json").write_text(
        json.dumps(result.derived_view, indent=2, default=str, ensure_ascii=False))
    (trial_dir / "recovery_decisions.json").write_text(
        json.dumps(result.recovery_decisions, indent=2, default=str, ensure_ascii=False))
    # K: actual observer records (not placeholder)
    (trial_dir / "progress_shadow.json").write_text(
        json.dumps(result.shadow_records, indent=2, default=str, ensure_ascii=False))
    # K: actual evidence events (not placeholder)
    (trial_dir / "evidence.jsonl").write_text(
        "\n".join(json.dumps(e, default=str, ensure_ascii=False) for e in result.evidence_events) if result.evidence_events else "")
    # E: recovery budget gate ledger
    (trial_dir / "recovery_budget_ledger.json").write_text(
        json.dumps(result.recovery_budget_ledger, indent=2, default=str, ensure_ascii=False))
    # Section 14: validator events
    (trial_dir / "validator_events.json").write_text(
        json.dumps(result.validator_events, indent=2, default=str, ensure_ascii=False))
    (trial_dir / "budget_ledger.json").write_text(
        json.dumps(result.provider_usage, indent=2, default=str))
    (trial_dir / "provider_usage.json").write_text(
        json.dumps(result.provider_usage, indent=2, default=str))
    # M: verbatim native judgement
    (trial_dir / "native_judgement.json").write_text(
        json.dumps(result.grader_result.get("judgement") or {}, indent=2, default=str, ensure_ascii=False))
    # M: verbatim native metrics (full MetricsCalculator report)
    (trial_dir / "native_metrics.json").write_text(
        json.dumps(result.grader_result.get("metrics_report") or result.grader_result.get("metrics_summary") or {}, indent=2, default=str, ensure_ascii=False))
    (trial_dir / "validity.json").write_text(
        json.dumps({"validity": result.validity,
                     "termination_reason": result.termination_reason,
                     "grader_error": result.grader_result.get("error"),
                     "error_diagnostics": result.error_diagnostics},
                    indent=2, default=str, ensure_ascii=False))
    (trial_dir / "trial_manifest.json").write_text(
        json.dumps(result.to_dict(), indent=2, default=str, ensure_ascii=False))


def run_canary(experiment_id: str = "phase5-real-canary-004") -> dict:
    """Run the Phase5 canary: 1 task × 6 arms via shared TrialExecutor."""
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

    budget = BudgetConfig(
        max_turns=budget_cfg.get("max_turns", 15),
        max_model_calls=budget_cfg.get("max_model_calls", 50),
    )

    results = []
    for arm_name in arms:
        arm = ControlArm(arm_name)
        logger.info("\n--- ARM: %s ---", arm_name)

        # Fresh driver per trial
        driver = LiveModelDriver(
            model_id=gen_cfg.get("model_id", "mimo-v2.5"),
            temperature=gen_cfg.get("temperature", 0.0),
            max_output_tokens=gen_cfg.get("max_output_tokens", 4096),
            thinking_enabled=gen_cfg.get("thinking_enabled", True),
        )
        budgeted_driver = BudgetedModelDriver(driver, max_model_calls=budget.max_model_calls)

        # Execute through shared TrialExecutor
        trial_result = execute_trial(
            arm=arm,
            task_json=task_json,
            tool_definitions=tool_definitions,
            model_driver=budgeted_driver,
            budget=budget,
            experiment_id=experiment_id,
            task_id=task_id,
            max_rounds=budget.max_turns,
        )

        # Persist artifacts
        trial_dir = Path(f"experiments/phase5/runs/{experiment_id}/{trial_result.trial_id}")
        _persist_trial_artifacts(trial_dir, trial_result)

        results.append(trial_result.to_dict())
        logger.info("Result: %s", json.dumps(trial_result.to_dict(), indent=2, default=str, ensure_ascii=False)[:400])

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
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "canary_report.json").write_text(json.dumps(report, indent=2, default=str, ensure_ascii=False))
    logger.info("\n=== CANARY COMPLETE ===")
    logger.info("Valid: %d/%d", report["trials_valid"], report["trials_expected"])
    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", default="phase5-real-canary-004")
    args = parser.parse_args()
    run_canary(experiment_id=args.id)
