import asyncio
import hashlib
import inspect
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from lhas.checkpoint import WorkingState, WorkingStateProjector, safe_event_projection
from lhas.command_validation import ExplicitCommandValidator, parse_verification_command
from lhas.context_builder import ContextBuilder
from lhas.domain.enums import EventType
from lhas.domain.models import Attempt, Project, Run, Task
from lhas.inner_agent.models import InnerAgentRequest
from lhas.inner_agent.openai_agents_backend import OpenAIAgentsBackend
from lhas.inner_agent.tool_adapter import allowed_tools
from lhas.inner_agent.trace import InnerAgentTrace
from lhas.persistence.event_store import EventStore
from lhas.persistence.repositories import AttemptRepository, ProjectRepository, RunRepository
from lhas.task_service import create_task
from lhas.tools.fakes import FakeTool
from lhas.tools.protocol import ToolRequest, ToolResult, ToolResultStatus
from lhas.tools.registry import ToolRegistry
from lhas.planning.models import CapabilitySpec
from lhas.workspace import CommandPolicy, CommandRule, RunWorkspaceManager, StagedWorkspace
from lhas.workspace.safe_cli import SafeCli
from lhas.workspace.tools import SafeCliTool, WorkspaceEditLinesTool, WorkspaceEditTool


def _source(tmp_path: Path, data: bytes = b"one\ntwo\nthree\n") -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "sample.txt").write_bytes(data)
    return source


def _request(capability: str, arguments: dict) -> ToolRequest:
    return ToolRequest(
        tool_call_id="call",
        task_id="task",
        run_id="run",
        attempt_id="attempt",
        capability=capability,
        arguments=arguments,
    )


def test_edit_lines_success_and_source_immutability(tmp_path):
    source = _source(tmp_path)
    original = (source / "sample.txt").read_bytes()
    workspace = StagedWorkspace.create(source, tmp_path / "stage")
    before = hashlib.sha256(original).hexdigest()
    result = asyncio.run(workspace.edit_lines("sample.txt", 2, 2, ["TWO", "2.5"], before))
    assert (workspace.root / "sample.txt").read_text(encoding="utf-8") == "one\nTWO\n2.5\nthree\n"
    assert result["lines_written"] == 2 and result["before_sha256"] == before
    assert (source / "sample.txt").read_bytes() == original


def test_edit_lines_stale_sha_guard(tmp_path):
    workspace = StagedWorkspace.create(_source(tmp_path), tmp_path / "stage")
    with pytest.raises(ValueError, match="STALE_FILE_VERSION"):
        asyncio.run(workspace.edit_lines("sample.txt", 1, 1, ["ONE"], "0" * 64))
    assert (workspace.root / "sample.txt").read_text(encoding="utf-8") == "one\ntwo\nthree\n"


@pytest.mark.parametrize("start,end", [(0, 1), (2, 1), (1, 99)])
def test_edit_lines_invalid_ranges(tmp_path, start, end):
    source = _source(tmp_path)
    workspace = StagedWorkspace.create(source, tmp_path / "stage")
    digest = hashlib.sha256((source / "sample.txt").read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="INVALID_LINE_RANGE"):
        asyncio.run(workspace.edit_lines("sample.txt", start, end, ["x"], digest))


def test_edit_lines_path_escape(tmp_path):
    source = _source(tmp_path)
    workspace = StagedWorkspace.create(source, tmp_path / "stage")
    tool = WorkspaceEditLinesTool(workspace)
    result = asyncio.run(tool.execute(_request("workspace.edit_lines", {
        "path": "../escape.txt", "start_line": 1, "end_line": 1,
        "new_lines": ["x"], "expected_sha256": "0" * 64,
    })))
    assert result.error_type == "WORKSPACE_PATH_ESCAPE"
    assert result.metadata["action"] == "USE_WORKSPACE_RELATIVE_CWD"


@pytest.mark.parametrize(
    "data,expected",
    [
        (b"one\ntwo", b"one\nTWO"),
        (b"one\ntwo\n", b"one\nTWO\n"),
        (b"one\r\ntwo\r\n", b"one\r\nTWO\r\n"),
    ],
)
def test_edit_lines_preserves_final_newline_and_crlf(tmp_path, data, expected):
    source = _source(tmp_path, data)
    workspace = StagedWorkspace.create(source, tmp_path / "stage")
    digest = hashlib.sha256(data).hexdigest()
    asyncio.run(workspace.edit_lines("sample.txt", 2, 2, ["TWO"], digest))
    assert (workspace.root / "sample.txt").read_bytes() == expected


