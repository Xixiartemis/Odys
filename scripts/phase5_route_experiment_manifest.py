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


# ── Formal B0/B1 experiment manifest (Experiment B, create-only) ─────

EVALUATOR_IDENTITY_VARIANTS = {
    # Three recorded variants exist in the repository (see
    # docs/phase5/B0B1_RUNNER_SOURCE_AUDIT.md P0-5).  None is replaced;
    # the formal manifest records each with its scope.
    "E1_legacy_recorded_benchmark_lock": (
        "412f9c3615bfd997af903f6514f716be94a774140975b82c8ff2fd16b6547833"
    ),
    "E2_lf_normalized_canonical": (
        "74958635c3794bdd1202ad2a35f2d31f9504221905f98b94173d8c87474be740"
    ),
    "E3_legacy_recorded_code_freeze": (
        "412f9c3615bfd997af903f6514f716be94a7741f"
    ),
    "scope_note": (
        "E2 = sha256 over LF-normalized judge.py+metrics.py+sandbox.py "
        "bytes (canary_runner._hash_files); E1/E3 are historical frozen "
        "records whose computing script is not in-repo"
    ),
}

PREREGISTRATION_DEFAULT = {
    "protocol_version": "recovery-route-eval-protocol-v1",
    "primary_metric": "verified_completion_rate",
    "primary_metric_definition": (
        "external Judge-verified valid trials / valid trials"
    ),
    "judge": "FROZEN_TOOLMAZE (JudgeSystem + MetricsCalculator)",
    "unique_difference_surface": "route_mode only (Q3a/Q3b/Q3d rule set)",
    "approval_status": "PENDING_HUMAN_APPROVAL",
}


def build_formal_b0b1_manifest(
    *,
    experiment_id: str,
    pairs: list[dict],
    repetitions: int = 1,
    seed: int = SELECTION_SEED,
    execution_status: str = "PENDING_HUMAN_APPROVAL",
    experiment_budget: dict | None = None,
    preregistration: dict | None = None,
    notes: list[str] | None = None,
) -> dict:
    """Build the formal, executable B0/B1 experiment manifest.

    ``pairs`` are task-fault pair execution units, each a dict with at
    least ``task_id`` (execution-unit id, base+mode), ``base_task_id``,
    ``complexity``, ``perturbation_mode`` and ``file_path``.  Multiple
    fault variants of one base task share ``cluster_id`` = base_task_id.

    The manifest satisfies BOTH contracts:
    - the canonical Runner fields (tasks / arm_definitions / arms /
      budgets / root_budget / seed / generation_config);
    - the Experiment B identity fields (route_arms / repetitions /
      preregistration / evaluator identity variants).

    Create-only: callers must refuse to overwrite existing files.
    Unapproved runs fail closed at the Runner's authorization gate.
    """
    if repetitions < 1:
        raise ValueError("repetitions must be >= 1")
    if not pairs:
        raise ValueError("pairs must not be empty")

    from lhas.phase5.provenance import arm_definitions_snapshot

    task_entries = []
    seen_condition_ids = set()
    for pair in pairs:
        base_task_id = str(pair.get("base_task_id") or str(pair["task_id"]).rsplit("_", 1)[0])
        mode = str(pair["perturbation_mode"])
        condition_id = str(pair.get("condition_id") or f"{pair['task_id']}")
        if condition_id in seen_condition_ids:
            raise ValueError(f"duplicate task-fault pair identity: {condition_id}")
        seen_condition_ids.add(condition_id)
        task_entries.append({
            "task_id": pair["task_id"],
            "base_task_id": base_task_id,
            "condition_id": condition_id,
            "cluster_id": str(pair.get("cluster_id") or base_task_id),
            "complexity": pair.get("complexity", ""),
            "perturbation_mode": mode,
            "file_path": pair.get("file_path", ""),
            "task_sha256": pair.get("task_sha256"),
        })

    arm_defs = arm_definitions_snapshot()
    manifest = {
        "schema_version": "phase5-b0b1-experiment-001",
        "experiment_id": experiment_id,
        "experiment_role": "B0B1_EXPERIMENT",
        "execution_status": execution_status,
        "execution_requires_human_approval": True,
        "real_provider_executed": "NO",
        "benchmark": dict(BENCHMARK_IDENTITY),
        "evaluator_identity_variants": dict(EVALUATOR_IDENTITY_VARIANTS),
        "generation_config": dict(GENERATION_CONFIG),
        "root_budget": dict(ROOT_BUDGET),
        "budgets": dict(ROOT_BUDGET),
        "route_arms": [dict(a) for a in ROUTE_ARMS],
        "arms": ["A3_ODYS_FULL"],
        "arm_definitions": {"A3_ODYS_FULL": arm_defs["A3_ODYS_FULL"]},
        "strategy_arm": "A3_ODYS_FULL",
        "unique_difference_surface": "route_mode only (Q3a/Q3b/Q3d rule set)",
        "seed": seed,
        "selection_seed": seed,
        "repetitions": repetitions,
        "tasks": task_entries,
        "selected_task_ids": [t["task_id"] for t in task_entries],
        "task_count": len(task_entries),
        "paired_design": (
            f"each task-fault pair runs B0 and B1 {repetitions} time(s)"
        ),
        "preregistration": dict(preregistration or PREREGISTRATION_DEFAULT),
        "notes": list(notes or []),
    }
    if experiment_budget:
        manifest["experiment_budget"] = dict(experiment_budget)
    manifest["manifest_hash"] = manifest_hash(manifest)
    return manifest


