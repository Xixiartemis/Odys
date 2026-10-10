"""Phase5 recovery-route experiment manifest generator (readiness gate).

Generates, with machine-verifiable self-hashes:

1. ``qualification`` — an INFRA_CANARY manifest for the provider-free
   qualification preflight (``lhas.phase5.canary_runner --preflight-only``).
2. ``experiment`` — the B0/B1 route-experiment manifest DRAFT with the
   dev-excluded, seed-frozen task selection (execution requires human
   approval; the draft is never executed by this script).
3. ``isolation`` — the auditable task list with per-file sha256 digests
   and the dev-set overlap check.

The script makes NO real model requests and writes ONLY new files under
``experiments/phase5/manifests/`` and ``experiments/phase5/readiness/``.
Historical manifests and runs are never modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

# ── Frozen provenance constants (copied from the validated canonical
#    manifest schema phase5-live-canary-002; the provider-free preflight
#    is the machine verification that these satisfy every validator). ──

BENCHMARK_IDENTITY = {
    "name": "toolmaze",
    "revision": "ef0798a",
    "repository_url": "https://github.com/Zhudongsheng75/ToolMaze",
    "commit_sha": "ef0798aa7f31ac9b33403254b1ef76e8673305fa",
    "dataset_revision": "08b0239a98b0b07839d673f5bc0ef7d836db9296",
    "dataset_digest": "9fcd7d7ec3c06afcee098877cca1ae8c29b87a1102be4b365a1055f824a76bc8",
    "evaluator_digest": "74958635c3794bdd1202ad2a35f2d31f9504221905f98b94173d8c87474be740",
    "dataset_hash_scope": "data/perturbed_tasks/**/*.json sorted by POSIX path, concatenated bytes",
}

GENERATION_CONFIG = {
    "model_id": "mimo-v2.5",
    "provider": "mimo",
    "thinking_enabled": True,
    "effective_temperature": 1.0,
    "effective_top_p": 0.95,
    "sampling_parameters_sent": False,
    "max_completion_tokens": 4096,
    "request_timeout": 120,
    "sdk_max_retries": 0,
    "supports_tool_choice": False,
    "supports_parallel_tool_calls": False,
}

ROOT_BUDGET = {
    "max_model_calls": 50,
    "max_turns": 15,
    "max_tokens": 100000,
    "token_budget": 100000,
}

# Implementation identity this readiness gate qualifies.
IMPLEMENTATION_SHA = "4956e91fbedc72c9fd6f747fe17eb16410c80979"
BASELINE_SHA = "973a612a02d984fe8312401b08b3c394e5994649"

# Development set: every real ToolMaze task referenced by tests, dev
# scripts or the development canary runs (see dataset isolation report).
DEV_TASK_IDS = [
    "C1_task_015_P1",
    "C1_task_036_P4",
    "C1_task_082_P0",
    "C1_task_082_P1",
]

SELECTION_SEED = 42  # frozen by the pilot manifests (selection_seed)

ROUTE_ARMS = [
    {
        "arm_id": "B0_BASELINE_ROUTE",
        "route_mode": "baseline",
        "recovery_strategy_arm": "A3_ODYS_FULL",
        "description": "frozen baseline recovery routing (Q3a/Q3b/Q3d disabled)",
    },
    {
        "arm_id": "B1_ROUTE_V1",
        "route_mode": "route-v1",
        "recovery_strategy_arm": "A3_ODYS_FULL",
        "description": "recovery-route-v1 (full Q0-Q3 rule set)",
    },
]


def manifest_hash(manifest: dict) -> str:
    """Same algorithm as canary_runner._manifest_hash (sha256 canonical)."""
    unsigned = {k: v for k, v in manifest.items() if k != "manifest_hash"}
    canonical = json.dumps(
        unsigned, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_qualification_manifest(*, task_id: str, output_dir: str) -> dict:
    base, mode = task_id.rsplit("_", 1)
    manifest = {
        "schema_version": "phase5-live-canary-002",
        "experiment_id": "phase5-route-readiness-qualification-001",
        "experiment_role": "INFRA_CANARY",
        "included_in_pilot_analysis": False,
        "included_in_confirmatory_analysis": False,
        "real_provider_executed": "NO",
        "qualification_scope": "PROVIDER_FREE_PREFLIGHT_ONLY",
        "base_exact_head": BASELINE_SHA,
        "implementation_sha": IMPLEMENTATION_SHA,
        "route_policy_version": "recovery-route-v1",
        "route_policy_fingerprint": "a056983f126c8963",
        "event_schema_version": "phase5-control-events-v2",
        "task": {
            "task_id": task_id,
            "complexity": base.split("_")[0],
            "perturbation_mode": mode,
            "file_path": f"perturbed_tasks/{base.split('_')[0].lower()}/{task_id}.json",
        },
        "arms": ["A3_ODYS_FULL"],
        "root_budget": dict(ROOT_BUDGET),
        "benchmark": dict(BENCHMARK_IDENTITY),
        "generation_config": dict(GENERATION_CONFIG),
        "output_dir": output_dir,
    }
    manifest["manifest_hash"] = manifest_hash(manifest)
    return manifest


def enumerate_tasks():
    from lhas.phase5.toolmaze_adapter import ToolMazeAdapter

    adapter = ToolMazeAdapter()
    return list(adapter.enumerate_tasks())


def draw_experiment_tasks(*, seed: int, exclude: list[str]) -> dict:
    """Reproduce select_20_tasks semantics on the dev-excluded pool.

    One task per (topology, perturbation) cell via random.Random(seed)
    choice, iterating C1..C4 x P0..P4 — identical to
    real_pilot_runner.select_20_tasks except candidates in ``exclude``
    are removed BEFORE rng.choice (so the frozen seed stays meaningful).
    """
    from lhas.phase5.types import PerturbationMode, TopologyClass

    tasks = enumerate_tasks()
    cells: dict[tuple[str, str], list] = {}
    for task in tasks:
        topo = task.topology.value if task.topology else "C1"
        mode = task.perturbation_mode.value
        cells.setdefault((topo, mode), []).append(task)

    rng = random.Random(seed)
    selected = []
    cell_audit = []
    for topo in TopologyClass:
        for mode in PerturbationMode:
            key = (topo.value, mode.value)
            candidates = sorted(cells.get(key, []), key=lambda t: t.task_id)
            excluded_here = [t.task_id for t in candidates if t.task_id in exclude]
            pool = [t for t in candidates if t.task_id not in exclude]
            chosen = rng.choice(pool) if pool else None
            if chosen is not None:
                selected.append(chosen)
            cell_audit.append({
                "cell": f"{topo.value}_{mode.value}",
                "candidate_count": len(candidates),
                "pool_count_after_exclusion": len(pool),
                "excluded_dev_ids": excluded_here,
                "selected_task_id": chosen.task_id if chosen else None,
            })
    return {"selected": selected, "cell_audit": cell_audit, "seed": seed}


def _task_fingerprints(task_ids: list[str]) -> list[dict]:
    from lhas.phase5.toolmaze_adapter import ToolMazeAdapter

    adapter = ToolMazeAdapter()
    out = []
    for task_id in sorted(task_ids):
        raw = adapter._find_task(task_id)
        source = raw.get("_source_file") if raw else None
        digest = None
        if source:
            path = _ROOT / "experiments" / "phase5" / "benchmarks" / "toolmaze" / "data" / source
            if path.is_file():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
        out.append({"task_id": task_id, "source_file": source, "sha256": digest})
    return out


def build_isolation_report() -> dict:
    draw = draw_experiment_tasks(seed=SELECTION_SEED, exclude=DEV_TASK_IDS)
    selected_ids = [t.task_id for t in draw["selected"]]
    overlap = sorted(set(selected_ids) & set(DEV_TASK_IDS))

    # The historical pilot selection (frozen, untouched) is checked read-only.
    pilot_path = _ROOT / "experiments" / "phase5" / "manifests" / "phase5-real-pilot-003.json"
    pilot_ids: list[str] = []
    if pilot_path.is_file():
        pilot = json.loads(pilot_path.read_text(encoding="utf-8"))
        pilot_ids = list(pilot.get("selected_task_ids", []))
    pilot_overlap = sorted(set(pilot_ids) & set(DEV_TASK_IDS))

    from lhas.phase5.recovery_route import route_policy_fingerprint

    return {
        "schema_version": "phase5-route-dataset-isolation-001",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "selection_seed": SELECTION_SEED,
        "selection_algorithm": (
            "real_pilot_runner.select_20_tasks semantics (stratified "
            "random.Random(seed).choice over C1..C4 x P0..P4 cells) with "
            "DEV_TASK_IDS removed from each cell BEFORE the draw"
        ),
        "dev_task_ids": sorted(DEV_TASK_IDS),
        "dev_set_definition": (
            "every real ToolMaze task referenced by tests, dev scripts or "
            "development canary runs up to and including 4956e91f"
        ),
        "selected_task_ids": sorted(selected_ids),
        "selected_task_count": len(selected_ids),
        "dev_overlap": overlap,
        "dev_overlap_check": "PASS" if not overlap else "BLOCKED",
        "historical_pilot_003_selected_task_ids": sorted(pilot_ids),
        "historical_pilot_003_dev_overlap": pilot_overlap,
        "historical_pilot_003_usable_as_final_eval_set": not pilot_overlap,
        "task_fingerprints": _task_fingerprints(selected_ids + DEV_TASK_IDS),
        "cell_audit": draw["cell_audit"],
        "route_policy_fingerprint": route_policy_fingerprint(),
    }


def build_experiment_manifest(*, isolation: dict) -> dict:
    selected = isolation["selected_task_ids"]
    overlap = isolation["dev_overlap"]
    manifest = {
        "schema_version": "phase5-route-experiment-001",
        "experiment_id": "phase5-route-experiment-B0B1-draft-001",
        "experiment_role": "PIPELINE_PILOT",
        "execution_status": "PENDING_HUMAN_APPROVAL",
        "real_provider_executed": "NO",
        "base_exact_head": BASELINE_SHA,
        "implementation_sha": IMPLEMENTATION_SHA,
        "route_policy_version": "recovery-route-v1",
        "route_policy_fingerprint": isolation["route_policy_fingerprint"],
        "event_schema_version": "phase5-control-events-v2",
        "execution_control_version": "execution-control-v1",
        "benchmark": dict(BENCHMARK_IDENTITY),
        "generation_config": dict(GENERATION_CONFIG),
        "root_budget": dict(ROOT_BUDGET),
        "route_arms": ROUTE_ARMS,
        "strategy_arm": "A3_ODYS_FULL",
        "unique_difference_surface": "route_mode only (Q3a/Q3b/Q3d rule set)",
        "selection_seed": isolation["selection_seed"],
        "selected_task_ids": selected,
        "task_count": len(selected),
        "dev_task_ids": isolation["dev_task_ids"],
        "dev_overlap": overlap,
        "dataset_isolation_check": isolation["dev_overlap_check"],
        "task_fingerprints": isolation["task_fingerprints"],
        "paired_design": "each task-condition pair runs B0 and B1 once",
        "execution_requires_human_approval": True,
    }
    manifest["manifest_hash"] = manifest_hash(manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["qualification", "experiment", "isolation"], required=True)
    parser.add_argument("--task-id", default="C1_task_036_P4")
    args = parser.parse_args()

    manifests_dir = _ROOT / "experiments" / "phase5" / "manifests"
    readiness_dir = _ROOT / "experiments" / "phase5" / "readiness"
    readiness_dir.mkdir(parents=True, exist_ok=True)

    if args.mode == "isolation":
        report = build_isolation_report()
        out = readiness_dir / "dataset_isolation_report.json"
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"wrote": str(out), "dev_overlap_check": report["dev_overlap_check"],
                          "selected_task_count": report["selected_task_count"]}, sort_keys=True))
        return

    if args.mode == "qualification":
        manifest = build_qualification_manifest(
            task_id=args.task_id,
            output_dir="experiments/phase5/runs/phase5-route-readiness-qualification-001",
        )
        out = manifests_dir / "phase5-route-readiness-qualification-001.json"
    else:
        isolation = build_isolation_report()
        (readiness_dir / "dataset_isolation_report.json").write_text(
            json.dumps(isolation, indent=2, ensure_ascii=False), encoding="utf-8")
        manifest = build_experiment_manifest(isolation=isolation)
        out = manifests_dir / "phase5-route-experiment-B0B1-draft-001.json"

    if out.exists():
        raise SystemExit(f"refusing to overwrite existing manifest: {out}")
    out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({
        "wrote": str(out),
        "experiment_id": manifest["experiment_id"],
        "manifest_hash": manifest["manifest_hash"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
