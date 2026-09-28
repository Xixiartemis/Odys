"""Phase5 Canary Runner - uses the shared TrialExecutor.

Every trial goes through the canonical path:
strategy.configure() -> create_observer() -> EvidenceLedger -> ExecutionEngine
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
from lhas.phase5.artifacts import ArtifactWriter
from lhas.phase5.live_driver import LiveModelDriver
from lhas.phase5.trial_executor import execute_trial, TrialResult
from lhas.phase5.types import BudgetConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _write_json_once(path: Path, value: object) -> None:
    """Create one structured JSON artifact and refuse all overwrites."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(ArtifactWriter._structured(value), handle, indent=2, ensure_ascii=False)


def _write_text_once(path: Path, value: str) -> None:
    """Create one text artifact and refuse all overwrites."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(value)


def _persist_trial_artifacts(trial_dir: Path, result: TrialResult):
    """Persist all required artifacts without overwriting a prior attempt."""
    _write_json_once(trial_dir / "official_trace.json", result.official_trace)
    _write_json_once(trial_dir / "derived_runtime_view.json", result.derived_view)
    _write_json_once(trial_dir / "recovery_decisions.json", result.recovery_decisions)
    _write_json_once(trial_dir / "progress_shadow.json", result.shadow_records)
    _write_text_once(
        trial_dir / "evidence.jsonl",
        "\n".join(json.dumps(ArtifactWriter._structured(e), ensure_ascii=False)
                  for e in result.evidence_events),
    )
    _write_json_once(trial_dir / "recovery_budget_ledger.json", result.recovery_budget_ledger)
    _write_json_once(trial_dir / "validator_events.json", result.validator_events)
    _write_json_once(trial_dir / "public_tool_observations.json", result.public_tool_observations)
    _write_json_once(trial_dir / "budget_ledger.json", result.provider_usage)
    _write_json_once(trial_dir / "provider_usage.json", result.provider_usage)
    _write_json_once(trial_dir / "native_judgement.json", result.grader_result.get("judgement") or {})
    _write_json_once(
        trial_dir / "native_metrics.json",
        result.grader_result.get("metrics_report") or result.grader_result.get("metrics_summary") or {},
    )
    _write_json_once(
        trial_dir / "validity.json",
        {
            "validity": result.validity,
            "termination_reason": result.termination_reason,
            "grader_error": result.grader_result.get("error"),
            "error_diagnostics": result.error_diagnostics,
        },
    )
    _write_json_once(trial_dir / "trial_manifest.json", result.to_dict())


def run_canary(experiment_id: str = "phase5-real-canary-004") -> dict:
    """Run the Phase5 canary: 1 task x 6 arms via shared TrialExecutor."""
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

    # TrialExecutor constructs the official ExecutionEngine and owns the
    # engine-selected runtime tool schema. Canary does not derive a second
    # schema from hidden execution_trace data.
    tool_definitions = None

    budget = BudgetConfig(
        max_turns=budget_cfg.get("max_turns", 15),
        max_model_calls=budget_cfg.get("max_model_calls", 50),
        token_budget=budget_cfg.get("token_budget", budget_cfg.get("max_tokens")),
    )

    results = []
    for arm_name in arms:
        arm = ControlArm(arm_name)
        logger.info("\n--- ARM: %s ---", arm_name)

        # Fresh driver per trial (Section H: TrialExecutor owns root budget)
        driver = LiveModelDriver(
            model_id=gen_cfg.get("model_id", "mimo-v2.5"),
            provider=gen_cfg.get("provider", "mimo"),
            temperature=gen_cfg.get("temperature", 0.0),
            top_p=gen_cfg.get("top_p", 1.0),
            max_output_tokens=gen_cfg.get("max_output_tokens", 4096),
            thinking_enabled=gen_cfg.get("thinking_enabled", True),
            request_timeout=gen_cfg.get("request_timeout", 120),
            max_retries=gen_cfg.get("max_retries", 3),
            supports_tool_choice=gen_cfg.get("supports_tool_choice", False),
            supports_parallel_tool_calls=gen_cfg.get("supports_parallel_tool_calls", False),
        )

        # Execute through shared TrialExecutor
        # TrialExecutor wraps with BudgetedModelDriver (single canonical owner)
        trial_result = execute_trial(
            arm=arm,
            task_json=task_json,
            tool_definitions=tool_definitions,
            model_driver=driver,
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
    _write_json_once(out_dir / "canary_report.json", report)
    logger.info("\n=== CANARY COMPLETE ===")
    logger.info("Valid: %d/%d", report["trials_valid"], report["trials_expected"])
    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", default="phase5-real-canary-004")
    args = parser.parse_args()
    run_canary(experiment_id=args.id)
