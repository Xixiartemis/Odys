import asyncio
import gc
import hashlib
import json
from pathlib import Path

import pytest

from lhas.workspace import (
    DurableWorkspaceSession,
    StagedWorkspace,
    WorkspaceBaselineCorrupt,
    WorkspaceSessionCorrupt,
    WorkspaceSourceDrift,
    tree_sha256,
)


def source_repo(tmp_path):
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "tests").mkdir()
    (source / "src" / "calculator.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8"
    )
    (source / "tests" / "test_calculator.py").write_text(
        "assert add(1, 2) == 3\n", encoding="utf-8"
    )
    return source


def test_durable_session_reopen_preserves_diff_and_source(tmp_path):
    source = source_repo(tmp_path)
    session_root = tmp_path / "session"
    source_before = tree_sha256(source)
    session = DurableWorkspaceSession.create(source, session_root, run_id="run-1", task_id="task-1")
    result = asyncio.run(session.workspace.edit_file("src/calculator.py", "return a - b", "return a + b"))
    before = asyncio.run(session.workspace.diff())
    patch_before = hashlib.sha256(before["diff"].encode("utf-8")).hexdigest()
    assert before["changed_files"] == ["src/calculator.py"]
    assert session.manifest.source_tree_sha256 == session.manifest.baseline_tree_sha256 == source_before
    assert not (session.workspace.root / "../manifest.json").resolve().is_relative_to(session.workspace.root)

    del session
    gc.collect()
    reopened = DurableWorkspaceSession.reopen(session_root)
    after = asyncio.run(reopened.workspace.diff())
    patch_after = hashlib.sha256(after["diff"].encode("utf-8")).hexdigest()
    assert after["changed_files"] == ["src/calculator.py"]
    assert patch_before == patch_after
    assert tree_sha256(source) == source_before
    assert not (reopened.workspace.root / "manifest.json").exists()


def test_session_manifest_is_versioned_and_paths_are_outside_work(tmp_path):
    source = source_repo(tmp_path)
    session = DurableWorkspaceSession.create(source, tmp_path / "session")
    manifest = json.loads((session.root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "workspace-session-v1"
    assert Path(manifest["baseline_dir"]) == session.root / "baseline"
    assert Path(manifest["work_dir"]) == session.root / "work"
    assert (session.root / "baseline").is_dir() and (session.root / "work").is_dir()
    assert not (session.workspace.root / "manifest.json").exists()


def test_baseline_corruption_is_rejected(tmp_path):
    source = source_repo(tmp_path)
    session = DurableWorkspaceSession.create(source, tmp_path / "session")
    (session.root / "baseline" / "src" / "calculator.py").write_text("corrupt\n", encoding="utf-8")
    with pytest.raises(WorkspaceBaselineCorrupt, match="WORKSPACE_BASELINE_CORRUPT"):
        DurableWorkspaceSession.reopen(session.root)


def test_source_drift_is_rejected_without_rebase(tmp_path):
    source = source_repo(tmp_path)
    session = DurableWorkspaceSession.create(source, tmp_path / "session")
    (source / "src" / "calculator.py").write_text("drifted\n", encoding="utf-8")
    with pytest.raises(WorkspaceSourceDrift, match="WORKSPACE_SOURCE_DRIFT"):
        DurableWorkspaceSession.reopen(session.root)


def test_manifest_corruption_and_escape_are_rejected(tmp_path):
    source = source_repo(tmp_path)
    session = DurableWorkspaceSession.create(source, tmp_path / "session")
    manifest_path = session.root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["work_dir"] = str(tmp_path / "outside-work")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(WorkspaceSessionCorrupt, match="WORKSPACE_SESSION_CORRUPT"):
        DurableWorkspaceSession.reopen(session.root)

    manifest_path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(WorkspaceSessionCorrupt, match="WORKSPACE_SESSION_CORRUPT"):
        DurableWorkspaceSession.reopen(session.root)


def test_missing_session_component_is_rejected(tmp_path):
    source = source_repo(tmp_path)
    session = DurableWorkspaceSession.create(source, tmp_path / "session")
    (session.root / "baseline").rename(session.root / "baseline-missing")
    with pytest.raises(WorkspaceSessionCorrupt, match="WORKSPACE_SESSION_CORRUPT"):
        DurableWorkspaceSession.reopen(session.root)


def test_cleanup_is_explicit_safe_and_idempotent(tmp_path):
    source = source_repo(tmp_path)
    source_before = tree_sha256(source)
    session = DurableWorkspaceSession.create(source, tmp_path / "session")
    root = session.root
    session.cleanup()
    session.cleanup()
    assert not root.exists()
    assert source.exists() and tree_sha256(source) == source_before


def test_session_create_failure_does_not_publish_partial_root(tmp_path, monkeypatch):
    source = source_repo(tmp_path)
    target = tmp_path / "session"
    original = StagedWorkspace.create

    def fail(*args, **kwargs):
        raise RuntimeError("copy failed")

    monkeypatch.setattr(StagedWorkspace, "create", fail)
    with pytest.raises(RuntimeError):
        DurableWorkspaceSession.create(source, target)
    assert not target.exists()
    assert not list(tmp_path.glob(".odys-session-build-*"))
    monkeypatch.setattr(StagedWorkspace, "create", original)