def test_edit_failure_recovery_hints(tmp_path):
    workspace = StagedWorkspace.create(_source(tmp_path), tmp_path / "stage")
    tool = WorkspaceEditTool(workspace)
    digest = hashlib.sha256((workspace.root / "sample.txt").read_bytes()).hexdigest()
    missing = asyncio.run(tool.execute(_request("workspace.edit", {
        "path": "sample.txt", "old_text": "absent", "new_text": "x", "expected_sha256": digest,
    })))
    assert missing.error_type == "EDIT_TARGET_NOT_FOUND"
    assert missing.metadata == {
        "action": "REREAD_THEN_LINE_EDIT",
        "retry_same_arguments": False,
        "suggested_capabilities": ["workspace.read", "workspace.edit_lines"],
    }
    ambiguous_source = tmp_path / "ambiguous"
    ambiguous_source.mkdir()
    (ambiguous_source / "x.txt").write_text("same same", encoding="utf-8")
    ambiguous_workspace = StagedWorkspace.create(ambiguous_source, tmp_path / "ambiguous-stage")
    ambiguous = asyncio.run(WorkspaceEditTool(ambiguous_workspace).execute(_request("workspace.edit", {
        "path": "x.txt", "old_text": "same", "new_text": "new",
    })))
    assert ambiguous.error_type == "EDIT_TARGET_AMBIGUOUS"
    assert ambiguous.metadata["action"] == "NARROW_TARGET_OR_LINE_EDIT"


def test_stale_and_command_not_allowed_feedback(tmp_path):
    workspace = StagedWorkspace.create(_source(tmp_path), tmp_path / "stage")
    stale = asyncio.run(WorkspaceEditLinesTool(workspace).execute(_request("workspace.edit_lines", {
        "path": "sample.txt", "start_line": 1, "end_line": 1,
        "new_lines": ["ONE"], "expected_sha256": "f" * 64,
    })))
    assert stale.error_type == "STALE_FILE_VERSION"
    assert stale.metadata["action"] == "REFRESH_FILE_VERSION"
    cli = SafeCliTool(workspace, CommandPolicy([CommandRule(["pytest", "-q"], allow_extra_args=False)]))
    denied = asyncio.run(cli.execute(_request("cli.exec", {"argv": ["git", "status"]})))
    assert denied.error_type == "COMMAND_NOT_ALLOWED"
    assert denied.metadata["allowed_command_prefixes"] == [["pytest", "-q"]]


def test_repeated_failure_adaptation_uses_only_signature():
    registry = ToolRegistry()
    registry.register(FakeTool(
        CapabilitySpec(name="safe.fail", description="fails"),
        lambda request: ToolResult(
            status=ToolResultStatus.FAILURE,
            error_type="EDIT_TARGET_NOT_FOUND",
            error_message="missing",
            metadata={"action": "REREAD_THEN_LINE_EDIT", "retry_same_arguments": False},
        ),
    ))
    trace = InnerAgentTrace()
    request = InnerAgentRequest(
        task_id="t", run_id="r", attempt_id="a", objective="x",
        allowed_capabilities=["safe.fail"],
    )
    tools, _ = allowed_tools(registry, request, trace)
    context = SimpleNamespace(tool_call_id="call", context={})
    raw = json.dumps({"path": "secret-value-that-must-not-persist"})
    first = asyncio.run(tools[0].on_invoke_tool(context, raw))
    second = asyncio.run(tools[0].on_invoke_tool(context, raw))
    assert "failure_repeat_count" not in first and "strategy_change_required" not in first
    assert second["failure_repeat_count"] == 2 and second["strategy_change_required"] is True
    persisted_shape = json.dumps(trace.items)
    assert "secret-value-that-must-not-persist" not in persisted_shape
    assert "args_sha256" in persisted_shape


def test_cp3_working_state_contains_bounded_failure_memory_without_raw_args(db):
    events = EventStore(db)
    events.append(EventType.INNER_AGENT_TOOL_OBSERVATION, run_id="r", payload={
        "capability": "workspace.edit",
        "status": "FAILURE",
        "error_type": "EDIT_TARGET_NOT_FOUND",
        "failure_repeat_count": 2,
        "strategy_change_required": True,
        "arguments": {"path": "must-not-survive"},
    })
    events.append(EventType.INNER_AGENT_TOOL_OBSERVATION, run_id="r", payload={
        "capability": "cli.exec",
        "status": "FAILURE",
        "error_type": "COMMAND_NOT_ALLOWED",
    })
    stored = events.list_for_run("r")
    state = WorkingStateProjector().project(None, stored)
    assert state.tool_failure_count == 2
    assert state.tool_failures_by_capability == {"workspace.edit": 1, "cli.exec": 1}
    assert state.tool_failures_by_type == {"EDIT_TARGET_NOT_FOUND": 1, "COMMAND_NOT_ALLOWED": 1}
    assert state.last_tool_failure_capability == "cli.exec"
    assert state.last_tool_failure_type == "COMMAND_NOT_ALLOWED"
    assert state.strategy_change_required is True
    projected = json.dumps([safe_event_projection(event) for event in stored])
    assert "must-not-survive" not in projected and "arguments" not in projected


