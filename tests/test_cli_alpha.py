import asyncio
import hashlib
import json
import re
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from lhas import HARNESS_VERSION
from lhas.cli import _print_interrupt, app
from lhas.cli_runtime import ProductRuntime, inspect_run
from lhas.cli_ui import project_view_state, render_dashboard, should_use_rich
from lhas.persistence.repositories import AttemptRepository, WorkspaceSessionBindingRepository
from lhas.resume import CrashPoint


runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_REPO = REPO_ROOT / "evals" / "fixtures" / "hv12_session_lifecycle"
GOAL = (
    "Repair tenant-scoped session isolation, prevent delayed messages from "
    "recreating deleted sessions, and preserve active/new session behavior."
)


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


@pytest.fixture(scope="module")
def offline_cli_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("odys-cli-e2e")
    db = root / "odys.db"
    before = _tree_hash(DEMO_REPO)
    result = runner.invoke(app, [
        "run", GOAL,
        "--repo", str(DEMO_REPO),
        "--verify", "pytest -q",
        "--provider", "offline",
        "--no-ui",
        "--yes",
        "--db", str(db),
    ])
    match = re.search(r"Run ID: ([0-9a-f]{32})", result.output)
    assert result.exit_code == 0, result.output
    assert match, result.output
    assert _tree_hash(DEMO_REPO) == before
    return {"db": db, "run_id": match.group(1), "result": result, "source_hash": before}


def test_odys_help_has_primary_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("run", "resume", "inspect", "runs", "version"):
        assert command in result.output
    assert "Plan. Act. Recover. Finish." in result.output


def test_odys_version_identifies_product_and_harness():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "Odys" in result.output
    assert "Harness: HV-1.5" in result.output
    assert "Package: 0.1.0" in result.output
    assert HARNESS_VERSION == "HV-1.5"


def test_run_rejects_missing_repo(tmp_path):
    result = runner.invoke(app, [
        "run", "goal", "--repo", str(tmp_path / "missing"),
        "--verify", "pytest -q", "--provider", "offline", "--yes",
    ])
    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_run_requires_verification_command():
    result = runner.invoke(app, [
        "run", "goal", "--repo", str(DEMO_REPO),
        "--provider", "offline", "--yes",
    ])
    assert result.exit_code != 0
    assert "--verify" in result.output


def test_run_validates_provider_before_execution(monkeypatch, tmp_path):
    monkeypatch.delenv("ODYS_AGENT_MODEL", raising=False)
    monkeypatch.delenv("ODYS_AGENT_API_KEY", raising=False)
    db = tmp_path / "missing-config.db"
    result = runner.invoke(app, [
        "run", "goal", "--repo", str(DEMO_REPO),
        "--verify", "pytest -q", "--provider", "mimo", "--yes", "--db", str(db),
    ])
    assert result.exit_code != 0
    assert "ODYS_AGENT_MODEL" in result.output and "ODYS_AGENT_API_KEY" in result.output
    assert not db.exists()


def test_no_ui_and_non_tty_use_plain_path():
    class NonTty:
        def isatty(self):
            return False
    assert should_use_rich(no_ui=True, stream=NonTty()) is False
    assert should_use_rich(no_ui=False, stream=NonTty()) is False


def test_ui_projection_is_bounded_and_excludes_raw_fields():
    inspection = {
        "run": {
            "id": "r", "status": "RUNNING", "harness": "HV-1.4",
            "provider": "offline", "model": "deterministic", "duration_ms": 5,
        },
        "attempts": [{"attempt_number": 1, "turn_count": 2}],
        "recovery": [],
        "workspace": {"session_id": "s", "changed_file_count": 1, "source_unchanged": True},
        "tools": {"total_calls": 3, "total_failures": 1},
        "validation": None,
        "events": [
            {
                "event_id": index,
                "event_type": "INNER_AGENT_TOOL_OBSERVATION",
                "capability": "workspace.read",
                "arguments": {"api_key": "secret"},
                "raw_model_transcript": "hidden",
            }
            for index in range(20)
        ],
    }
    state = project_view_state(
        inspection, goal="goal", provider="offline", model="deterministic",
        max_attempts=3, max_turns=20,
    )
    encoded = json.dumps(state)
    assert len(state["recent_activity"]) == 8
    assert "secret" not in encoded and "arguments" not in encoded and "raw_model_transcript" not in encoded
    rendered = Console(record=True, width=100)
    rendered.print(render_dashboard(state))
    assert "Odys" in rendered.export_text()


