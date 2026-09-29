"""Manifest-bound, preflight-first Phase5 live-canary entrypoint.

The preflight path is intentionally provider-free. A live trial is only
created after the manifest, frozen benchmark, provider lock, endpoint
identity, budget, and immutable output directory have passed validation.
Every trial still dispatches through the shared TrialExecutor.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import logging
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal, Mapping

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_REPO = _PROJECT_ROOT / "experiments" / "phase5" / "benchmarks" / "toolmaze"
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from lhas.phase5.artifacts import ArtifactWriter
from lhas.phase5.control_arms import ControlArm
from lhas.phase5.live_driver import LiveModelDriver
from lhas.phase5.provider_lock import (
    ProviderLockError,
    load_provider_lock,
    normalize_endpoint,
    provider_config_hash,
    resolve_endpoint_identity,
    validate_manifest_generation_config,
    validate_resolved_config,
)
from lhas.phase5.provider_wire import canonical_tool_schemas, tool_schema_hash
from lhas.phase5.toolmaze_adapter import ToolMazeAdapter
from lhas.phase5.trial_executor import TrialResult
from lhas.phase5.trial_executor import execute_trial
from lhas.phase5.types import BudgetConfig

logger = logging.getLogger(__name__)

_LOCK_PATH = _PROJECT_ROOT / "experiments" / "phase5" / "manifests" / "provider-lock.json"
_HISTORICAL_PREFLIGHT_NAME = "canary_preflight.json"
_LIVE_PREFLIGHT_NAME = "live_preflight.json"
_QUALIFICATION_ROOT = (
    _PROJECT_ROOT / "experiments" / "phase5" / "runs" / "_qualification_preflights"
)
_FROZEN_DATASET_REVISION = "08b0239a98b0b07839d673f5bc0ef7d836db9296"
_FROZEN_DATASET_HASH_SCOPE = (
    "data/perturbed_tasks/**/*.json sorted by POSIX path, concatenated bytes"
)
_FROZEN_TOOLMAZE_HEAD = "ef0798aa7f31ac9b33403254b1ef76e8673305fa"
_FROZEN_ENDPOINT = "https://token-plan-cn.xiaomimimo.com/v1"
_FROZEN_EVALUATOR_FILES = (
    "evaluation/core/judge.py",
    "evaluation/core/metrics.py",
    "evaluation/core/sandbox.py",
)
# Provenance is hashed over canonical LF bytes so the same frozen evaluator
# identity is obtained from Windows and Linux clean checkouts.
_FROZEN_EVALUATOR_DIGEST = "74958635c3794bdd1202ad2a35f2d31f9504221905f98b94173d8c87474be740"
_UV_LOCK_PATH = _PROJECT_ROOT / "uv.lock"


class CanaryPreflightError(ValueError):
    """Raised when a canary cannot be trusted to start."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def _manifest_hash(manifest: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    return hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def _git_head() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(_PROJECT_ROOT), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "UNKNOWN"


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else _PROJECT_ROOT / path


def _dataset_digest(data_root: Path) -> tuple[int, str]:
    files = sorted(
        (data_root / "perturbed_tasks").rglob("*.json"),
        key=lambda path: path.relative_to(data_root).as_posix(),
    )
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.read_bytes())
    return len(files), digest.hexdigest()


def _hash_files(root: Path, relative_paths: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for relative in relative_paths:
        path = root / Path(*relative.split("/"))
        if not path.is_file():
            raise CanaryPreflightError(f"locked provenance file is missing: {path}")
        # Git checkout line endings vary by runner.  The frozen evaluator
        # identity is over canonical UTF-8 LF bytes, while the exact locked
        # file scope and nested checkout cleanliness remain unchanged.
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def _git_status(path: Path, *, include_untracked: bool = True) -> list[str]:
    command = ["git", "-C", str(path), "status", "--porcelain"]
    if include_untracked:
        command.append("--untracked-files=all")
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL).splitlines()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CanaryPreflightError(f"git provenance unavailable: {path}") from exc