def test_cp3_executor_context_carries_failure_counts_without_raw_args():
    project = Project(name="context")
    task = Task(
        project_id=project.id,
        title="repair",
        objective="repair safely",
    )
    state = WorkingState(
        task_id=task.id,
        run_id="run",
        tool_failure_count=7,
        tool_failures_by_capability={"workspace.edit": 7},
        tool_failures_by_type={"EDIT_TARGET_NOT_FOUND": 7},
        last_tool_failure_capability="workspace.edit",
        last_tool_failure_type="EDIT_TARGET_NOT_FOUND",
        strategy_change_required=True,
    )
    snapshot = ContextBuilder(policy="CP-3").build(
        task=task,
        attempt_number=2,
        working_state=state.model_dump(mode="json"),
        recent_history=[{
            "event_type": "INNER_AGENT_TOOL_OBSERVATION",
            "capability": "workspace.edit",
            "error_type": "EDIT_TARGET_NOT_FOUND",
            "strategy_change_required": True,
        }],
    )
    context = ContextBuilder(policy="CP-3").to_executor_context(snapshot)
    encoded = json.dumps(context)
    assert '"tool_failure_count":7' in context["working_state"]
    assert '"workspace.edit":7' in context["working_state"]
    assert "EDIT_TARGET_NOT_FOUND" in context["working_state"]
    assert "arguments" not in encoded and "old_text" not in encoded


def test_inner_agent_instructions_require_early_verification_and_adaptation():
    source = inspect.getsource(OpenAIAgentsBackend._instructions)
    assert "workspace.edit_lines" in source
    assert "retry_same_arguments=false" in source
    assert "edit -> validate -> observe" in source
    assert "hidden reasoning" in source


def _validation_case(tmp_path, argv):
    source = tmp_path / "repo"
    source.mkdir()
    (source / "marker.txt").write_text("ok", encoding="utf-8")
    db = __import__("lhas.persistence.database", fromlist=["Database"]).Database(tmp_path / "runtime.db")
    db.init_db()
    project = Project(name="validator", root_path=str(source))
    ProjectRepository(db).create(project)
    task = create_task(db, project_id=project.id, title="validate", objective="validate")
    run = RunRepository(db).create(Run(task_id=task.id))
    attempt = AttemptRepository(db).create(Attempt(run_id=run.id, attempt_number=1))
    manager = RunWorkspaceManager(db, tmp_path / "sessions", source_root=source)
    manager.create_for_run(task, run)
    return db, task, attempt, manager, argv


def test_command_parser_rejects_shell_composition():
    assert parse_verification_command("pytest -q") == ["pytest", "-q"]
    with pytest.raises(ValueError, match="VERIFICATION_COMMAND_INVALID"):
        parse_verification_command("pytest -q && whoami")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows command-line quoting")
def test_command_parser_preserves_windows_quoted_path():
    assert parse_verification_command('"C:\\Program Files\\Python\\python.exe" -m pytest') == [
        "C:\\Program Files\\Python\\python.exe", "-m", "pytest",
    ]


def test_explicit_command_validator_pass_fail_and_workspace_cwd(tmp_path):
    argv = [sys.executable, "-c", "from pathlib import Path; raise SystemExit(0 if Path('marker.txt').read_text() == 'ok' else 3)"]
    db, task, attempt, manager, _ = _validation_case(tmp_path, argv)
    result = asyncio.run(ExplicitCommandValidator(db, manager, argv).validate(task=task, attempt=attempt, result=None))
    assert result.passed is True
    evidence = json.loads(result.evidence)
    assert evidence["exit_code"] == 0 and evidence["timed_out"] is False
    failing = [sys.executable, "-c", "raise SystemExit(7)"]
    failed = asyncio.run(ExplicitCommandValidator(db, manager, failing).validate(task=task, attempt=attempt, result=None))
    assert failed.passed is False and json.loads(failed.evidence)["exit_code"] == 7
    db.close()


def test_explicit_command_validator_timeout_and_bounded_output(tmp_path):
    argv = [sys.executable, "-c", "import time; time.sleep(1)"]
    db, task, attempt, manager, _ = _validation_case(tmp_path, argv)
    timed = asyncio.run(ExplicitCommandValidator(db, manager, argv, timeout_seconds=0.1).validate(task=task, attempt=attempt, result=None))
    evidence = json.loads(timed.evidence)
    assert timed.passed is False and evidence["timed_out"] is True
    noisy = [sys.executable, "-c", "import sys; print('x'*5000); print('y'*5000, file=sys.stderr)"]
    bounded = asyncio.run(ExplicitCommandValidator(db, manager, noisy, max_output_bytes=1024).validate(task=task, attempt=attempt, result=None))
    bounded_evidence = json.loads(bounded.evidence)
    assert bounded.passed is True
    assert bounded_evidence["stdout_truncated"] and bounded_evidence["stderr_truncated"]
    assert len(bounded.stdout.encode()) <= 1024 and len(bounded.stderr.encode()) <= 1024
    db.close()


def test_command_validator_and_safe_cli_never_use_shell_true():
    source = inspect.getsource(ExplicitCommandValidator) + inspect.getsource(SafeCli)
    assert "shell=True" not in source
    assert "create_subprocess_exec" in source
