"""Tests for the concrete benchmark tool layer (P44).

Validates:
  - Each tool individually (read_file, write_file, list_files, run_test, inspect_state)
  - create_benchmark_tool_registry returns non-empty registry
  - Tool execution trace (each tool call is logged)
  - Registry has the expected number of tools registered
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from evals.reliability.tools import (
    DiffTool,
    EditLinesTool,
    InspectStateTool,
    ListFilesTool,
    ReadFileTool,
    RunTestTool,
    SearchFilesTool,
    WriteFileTool,
    create_benchmark_tool_registry,
)
from lhas.tools.protocol import ToolRequest, ToolResultStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(
    capability: str,
    tool_name: str,
    arguments: dict | None = None,
) -> ToolRequest:
    return ToolRequest(
        tool_call_id="tc-1",
        task_id="task-1",
        run_id="run-1",
        attempt_id="att-1",
        capability=capability,
        capability_id=capability,
        tool_name=tool_name,
        arguments=arguments or {},
    )


def _run(coro):
    """Run an async coroutine in a fresh event loop."""
    return asyncio.run(coro)


@pytest.fixture()
def ws() -> Path:
    """Create a temporary workspace directory for testing."""
    d = Path(tempfile.mkdtemp(prefix="p44_benchmark_ws_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Individual tool tests
# ---------------------------------------------------------------------------

class TestListFilesTool:
    def test_list_files_returns_files(self, ws: Path) -> None:
        (ws / "a.txt").write_text("hello")
        (ws / "sub").mkdir()
        (ws / "sub" / "b.txt").write_text("world")

        tool = ListFilesTool(ws)
        result = _run(tool.execute(_make_request("workspace.list", "workspace.list", {"directory": "."})))

        assert result.status is ToolResultStatus.SUCCESS
        files = result.output["files"]
        assert "a.txt" in files
        assert "sub/b.txt" in files

    def test_list_files_empty_dir(self, ws: Path) -> None:
        tool = ListFilesTool(ws)
        result = _run(tool.execute(_make_request("workspace.list", "workspace.list", {"directory": "."})))

        assert result.status is ToolResultStatus.SUCCESS
        assert result.output["files"] == []

    def test_list_files_rejects_non_directory(self, ws: Path) -> None:
        (ws / "file.txt").write_text("x")
        tool = ListFilesTool(ws)
        result = _run(tool.execute(_make_request("workspace.list", "workspace.list", {"directory": "file.txt"})))

        assert result.status is ToolResultStatus.FAILURE

    def test_list_files_rejects_escape(self, ws: Path) -> None:
        tool = ListFilesTool(ws)
        result = _run(tool.execute(_make_request("workspace.list", "workspace.list", {"directory": "../.."})))

        assert result.status is ToolResultStatus.FAILURE


class TestReadFileTool:
    def test_read_file_returns_content(self, ws: Path) -> None:
        (ws / "hello.txt").write_text("hello world")

        tool = ReadFileTool(ws)
        result = _run(tool.execute(_make_request("workspace.read", "workspace.read", {"path": "hello.txt"})))

        assert result.status is ToolResultStatus.SUCCESS
        assert result.output["content"] == "hello world"
        assert result.output["size"] == 11

    def test_read_file_missing(self, ws: Path) -> None:
        tool = ReadFileTool(ws)
        result = _run(tool.execute(_make_request("workspace.read", "workspace.read", {"path": "nope.txt"})))

        assert result.status is ToolResultStatus.FAILURE

    def test_read_file_rejects_escape(self, ws: Path) -> None:
        tool = ReadFileTool(ws)
        result = _run(tool.execute(_make_request("workspace.read", "workspace.read", {"path": "../../../etc/passwd"})))

        assert result.status is ToolResultStatus.FAILURE


class TestWriteFileTool:
    def test_write_file_creates(self, ws: Path) -> None:
        tool = WriteFileTool(ws)
        result = _run(tool.execute(
            _make_request("workspace.edit", "workspace.edit", {"path": "out.txt", "content": "data"})
        ))

        assert result.status is ToolResultStatus.SUCCESS
        assert (ws / "out.txt").read_text() == "data"
        assert result.output["bytes_written"] == 4
        assert "checksum" in result.output

    def test_write_file_creates_parents(self, ws: Path) -> None:
        tool = WriteFileTool(ws)
        result = _run(tool.execute(
            _make_request("workspace.edit", "workspace.edit", {"path": "deep/nested/file.txt", "content": "x"})
        ))

        assert result.status is ToolResultStatus.SUCCESS
        assert (ws / "deep" / "nested" / "file.txt").read_text() == "x"


class TestSearchFilesTool:
    def test_search_finds_pattern(self, ws: Path) -> None:
        (ws / "a.py").write_text("import os\nprint('hello')\n")
        (ws / "b.py").write_text("import sys\nprint('world')\n")

        tool = SearchFilesTool(ws)
        result = _run(tool.execute(
            _make_request("workspace.search", "workspace.search", {"pattern": "import"})
        ))

        assert result.status is ToolResultStatus.SUCCESS
        assert result.output["total"] == 2

    def test_search_no_match(self, ws: Path) -> None:
        (ws / "a.txt").write_text("nothing here")
        tool = SearchFilesTool(ws)
        result = _run(tool.execute(
            _make_request("workspace.search", "workspace.search", {"pattern": "ZZZZZ"})
        ))

        assert result.status is ToolResultStatus.SUCCESS
        assert result.output["total"] == 0


class TestEditLinesTool:
    def test_edit_lines_replaces(self, ws: Path) -> None:
        (ws / "f.txt").write_text("hello world")

        tool = EditLinesTool(ws)
        result = _run(tool.execute(
            _make_request("workspace.edit_lines", "workspace.edit_lines", {
                "path": "f.txt", "old_string": "world", "new_string": "python",
            })
        ))

        assert result.status is ToolResultStatus.SUCCESS
        assert (ws / "f.txt").read_text() == "hello python"

    def test_edit_lines_no_match(self, ws: Path) -> None:
        (ws / "f.txt").write_text("hello world")
        tool = EditLinesTool(ws)
        result = _run(tool.execute(
            _make_request("workspace.edit_lines", "workspace.edit_lines", {
                "path": "f.txt", "old_string": "ZZZZZ", "new_string": "new",
            })
        ))

        assert result.status is ToolResultStatus.FAILURE


class TestDiffTool:
    def test_diff_shows_changes(self, ws: Path) -> None:
        (ws / "f.txt").write_text("line1\nline2\n")

        tool = DiffTool(ws)
        result = _run(tool.execute(
            _make_request("workspace.diff", "workspace.diff", {
                "path": "f.txt", "new_content": "line1\nline3\n",
            })
        ))

        assert result.status is ToolResultStatus.SUCCESS
        assert result.output["changed"] is True
        assert "line3" in result.output["diff"]

    def test_diff_no_change(self, ws: Path) -> None:
        (ws / "f.txt").write_text("same")
        tool = DiffTool(ws)
        result = _run(tool.execute(
            _make_request("workspace.diff", "workspace.diff", {
                "path": "f.txt", "new_content": "same",
            })
        ))

        assert result.status is ToolResultStatus.SUCCESS
        assert result.output["changed"] is False


class TestRunTestTool:
    def test_run_test_success(self, ws: Path) -> None:
        tool = RunTestTool(ws)
        result = _run(tool.execute(
            _make_request("cli.exec", "cli.exec", {"command": "echo ok", "cwd": "."})
        ))

        assert result.status is ToolResultStatus.SUCCESS
        assert result.output["passed"] is True
        assert result.output["exit_code"] == 0
        assert "ok" in result.output["stdout"]

    def test_run_test_failure(self, ws: Path) -> None:
        tool = RunTestTool(ws)
        result = _run(tool.execute(
            _make_request("cli.exec", "cli.exec", {"command": "exit 1", "cwd": "."})
        ))

        assert result.status is ToolResultStatus.SUCCESS
        assert result.output["passed"] is False
        assert result.output["exit_code"] == 1

    def test_run_test_rejects_escape(self, ws: Path) -> None:
        tool = RunTestTool(ws)
        result = _run(tool.execute(
            _make_request("cli.exec", "cli.exec", {"command": "echo hi", "cwd": "../.."})
        ))

        assert result.status is ToolResultStatus.FAILURE


class TestInspectStateTool:
    def test_inspect_state(self, ws: Path) -> None:
        (ws / "a.txt").write_text("hello")

        tool = InspectStateTool(ws)
        result = _run(tool.execute(
            _make_request("observer.inspect_state", "observer.inspect_state", {"workspace": "."})
        ))

        assert result.status is ToolResultStatus.SUCCESS
        assert "a.txt" in result.output["files"]
        assert "a.txt" in result.output["checksums"]
        assert isinstance(result.output["tests_passed"], bool)


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------

class TestBenchmarkToolRegistry:
    def test_registry_non_empty(self, ws: Path) -> None:
        registry = create_benchmark_tool_registry(ws)
        assert len(registry.list_capabilities()) > 0

    def test_registry_has_expected_tools(self, ws: Path) -> None:
        registry = create_benchmark_tool_registry(ws)
        capabilities = registry.list_capabilities()

        expected = {
            "workspace.list",
            "workspace.read",
            "workspace.search",
            "workspace.edit",
            "workspace.edit_lines",
            "workspace.diff",
            "cli.exec",
            "observer.inspect_state",
        }
        assert set(capabilities) == expected
        assert len(capabilities) == len(expected)

    def test_registry_resolve_each_tool(self, ws: Path) -> None:
        registry = create_benchmark_tool_registry(ws)
        for name in registry.list_capabilities():
            tool = registry.resolve(name)
            assert hasattr(tool, "capability")
            assert hasattr(tool, "execute")
            assert tool.capability.name == name


# ---------------------------------------------------------------------------
# Execution trace test
# ---------------------------------------------------------------------------

class TestToolExecutionTrace:
    """Verify that each tool call produces a traceable result."""

    def test_execution_trace_all_tools(self, ws: Path) -> None:
        """Every tool invocation should return a well-formed ToolResult."""
        registry = create_benchmark_tool_registry(ws)
        trace: list[dict] = []

        # workspace.list
        tool = registry.resolve("workspace.list")
        result = _run(tool.execute(
            _make_request("workspace.list", "workspace.list", {"directory": "."})
        ))
        trace.append({"tool": "workspace.list", "status": result.status.value})
        assert result.status is ToolResultStatus.SUCCESS

        # workspace.write then workspace.read
        (ws / "trace.txt").write_text("trace content")
        tool = registry.resolve("workspace.read")
        result = _run(tool.execute(
            _make_request("workspace.read", "workspace.read", {"path": "trace.txt"})
        ))
        trace.append({"tool": "workspace.read", "status": result.status.value})
        assert result.status is ToolResultStatus.SUCCESS

        # workspace.search
        tool = registry.resolve("workspace.search")
        result = _run(tool.execute(
            _make_request("workspace.search", "workspace.search", {"pattern": "trace"})
        ))
        trace.append({"tool": "workspace.search", "status": result.status.value})
        assert result.status is ToolResultStatus.SUCCESS

        # workspace.edit
        tool = registry.resolve("workspace.edit")
        result = _run(tool.execute(
            _make_request("workspace.edit", "workspace.edit", {"path": "out.txt", "content": "data"})
        ))
        trace.append({"tool": "workspace.edit", "status": result.status.value})
        assert result.status is ToolResultStatus.SUCCESS

        # workspace.edit_lines
        tool = registry.resolve("workspace.edit_lines")
        result = _run(tool.execute(
            _make_request("workspace.edit_lines", "workspace.edit_lines", {
                "path": "out.txt", "old_string": "data", "new_string": "updated",
            })
        ))
        trace.append({"tool": "workspace.edit_lines", "status": result.status.value})
        assert result.status is ToolResultStatus.SUCCESS

        # workspace.diff
        tool = registry.resolve("workspace.diff")
        result = _run(tool.execute(
            _make_request("workspace.diff", "workspace.diff", {
                "path": "out.txt", "new_content": "new data",
            })
        ))
        trace.append({"tool": "workspace.diff", "status": result.status.value})
        assert result.status is ToolResultStatus.SUCCESS

        # cli.exec
        tool = registry.resolve("cli.exec")
        result = _run(tool.execute(
            _make_request("cli.exec", "cli.exec", {"command": "echo trace_ok", "cwd": "."})
        ))
        trace.append({"tool": "cli.exec", "status": result.status.value})
        assert result.status is ToolResultStatus.SUCCESS

        # observer.inspect_state
        tool = registry.resolve("observer.inspect_state")
        result = _run(tool.execute(
            _make_request("observer.inspect_state", "observer.inspect_state", {"workspace": "."})
        ))
        trace.append({"tool": "observer.inspect_state", "status": result.status.value})
        assert result.status is ToolResultStatus.SUCCESS

        # Verify all 8 tools were traced
        assert len(trace) == 8
        assert all(entry["status"] == "SUCCESS" for entry in trace)
        traced_names = [entry["tool"] for entry in trace]
        assert len(set(traced_names)) == 8  # all unique
