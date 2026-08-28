"""Application-layer assembly for the Odys CLI Alpha."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from lhas import HARNESS_VERSION
from lhas.checkpoint import CheckpointRepository, safe_event_projection
from lhas.command_validation import ExplicitCommandValidator, explicit_command_policy
from lhas.context_builder import ContextBuilder
from lhas.domain.enums import RunStatus
from lhas.domain.models import Project
from lhas.inner_agent import AgentsSdkModelConfig, InnerAgentExecutor, InnerAgentResult, InnerAgentStatus, OpenAIAgentsBackend
from lhas.inner_agent.tool_adapter import safe_tool_summary
from lhas.orchestrator_v2 import RecoveringOrchestrator
from lhas.persistence.database import Database
from lhas.persistence.event_store import EventStore
from lhas.persistence.phaseb_repos import ContextSnapshotRepository, FailureReportRepository, RecoveryActionRepository, ValidationResultRepository
from lhas.persistence.repositories import AttemptRepository, ProjectRepository, RunRepository, TaskRepository, WorkspaceSessionBindingRepository
from lhas.task_service import create_task
from lhas.tools.protocol import ToolRequest
from lhas.tools.registry import ToolRegistry
from lhas.workspace import RunWorkspaceManager, register_staged_workspace_tools, tree_sha256
from lhas.workspace.session import DurableWorkspaceSession


CLI_CONFIG_PREFIX = "ODYS_CLI_CONFIG:"
DEFAULT_PROVIDER = "mimo"
ALLOWED_CAPABILITIES = [
    "workspace.list",
    "workspace.read",
    "workspace.search",
    "workspace.edit",
    "workspace.edit_lines",
    "workspace.diff",
    "workspace.restore",
    "cli.exec",
]
ALLOWED_SIDE_EFFECTS = ["workspace.edit", "workspace.edit_lines", "workspace.restore"]
_SECRET_TEXT = re.compile(r"(?i)(api[_-]?key|authorization|token|secret|password)\s*[:=]\s*\S+")


class CliConfigurationError(ValueError):
    pass


def _bounded_text(value: Any, limit: int = 512) -> str:
    text = _SECRET_TEXT.sub(r"\1=[REDACTED]", str(value or ""))
    return text[:limit]


def _paths_overlap(left: Path, right: Path) -> bool:
    left = left.resolve()
    right = right.resolve()
    return left == right or left in right.parents or right in left.parents


def _project_name(root: Path) -> str:
    digest = hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()[:16]
    return f"odys-repo-{digest}"


def _task_title(goal: str) -> str:
    compact = " ".join(goal.split())
    return (compact[:125] + "...") if len(compact) > 128 else compact


def encode_cli_config(*, verify_argv: list[str], max_turns: int, provider: str, model: str) -> str:
    payload = {
        "schema_version": "odys-cli-run-v1",
        "verify_argv": verify_argv,
        "max_turns": max_turns,
        "provider": provider,
        "model": model,
    }
    return CLI_CONFIG_PREFIX + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decode_cli_config(task) -> dict[str, Any]:
    for value in task.constraints:
        if isinstance(value, str) and value.startswith(CLI_CONFIG_PREFIX):
            try:
                data = json.loads(value[len(CLI_CONFIG_PREFIX):])
            except json.JSONDecodeError as exc:
                raise CliConfigurationError("PERSISTED_CLI_CONFIG_INVALID") from exc
            if (
                data.get("schema_version") != "odys-cli-run-v1"
                or not isinstance(data.get("verify_argv"), list)
                or not data["verify_argv"]
                or not all(isinstance(item, str) and item for item in data["verify_argv"])
            ):
                raise CliConfigurationError("PERSISTED_CLI_CONFIG_INVALID")
            return data
    raise CliConfigurationError("PERSISTED_CLI_CONFIG_MISSING")


@dataclass(frozen=True)
class ProviderSettings:
    profile: str
    model: str
    config: AgentsSdkModelConfig | None = None


def resolve_provider_settings(profile: str | None, *, persisted_model: str | None = None) -> ProviderSettings:
    selected = (profile or os.getenv("ODYS_AGENT_PROVIDER_PROFILE") or DEFAULT_PROVIDER).strip().lower()
    if selected == "offline":
        return ProviderSettings(profile="offline", model="offline-deterministic")
    model = persisted_model or os.getenv("ODYS_AGENT_MODEL")
    api_key = os.getenv("ODYS_AGENT_API_KEY")
    if not model or not api_key:
        raise CliConfigurationError("provider configuration requires ODYS_AGENT_MODEL and ODYS_AGENT_API_KEY")
    try:
        config = AgentsSdkModelConfig(
            model=model,
            api_key=api_key,
            base_url=os.getenv("ODYS_AGENT_BASE_URL"),
            api_mode=os.getenv("ODYS_AGENT_API_MODE"),
            provider_profile=selected,
        )
        config.validate()
    except ValueError as exc:
        raise CliConfigurationError(str(exc)) from exc
    return ProviderSettings(profile=selected, model=model, config=config)


class OfflineDemoBackend:
    """Deterministic network-free backend for CLI integration and demos."""

    name = "offline-demo"

    def __init__(self, registry: ToolRegistry, verify_argv: list[str]):
        self.registry = registry
        self.verify_argv = list(verify_argv)

    async def run(self, request):
        trace: list[dict[str, Any]] = []
        tool_calls = 0

        async def call(capability: str, arguments: dict[str, Any]):
            nonlocal tool_calls
            tool_calls += 1
            trace.append({"event": "TOOL_STARTED", "tool_name": capability, "tool_call_id": f"offline-{tool_calls}"})
            result = await self.registry.resolve(capability).execute(ToolRequest(
                tool_call_id=f"offline-{tool_calls}",
                task_id=request.task_id,
                run_id=request.run_id,
                attempt_id=request.attempt_id,
                capability=capability,
                arguments=arguments,
                context=request.context,
                metadata=request.metadata,
            ))
            summary = safe_tool_summary(capability, arguments, result)
            trace.append({"event": "TOOL_OBSERVATION_SUMMARY", **summary})
            trace.append({"event": "TOOL_COMPLETED", "tool_name": capability, "tool_call_id": f"offline-{tool_calls}"})
            if result.status.value == "SUCCESS" and capability in {"workspace.edit_lines", "workspace.diff"}:
                safe = {
                    key: result.output[key]
                    for key in ("path", "changed_files", "files_changed", "lines_added", "lines_removed", "truncated", "before_sha256", "after_sha256", "start_line", "end_line", "lines_written")
                    if isinstance(result.output, dict) and key in result.output
                }
                trace.append({"event": "WORKSPACE_CHANGE", "tool_name": capability, "summary": safe})
                if capability == "workspace.diff":
                    trace.append({"event": "WORKSPACE_PATCH", "patch": result.output})
            return result

        target = "src/session_store.py"
        read = await call("workspace.read", {"path": target})
        if read.status.value == "SUCCESS":
            content = read.output["content"]
            replacements = {
                "self._cache: dict[str, Session] = {}": "self._cache: dict[tuple[str, str], Session] = {}",
                "self._cache.get(session_id)": "self._cache.get(key)",
                "self._cache[session_id] = session": "self._cache[key] = session",
                "self._cache.pop(session_id, None)": "self._cache.pop((tenant_id, session_id), None)",
                "session_id in self._cache": "(tenant_id, session_id) in self._cache",
            }
            updated = content
            for old, new in replacements.items():
                updated = updated.replace(old, new)
            if updated != content:
                edit = await call("workspace.edit_lines", {
                    "path": target,
                    "start_line": 1,
                    "end_line": int(read.output["total_lines"]),
                    "new_lines": updated.splitlines(),
                    "expected_sha256": read.output["sha256"],
                })
                if edit.status.value != "SUCCESS":
                    return InnerAgentResult(
                        status=InnerAgentStatus.FAILURE,
                        error_type=edit.error_type,
                        error_message=edit.error_message,
                        turn_count=1,
                        tool_call_count=tool_calls,
                        trace=trace,
                    )
        router = await call("workspace.read", {"path": "src/message_router.py"})
        if router.status.value == "SUCCESS":
            router_content = router.output["content"]
            router_updated = router_content.replace(
                "return service.create_session(tenant_id, session_id, message)",
                "return None",
            )
            if router_updated != router_content:
                router_edit = await call("workspace.edit_lines", {
                    "path": "src/message_router.py",
                    "start_line": 1,
                    "end_line": int(router.output["total_lines"]),
                    "new_lines": router_updated.splitlines(),
                    "expected_sha256": router.output["sha256"],
                })
                if router_edit.status.value != "SUCCESS":
                    return InnerAgentResult(
                        status=InnerAgentStatus.FAILURE,
                        error_type=router_edit.error_type,
                        error_message=router_edit.error_message,
                        turn_count=1,
                        tool_call_count=tool_calls,
                        trace=trace,
                    )
        verify = await call("cli.exec", {"argv": self.verify_argv, "cwd": "."})
        diff = await call("workspace.diff", {})
        passed = verify.status.value == "SUCCESS" and verify.output.get("exit_code") == 0
        return InnerAgentResult(
            status=InnerAgentStatus.SUCCESS if passed else InnerAgentStatus.FAILURE,
            final_output="offline verification passed" if passed else "offline verification failed",
            completion_claim=passed,
            error_type=None if passed else (verify.error_type or "VERIFICATION_FAILED"),
            error_message=None if passed else "configured verification did not pass",
            turn_count=1,
            tool_call_count=tool_calls,
            trace=trace,
            artifacts={"workspace_patch": diff.output} if diff.status.value == "SUCCESS" else {},
            provider_metadata={"profile": "offline", "network": False},
        )


@dataclass
class PreparedExecution:
    runtime: "ProductRuntime"
    task: Any
    orchestrator: RecoveringOrchestrator
    verify_argv: list[str]
    max_turns: int
    provider: str
    model: str
    resume_run_id: str | None = None

    async def execute(self):
        if self.resume_run_id:
            return await self.orchestrator.resume_run(self.resume_run_id)
        return await self.orchestrator.execute_task(self.task.id)


class ProductRuntime:
    def __init__(self, db_file: str | Path):
        self.db_file = Path(db_file).expanduser().resolve()
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.db_file)
        self.db.init_db()

    def close(self) -> None:
        self.db.close()

    def _sessions_root_for_new(self, source_root: Path) -> Path:
        candidate = (self.db_file.parent / "workspaces").resolve()
        if not _paths_overlap(candidate, source_root):
            return candidate
        digest = hashlib.sha256(str(self.db_file).encode("utf-8")).hexdigest()[:16]
        return (Path.home() / ".odys" / "workspaces" / digest).resolve()

    def _workspace_manager_for_resume(self, run_id: str) -> RunWorkspaceManager:
        binding = WorkspaceSessionBindingRepository(self.db).get_by_run(run_id)
        if binding is None:
            return RunWorkspaceManager(self.db, self.db_file.parent / "workspaces")
        return RunWorkspaceManager(self.db, Path(binding.session_root).parent)

    def prepare_new(
        self,
        *,
        goal: str,
        repo: str | Path,
        verify_argv: list[str],
        max_attempts: int,
        max_turns: int,
        provider: str | None,
    ) -> PreparedExecution:
        source = Path(repo).expanduser().resolve()
        if not source.is_dir():
            raise CliConfigurationError(f"repository path does not exist or is not a directory: {source}")
        if max_attempts < 1 or max_turns < 1:
            raise CliConfigurationError("max attempts and max turns must be positive")
        settings = resolve_provider_settings(provider)
        sessions_root = self._sessions_root_for_new(source)
        if _paths_overlap(sessions_root, source):
            raise CliConfigurationError("workspace session root overlaps source repository")
        projects = ProjectRepository(self.db)
        name = _project_name(source)
        project = projects.get_by_name(name)
        if project is None:
            project = projects.create(Project(name=name, type="software-repository", root_path=str(source)))
        elif Path(project.root_path or "").resolve() != source:
            raise CliConfigurationError("project repository identity mismatch")
        config_record = encode_cli_config(
            verify_argv=verify_argv,
            max_turns=max_turns,
            provider=settings.profile,
            model=settings.model,
        )
        task = create_task(
            self.db,
            project_id=project.id,
            title=_task_title(goal),
            objective=goal,
            constraints=[
                "Never mutate the source repository; use only the durable staged workspace.",
                "Use only explicitly allowed tools and verification command.",
                config_record,
            ],
            acceptance_criteria=[
                "The configured verification command exits with code 0.",
                "The source repository remains unchanged.",
            ],
            max_attempts=max_attempts,
            timeout_seconds=max(300.0, float(max_turns) * 90.0),
        )
        manager = RunWorkspaceManager(self.db, sessions_root, source_root=source)
        orchestrator = self._build_orchestrator(task, manager, verify_argv, max_turns, settings)
        return PreparedExecution(self, task, orchestrator, verify_argv, max_turns, settings.profile, settings.model)

    def prepare_resume(self, run_id: str) -> PreparedExecution:
        run = RunRepository(self.db).get(run_id)
        if run is None:
            raise CliConfigurationError(f"run {run_id} not found")
        task = TaskRepository(self.db).get(run.task_id)
        if task is None:
            raise CliConfigurationError(f"task {run.task_id} not found")
        config = decode_cli_config(task)
        settings = resolve_provider_settings(config["provider"], persisted_model=config["model"]) if run.status is RunStatus.RUNNING else ProviderSettings(config["provider"], config["model"])
        manager = self._workspace_manager_for_resume(run_id)
        orchestrator = self._build_orchestrator(task, manager, list(config["verify_argv"]), int(config["max_turns"]), settings)
        return PreparedExecution(self, task, orchestrator, list(config["verify_argv"]), int(config["max_turns"]), settings.profile, settings.model, resume_run_id=run_id)

    def _build_orchestrator(self, task, manager, verify_argv, max_turns, settings):
        policy = explicit_command_policy(verify_argv)

        def executor_factory(workspace):
            registry = ToolRegistry()
            register_staged_workspace_tools(registry, workspace, policy)
            backend = OfflineDemoBackend(registry, verify_argv) if settings.profile == "offline" else OpenAIAgentsBackend(registry, config=settings.config)
            return InnerAgentExecutor(
                backend,
                allowed_capabilities=ALLOWED_CAPABILITIES,
                allowed_side_effect_capabilities=ALLOWED_SIDE_EFFECTS,
                db=self.db,
                max_turns=max_turns,
            )

        validator = ExplicitCommandValidator(self.db, manager, verify_argv)
        return RecoveringOrchestrator(
            self.db,
            workspace_executor_factory=executor_factory,
            workspace_manager=manager,
            validator=validator,
            context_builder=ContextBuilder(policy="CP-3"),
            executor_type="InnerAgentExecutor",
            provider=settings.profile,
            model=settings.model,
            harness_version=HARNESS_VERSION,
            context_policy_version="CP-3",
            dataset_version="CLI-ALPHA",
        )

    def latest_run_for_task(self, task_id: str):
        runs = RunRepository(self.db).list_for_task(task_id)
        return runs[-1] if runs else None


def _duration_ms(run) -> int | None:
    start = run.started_at or run.created_at
    finish = run.finished_at or datetime.now(timezone.utc)
    if start is None:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if finish.tzinfo is None:
        finish = finish.replace(tzinfo=timezone.utc)
    return max(0, int((finish - start).total_seconds() * 1000))


def inspect_run(db: Database, run_id: str, *, include_events: bool = False, recent_event_limit: int = 100) -> dict[str, Any]:
    runs = RunRepository(db)
    run = runs.get(run_id)
    if run is None:
        raise CliConfigurationError(f"run {run_id} not found")
    task = TaskRepository(db).get(run.task_id)
    project = ProjectRepository(db).get(task.project_id) if task else None
    attempts = AttemptRepository(db).list_for_run(run.id)
    events = EventStore(db).list_for_run(run.id)
    validation_repo = ValidationResultRepository(db)
    failure_repo = FailureReportRepository(db)
    action_repo = RecoveryActionRepository(db)
    snapshot_repo = ContextSnapshotRepository(db)
    checkpoint_repo = CheckpointRepository(db)
    bindings = WorkspaceSessionBindingRepository(db)
    binding = bindings.get_by_run(run.id)

    attempt_rows = []
    total_turns = total_calls = total_failures = 0
    calls_by_capability: Counter[str] = Counter()
    failures_by_capability: Counter[str] = Counter()
    failures_by_type: Counter[str] = Counter()
    latest_validation = None
    recovery_rows = []
    for attempt in attempts:
        attempt_events = [event for event in events if event.attempt_id == attempt.id]
        observations = [event.payload for event in attempt_events if event.event_type.value == "INNER_AGENT_TOOL_OBSERVATION"]
        terminal = next((event.payload for event in reversed(attempt_events) if event.event_type.value in {"INNER_AGENT_COMPLETED", "INNER_AGENT_FAILED"}), {})
        turns = int(terminal.get("turn_count", 0) or 0)
        calls = len(observations)
        failures = sum(1 for payload in observations if payload.get("status") == "FAILURE")
        total_turns += turns
        total_calls += calls
        total_failures += failures
        for payload in observations:
            capability = str(payload.get("capability") or "UNKNOWN")
            calls_by_capability[capability] += 1
            if payload.get("status") == "FAILURE":
                failures_by_capability[capability] += 1
                failures_by_type[str(payload.get("error_type") or "UNKNOWN")] += 1
        validation = validation_repo.get_for_attempt(attempt.id)
        if validation is not None:
            latest_validation = validation
        snapshot = snapshot_repo.latest_for_attempt(attempt.id)
        reports = failure_repo.list_for_attempt(attempt.id)
        actions = action_repo.list_for_attempt(attempt.id)
        checkpoint = next((item for item in reversed(checkpoint_repo.list_for_run(run.id)) if item.attempt_id == attempt.id), None)
        attempt_rows.append({
            "attempt_number": attempt.attempt_number,
            "status": attempt.status.value,
            "error_type": attempt.error_type,
            "context_policy": snapshot.policy if snapshot else None,
            "turn_count": turns,
            "tool_calls": calls,
            "tool_failures": failures,
            "validation": None if validation is None else ("PASS" if validation.passed else "FAIL"),
            "duration_ms": attempt.duration_ms,
        })
        if reports or actions or checkpoint:
            recovery_rows.append({
                "attempt_number": attempt.attempt_number,
                "failure_report": None if not reports else {
                    "failure_type": reports[-1].failure_type.value,
                    "failure_class": reports[-1].failure_class.value,
                    "summary": _bounded_text(reports[-1].summary),
                },
                "recovery_action": None if not actions else {
                    "action": actions[-1].action_type.value,
                    "reason": _bounded_text(actions[-1].reason),
                },
                "checkpoint": None if checkpoint is None else {
                    "id": checkpoint.id,
                    "attempt_number": checkpoint.attempt_number,
                    "event_cursor": checkpoint.event_cursor,
                },
                "cp3_used": bool(snapshot and snapshot.policy == "CP-3"),
            })

    workspace = {
        "session_id": binding.session_id if binding else None,
        "state": binding.state if binding else None,
        "changed_file_count": 0,
        "changed_files": [],
        "diff_summary": {},
        "source_unchanged": None,
    }
    if binding is not None:
        try:
            session = DurableWorkspaceSession.reopen(binding.session_root)
            diff = asyncio.run(session.workspace.diff()) if not _in_running_loop() else None
            if diff is not None:
                workspace.update({
                    "changed_file_count": int(diff["files_changed"]),
                    "changed_files": list(diff["changed_files"])[:100],
                    "diff_summary": {
                        "files_changed": int(diff["files_changed"]),
                        "lines_added": int(diff["lines_added"]),
                        "lines_removed": int(diff["lines_removed"]),
                        "truncated": bool(diff["truncated"]),
                    },
                    "source_unchanged": tree_sha256(session.manifest.source_root) == session.manifest.source_tree_sha256,
                })
        except Exception as exc:
            workspace["integrity_error"] = type(exc).__name__
            workspace["source_unchanged"] = False

    validation_summary = None
    if latest_validation is not None:
        try:
            evidence = json.loads(latest_validation.evidence or "{}")
        except json.JSONDecodeError:
            evidence = {"summary": _bounded_text(latest_validation.evidence)}
        validation_summary = {
            "status": "PASS" if latest_validation.passed else "FAIL",
            "evidence": evidence,
        }
    config = decode_cli_config(task)
    result = {
        "run": {
            "id": run.id,
            "status": run.status.value,
            "harness": run.harness_version,
            "task_id": task.id,
            "task_title": task.title,
            "goal": _bounded_text(task.objective, 1024),
            "source_root": str(Path(project.root_path).resolve()) if project and project.root_path else None,
            "workspace_session": workspace["session_id"],
            "provider": run.provider,
            "model": run.model,
            "duration_ms": _duration_ms(run),
            "created_at": run.created_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        },
        "attempts": attempt_rows,
        "recovery": recovery_rows,
        "workspace": workspace,
        "tools": {
            "total_calls": total_calls,
            "total_failures": total_failures,
            "failure_rate": (total_failures / total_calls) if total_calls else 0.0,
            "calls_by_capability": dict(sorted(calls_by_capability.items())),
            "failures_by_capability": dict(sorted(failures_by_capability.items())),
            "failures_by_type": dict(sorted(failures_by_type.items())),
            "total_turns": total_turns,
        },
        "validation": validation_summary,
        "verification_command": list(config["verify_argv"]),
    }
    if include_events:
        limit = max(1, min(int(recent_event_limit), 100))
        result["events"] = [safe_event_projection(event) for event in events[-limit:]]
    return result


def _in_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


async def inspect_run_async(db: Database, run_id: str, *, include_events: bool = False, recent_event_limit: int = 100) -> dict[str, Any]:
    # Workspace.diff is async. Run the synchronous repository projection in a
    # worker thread where inspect_run can safely own a small event loop.
    return await asyncio.to_thread(inspect_run, db, run_id, include_events=include_events, recent_event_limit=recent_event_limit)


def list_recent_runs(db: Database, *, limit: int = 20, status: str | None = None) -> list[dict[str, Any]]:
    wanted = status.upper() if status else None
    rows = []
    attempts = AttemptRepository(db)
    run_repo = RunRepository(db)
    for task in TaskRepository(db).list():
        for run in run_repo.list_for_task(task.id):
            if wanted and run.status.value != wanted:
                continue
            rows.append({
                "status": run.status.value,
                "run_id": run.id,
                "task_title": task.title,
                "attempts": len(attempts.list_for_run(run.id)),
                "provider_model": f"{run.provider}/{run.model}",
                "updated_at": (run.finished_at or run.started_at or run.created_at).isoformat(),
            })
    rows.sort(key=lambda item: item["updated_at"], reverse=True)
    return rows[:max(1, min(int(limit), 100))]


def result_label(run_status: str) -> str:
    return "PASS" if run_status == "COMPLETED" else "FAIL"