def test_ctrl_c_resume_hint(capsys):
    _print_interrupt("a" * 32)
    output = capsys.readouterr().out
    assert "RESULT: INTERRUPTED" in output
    assert f"odys resume {'a' * 32}" in output


def test_offline_run_uses_full_cli_path_and_passes(offline_cli_run):
    output = offline_cli_run["result"].output
    assert "RESULT: PASS" in output
    assert "Validation status: PASS" in output
    assert "Changed files: 2" in output
    assert "Total tool calls: 6" in output
    assert _tree_hash(DEMO_REPO) == offline_cli_run["source_hash"]


def test_inspect_human_and_json_are_safe(offline_cli_run):
    args = ["inspect", offline_cli_run["run_id"], "--db", str(offline_cli_run["db"])]
    human = runner.invoke(app, args)
    assert human.exit_code == 0
    assert "Attempts" in human.output and "source_unchanged=True" in human.output
    structured = runner.invoke(app, args + ["--json", "--events"])
    assert structured.exit_code == 0
    assert '"status": "COMPLETED"' in structured.output
    assert "ODYS_AGENT_API_KEY" not in structured.output
    assert "raw_model_transcript" not in structured.output
    assert '"arguments"' not in structured.output


def test_runs_lists_recent_product_run(offline_cli_run):
    result = runner.invoke(app, ["runs", "--limit", "5", "--status", "COMPLETED", "--db", str(offline_cli_run["db"])])
    assert result.exit_code == 0
    assert offline_cli_run["run_id"] in result.output
    assert "offline/offline-deterministic" in result.output


def test_resume_terminal_run_uses_same_summary(offline_cli_run):
    result = runner.invoke(app, ["resume", offline_cli_run["run_id"], "--no-ui", "--db", str(offline_cli_run["db"])])
    assert result.exit_code == 0
    assert "RESULT: PASS" in result.output
    assert f"Run ID: {offline_cli_run['run_id']}" in result.output


def test_resume_running_attempt_reuses_durable_workspace(tmp_path):
    db_path = tmp_path / "resume.db"
    source_before = _tree_hash(DEMO_REPO)
    runtime = ProductRuntime(db_path)
    prepared = runtime.prepare_new(
        goal=GOAL,
        repo=DEMO_REPO,
        verify_argv=["pytest", "-q"],
        max_attempts=3,
        max_turns=20,
        provider="offline",
    )

    class StopAfterAttemptStarted:
        def hit(self, point, **context):
            if point is CrashPoint.AFTER_ATTEMPT_STARTED:
                raise RuntimeError("simulated process interruption")

    prepared.orchestrator.crash_injector = StopAfterAttemptStarted()
    with pytest.raises(RuntimeError, match="simulated process interruption"):
        asyncio.run(prepared.execute())
    run = runtime.latest_run_for_task(prepared.task.id)
    before_binding = WorkspaceSessionBindingRepository(runtime.db).get_by_run(run.id)
    before_root = before_binding.session_root
    before_session = before_binding.session_id
    runtime.close()

    resumed = runner.invoke(app, ["resume", run.id, "--no-ui", "--db", str(db_path)])
    assert resumed.exit_code == 0, resumed.output
    assert "RESULT: PASS" in resumed.output

    reopened = ProductRuntime(db_path)
    after_binding = WorkspaceSessionBindingRepository(reopened.db).get_by_run(run.id)
    attempts = AttemptRepository(reopened.db).list_for_run(run.id)
    data = inspect_run(reopened.db, run.id)
    reopened.close()
    assert after_binding.session_root == before_root
    assert after_binding.session_id == before_session
    assert [attempt.status.value for attempt in attempts] == ["CRASHED", "COMPLETED"]
    assert attempts[0].error_type == "PROCESS_INTERRUPTED"
    assert data["workspace"]["source_unchanged"] is True
    assert _tree_hash(DEMO_REPO) == source_before


def test_historical_canonical_artifacts_are_byte_identical():
    expected = {
        "evals/runs/HV12-LIVE-001.json": "144985bc68dbc2d3e8ecbde669c7f14d28adb4d8c3a693668b4b5acaa1603af9",
        "evals/runs/HV13-LIVE-001.json": "3e6ecf1b718d2e8f0a292fa4cf8f1d2d71c455128f3d0a23fabad81079840bb5",
        "evals/runs/HV13-LIVE-001.claim.json": "67b8977da846e78cc933c3893ecbc4851b870c8620de00ac222205b26a21cc03",
    }
    for relative, digest in expected.items():
        assert hashlib.sha256((REPO_ROOT / relative).read_bytes()).hexdigest() == digest
