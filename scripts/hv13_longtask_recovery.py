"""Controlled HV-1.3 long-task process-recovery validation.

The default command is an offline deterministic process-boundary run over the
same session-lifecycle fixture used by HV12.  Live execution is deliberately
explicit, one-shot, and configuration-gated.  This module never starts a live
model merely by being imported.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any

# Direct ``python scripts/hv13_longtask_recovery.py`` execution places only
# ``scripts/`` on sys.path; add the repository root so the shared historical
# helper module remains importable without making the script stateful.
_SCRIPT_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_SCRIPT_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_REPO_ROOT))

from lhas import HARNESS_VERSION
from lhas.checkpoint import CheckpointRepository
from lhas.domain.enums import AttemptStatus, EventType, RunStatus, TaskStatus
from lhas.domain.models import Project
from lhas.executors.protocol import ExecutionRequest, ExecutionResult
from lhas.failure import RuleFailureClassifier
from lhas.inner_agent import AgentsSdkModelConfig
from lhas.orchestrator_v2 import RecoveringOrchestrator
from lhas.persistence.database import Database
from lhas.persistence.event_store import EventStore
from lhas.persistence.phaseb_repos import (
    ContextSnapshotRepository,
    FailureReportRepository,
    RecoveryActionRepository,
    ValidationResultRepository,
)
from lhas.persistence.repositories import (
    AttemptRepository,
    ProjectRepository,
    RunRepository,
    TaskRepository,
    WorkspaceSessionBindingRepository,
)
from lhas.task_service import create_task
from lhas.workspace import RunWorkspaceManager, tree_sha256

from scripts.hv12_longtask_recovery import (
    CAPABILITIES,
    FIXTURE_ROOT,
    FixturePytestValidator,
    ExecutorCallRecorder,
    _child_env,
    _diff_summary,
    _live_executor_factory,
    _local_run_id,
    _local_session_root,
    _poll_for_crash_trigger,
    _pytest,
    _read_snapshot,
    _read_worker_error,
    _read_worker_summary,
    _replace_if_present,
    _terminate_process,
    _workspace_state,
    _write_evidence,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DRY_OUTPUT = REPO_ROOT / "evals" / "runs" / "HV13-DRY-001.json"
DEFAULT_LIVE_OUTPUT = REPO_ROOT / "evals" / "runs" / "HV13-LIVE-001.json"
DEFAULT_LIVE_CLAIM = REPO_ROOT / "evals" / "runs" / "HV13-LIVE-001.claim.json"
PHASE = "HV13_LONGTASK_VALIDATION"
EXPERIMENT_ID = "HV13-LONGTASK-VALIDATION"
FIXTURE_VERSION = "HV12-SESSION-LIFECYCLE-1"
COMPARISON_BASELINE_ID = "HV12-LIVE-001"
COMPARISON_BASELINE_HARNESS = "HV-1.2"
MAX_ATTEMPTS = 3
INNER_TURN_BUDGET = 20
SIDE_EFFECT_CAPABILITIES = ["workspace.edit"]
EXPECTED_HARNESS_VERSION = "HV-1.3"


class DryRunCrashExecutor:
    name = "HV13DeterministicProcessRecoveryExecutor-A"

    def __init__(self, workspace, recorder: ExecutorCallRecorder):
        self.workspace = workspace
        self.recorder = recorder

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.recorder.record(request.attempt_number)
        await _replace_if_present(
            self.workspace,
            "src/session_store.py",
            "self._cache: dict[str, Session] = {}",
            "self._cache: dict[tuple[str, str], Session] = {}",
        )
        await _replace_if_present(
            self.workspace,
            "src/session_store.py",
            "self._cache.get(session_id)",
            "self._cache.get(key)",
        )
        await _replace_if_present(
            self.workspace,
            "src/session_store.py",
            "self._cache[session_id] = session",
            "self._cache[key] = session",
        )
        # The parent terminates this actual child process after a stable edit.
        await asyncio.Event().wait()


class DryRunResumeExecutor:
    name = "HV13DeterministicProcessRecoveryExecutor-B"

    def __init__(self, workspace, recorder: ExecutorCallRecorder):
        self.workspace = workspace
        self.recorder = recorder

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.recorder.record(request.attempt_number)
        await _replace_if_present(
            self.workspace,
            "src/message_router.py",
            "        return service.create_session(tenant_id, session_id, message)",
            "        return None",
        )
        # Deliberately leave the useful patch durable but return a non-success
        # executor result.  HV-1.3 must arbitrate this outcome with the
        # validator instead of starting Attempt 3.
        return ExecutionResult(
            status="FAILURE",
            output="deterministic repair complete; turn budget exhausted",
            error_type="AGENT_TURN_LIMIT",
            error_message="deterministic turn limit after durable repair",
        )


def _task_objective() -> tuple[str, list[str], list[str]]:
    objective = (
        "Repair the synthetic tenant-scoped session lifecycle repository. "
        "Trace behavior across models.py, session_store.py, session_service.py, "
        "and message_router.py. Preserve normal active/new-session behavior."
    )
    constraints = [
        "Use only the allow-listed workspace capabilities.",
        "The only allowed side effect is workspace.edit.",
        "Use cli.exec only for pytest commands; no shell, git mutation, or network.",
        "Do not modify the immutable source repository; validate the durable work tree.",
    ]
    acceptance = [
        "pytest passes in the durable workspace.",
        "Same session_id values are isolated by tenant.",
        "Delayed messages do not recreate deleted/tombstoned sessions.",
        "Active and new sessions continue to accept messages.",
    ]
    return objective, constraints, acceptance


def _new_orchestrator(
    db: Database,
    *,
    task,
    sessions_root: Path,
    mode: str,
    worker: str,
):
    manager = RunWorkspaceManager(db, sessions_root)
    validator = FixturePytestValidator(sessions_root)
    recorder = ExecutorCallRecorder()
    if mode == "deterministic_process_recovery_fixture":
        if worker == "a":
            factory = lambda workspace: DryRunCrashExecutor(workspace, recorder)
        else:
            factory = lambda workspace: DryRunResumeExecutor(workspace, recorder)
        executor_type = "HV13DeterministicProcessRecoveryExecutor"
        provider = "deterministic"
        model = "scripted-process-recovery"
    else:
        config = AgentsSdkModelConfig(provider_profile="mimo", api_mode="chat_completions")
        factory = lambda workspace: _live_executor_factory(workspace, db, config, recorder)
        executor_type = "InnerAgentExecutor"
        provider = "mimo"
        model = config.model or ""

    orchestrator = RecoveringOrchestrator(
        db,
        workspace_executor_factory=factory,
        workspace_manager=manager,
        validator=validator,
        classifier=RuleFailureClassifier(),
        harness_version=HARNESS_VERSION,
        context_policy_version="CP-2",
        executor_type=executor_type,
        provider=provider,
        model=model,
        dataset_version=FIXTURE_VERSION,
        experiment_id=EXPERIMENT_ID,
    )
    return orchestrator, validator, recorder


def _worker_a(args: argparse.Namespace) -> int:
    db = Database(args.db_path)
    db.init_db()
    try:
        task = TaskRepository(db).get(args.task_id)
        if task is None:
            return 2
        orchestrator, _validator, _recorder = _new_orchestrator(
            db,
            task=task,
            sessions_root=Path(args.sessions_root),
            mode=args.mode,
            worker="a",
        )
        asyncio.run(orchestrator.execute_task(task.id))
        return 0
    except Exception as exc:
        Path(args.control_dir).mkdir(parents=True, exist_ok=True)
        (Path(args.control_dir) / "process-a-error.json").write_text(
            json.dumps({"error_type": type(exc).__name__}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return 1
    finally:
        db.close()


def _worker_b(args: argparse.Namespace) -> int:
    db = Database(args.db_path)
    db.init_db()
    validator: FixturePytestValidator | None = None
    recorder: ExecutorCallRecorder | None = None
    try:
        run = RunRepository(db).get(args.run_id)
        if run is None:
            return 2
        task = TaskRepository(db).get(run.task_id)
        if task is None:
            return 2
        orchestrator, validator, recorder = _new_orchestrator(
            db,
            task=task,
            sessions_root=Path(args.sessions_root),
            mode=args.mode,
            worker="b",
        )
        resumed = asyncio.run(orchestrator.resume_run(run.id))
        summary = {
            "run_status": resumed.status.value,
            "validator_calls": validator.calls if validator else None,
            "executor_calls": list(recorder.attempt_numbers) if recorder else [],
        }
        Path(args.control_dir).mkdir(parents=True, exist_ok=True)
        (Path(args.control_dir) / "process-b-summary.json").write_text(
            json.dumps(summary, sort_keys=True) + "\n", encoding="utf-8"
        )
        return 0
    except Exception as exc:
        Path(args.control_dir).mkdir(parents=True, exist_ok=True)
        (Path(args.control_dir) / "process-b-error.json").write_text(
            json.dumps({"error_type": type(exc).__name__}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return 1
    finally:
        db.close()


def _spawn_worker(role: str, args: argparse.Namespace, *, run_id: str | None = None) -> subprocess.Popen:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--role",
        f"worker-{role}",
        "--mode",
        args.mode,
        "--db",
        str(args.db_path),
        "--task-id",
        args.task_id,
        "--sessions-root",
        str(args.sessions_root),
        "--control-dir",
        str(args.control_dir),
    ]
    if run_id:
        command.extend(["--run-id", run_id])
    return subprocess.Popen(
        command,
        cwd=str(REPO_ROOT),
        env=_child_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _arbitration_observation(db: Database, run_id: str) -> dict[str, Any]:
    events = EventStore(db).list_for_run(run_id)
    attempts = AttemptRepository(db).list_for_run(run_id)
    by_id = {attempt.id: attempt for attempt in attempts}
    event = next(
        (
            event
            for event in events
            if event.event_type is EventType.VALIDATION_STARTED
            and event.payload.get("outcome_arbitration") is True
        ),
        None,
    )
    if event is None or event.attempt_id not in by_id:
        return {
            "outcome_arbitration_observed": False,
            "outcome_arbitration_attempt_number": None,
            "outcome_arbitration_executor_status": None,
            "outcome_arbitration_error_type": None,
            "outcome_arbitration_validation_passed": None,
            "next_attempt_suppressed_after_arbitration": None,
            "attempts_after_arbitration": None,
        }
    attempt = by_id[event.attempt_id]
    validation = ValidationResultRepository(db).get_for_attempt(attempt.id)
    later_attempts = [item for item in attempts if item.attempt_number > attempt.attempt_number]
    return {
        "outcome_arbitration_observed": True,
        "outcome_arbitration_attempt_number": attempt.attempt_number,
        "outcome_arbitration_executor_status": event.payload.get("executor_attempt_status"),
        "outcome_arbitration_error_type": event.payload.get("executor_error_type"),
        "outcome_arbitration_validation_passed": validation.passed if validation else None,
        "next_attempt_suppressed_after_arbitration": not later_attempts,
        "attempts_after_arbitration": len(later_attempts),
    }


def _attempt_summaries(db: Database, run_id: str) -> list[dict[str, Any]]:
    attempts = AttemptRepository(db).list_for_run(run_id)
    validations = ValidationResultRepository(db)
    return [
        {
            "attempt_number": attempt.attempt_number,
            "status": attempt.status.value,
            "error_type": attempt.error_type,
            "validation_persisted": validations.get_for_attempt(attempt.id) is not None,
            "validation_passed": (
                validations.get_for_attempt(attempt.id).passed
                if validations.get_for_attempt(attempt.id) is not None
                else None
            ),
        }
        for attempt in attempts
    ]


def _live_config_is_valid() -> bool:
    if not os.getenv("ODYS_AGENT_MODEL") or not os.getenv("ODYS_AGENT_API_KEY"):
        return False
    try:
        config = AgentsSdkModelConfig(provider_profile="mimo", api_mode="chat_completions")
        config.validate()
    except (TypeError, ValueError):
        return False
    return True


def _claim_path_for(output_path: Path) -> Path:
    if output_path == DEFAULT_LIVE_OUTPUT:
        return DEFAULT_LIVE_CLAIM
    return output_path.with_name(f"{output_path.stem}.claim.json")


def _task_description() -> dict[str, Any]:
    objective, constraints, acceptance = _task_objective()
    return {"objective": objective, "constraints": constraints, "acceptance_criteria": acceptance}


def run_evaluation(
    *,
    mode: str = "deterministic_process_recovery_fixture",
    output_path: Path | None = None,
    claim_path: Path | None = None,
    observation_timeout: float = 30.0,
    poll_interval: float = 0.2,
    resume_timeout: float = 180.0,
) -> dict[str, Any]:
    if output_path is None:
        output_path = DEFAULT_LIVE_OUTPUT if mode == "live_real_model" else DEFAULT_DRY_OUTPUT
    output_path = Path(output_path)
    if mode == "live_real_model":
        claim_path = Path(claim_path) if claim_path is not None else _claim_path_for(output_path)
        if output_path.exists():
            return {"status": "LIVE_RESULT_EXISTS", "mode": mode, "live_run_executed": False}
        if claim_path.exists():
            return {"status": "LIVE_CLAIM_EXISTS", "mode": mode, "live_run_executed": False}
        if HARNESS_VERSION != EXPECTED_HARNESS_VERSION:
            return {"status": "HARNESS_MISMATCH", "mode": mode, "live_run_executed": False}
        if not _live_config_is_valid():
            return {"status": "SKIPPED_CONFIG", "mode": mode, "live_run_executed": False}
        git_sha = _git_sha()
        try:
            _write_evidence(
                claim_path,
                {
                    "evaluation_id": "HV13-LIVE-001",
                    "git_sha": git_sha,
                    "harness_version": HARNESS_VERSION,
                    "execution_claimed": True,
                },
                exclusive=True,
            )
        except FileExistsError:
            return {"status": "LIVE_CLAIM_EXISTS", "mode": mode, "live_run_executed": False}

    started = time.monotonic()
    git_sha, code_commit_clean = _git_identity()
    fixture_source_sha = tree_sha256(FIXTURE_ROOT)
    with tempfile.TemporaryDirectory(prefix="hv13-longtask-", ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        source_root = root / "source"
        sessions_root = root / "sessions"
        control_dir = root / "control"
        shutil.copytree(FIXTURE_ROOT, source_root)
        source_snapshot_sha_before = tree_sha256(source_root)
        initial_tests = _pytest(source_root)
        db_path = root / "run.sqlite"
        db = Database(db_path)
        db.init_db()
        try:
            project = ProjectRepository(db).create(
                Project(name="hv13-session-lifecycle", root_path=str(source_root))
            )
            task_info = _task_description()
            task = create_task(
                db,
                project_id=project.id,
                title="HV-1.3 long-task process recovery validation",
                objective=task_info["objective"],
                constraints=task_info["constraints"],
                acceptance_criteria=task_info["acceptance_criteria"],
                max_attempts=MAX_ATTEMPTS,
                timeout_seconds=600.0,
            )
        finally:
            db.close()

        args = argparse.Namespace(
            mode=mode,
            db_path=db_path,
            task_id=task.id,
            sessions_root=sessions_root,
            control_dir=control_dir,
        )
        process_a = _spawn_worker("a", args)
        (
            crash_trigger,
            run_id,
            pre_crash_state,
            process_a_exit,
            forced,
            termination_mechanism,
        ) = _poll_for_crash_trigger(
            process_a,
            args,
            timeout_seconds=observation_timeout,
            poll_interval=poll_interval,
        )
        post_crash_state = _read_snapshot(db_path, source_root, run_id)
        process_b_exit: int | None = None
        process_b_started = False
        worker_summary: dict[str, Any] = {}
        if crash_trigger == "DURABLE_MUTATION_STABLE" and run_id:
            process_b_started = True
            process_b = _spawn_worker("b", args, run_id=run_id)
            try:
                process_b_exit = process_b.wait(timeout=resume_timeout)
            except subprocess.TimeoutExpired:
                _terminate_process(process_b)
                process_b_exit = process_b.returncode
            worker_summary = _read_worker_summary(control_dir)
        worker_errors = {
            worker: error_type
            for worker in ("a", "b")
            if (error_type := _read_worker_error(control_dir, worker)) is not None
        }

        final_state = _read_snapshot(db_path, source_root, run_id)
        final_tests = {"status": "FAIL", "exit_code": None, "timed_out": False, "duration_ms": 0}
        if run_id:
            _session_id, final_session_root = _local_session_root(db_path, run_id)
            if final_session_root is not None:
                final_tests = _pytest(final_session_root / "work")
        source_snapshot_sha_after = tree_sha256(source_root)
        repository_fixture_unchanged = tree_sha256(FIXTURE_ROOT) == fixture_source_sha
        temporary_source_snapshot_unchanged = source_snapshot_sha_after == source_snapshot_sha_before

        db = Database(db_path)
        db.init_db()
        try:
            final_run = RunRepository(db).get(run_id) if run_id else None
            final_task = TaskRepository(db).get(task.id)
            attempts = AttemptRepository(db).list_for_run(run_id) if run_id else []
            attempt_metrics = _attempt_summaries(db, run_id) if run_id else []
            checkpoints = CheckpointRepository(db).list_for_run(run_id) if run_id else []
            duplicates = (
                _duplicate_metrics(db, run_id)
                if run_id
                else {
                    "duplicate_attempts": 0,
                    "duplicate_validations": 0,
                    "duplicate_failure_reports": 0,
                    "duplicate_recovery_actions": 0,
                    "duplicate_checkpoints": 0,
                }
            )
            arbitration = _arbitration_observation(db, run_id) if run_id else {
                "outcome_arbitration_observed": False,
                "outcome_arbitration_attempt_number": None,
                "outcome_arbitration_executor_status": None,
                "outcome_arbitration_error_type": None,
                "outcome_arbitration_validation_passed": None,
                "next_attempt_suppressed_after_arbitration": None,
                "attempts_after_arbitration": None,
            }
            same_workspace = bool(
                pre_crash_state
                and pre_crash_state.get("workspace_session_id")
                and pre_crash_state.get("workspace_session_id") == final_state.get("workspace_session_id")
            )
            functional_validation_passed = initial_tests["status"] == "FAIL" and final_tests["status"] == "PASS"
            agent_completion_passed = False
            outer_task_completed = bool(
                final_run
                and final_run.status is RunStatus.COMPLETED
                and final_task
                and final_task.status is TaskStatus.COMPLETED
            )
            final_patch_nonempty = final_state["files_changed"] > 0 and bool(final_state["patch_sha256"])
            duplicate_rows_zero = all(
                duplicates[key] == 0
                for key in (
                    "duplicate_validations",
                    "duplicate_failure_reports",
                    "duplicate_recovery_actions",
                    "duplicate_checkpoints",
                )
            )
            process_recovery_passed = bool(
                crash_trigger == "DURABLE_MUTATION_STABLE"
                and forced
                and process_b_started
                and process_b_exit == 0
                and same_workspace
                and final_tests["status"] == "PASS"
                and repository_fixture_unchanged
                and temporary_source_snapshot_unchanged
                and duplicate_rows_zero
            )
            long_horizon_result = "PASS" if all(
                (
                    functional_validation_passed,
                    forced,
                    process_b_started,
                    process_b_exit == 0,
                    same_workspace,
                    final_tests["status"] == "PASS",
                    outer_task_completed,
                    repository_fixture_unchanged,
                    temporary_source_snapshot_unchanged,
                    final_patch_nonempty,
                    duplicate_rows_zero,
                )
            ) else "INCONCLUSIVE" if crash_trigger == "CRASH_TRIGGER_NOT_REACHED" else "FAIL"
            result = {
                "status": long_horizon_result,
                "long_horizon_result": long_horizon_result,
                "mode": mode,
                "evaluation_id": "HV13-DRY-001" if mode != "live_real_model" else "HV13-LIVE-001",
                "phase": PHASE,
                "experiment_id": EXPERIMENT_ID,
                "git_sha": git_sha,
                "code_commit_clean": code_commit_clean,
                "harness_version": HARNESS_VERSION,
                "comparison_baseline_id": COMPARISON_BASELINE_ID,
                "comparison_baseline_harness": COMPARISON_BASELINE_HARNESS,
                "fixture_version": FIXTURE_VERSION,
                "fixture": "evals/fixtures/hv12_session_lifecycle",
                "same_fixture": True,
                "same_max_attempts": MAX_ATTEMPTS == 3,
                "same_inner_turn_budget": INNER_TURN_BUDGET == 20,
                "same_capability_policy": CAPABILITIES == [
                    "workspace.list",
                    "workspace.read",
                    "workspace.search",
                    "workspace.edit",
                    "workspace.diff",
                    "cli.exec",
                ],
                "same_crash_trigger_policy": True,
                "fixture_source_sha": fixture_source_sha,
                "source_snapshot_sha_before": source_snapshot_sha_before,
                "source_snapshot_sha_after": source_snapshot_sha_after,
                "task_id": task.id,
                "run_id": run_id,
                "max_attempts": MAX_ATTEMPTS,
                "inner_turn_budget": INNER_TURN_BUDGET,
                "allowed_capabilities": CAPABILITIES,
                "allowed_side_effect_capabilities": SIDE_EFFECT_CAPABILITIES,
                "cli_policy": ["pytest"],
                "provider": {
                    "model": os.getenv("ODYS_AGENT_MODEL") if mode == "live_real_model" else "scripted-process-recovery",
                    "provider_profile": "mimo" if mode == "live_real_model" else "deterministic",
                    "api_mode": "chat_completions" if mode == "live_real_model" else None,
                    "base_url_configured": bool(os.getenv("ODYS_AGENT_BASE_URL")) if mode == "live_real_model" else False,
                    "api_key_configured": bool(os.getenv("ODYS_AGENT_API_KEY")) if mode == "live_real_model" else False,
                },
                "initial_tests": initial_tests,
                "final_tests": final_tests,
                "functional_validation_passed": functional_validation_passed,
                "agent_completion_passed": agent_completion_passed,
                "outer_task_completed": outer_task_completed,
                "process_recovery_passed": process_recovery_passed,
                "repository_fixture_unchanged": repository_fixture_unchanged,
                "temporary_source_snapshot_unchanged": temporary_source_snapshot_unchanged,
                "source_repository_unchanged": repository_fixture_unchanged,
                "durable_workspace_session_reused": same_workspace,
                "same_workspace_session": same_workspace,
                "fresh_process_b_created": process_b_started,
                "resume_run_invoked": process_b_started,
                "final_patch_nonempty": final_patch_nonempty,
                "attempts": attempt_metrics,
                "attempt_metrics": attempt_metrics,
                "attempt_count": len(attempts),
                "attempt_3_exists": any(attempt.attempt_number == 3 for attempt in attempts),
                **arbitration,
                "crash": {
                    "trigger": crash_trigger,
                    "forced_process_termination": forced,
                    "termination_mechanism": termination_mechanism,
                    "process_A_pid": process_a.pid,
                    "process_A_exit_code": process_a_exit,
                    "post_crash_state": post_crash_state,
                },
                "forced_process_termination": forced,
                "termination_mechanism": termination_mechanism,
                "process_A_pid": process_a.pid,
                "process_A_exit_code": process_a_exit,
                "worker_error_types": worker_errors,
                "outer_harness_metrics": {
                    "process_instances": 3 if process_b_started else 2,
                    "process_a_forced_termination": forced,
                    "crash_trigger": crash_trigger,
                    "files_changed_at_crash": (pre_crash_state or {}).get("files_changed", 0),
                    "patch_sha_at_crash": (pre_crash_state or {}).get("patch_sha256"),
                    "attempts_before_crash": int((post_crash_state or {}).get("attempt_count", 0)),
                    "attempts_before_resume": int((post_crash_state or {}).get("attempt_count", 0)),
                    "attempts_total": len(attempts),
                    "new_attempts_after_resume": max(
                        0, len(attempts) - int((post_crash_state or {}).get("attempt_count", 0))
                    ) if process_b_started else 0,
                    "checkpoints_created": len(checkpoints),
                    "cp3_attempts": sum(
                        1
                        for attempt in attempts
                        if ContextSnapshotRepository(db).get(attempt.context_snapshot_id)
                        and ContextSnapshotRepository(db).get(attempt.context_snapshot_id).policy == "CP-3"
                    ),
                    "workspace_session_id": final_state.get("workspace_session_id"),
                    "same_workspace_session_after_restart": same_workspace,
                    "executor_calls_after_resume": worker_summary.get("executor_calls"),
                    "validator_calls_after_resume": worker_summary.get("validator_calls"),
                    **duplicates,
                    "repository_fixture_unchanged": repository_fixture_unchanged,
                    "temporary_source_snapshot_unchanged": temporary_source_snapshot_unchanged,
                    "source_unchanged": temporary_source_snapshot_unchanged,
                },
                "duplicate_durable_rows": {
                    "validations": duplicates["duplicate_validations"],
                    "failure_reports": duplicates["duplicate_failure_reports"],
                    "recovery_actions": duplicates["duplicate_recovery_actions"],
                    "checkpoints": duplicates["duplicate_checkpoints"],
                },
                "evidence_safety": {
                    "raw_diff_persisted": False,
                    "raw_model_transcript_persisted": False,
                    "raw_tool_arguments_persisted": False,
                    "raw_stdout_stderr_persisted": False,
                    "credentials_persisted": False,
                    "precrash_trace_incomplete": bool(
                        attempts and attempts[0].status is AttemptStatus.CRASHED
                    ),
                },
                "live_config_gated": True,
                "live_run_executed": mode == "live_real_model",
                "total_wall_duration_ms": int((time.monotonic() - started) * 1000),
            }
        finally:
            db.close()
    if output_path is not None:
        try:
            _write_evidence(output_path, result, exclusive=mode == "live_real_model")
        except FileExistsError:
            if mode != "live_real_model":
                raise
            return {"status": "LIVE_RESULT_EXISTS", "mode": mode, "live_run_executed": False}
    return result


def _git_sha() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def _git_identity() -> tuple[str | None, bool]:
    sha = _git_sha()
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(REPO_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except OSError:
        return sha, False
    return sha, status.returncode == 0 and not status.stdout.strip()


def _duplicate_metrics(db: Database, run_id: str) -> dict[str, int]:
    attempts = AttemptRepository(db).list_for_run(run_id)
    validations = ValidationResultRepository(db)
    failures = FailureReportRepository(db)
    actions = RecoveryActionRepository(db)
    checkpoints = CheckpointRepository(db).list_for_run(run_id)
    return {
        "duplicate_attempts": len(attempts) - len({attempt.attempt_number for attempt in attempts}),
        "duplicate_validations": sum(
            max(0, len(validations.list_for_attempt(attempt.id)) - 1) for attempt in attempts
        ),
        "duplicate_failure_reports": sum(
            max(0, len(failures.list_for_attempt(attempt.id)) - 1) for attempt in attempts
        ),
        "duplicate_recovery_actions": sum(
            max(0, len(actions.list_for_attempt(attempt.id)) - 1) for attempt in attempts
        ),
        "duplicate_checkpoints": len(checkpoints) - len({checkpoint.attempt_id for checkpoint in checkpoints}),
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=["parent", "worker-a", "worker-b"], default="parent")
    parser.add_argument(
        "--mode",
        choices=["deterministic_process_recovery_fixture", "live_real_model"],
        default="deterministic_process_recovery_fixture",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--observation-timeout", type=float, default=30.0)
    parser.add_argument("--poll-interval", type=float, default=0.2)
    parser.add_argument("--resume-timeout", type=float, default=180.0)
    parser.add_argument("--db", dest="db_path", type=Path)
    parser.add_argument("--task-id")
    parser.add_argument("--run-id")
    parser.add_argument("--sessions-root", type=Path)
    parser.add_argument("--control-dir", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.role == "worker-a":
        return _worker_a(args)
    if args.role == "worker-b":
        return _worker_b(args)
    output = args.output or (DEFAULT_LIVE_OUTPUT if args.mode == "live_real_model" else DEFAULT_DRY_OUTPUT)
    result = run_evaluation(
        mode=args.mode,
        output_path=output,
        observation_timeout=args.observation_timeout,
        poll_interval=args.poll_interval,
        resume_timeout=args.resume_timeout,
    )
    print(f"STATUS={result['status']}")
    print(f"RESULT={output.as_posix()}")
    return 0 if result["status"] in {"PASS", "SKIPPED_CONFIG"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
