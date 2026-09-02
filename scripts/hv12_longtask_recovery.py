"""Manual HV-1.2 long-task process-recovery evaluation.

The default command is the fully offline deterministic dry run.  The live
mode is deliberately explicit and requires ``ODYS_AGENT_MODEL`` plus
``ODYS_AGENT_API_KEY``; this module never starts a live run merely by being
imported.  The parent observes only local durable state and starts two fresh
worker processes.  It never reuses a Python object from worker A in worker B.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
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

from lhas import HARNESS_VERSION
from lhas.checkpoint import CheckpointRepository
from lhas.domain.enums import AttemptStatus, ExecutionStatus, RunStatus, TaskStatus
from lhas.domain.models import Project
from lhas.executors.protocol import ExecutionRequest, ExecutionResult
from lhas.failure import RuleFailureClassifier
from lhas.inner_agent import AgentsSdkModelConfig, InnerAgentExecutor, OpenAIAgentsBackend
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
from lhas.tools.registry import ToolRegistry
from lhas.validation import ValidationCheck, ValidationResult
from lhas.workspace import (
    CommandPolicy,
    CommandRule,
    RunWorkspaceManager,
    WorkspaceDiffTool,
    WorkspaceEditTool,
    register_workspace_tools,
    tree_sha256,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "evals" / "fixtures" / "hv12_session_lifecycle"
DEFAULT_DRY_OUTPUT = REPO_ROOT / "evals" / "runs" / "HV12-DRY-001.json"
DEFAULT_LIVE_OUTPUT = REPO_ROOT / "evals" / "runs" / "HV12-LIVE-001.json"
PHASE = "HV12_LONGTASK_BASELINE"
FIXTURE_VERSION = "HV12-SESSION-LIFECYCLE-1"
HISTORICAL_HARNESS_VERSION = "HV-1.2"
MAX_ATTEMPTS = 3
INNER_TURN_BUDGET = 20
CAPABILITIES = [
    "workspace.list",
    "workspace.read",
    "workspace.search",
    "workspace.edit",
    "workspace.diff",
    "cli.exec",
]
SIDE_EFFECT_CAPABILITIES = ["workspace.edit"]
EXCLUDED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"}


def _file_hashes(root: Path) -> dict[str, str]:
    root = root.resolve()
    hashes: dict[str, str] = {}
    if not root.is_dir():
        return hashes
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in relative.parts) or not path.is_file() or path.is_symlink():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            # Workspace.edit uses an atomic temporary file.  A parent poll can
            # observe that file between creation and replace, including while
            # Windows still denies the transient handle.  Omit it from this
            # poll; the next stable observation will hash the final file.
            continue
        hashes[relative.as_posix()] = hashlib.sha256(data).hexdigest()
    return hashes


def _diff_summary(baseline: Path, work: Path) -> dict[str, Any]:
    before = _file_hashes(baseline)
    after = _file_hashes(work)
    changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
    added = removed = 0
    patch_parts: list[str] = []
    for relative in changed:
        before_text = (baseline / relative).read_text(encoding="utf-8", errors="replace") if relative in before else ""
        after_text = (work / relative).read_text(encoding="utf-8", errors="replace") if relative in after else ""
        lines = list(
            difflib.unified_diff(
                before_text.splitlines(keepends=True),
                after_text.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
            )
        )
        patch_parts.extend(lines)
        added += sum(1 for line in lines if line.startswith("+") and not line.startswith("+++") )
        removed += sum(1 for line in lines if line.startswith("-") and not line.startswith("---") )
    patch = "".join(patch_parts).encode("utf-8")
    source_changed = [path for path in changed if path.startswith("src/")]
    return {
        "changed_files": changed[:100],
        "files_changed": len(changed),
        "source_files_changed": source_changed[:100],
        "lines_added": added,
        "lines_removed": removed,
        "truncated": False,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
    }


def _pytest(root: Path, timeout_seconds: float = 90.0) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
        exit_code = completed.returncode
        timed_out = False
    except subprocess.TimeoutExpired:
        exit_code = None
        timed_out = True
    return {
        "status": "PASS" if exit_code == 0 else "FAIL",
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


class FixturePytestValidator:
    """Deterministic validator; only bounded test status is persisted."""

    def __init__(self, sessions_root: Path):
        self.sessions_root = Path(sessions_root)
        self.calls = 0

    async def validate(self, *, task, attempt, result):
        self.calls += 1
        work_root = self.sessions_root / attempt.run_id / "work"
        outcome = _pytest(work_root)
        passed = outcome["status"] == "PASS"
        return ValidationResult(
            attempt_id=attempt.id,
            passed=passed,
            checks=[
                ValidationCheck(
                    name="fixture_pytest",
                    passed=passed,
                    detail=None if passed else "pytest did not pass",
                )
            ],
            evidence=f"pytest={outcome['status']}; exit_code={outcome['exit_code']}",
            # Do not persist raw stdout/stderr from the fixture process.
            stdout=None,
            stderr=None,
            duration_ms=outcome["duration_ms"],
        )


async def _replace_if_present(workspace, path: str, old_text: str, new_text: str) -> None:
    candidate = Path(workspace.root) / path
    current = candidate.read_text(encoding="utf-8")
    if old_text in current:
        await workspace.edit_file(path, old_text, new_text)


class DryRunCrashExecutor:
    name = "DeterministicProcessRecoveryExecutor-A"

    def __init__(self, workspace, recorder: ExecutorCallRecorder | None = None):
        self.workspace = workspace
        self.calls: list[int] = []
        self.recorder = recorder

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls.append(request.attempt_number)
        if self.recorder is not None:
            self.recorder.record(request.attempt_number)
        await _replace_if_present(
            self.workspace,
            "src/session_store.py",
            "self._cache: dict[str, Session] = {}",
            "self._cache: dict[tuple[str, str], Session] = {}",
        )
        await _replace_if_present(self.workspace, "src/session_store.py", "self._cache.get(session_id)", "self._cache.get(key)")
        await _replace_if_present(self.workspace, "src/session_store.py", "self._cache[session_id] = session", "self._cache[key] = session")
        # This is deliberately an OS-process kill point, not a Python exception.
        await asyncio.Event().wait()


class DryRunResumeExecutor:
    name = "DeterministicProcessRecoveryExecutor-B"

    def __init__(self, workspace, recorder: ExecutorCallRecorder | None = None):
        self.workspace = workspace
        self.calls: list[int] = []
        self.recorder = recorder

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls.append(request.attempt_number)
        if self.recorder is not None:
            self.recorder.record(request.attempt_number)
        await _replace_if_present(
            self.workspace,
            "src/message_router.py",
            "        return service.create_session(tenant_id, session_id, message)",
            "        return None",
        )
        return ExecutionResult(status=ExecutionStatus.SUCCESS, output="deterministic continuation complete")


class ExecutorCallRecorder:
    """Shared evaluation-only call accounting across factory-created executors."""

    def __init__(self) -> None:
        self.attempt_numbers: list[int] = []

    def record(self, attempt_number: int) -> None:
        self.attempt_numbers.append(attempt_number)


class BudgetedInnerAgentExecutor:
    """Evaluation adapter that fixes the per-attempt budget at exactly 20 turns."""

    name = "InnerAgentExecutor"

    def __init__(self, inner: InnerAgentExecutor, recorder: ExecutorCallRecorder | None = None):
        self.inner = inner
        self.recorder = recorder

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if self.recorder is not None:
            self.recorder.record(request.attempt_number)
        request = request.model_copy(
            update={"metadata": {**request.metadata, "max_turns": INNER_TURN_BUDGET}}
        )
        return await self.inner.execute(request)


def _live_executor_factory(
    workspace,
    db: Database,
    config: AgentsSdkModelConfig,
    recorder: ExecutorCallRecorder,
):
    registry = ToolRegistry()
    policy = CommandPolicy([CommandRule(["pytest"], allow_extra_args=True)])
    register_workspace_tools(registry, workspace, policy)
    registry.register(WorkspaceEditTool(workspace))
    registry.register(WorkspaceDiffTool(workspace))
    backend = OpenAIAgentsBackend(registry, config)
    inner = InnerAgentExecutor(
        backend,
        allowed_capabilities=CAPABILITIES,
        allowed_side_effect_capabilities=SIDE_EFFECT_CAPABILITIES,
        db=db,
    )
    return BudgetedInnerAgentExecutor(inner, recorder)


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
    control_dir: Path,
    worker: str,
):
    manager = RunWorkspaceManager(db, sessions_root)
    validator = FixturePytestValidator(sessions_root)
    recorder = ExecutorCallRecorder()
    if mode == "deterministic_process_recovery_fixture":
        if worker == "a":
            def factory(workspace):
                return DryRunCrashExecutor(workspace, recorder)

        else:
            def factory(workspace):
                return DryRunResumeExecutor(workspace, recorder)

        executor_type = "ScriptedProcessRecoveryExecutor"
        provider = "deterministic"
        model = "scripted-process-recovery"
    else:
        config = AgentsSdkModelConfig(provider_profile="mimo", api_mode="chat_completions")

        def factory(workspace):
            return _live_executor_factory(workspace, db, config, recorder)

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
        dataset_version="HV12-SESSION-LIFECYCLE-1",
        experiment_id="HV12-LONGTASK-BASELINE",
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
            control_dir=Path(args.control_dir),
            worker="a",
        )
        asyncio.run(orchestrator.execute_task(task.id))
        return 0
    except Exception as exc:
        # Persist only a stable error type for parent diagnostics.
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
            control_dir=Path(args.control_dir),
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


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    entries = [str(REPO_ROOT / "src"), str(REPO_ROOT)]
    if env.get("PYTHONPATH"):
        entries.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(entries)
    return env


def _worker_python_command() -> list[str]:
    """Return the active evaluation interpreter, including under uv on Linux."""

    return [sys.executable]


def _spawn_worker(role: str, args: argparse.Namespace, *, run_id: str | None = None) -> subprocess.Popen:
    command = [
        *_worker_python_command(),
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


def _terminate_process(process: subprocess.Popen, timeout_seconds: float = 5.0) -> str | None:
    if process.poll() is not None:
        return None
    process.terminate()
    try:
        process.wait(timeout=timeout_seconds)
        return "subprocess.Popen.terminate"
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout_seconds)
        return "subprocess.Popen.kill"


def _local_run_id(db_path: Path, task_id: str) -> str | None:
    try:
        with sqlite3.connect(str(db_path), timeout=1.0) as connection:
            row = connection.execute(
                "SELECT id FROM runs WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            return str(row[0]) if row else None
    except sqlite3.Error:
        return None


def _local_session_root(db_path: Path, run_id: str) -> tuple[str | None, Path | None]:
    try:
        with sqlite3.connect(str(db_path), timeout=1.0) as connection:
            row = connection.execute(
                "SELECT session_id, session_root FROM workspace_sessions WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            return (str(row[0]), Path(str(row[1]))) if row else (None, None)
    except sqlite3.Error:
        return None, None


def _poll_for_crash_trigger(
    process: subprocess.Popen,
    args: argparse.Namespace,
    *,
    timeout_seconds: float,
    poll_interval: float,
) -> tuple[str, str | None, dict[str, Any] | None, int | None, bool, str | None]:
    deadline = time.monotonic() + timeout_seconds
    stable_hash: str | None = None
    stable_observations = 0
    process_a_forced_termination = False
    termination_mechanism: str | None = None
    process_exit_code: int | None = None
    while time.monotonic() < deadline:
        run_id = _local_run_id(args.db_path, args.task_id)
        if run_id:
            session_id, session_root = _local_session_root(args.db_path, run_id)
            if session_root is not None:
                state = _workspace_state(session_root)
                if state["source_files_changed"] and state["work_tree_sha"]:
                    if state["work_tree_sha"] == stable_hash:
                        stable_observations += 1
                    else:
                        stable_hash = state["work_tree_sha"]
                        stable_observations = 1
                    if stable_observations >= 2:
                        if process.poll() is None:
                            termination_mechanism = _terminate_process(process)
                            process_a_forced_termination = termination_mechanism is not None
                            process_exit_code = process.returncode
                            return (
                                "DURABLE_MUTATION_STABLE",
                                run_id,
                                {**state, "workspace_session_id": session_id},
                                process_exit_code,
                                process_a_forced_termination,
                                termination_mechanism,
                            )
                        process_exit_code = process.poll()
                        return (
                            "PROCESS_A_EXITED_BEFORE_FORCED_TERMINATION",
                            run_id,
                            {**state, "workspace_session_id": session_id},
                            process_exit_code,
                            False,
                            None,
                        )
        if process.poll() is not None:
            process_exit_code = process.poll()
            return "PROCESS_A_EXITED_BEFORE_CRASH_TRIGGER", run_id, None, process_exit_code, False, None
        time.sleep(poll_interval)

    termination_mechanism = _terminate_process(process)
    process_a_forced_termination = termination_mechanism is not None
    process_exit_code = process.returncode
    return "CRASH_TRIGGER_NOT_REACHED", _local_run_id(args.db_path, args.task_id), None, process_exit_code, process_a_forced_termination, termination_mechanism


def _workspace_state(session_root: Path) -> dict[str, Any]:
    baseline = session_root / "baseline"
    work = session_root / "work"
    if not baseline.is_dir() or not work.is_dir():
        return {
            "changed_files": [],
            "files_changed": 0,
            "source_files_changed": [],
            "lines_added": 0,
            "lines_removed": 0,
            "truncated": False,
            "patch_sha256": hashlib.sha256(b"").hexdigest(),
            "source_tree_sha": None,
            "work_tree_sha": None,
        }
    summary = _diff_summary(baseline, work)
    summary["source_tree_sha"] = tree_sha256(baseline)
    summary["work_tree_sha"] = tree_sha256(work)
    return summary


def _safe_usage(raw: Any) -> dict[str, int | None]:
    raw = raw if isinstance(raw, dict) else {}
    result: dict[str, int | None] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = raw.get(key)
        result[key] = int(value) if isinstance(value, (int, float)) and value > 0 else None
    return result


def _persisted_completion_claim(persisted_result: dict[str, Any]) -> bool | None:
    """Read the claim from the nested InnerAgentResult when available."""

    candidates = [persisted_result]
    raw = persisted_result.get("raw")
    if isinstance(raw, dict):
        candidates.append(raw)
    for candidate in candidates:
        value = candidate.get("completion_claim")
        if isinstance(value, bool):
            return value
    return None


def _termination_status(attempt, inner_event) -> str:
    if attempt.error_type == "PROCESS_INTERRUPTED":
        return "FORCED_PROCESS_TERMINATION"
    if attempt.error_type == "AGENT_TURN_LIMIT":
        return "TURN_LIMIT"
    if attempt.status is AttemptStatus.COMPLETED:
        return "COMPLETED"
    if attempt.error_type:
        return attempt.error_type
    if inner_event is not None and inner_event.event_type.value == "INNER_AGENT_COMPLETED":
        return "COMPLETED"
    return attempt.status.value


def _attempt_metrics(db: Database, run_id: str) -> list[dict[str, Any]]:
    attempts = AttemptRepository(db).list_for_run(run_id)
    events = EventStore(db).list_for_run(run_id)
    snapshots = ContextSnapshotRepository(db)
    checkpoints = CheckpointRepository(db).list_for_run(run_id)
    metrics: list[dict[str, Any]] = []
    for attempt in attempts:
        attempt_events = [event for event in events if event.attempt_id == attempt.id]
        completed = next(
            (event for event in attempt_events if event.event_type.value == "INNER_AGENT_COMPLETED"), None
        )
        failed = next(
            (event for event in attempt_events if event.event_type.value == "INNER_AGENT_FAILED"), None
        )
        inner_event = completed or failed
        safe_result: dict[str, Any] = {}
        if attempt.executor_result:
            try:
                parsed = json.loads(attempt.executor_result)
                if isinstance(parsed, dict):
                    safe_result = parsed
            except (TypeError, ValueError, json.JSONDecodeError):
                safe_result = {}
        observations = [
            event.payload for event in attempt_events if event.event_type.value == "INNER_AGENT_TOOL_OBSERVATION"
        ]
        by_capability: dict[str, int] = {}
        failure_by_type: dict[str, int] = {}
        failure_by_capability: dict[str, int] = {}
        for observation in observations:
            capability = observation.get("capability")
            if isinstance(capability, str):
                by_capability[capability] = by_capability.get(capability, 0) + 1
            if observation.get("status") == "FAILURE":
                error_type = observation.get("error_type") or "UNKNOWN"
                failure_by_type[str(error_type)] = failure_by_type.get(str(error_type), 0) + 1
                if isinstance(capability, str):
                    failure_by_capability[capability] = failure_by_capability.get(capability, 0) + 1
        snapshot = snapshots.get(attempt.context_snapshot_id) if attempt.context_snapshot_id else None
        snapshot_metrics = snapshot.metrics if snapshot else {}
        usage = attempt.usage or safe_result.get("usage")
        inner_payload = inner_event.payload if inner_event else {}
        trace_complete = inner_event is not None
        metrics.append(
            {
                "attempt_number": attempt.attempt_number,
                "attempt_status": attempt.status.value,
                "inner_agent_status": (
                    "SUCCESS" if completed else "FAILURE" if failed else None
                ),
                "error_type": attempt.error_type,
                "termination_status": _termination_status(attempt, inner_event),
                "turn_count": inner_payload.get("turn_count", safe_result.get("turn_count")) if trace_complete else None,
                "tool_call_count": inner_payload.get("tool_call_count", safe_result.get("tool_call_count")) if trace_complete else None,
                "tool_calls_by_capability": by_capability if trace_complete else None,
                "tool_failure_count": sum(failure_by_type.values()) if trace_complete else None,
                "tool_failures_by_type": failure_by_type if trace_complete else None,
                "tool_failures_by_capability": failure_by_capability if trace_complete else None,
                "duration_ms": attempt.duration_ms,
                **_safe_usage(usage),
                "completion_claim_present": _persisted_completion_claim(safe_result),
                "context_policy": snapshot.policy if snapshot else None,
                "checkpoint_used": snapshot_metrics.get("checkpoint_used") if isinstance(snapshot_metrics, dict) else None,
                "checkpoint_created": any(checkpoint.attempt_id == attempt.id for checkpoint in checkpoints),
                "pre_crash_tool_trace_complete": trace_complete,
            }
        )
    return metrics


def _duplicate_metrics(db: Database, run_id: str) -> dict[str, int]:
    attempts = AttemptRepository(db).list_for_run(run_id)
    validations = ValidationResultRepository(db)
    failures = FailureReportRepository(db)
    actions = RecoveryActionRepository(db)
    checkpoints = CheckpointRepository(db).list_for_run(run_id)
    return {
        "duplicate_attempts": len(attempts) - len({attempt.attempt_number for attempt in attempts}),
        "duplicate_validations": sum(max(0, len(validations.list_for_attempt(attempt.id)) - 1) for attempt in attempts),
        "duplicate_failure_reports": sum(max(0, len(failures.list_for_attempt(attempt.id)) - 1) for attempt in attempts),
        "duplicate_recovery_actions": sum(max(0, len(actions.list_for_attempt(attempt.id)) - 1) for attempt in attempts),
        "duplicate_checkpoints": len(checkpoints) - len({checkpoint.attempt_id for checkpoint in checkpoints}),
    }


def _read_snapshot(db_path: Path, source_root: Path, run_id: str | None) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "run_status": None,
        "task_status": None,
        "latest_attempt_status": None,
        "attempt_count": 0,
        "workspace_session_id": None,
        "changed_files": [],
        "files_changed": 0,
        "patch_sha256": hashlib.sha256(b"").hexdigest(),
        "source_tree_sha": tree_sha256(source_root),
        "work_tree_sha": None,
    }
    if run_id is None:
        return snapshot
    db = Database(db_path)
    db.init_db()
    try:
        run = RunRepository(db).get(run_id)
        task = TaskRepository(db).get(run.task_id) if run else None
        attempts = AttemptRepository(db).list_for_run(run_id)
        binding = WorkspaceSessionBindingRepository(db).get_by_run(run_id)
        snapshot["run_status"] = run.status.value if run else None
        snapshot["task_status"] = task.status.value if task else None
        snapshot["latest_attempt_status"] = attempts[-1].status.value if attempts else None
        snapshot["attempt_count"] = len(attempts)
        snapshot["workspace_session_id"] = binding.session_id if binding else None
        if binding:
            state = _workspace_state(Path(binding.session_root))
            for key in ("changed_files", "files_changed", "patch_sha256", "source_tree_sha", "work_tree_sha"):
                snapshot[key] = state[key]
    finally:
        db.close()
    return snapshot


def _read_worker_summary(control_dir: Path) -> dict[str, Any]:
    path = control_dir / "process-b-summary.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        "run_status": value.get("run_status") if isinstance(value.get("run_status"), str) else None,
        "validator_calls": value.get("validator_calls") if isinstance(value.get("validator_calls"), int) else None,
        "executor_calls": value.get("executor_calls") if isinstance(value.get("executor_calls"), list) else [],
    }


def _read_worker_error(control_dir: Path, worker: str) -> str | None:
    path = control_dir / f"process-{worker}-error.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    error_type = value.get("error_type") if isinstance(value, dict) else None
    return error_type if isinstance(error_type, str) else None


def _git_identity() -> tuple[str | None, bool]:
    """Return HEAD and whether the code was clean before the evaluation ran."""

    try:
        sha_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(REPO_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except OSError:
        return None, False
    sha = sha_result.stdout.strip() if sha_result.returncode == 0 else None
    clean = status_result.returncode == 0 and not status_result.stdout.strip()
    return sha or None, clean


def _write_evidence(path: Path, payload: dict[str, Any], *, exclusive: bool) -> None:
    """Write JSON atomically; live evidence uses exclusive creation."""

    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if exclusive:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(str(path), flags, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_text(serialized, encoding="utf-8")
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def run_evaluation(
    *,
    mode: str = "deterministic_process_recovery_fixture",
    output_path: Path | None = None,
    observation_timeout: float = 30.0,
    poll_interval: float = 0.2,
    resume_timeout: float = 180.0,
) -> dict[str, Any]:
    if output_path is None:
        output_path = DEFAULT_LIVE_OUTPUT if mode == "live_real_model" else DEFAULT_DRY_OUTPUT
    if mode == "live_real_model" and output_path.exists():
        return {"status": "LIVE_RESULT_EXISTS", "mode": mode, "live_run_executed": False}
    if mode == "live_real_model" and not (os.getenv("ODYS_AGENT_MODEL") and os.getenv("ODYS_AGENT_API_KEY")):
        return {"status": "SKIPPED_CONFIG", "mode": mode, "live_run_executed": False}
    started = time.monotonic()
    git_sha, code_commit_clean = _git_identity()
    fixture_source_sha = tree_sha256(FIXTURE_ROOT)
    # Windows can release a killed child's SQLite handle a little after the
    # parent observes process termination; keep cleanup bounded and harmless.
    with tempfile.TemporaryDirectory(prefix="hv12-longtask-", ignore_cleanup_errors=True) as temporary:
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
                Project(name="hv12-session-lifecycle", root_path=str(source_root))
            )
            objective, constraints, acceptance = _task_objective()
            task = create_task(
                db,
                project_id=project.id,
                title="HV-1.2 session lifecycle process recovery",
                objective=objective,
                constraints=constraints,
                acceptance_criteria=acceptance,
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
        crash_trigger, run_id, pre_crash_state, process_a_exit, forced, mechanism = _poll_for_crash_trigger(
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
        if final_state["work_tree_sha"] and run_id:
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
            attempt_metrics = _attempt_metrics(db, run_id) if run_id else []
            duplicates = _duplicate_metrics(db, run_id) if run_id else {
                "duplicate_attempts": 0,
                "duplicate_validations": 0,
                "duplicate_failure_reports": 0,
                "duplicate_recovery_actions": 0,
                "duplicate_checkpoints": 0,
            }
            checkpoints = CheckpointRepository(db).list_for_run(run_id) if run_id else []
            first_validation = (
                ValidationResultRepository(db).get_for_attempt(
                    AttemptRepository(db).list_for_run(run_id)[0].id
                )
                if run_id and AttemptRepository(db).list_for_run(run_id)
                else None
            )
            same_workspace = bool(
                pre_crash_state
                and pre_crash_state.get("workspace_session_id")
                and pre_crash_state.get("workspace_session_id") == final_state.get("workspace_session_id")
            )
            functional_validation_passed = initial_tests["status"] == "FAIL" and final_tests["status"] == "PASS"
            outer_task_completed = bool(
                final_run and final_run.status is RunStatus.COMPLETED and final_task and final_task.status is TaskStatus.COMPLETED
            )
            final_patch_nonempty = final_state["files_changed"] > 0 and bool(final_state["patch_sha256"])
            process_recovery_passed = bool(
                crash_trigger == "DURABLE_MUTATION_STABLE"
                and forced
                and process_b_started
                and process_b_exit == 0
                and same_workspace
                and outer_task_completed
            )
            agent_completion_passed = any(
                metric.get("completion_claim_present") is True for metric in attempt_metrics
            )
            status = "PASS" if all(
                (
                    functional_validation_passed,
                    outer_task_completed,
                    repository_fixture_unchanged,
                    temporary_source_snapshot_unchanged,
                    same_workspace,
                    final_patch_nonempty,
                    process_recovery_passed,
                )
            ) else "FAIL" if crash_trigger != "CRASH_TRIGGER_NOT_REACHED" else "INCONCLUSIVE"
            result = {
                "status": status,
                "mode": mode,
                "evaluation_id": "HV12-DRY-001" if mode != "live_real_model" else "HV12-LIVE-001",
                "phase": PHASE,
                "git_sha": git_sha,
                "code_commit_clean": code_commit_clean,
                "harness_version": HARNESS_VERSION,
                "fixture_version": FIXTURE_VERSION,
                "fixture": "evals/fixtures/hv12_session_lifecycle",
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
                "validator_final_patch_nonempty": final_patch_nonempty,
                "pre_crash_tool_trace_complete": bool(
                    attempt_metrics and attempt_metrics[0].get("pre_crash_tool_trace_complete")
                ) if attempt_metrics else False,
                "crash": {
                    "trigger": crash_trigger,
                    "process_a_pid": process_a.pid,
                    "process_a_forced_termination": forced,
                    "process_a_termination_mechanism": mechanism,
                    "process_a_exit_code": process_a_exit,
                    "post_crash_state": post_crash_state,
                },
                "attempt_metrics": attempt_metrics,
                "worker_error_types": worker_errors,
                "outer_harness_metrics": {
                    "process_instances": 3 if process_b_started else 2,
                    "process_a_forced_termination": forced,
                    "crash_trigger": crash_trigger,
                    "files_changed_at_crash": (pre_crash_state or {}).get("files_changed", 0),
                    "patch_sha_at_crash": (pre_crash_state or {}).get("patch_sha256"),
                    "attempts_before_crash": int((post_crash_state or {}).get("attempt_count", 0)),
                    "attempts_before_resume": int((post_crash_state or {}).get("attempt_count", 0)),
                    "attempts_total": len(attempt_metrics),
                    "crashed_attempts": sum(1 for metric in attempt_metrics if metric["attempt_status"] == "CRASHED"),
                    "new_attempts_after_resume": (
                        max(
                            0,
                            len(attempt_metrics) - int((post_crash_state or {}).get("attempt_count", 0)),
                        )
                        if process_b_started
                        else 0
                    ),
                    "resume_validation_passed": first_validation.passed if first_validation else None,
                    "checkpoints_created": len(checkpoints),
                    "cp3_attempts": sum(1 for metric in attempt_metrics if metric.get("context_policy") == "CP-3"),
                    "workspace_session_id": final_state.get("workspace_session_id"),
                    "same_workspace_session_after_restart": same_workspace,
                    "executor_calls_after_resume": worker_summary.get("executor_calls"),
                    "validator_calls_after_resume": worker_summary.get("validator_calls"),
                    **duplicates,
                    "repository_fixture_unchanged": repository_fixture_unchanged,
                    "temporary_source_snapshot_unchanged": temporary_source_snapshot_unchanged,
                    "source_unchanged": temporary_source_snapshot_unchanged,
                },
                "evidence_safety": {
                    "raw_diff_persisted": False,
                    "raw_model_transcript_persisted": False,
                    "raw_tool_arguments_persisted": False,
                    "raw_stdout_stderr_persisted": False,
                    "credentials_persisted": False,
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
    if HARNESS_VERSION != HISTORICAL_HARNESS_VERSION:
        print("STATUS=HISTORICAL_HARNESS_MISMATCH")
        return 2
    if args.mode == "live_real_model" and not (os.getenv("ODYS_AGENT_MODEL") and os.getenv("ODYS_AGENT_API_KEY")):
        print("STATUS=SKIPPED_CONFIG")
        return 0
    output = args.output or (DEFAULT_LIVE_OUTPUT if args.mode == "live_real_model" else DEFAULT_DRY_OUTPUT)
    if args.mode == "live_real_model" and output.exists():
        print("STATUS=LIVE_RESULT_EXISTS")
        return 2
    result = run_evaluation(
        mode=args.mode,
        output_path=output,
        observation_timeout=args.observation_timeout,
        poll_interval=args.poll_interval,
        resume_timeout=args.resume_timeout,
    )
    print(f"STATUS={result['status']}")
    print(f"RESULT={output.as_posix()}")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