# ── Candidate task-fault pair sampling (B1 stage) ────────────────────

# Base-level dev exclusion: every P0–P4 variant of a base task whose
# description/fixtures were exposed to development is contaminated at
# the base level.  The 4 dev units belong to these 3 bases.
DEV_BASE_TASK_IDS = ["C1_task_015", "C1_task_036", "C1_task_082"]
FAULT_MODES = ["P1", "P2", "P3", "P4"]  # P0 = no perturbation, not a fault
TOPOLOGIES = ["C1", "C2", "C3", "C4"]

HISTORICAL_MANIFEST_FILES = (
    "phase5-real-pilot-001.json",
    "phase5-real-pilot-002.json",
    "phase5-real-pilot-003.json",
)


def historical_manifest_bases() -> set[str]:
    """Base task ids referenced by ANY historical dev manifest.

    Strictest contamination reading: a task chosen for a historical dev
    experiment (even if never executed) stays out of the final set.
    """
    manifests_dir = _ROOT / "experiments" / "phase5" / "manifests"
    bases: set[str] = set()
    for name in HISTORICAL_MANIFEST_FILES:
        path = manifests_dir / name
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for unit in data.get("selected_task_ids", []):
            bases.add(str(unit).rsplit("_", 1)[0])
    for path in manifests_dir.glob("phase5-real-canary-*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        task = data.get("task") or {}
        if isinstance(task, dict) and task.get("task_id"):
            bases.add(str(task["task_id"]).rsplit("_", 1)[0])
    return bases

PERTURBATION_SEMANTICS = {
    "P0": {"label": "NP (no perturbation)", "is_fault": False,
           "fault_trigger_confirmable": False,
           "note": "no fault injected; MUST NOT enter the fault-recovery denominator"},
    "P1": {"label": "explicit-transient", "is_fault": True,
           "fault_trigger_confirmable": True,
           "note": "explicit transient failure evidence in tool results"},
    "P2": {"label": "explicit-permanent", "is_fault": True,
           "fault_trigger_confirmable": True,
           "note": "explicit permanent failure evidence in tool results"},
    "P3": {"label": "implicit-transient", "is_fault": True,
           "fault_trigger_confirmable": True,
           "note": "implicit fault; corruption_type + perturbation_point in task file"},
    "P4": {"label": "implicit-permanent", "is_fault": True,
           "fault_trigger_confirmable": True,
           "note": "implicit fault; corruption_type + perturbation_point in task file"},
}


def candidate_pool() -> list[dict]:
    """Dev-base- and history-base-excluded P1–P4 task-fault pair pool
    (read-only over the frozen dataset)."""
    from lhas.phase5.toolmaze_adapter import ToolMazeAdapter

    excluded_bases = set(DEV_BASE_TASK_IDS) | historical_manifest_bases()
    adapter = ToolMazeAdapter()
    pool = []
    for desc in adapter.enumerate_tasks():
        mode = desc.perturbation_mode.value
        if mode not in FAULT_MODES:
            continue
        base = desc.native_metadata.get("original_task_id") or desc.task_id.rsplit("_", 1)[0]
        if base in excluded_bases:
            continue
        raw = adapter._find_task(desc.task_id) or {}
        pool.append({
            "task_id": desc.task_id,
            "base_task_id": base,
            "complexity": desc.complexity or "",
            "perturbation_mode": mode,
            "file_path": str(raw.get("_source_file", "")),
            "corruption_type": str(raw.get("corruption_type", "")),
            "perturbation_point": raw.get("perturbation_point"),
        })
    pool.sort(key=lambda p: p["task_id"])
    return pool


def sample_pairs(pool: list[dict], *, total: int, seed: int) -> dict:
    """Deterministic stratified draw: C1–C4 × P1–P4 cells, one variant
    per base task (cluster_id unique), fixed seed, auditable cell log."""
    rng = random.Random(seed)
    per_cell = max(1, total // (len(TOPOLOGIES) * len(FAULT_MODES)))
    used_bases: set[str] = set()
    cells: dict[str, list[dict]] = {}
    for pair in pool:
        cells.setdefault(f"{pair['complexity']}_{pair['perturbation_mode']}", []).append(pair)

    selected: list[dict] = []
    audit: list[dict] = []
    for cell in sorted(cells):
        available = [p for p in cells[cell] if p["base_task_id"] not in used_bases]
        chosen: list[dict] = []
        for _ in range(min(per_cell, len(available))):
            pick = rng.choice(available)
            available.remove(pick)
            used_bases.add(pick["base_task_id"])
            chosen.append(pick)
        selected.extend(chosen)
        audit.append({
            "cell": cell,
            "pool_size": len(cells[cell]),
            "selected": [p["task_id"] for p in chosen],
        })

    extras_needed = total - len(selected)
    order = sorted(cells)
    rng.shuffle(order)
    for cell in order:
        if extras_needed <= 0:
            break
        available = [p for p in cells[cell] if p["base_task_id"] not in used_bases]
        if not available:
            continue
        pick = rng.choice(available)
        used_bases.add(pick["base_task_id"])
        selected.append(pick)
        for entry in audit:
            if entry["cell"] == cell:
                entry["selected"].append(pick["task_id"])
        extras_needed -= 1

    selected.sort(key=lambda p: p["task_id"])
    return {
        "selected": selected,
        "cell_audit": audit,
        "seed": seed,
        "requested_total": total,
        "selected_total": len(selected),
        "distinct_base_tasks": len({p["base_task_id"] for p in selected}),
    }


def build_candidates_report(*, seed: int = SELECTION_SEED) -> dict:
    """Feasibility for 30/50/100 task-fault pairs + the 50-pair draft."""
    pool = candidate_pool()
    draws = {n: sample_pairs(pool, total=n, seed=seed) for n in (30, 50, 100)}
    return {
        "schema_version": "phase5-b0b1-candidates-001",
        "status": "DRAFT_NOT_APPROVED",
        "selection_seed": seed,
        "perturbation_semantics": PERTURBATION_SEMANTICS,
        "exclusion_rules": [
            "P0 pairs excluded (no fault — cannot enter fault-recovery denominator)",
            f"base-task-level dev exclusion: {sorted(DEV_BASE_TASK_IDS)} (all P0-P4 variants)",
            "base-task-level historical-manifest exclusion: every base listed in "
            "pilot-001/002/003 or canary manifests (strictest reading — even "
            "manifest-listed but never-executed tasks stay out)",
            "at most one fault variant per base task (cluster_id unique)",
        ],
        "raw_pool_pairs": 2000,
        "raw_pool_bases": 400,
        "pool_after_exclusion_pairs": len(pool),
        "pool_after_exclusion_bases": len({p["base_task_id"] for p in pool}),
        "feasibility": {
            str(n): {
                "feasible": draws[n]["selected_total"] == n,
                "selected_total": draws[n]["selected_total"],
                "distinct_base_tasks": draws[n]["distinct_base_tasks"],
                "cells_covered": sum(1 for a in draws[n]["cell_audit"] if a["selected"]),
            }
            for n in (30, 50, 100)
        },
        "candidate_50": draws[50],
    }


def build_candidate_manifest_50(*, report: dict) -> dict:
    pairs = []
    for pair in report["candidate_50"]["selected"]:
        entry = dict(pair)
        raw_path = (
            _ROOT / "experiments" / "phase5" / "benchmarks" / "toolmaze"
            / "data" / pair["file_path"]
        )
        entry["task_sha256"] = (
            hashlib.sha256(raw_path.read_bytes()).hexdigest()
            if raw_path.is_file() else None
        )
        entry["condition_id"] = pair["task_id"]
        entry["cluster_id"] = pair["base_task_id"]
        pairs.append(entry)
    return build_formal_b0b1_manifest(
        experiment_id="phase5-b0b1-candidates-50-draft-001",
        pairs=pairs,
        repetitions=1,
        seed=report["selection_seed"],
        execution_status="DRAFT_CANDIDATE_PENDING_APPROVAL",
        notes=[
            "DRAFT candidate set for G5 approval — NOT approved, NOT executed.",
            "50 task-fault pairs over 50 distinct base tasks (cluster_id unique).",
            "Fault-recovery success rate denominator counts P1-P4 pairs only;",
            "per-trial fault trigger must be confirmed by evidence first.",
        ],
    )


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
    parser.add_argument(
        "--mode",
        choices=["qualification", "experiment", "isolation", "candidates"],
        required=True,
    )
    parser.add_argument("--task-id", default="C1_task_036_P4")
    args = parser.parse_args()

    manifests_dir = _ROOT / "experiments" / "phase5" / "manifests"
    readiness_dir = _ROOT / "experiments" / "phase5" / "readiness"
    readiness_dir.mkdir(parents=True, exist_ok=True)

    if args.mode == "candidates":
        report = build_candidates_report()
        report_out = readiness_dir / "b0b1_candidates_feasibility.json"
        if report_out.exists():
            raise SystemExit(f"refusing to overwrite existing report: {report_out}")
        report_out.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        manifest = build_candidate_manifest_50(report=report)
        out = manifests_dir / f"{manifest['experiment_id']}.json"
        if out.exists():
            raise SystemExit(f"refusing to overwrite existing manifest: {out}")
        out.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(json.dumps({
            "wrote": [str(report_out), str(out)],
            "feasibility": report["feasibility"],
            "manifest_hash": manifest["manifest_hash"],
        }, sort_keys=True))
        return

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