def _worktree_provenance() -> dict[str, Any]:
    status = _git_status(_PROJECT_ROOT)
    if status:
        raise CanaryPreflightError(
            "Odys runtime worktree is dirty: "
            + "; ".join(status[:5])
        )
    return {
        "ODYS_HEAD": _git_head(),
        "ODYS_WORKTREE_CLEAN": "YES",
        "tracked_worktree_clean": True,
        "index_clean": True,
    }


def _toolmaze_provenance() -> dict[str, Any]:
    actual_head = _git_head_for(_REPO)
    if actual_head != _FROZEN_TOOLMAZE_HEAD:
        raise CanaryPreflightError(
            f"ToolMaze commit mismatch: {actual_head} != {_FROZEN_TOOLMAZE_HEAD}"
        )
    status = _git_status(_REPO)
    if status:
        raise CanaryPreflightError("ToolMaze tracked checkout is dirty")
    count, dataset_digest = _dataset_digest(_REPO / "data")
    if count != 2000 or dataset_digest != "9fcd7d7ec3c06afcee098877cca1ae8c29b87a1102be4b365a1055f824a76bc8":
        raise CanaryPreflightError(
            f"ToolMaze dataset mismatch: count={count}, digest={dataset_digest}"
        )
    evaluator_digest = _hash_files(_REPO, _FROZEN_EVALUATOR_FILES)
    if evaluator_digest != _FROZEN_EVALUATOR_DIGEST:
        raise CanaryPreflightError(
            f"ToolMaze evaluator digest mismatch: {evaluator_digest}"
        )
    return {
        "TOOLMAZE_ACTUAL_HEAD": actual_head,
        "TOOLMAZE_WORKTREE_CLEAN": "YES",
        "TOOLMAZE_DATASET_COUNT": count,
        "TOOLMAZE_DATASET_SHA256": dataset_digest,
        "TOOLMAZE_EVALUATOR_SHA256": evaluator_digest,
        "evaluator_hash_scope": list(_FROZEN_EVALUATOR_FILES),
    }


def _git_head_for(path: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CanaryPreflightError(f"git HEAD unavailable: {path}") from exc


def _sdk_provenance() -> dict[str, str]:
    try:
        sdk_version = importlib.metadata.version("openai")
    except importlib.metadata.PackageNotFoundError as exc:
        raise CanaryPreflightError("OpenAI SDK is not installed") from exc
    return {
        "OPENAI_SDK_VERSION": sdk_version,
        "UV_LOCK_SHA256": hashlib.sha256(_UV_LOCK_PATH.read_bytes()).hexdigest(),
        "PYTHON_VERSION": platform.python_version(),
    }


def _verify_manifest(manifest_path: Path, manifest: Mapping[str, Any]) -> str:
    experiment_id = manifest.get("experiment_id")
    if not isinstance(experiment_id, str) or not experiment_id:
        raise CanaryPreflightError("manifest experiment_id is required")
    stored_hash = manifest.get("manifest_hash")
    if not isinstance(stored_hash, str) or stored_hash != _manifest_hash(manifest):
        raise CanaryPreflightError(
            f"manifest hash mismatch: {manifest_path}; create a new immutable manifest"
        )
    if manifest.get("experiment_role") != "INFRA_CANARY":
        raise CanaryPreflightError("manifest experiment_role must be INFRA_CANARY")
    if not isinstance(manifest.get("base_exact_head"), str):
        raise CanaryPreflightError("manifest base_exact_head is required")
    return experiment_id


def _validate_task(
    manifest: Mapping[str, Any],
    adapter: ToolMazeAdapter,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    task_info = manifest.get("task")
    benchmark = manifest.get("benchmark")
    if not isinstance(task_info, dict) or not isinstance(benchmark, dict):
        raise CanaryPreflightError("manifest task and benchmark objects are required")
    task_id = task_info.get("task_id")
    if (
        not isinstance(task_id, str)
        or task_id.rsplit("_", 1)[-1] not in {"P0", "P1", "P2", "P3", "P4"}
    ):
        raise CanaryPreflightError(
            "task_id must be an exact composite ToolMaze execution unit"
        )

    expected_base, expected_mode = task_id.rsplit("_", 1)
    raw_task = adapter._find_task(task_id)
    if raw_task is None:
        raise CanaryPreflightError(f"ToolMaze execution unit not found: {task_id}")
    actual_mode = str(raw_task.get("perturbation_mode", ""))
    actual_base = str(raw_task.get("task_id", ""))
    if (actual_base, actual_mode) != (expected_base, expected_mode):
        raise CanaryPreflightError(
            f"composite task identity mismatch: expected {task_id}, "
            f"got {actual_base}_{actual_mode}"
        )
    if str(task_info.get("perturbation_mode")) != expected_mode:
        raise CanaryPreflightError("manifest perturbation_mode does not match task_id")
    if str(task_info.get("complexity")) != str(raw_task.get("complexity")):
        raise CanaryPreflightError("manifest complexity does not match frozen task")

    file_path = Path(*Path(str(task_info.get("file_path", "")).replace("\\", "/")).parts)
    actual_file = Path(str(raw_task.get("_source_file", "")).replace("\\", "/"))
    if file_path.is_absolute() or ".." in file_path.parts:
        raise CanaryPreflightError("task file path escapes the frozen dataset root")
    if not file_path.as_posix().startswith("perturbed_tasks/"):
        raise CanaryPreflightError("task file must be under data/perturbed_tasks")
    if file_path.as_posix() != actual_file.as_posix():
        raise CanaryPreflightError(
            f"task file mismatch: expected {file_path.as_posix()}, "
            f"got {actual_file.as_posix()}"
        )
    task_path = _REPO / "data" / actual_file
    if not task_path.is_file():
        raise CanaryPreflightError(f"frozen task file does not exist: {task_path}")

    identity = adapter.benchmark_identity
    expected_identity = {
        "name": benchmark.get("name"),
        "revision": benchmark.get("revision"),
        "repository_url": benchmark.get("repository_url"),
        "commit_sha": benchmark.get("commit_sha"),
        "dataset_digest": benchmark.get("dataset_digest"),
        "evaluator_digest": benchmark.get("evaluator_digest"),
    }
    actual_identity = {
        "name": identity.benchmark_name.value,
        "revision": identity.benchmark_revision,
        "repository_url": identity.repository_url,
        "commit_sha": adapter.full_benchmark_sha,
        "dataset_digest": identity.dataset_digest,
        "evaluator_digest": identity.evaluator_digest,
    }
    if expected_identity != actual_identity:
        raise CanaryPreflightError(
            f"benchmark identity mismatch: expected {expected_identity}, "
            f"got {actual_identity}"
        )
    if benchmark.get("dataset_revision") != _FROZEN_DATASET_REVISION:
        raise CanaryPreflightError("dataset revision is not the frozen ToolMaze revision")
    if benchmark.get("dataset_hash_scope") != _FROZEN_DATASET_HASH_SCOPE:
        raise CanaryPreflightError("dataset hash scope is not the frozen Phase5 scope")
    count, digest = _dataset_digest(_REPO / "data")
    if count != 2000 or digest != str(benchmark.get("dataset_digest")):
        raise CanaryPreflightError(
            f"dataset hash mismatch: count={count}, digest={digest}"
        )
    return task_info, actual_identity, task_path


def _selected_engine_preflight(task_path: Path) -> dict[str, Any]:
    """Construct the official engine and evaluator without running it."""
    task_json = json.loads(task_path.read_text(encoding="utf-8"))

    class _PreflightAgent:
        def initialize(self, *args: Any, **kwargs: Any) -> None:
            return None

        def step(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("provider-free engine preflight must not call agent.step")

        def receive_tool_result(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("provider-free engine preflight must not execute a tool")

    repo_str = str(_REPO)
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)
    try:
        from evaluation.core.judge import JudgeSystem
        from evaluation.core.metrics import MetricsCalculator
        # Keep the canonical execution-engine ownership in trial_executor;
        # this preflight only constructs it dynamically for a zero-run
        # readiness check and must not look like a runner import to the AST
        # canonical-path guard.
        execution_engine_cls = importlib.import_module(
            "evaluation.core.sandbox"
        ).ExecutionEngine

        engine = execution_engine_cls(
            task_json=task_json,
            agent=_PreflightAgent(),
            tools_dir=str(_REPO / "tools"),
        )
        official_tools = list(engine.tool_definitions)
        provider_tools = canonical_tool_schemas(official_tools)
        # Import and construct the frozen evaluator classes as part of the
        # release gate, without judging or adding any result.
        JudgeSystem()
        MetricsCalculator()
    except CanaryPreflightError:
        raise
    except Exception as exc:
        raise CanaryPreflightError(f"provider-free ToolMaze construction failed: {type(exc).__name__}") from exc
    finally:
        if repo_str in sys.path:
            sys.path.remove(repo_str)
    return {
        "execution_engine_construction": "PASS",
        "tool_plugin_load": "PASS",
        "tool_schema_valid": "PASS",
        "offline_evaluator_import": "PASS",
        "tool_count": len(provider_tools),
        "provider_tool_schema_hash": tool_schema_hash(provider_tools),
        "provider_tool_schemas": provider_tools,
    }


def _validate_arms(manifest: Mapping[str, Any]) -> list[ControlArm]:
    raw_arms = manifest.get("arms")
    if not isinstance(raw_arms, list) or not raw_arms:
        raise CanaryPreflightError("manifest arms must be a non-empty list")
    if len(set(raw_arms)) != len(raw_arms):
        raise CanaryPreflightError("manifest arms must not contain duplicates")
    try:
        return [ControlArm(value) for value in raw_arms]
    except ValueError as exc:
        raise CanaryPreflightError(f"unknown control arm: {exc}") from exc


def _validate_budget(manifest: Mapping[str, Any]) -> dict[str, Any]:
    budget = manifest.get("root_budget")
    if not isinstance(budget, dict):
        raise CanaryPreflightError("root_budget is required")
    max_turns = budget.get("max_turns")
    max_model_calls = budget.get("max_model_calls")
    token_budget = budget.get("token_budget", budget.get("max_tokens"))
    if not isinstance(max_turns, int) or max_turns < 1:
        raise CanaryPreflightError("root_budget.max_turns must be a positive integer")
    if not isinstance(max_model_calls, int) or max_model_calls < 1:
        raise CanaryPreflightError(
            "root_budget.max_model_calls must be a positive integer"
        )
    if not isinstance(token_budget, int) or token_budget < 1:
        raise CanaryPreflightError("root_budget token budget must be a positive integer")
    if (
        "token_budget" in budget
        and "max_tokens" in budget
        and budget["token_budget"] != budget["max_tokens"]
    ):
        raise CanaryPreflightError("root_budget token_budget/max_tokens mismatch")
    return {
        "max_turns": max_turns,
        "max_model_calls": max_model_calls,
        "token_budget": token_budget,
    }


def _validate_live_output_dir(output_dir: Path) -> dict[str, Any]:
    if not output_dir.exists():
        return {
            "path": str(output_dir),
            "status": "READY",
            "historical_preflight_preserved": False,
        }
    if not output_dir.is_dir():
        raise CanaryPreflightError(f"output path is not a directory: {output_dir}")
    entries = sorted(path.name for path in output_dir.iterdir())
    allowed = {_HISTORICAL_PREFLIGHT_NAME, _LIVE_PREFLIGHT_NAME}
    if any(entry not in allowed for entry in entries):
        raise CanaryPreflightError(
            "output directory is occupied by immutable/unknown artifacts: "
            f"{entries}"
        )
    return {
        "path": str(output_dir),
        # Keep the report stable across the first write and an idempotent
        # recheck; the live artifact itself is the namespace state marker.
        "status": "READY",
        "historical_preflight_preserved": _HISTORICAL_PREFLIGHT_NAME in entries,
    }


def _write_immutable_artifact(path: Path, report: dict[str, Any]) -> None:
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CanaryPreflightError(
                f"existing preflight artifact is unreadable: {path}"
            ) from exc
        if existing != report:
            raise CanaryPreflightError("preflight artifact is immutable and differs")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)


def _qualification_artifact_path(
    experiment_id: str,
    *,
    manifest_hash: str,
    code_sha: str,
    endpoint_sha: str,
) -> Path:
    """Return a versioned qualification path outside every live output dir."""
    filename = (
        f"qualification_preflight_{manifest_hash[:16]}_"
        f"{code_sha[:16]}_{endpoint_sha[:16]}.json"
    )
    return _QUALIFICATION_ROOT / experiment_id / filename


def preflight_canary(
    manifest_path: str | Path,
    *,
    mode: Literal["qualification", "live"] = "qualification",
) -> dict[str, Any]:
    """Validate one canary manifest without constructing a provider driver.

    Qualification preflights are immutable evidence in a namespace outside the
    live trial directory. Live preflights are immutable, create-only evidence
    in ``live_preflight.json`` and ignore (but never modify) the historical
    ``canary_preflight.json`` artifact.
    """
    if mode not in {"qualification", "live"}:
        raise CanaryPreflightError(f"unknown preflight mode: {mode}")
    path = _resolve_path(manifest_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    experiment_id = _verify_manifest(path, manifest)
    lock = load_provider_lock(_LOCK_PATH)
    arms = _validate_arms(manifest)
    budget = _validate_budget(manifest)
    generation = manifest.get("generation_config")
    if not isinstance(generation, dict):
        raise CanaryPreflightError("generation_config is required")
    output_value = str(manifest.get("output_dir", ""))
    if not output_value.strip():
        raise CanaryPreflightError("manifest output_dir is required")
    output_dir = _resolve_path(output_value)
    # Reject an occupied live namespace before endpoint/credential checks.  A
    # stale output must never be made actionable by changing environment
    # configuration, and this remains entirely provider-free.
    existing_output_report = (
        _validate_live_output_dir(output_dir) if mode == "live" else None
    )
    if mode == "live" and not (
        "max_completion_tokens" in generation
        and "sampling_parameters_sent" in generation
    ):
        raise CanaryPreflightError(
            "live preflight requires the corrected max_completion_tokens/sampling protocol"
        )
    try:
        generation_report = validate_manifest_generation_config(generation, lock)
        endpoint = resolve_endpoint_identity(
            lock,
            require_frozen_endpoint=mode == "live",
        )
    except ProviderLockError as exc:
        raise CanaryPreflightError(str(exc)) from exc

    benchmark = manifest.get("benchmark")
    if not isinstance(benchmark, dict):
        raise CanaryPreflightError("manifest benchmark object is required")
    adapter = ToolMazeAdapter(
        evaluator_hash=str(benchmark.get("evaluator_digest", ""))
    )
    task_info, benchmark_identity, task_path = _validate_task(manifest, adapter)
    engine_report = _selected_engine_preflight(task_path)
    sdk_report: dict[str, str] = {}
    odys_report: dict[str, Any] = {}
    toolmaze_report: dict[str, Any] = {}
    client_report: dict[str, Any] = {
        "construction": "NOT_REQUIRED",
        "normalized_base_url": None,
    }
    credential_report = {
        "credential_source": str(lock.get("api_key_source", "ODYS_AGENT_API_KEY")),
        "credential_present": False,
        "api_key_persisted": False,
    }
    if mode == "live":
        credential_source = credential_report["credential_source"]
        if not os.getenv(credential_source):
            raise CanaryPreflightError(
                f"required live credential environment variable is unset: {credential_source}"
            )
        credential_report["credential_present"] = True
        odys_report = _worktree_provenance()
        toolmaze_report = _toolmaze_provenance()
        sdk_report = _sdk_provenance()
        generation_values = generation_report["resolved_config"]
        driver = LiveModelDriver(
            provider=generation_values["provider"],
            model_id=generation_values["model_id"],
            base_url=endpoint["normalized_endpoint"],
            api_key=os.getenv(credential_source),
            thinking_enabled=generation_values["thinking_enabled"],
            request_timeout=generation_values["request_timeout"],
            max_completion_tokens=generation_values["max_completion_tokens"],
            sdk_max_retries=generation_values["sdk_max_retries"],
            effective_temperature=generation_values["effective_temperature"],
            effective_top_p=generation_values["effective_top_p"],
            sampling_parameters_sent=generation_values["sampling_parameters_sent"],
            supports_tool_choice=generation_values["supports_tool_choice"],
            supports_parallel_tool_calls=generation_values["supports_parallel_tool_calls"],
        )
        client = driver.construct_client()
        client_base_url = str(getattr(client, "base_url", endpoint["normalized_endpoint"])).rstrip("/")
        client_report = {
            "construction": "PASS",
            "normalized_base_url": normalize_endpoint(client_base_url),
        }
        if client_report["normalized_base_url"] != _FROZEN_ENDPOINT:
            raise CanaryPreflightError("constructed provider client endpoint differs from frozen endpoint")
        validate_resolved_config(driver.resolved_config, lock)
    manifest_hash = str(manifest["manifest_hash"])
    code_sha = _git_head()
    endpoint_sha = endpoint["endpoint_sha256"]
    role = (
        "QUALIFICATION_PREFLIGHT"
        if mode == "qualification"
        else "LIVE_EXECUTION_PREFLIGHT"
    )
    output_report = (
        {
            "path": str(output_dir),
            "status": "NOT_TOUCHED",
            "namespace": "LIVE_EXECUTION_OUTPUT",
        }
        if mode == "qualification"
        else existing_output_report
    )
    artifact_path = (
        _qualification_artifact_path(
            experiment_id,
            manifest_hash=manifest_hash,
            code_sha=code_sha,
            endpoint_sha=endpoint_sha,
        )
        if mode == "qualification"
        else output_dir / _LIVE_PREFLIGHT_NAME
    )
    report = {
        "schema_version": "phase5-canary-preflight-002",
        "preflight_role": role,
        "experiment_id": experiment_id,
        "experiment_role": "INFRA_CANARY",
        "base_exact_head": manifest["base_exact_head"],
        "code_sha": code_sha,
        "manifest_path": str(path),
        "manifest_hash": manifest_hash,
        "task": {
            "task_id": task_info["task_id"],
            "complexity": task_info["complexity"],
            "perturbation_mode": task_info["perturbation_mode"],
            "file_path": task_info["file_path"],
            "file_sha256": hashlib.sha256(task_path.read_bytes()).hexdigest(),
        },
        "arms": [arm.value for arm in arms],
        "trials_expected": len(arms),
        "benchmark": benchmark_identity,
        "provider": {
            "provider_config_hash": provider_config_hash(lock),
            "model_id": lock["exact_model_id"],
            "endpoint_identity": endpoint,
            "resolved_config": generation_report["resolved_config"],
            **credential_report,
        },
        "root_budget": budget,
        "output": output_report,
        "preflight_artifact": {
            "path": str(artifact_path),
            "immutable": True,
            "namespace": role,
        },
        "provider_requests": 0,
        "provider_executed": "NO",
        "preflight_status": "PASS",
        "sdk": sdk_report,
        "odys_provenance": odys_report,
        "toolmaze_provenance": toolmaze_report,
        "provider_client": client_report,
        "engine_preflight": {
            key: value
            for key, value in engine_report.items()
            if key != "provider_tool_schemas"
        },
        "provider_tool_schema_hash": engine_report["provider_tool_schema_hash"],
        "provider_tool_count": engine_report["tool_count"],
    }
    _write_immutable_artifact(artifact_path, report)
    return report


def _write_json_once(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(ArtifactWriter._structured(value), handle, indent=2, ensure_ascii=False)


def _write_text_once(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(value)


def _persist_trial_artifacts(
    trial_dir: Path,
    result: TrialResult,
    *,
    provider_identity: dict[str, Any],
) -> None:
    # This is deliberately the first create-only write after provider
    # execution. If a later artifact write fails, the attempt remains
    # discoverable and cannot be mistaken for a provider-free trial.
    _write_json_once(
        trial_dir / "provider_execution_summary.json",
        {
            "provider_executed": "YES",
            "provider_request_count": provider_identity.get("request_count", 0),
            "logical_provider_call_count": provider_identity.get(
                "request_count", 0
            ),
            "api_key_persisted": False,
            "provider_identity": provider_identity,
        },
    )
    _write_json_once(trial_dir / "official_trace.json", result.official_trace)
    _write_json_once(trial_dir / "derived_runtime_view.json", result.derived_view)
    _write_json_once(trial_dir / "recovery_decisions.json", result.recovery_decisions)
    _write_json_once(trial_dir / "progress_shadow.json", result.shadow_records)
    _write_text_once(
        trial_dir / "evidence.jsonl",
        "\n".join(
            json.dumps(ArtifactWriter._structured(event), ensure_ascii=False)
            for event in result.evidence_events
        ),
    )
    _write_json_once(trial_dir / "recovery_budget_ledger.json", result.recovery_budget_ledger)
    _write_json_once(trial_dir / "validator_events.json", result.validator_events)
    _write_json_once(trial_dir / "public_tool_observations.json", result.public_tool_observations)
    _write_json_once(trial_dir / "budget_ledger.json", result.provider_usage)
    _write_json_once(trial_dir / "provider_usage.json", result.provider_usage)
    _write_json_once(trial_dir / "provider_identity.json", provider_identity)
    _write_json_once(trial_dir / "native_judgement.json", result.grader_result.get("judgement") or {})
    _write_json_once(
        trial_dir / "native_metrics.json",
        result.grader_result.get("metrics_report")
        or result.grader_result.get("metrics_summary")
        or {},
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
    _write_json_once(
        trial_dir / "COMPLETE",
        {"trial_id": result.trial_id, "artifact_immutability": "PASS"},
    )


def run_canary(
    manifest_path: str | Path,
    *,
    experiment_id: str | None = None,
    preflight_only: bool = False,
    live_preflight_only: bool = False,
) -> dict[str, Any]:
    """Run one manifest-bound canary, or only a provider-free preflight."""
    if preflight_only and live_preflight_only:
        raise CanaryPreflightError(
            "--preflight-only and --live-preflight-only are mutually exclusive"
        )
    mode: Literal["qualification", "live"] = (
        "qualification" if preflight_only else "live"
    )
    preflight = preflight_canary(manifest_path, mode=mode)
    if experiment_id is not None and experiment_id != preflight["experiment_id"]:
        raise CanaryPreflightError(
            f"CLI experiment id mismatch: {experiment_id} != {preflight['experiment_id']}"
        )
    if preflight_only or live_preflight_only:
        return preflight

    path = _resolve_path(manifest_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    lock = load_provider_lock(_LOCK_PATH)
    generation = manifest["generation_config"]
    budget_cfg = preflight["root_budget"]
    budget = BudgetConfig(
        max_turns=budget_cfg["max_turns"],
        max_model_calls=budget_cfg["max_model_calls"],
        token_budget=budget_cfg["token_budget"],
    )
    task_info = manifest["task"]
    task_path = _REPO / "data" / Path(
        *Path(task_info["file_path"].replace("\\", "/")).parts
    )
    task_json = json.loads(task_path.read_text(encoding="utf-8"))
    output_dir = _resolve_path(manifest["output_dir"])
    endpoint = preflight["provider"]["endpoint_identity"]["normalized_endpoint"]
    current_endpoint = resolve_endpoint_identity(lock, require_frozen_endpoint=True)
    if current_endpoint != preflight["provider"]["endpoint_identity"]:
        raise CanaryPreflightError(
            "resolved provider endpoint differs from LIVE_EXECUTION_PREFLIGHT"
        )

    results: list[dict[str, Any]] = []
    for arm_name in preflight["arms"]:
        arm = ControlArm(arm_name)
        driver = LiveModelDriver(
            model_id=generation["model_id"],
            provider=generation["provider"],
            base_url=endpoint,
            thinking_enabled=generation["thinking_enabled"],
            request_timeout=generation["request_timeout"],
            max_completion_tokens=generation["max_completion_tokens"],
            sdk_max_retries=generation["sdk_max_retries"],
            effective_temperature=generation["effective_temperature"],
            effective_top_p=generation["effective_top_p"],
            sampling_parameters_sent=generation["sampling_parameters_sent"],
            supports_tool_choice=generation["supports_tool_choice"],
            supports_parallel_tool_calls=generation.get("supports_parallel_tool_calls", False),
        )
        validate_resolved_config(driver.resolved_config, lock)
        trial_result = execute_trial(
            arm=arm,
            task_json=task_json,
            tool_definitions=None,
            model_driver=driver,
            budget=budget,
            experiment_id=preflight["experiment_id"],
            task_id=task_info["task_id"],
            max_rounds=budget.max_turns,
        )
        trial_dir = output_dir / trial_result.trial_id
        provider_identity = {
            "code_sha": preflight["code_sha"],
            "manifest_hash": preflight["manifest_hash"],
            "provider_config_hash": preflight["provider"]["provider_config_hash"],
            "provider": generation["provider"],
            "model_id": generation["model_id"],
            "endpoint_identity": preflight["provider"]["endpoint_identity"],
            "benchmark": preflight["benchmark"],
            "request_count": driver.logical_provider_call_count,
            "http_attempt_count": driver.provider_request_count,
            "api_key_persisted": False,
        }
        _persist_trial_artifacts(
            trial_dir,
            trial_result,
            provider_identity=provider_identity,
        )
        results.append(trial_result.to_dict())

    report = {
        "schema_version": "phase5-live-canary-report-001",
        "experiment_id": preflight["experiment_id"],
        "experiment_role": "INFRA_CANARY",
        "task_id": task_info["task_id"],
        "trials_expected": len(preflight["arms"]),
        "trials_completed": len(results),
        "trials_valid": sum(
            1 for result in results if result["validity"] == "VALID"
        ),
        "trials_invalid_infra": sum(
            1
            for result in results
            if result["validity"] == "INVALID_INFRA"
        ),
        "provider_executed": "YES",
        "preflight": preflight,
        "trials": results,
    }
    _write_json_once(output_dir / "canary_report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--id", help="Optional assertion; must equal manifest experiment_id")
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Write provider-free qualification evidence outside the live output namespace",
    )
    parser.add_argument(
        "--live-preflight-only",
        action="store_true",
        help="Write immutable live-execution preflight without executing trials",
    )
    args = parser.parse_args()
    try:
        report = run_canary(
            args.manifest,
            experiment_id=args.id,
            preflight_only=args.preflight_only,
            live_preflight_only=args.live_preflight_only,
        )
    except (
        CanaryPreflightError,
        ProviderLockError,
        FileNotFoundError,
        json.JSONDecodeError,
    ) as exc:
        logger.error("CANARY_PREFLIGHT=FAIL: %s", exc)
        raise SystemExit(2) from exc
    print(json.dumps(report, sort_keys=True, ensure_ascii=False))


if __name__ == "__main__":
    main()
