"""Durable E6-A workspace sessions.

The session keeps an immutable baseline outside the agent-visible work tree.
Only ``work/`` is handed to workspace tools; ``manifest.json`` and ``baseline/``
are runtime infrastructure used to validate a later reopen.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from .errors import (
    WorkspaceBaselineCorrupt,
    WorkspaceSessionCorrupt,
    WorkspaceSessionError,
    WorkspaceSourceDrift,
)
from .models import WorkspaceLimits
from .staged import StagedWorkspace


SESSION_SCHEMA_VERSION = "workspace-session-v1"
_SESSION_STATES = {"OPEN", "CLOSED"}


class WorkspaceSessionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SESSION_SCHEMA_VERSION
    session_id: str
    run_id: Optional[str] = None
    task_id: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_root: str
    baseline_dir: str
    work_dir: str
    source_tree_sha256: str
    baseline_tree_sha256: str
    limits: dict[str, Any]
    state: str = "OPEN"


def _canonical_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve(strict=False)


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _overlap(left: Path, right: Path) -> bool:
    return _same_path(left, right) or left in right.parents or right in left.parents


def _fsync_file(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    except OSError:
        # Directory fsync is unavailable on some Windows filesystems.  The
        # atomic rename remains the publication boundary there.
        pass


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _limits_dict(limits: WorkspaceLimits) -> dict[str, Any]:
    data = asdict(limits)
    data["excluded_dirs"] = sorted(str(item) for item in limits.excluded_dirs)
    return data


def _limits_from_dict(value: Any) -> WorkspaceLimits:
    if not isinstance(value, dict):
        raise WorkspaceSessionCorrupt("WORKSPACE_SESSION_CORRUPT")
    allowed = {field.name for field in fields(WorkspaceLimits)}
    if set(value) - allowed:
        raise WorkspaceSessionCorrupt("WORKSPACE_SESSION_CORRUPT")
    data = dict(value)
    if "excluded_dirs" in data:
        if not isinstance(data["excluded_dirs"], list) or not all(isinstance(item, str) for item in data["excluded_dirs"]):
            raise WorkspaceSessionCorrupt("WORKSPACE_SESSION_CORRUPT")
        data["excluded_dirs"] = set(data["excluded_dirs"])
    try:
        return WorkspaceLimits(**data)
    except (TypeError, ValueError) as exc:
        raise WorkspaceSessionCorrupt("WORKSPACE_SESSION_CORRUPT") from exc


def tree_sha256(root: str | Path, limits: WorkspaceLimits | None = None) -> str:
    """Hash sorted ``relative path + file content hash`` entries."""
    base = _canonical_path(root)
    if not base.is_dir():
        raise WorkspaceSessionError("WORKSPACE_SOURCE_DRIFT")
    excluded = (limits or WorkspaceLimits()).excluded_dirs
    entries: list[tuple[str, str]] = []
    for path in sorted(base.rglob("*"), key=lambda item: item.relative_to(base).as_posix()):
        relative = path.relative_to(base)
        if any(part in excluded for part in relative.parts):
            continue
        if path.is_symlink():
            continue
        if not path.is_file():
            continue
        resolved = path.resolve(strict=False)
        if resolved != base and base not in resolved.parents:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append((relative.as_posix(), digest))
    canonical = "".join(f"{relative}\0{digest}\n" for relative, digest in entries).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class DurableWorkspaceSession:
    """A restart-safe baseline/work pair with a validated manifest."""

    def __init__(self, session_root: str | Path, manifest: WorkspaceSessionManifest, workspace: StagedWorkspace):
        self.root = _canonical_path(session_root)
        self.manifest = manifest
        self.workspace = workspace
        self.staged_workspace = workspace

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @classmethod
    def create(
        cls,
        source_root: str | Path,
        session_root: str | Path,
        *,
        limits: WorkspaceLimits | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> "DurableWorkspaceSession":
        source = _canonical_path(source_root)
        root = _canonical_path(session_root)
        limits = limits or WorkspaceLimits()
        cls._assert_disjoint(source, root)
        if not source.is_dir():
            raise WorkspaceSessionError("WORKSPACE_SOURCE_DRIFT")
        if root.exists():
            raise WorkspaceSessionError("WORKSPACE_SESSION_EXISTS")
        root.parent.mkdir(parents=True, exist_ok=True)
        build = Path(tempfile.mkdtemp(prefix=".odys-session-build-", dir=str(root.parent)))
        try:
            work = build / "work"
            baseline = build / "baseline"
            # Reuse the existing bounded, symlink-safe staging copy logic.
            StagedWorkspace.create(source, work, limits)
            shutil.copytree(work, baseline, symlinks=False)
            source_hash = tree_sha256(source, limits)
            baseline_hash = tree_sha256(baseline, limits)
            if source_hash != baseline_hash:
                raise WorkspaceBaselineCorrupt("WORKSPACE_BASELINE_CORRUPT")
            manifest = WorkspaceSessionManifest(
                session_id=session_id or _new_session_id(),
                run_id=run_id,
                task_id=task_id,
                source_root=str(source),
                baseline_dir=str((root / "baseline").resolve(strict=False)),
                work_dir=str((root / "work").resolve(strict=False)),
                source_tree_sha256=source_hash,
                baseline_tree_sha256=baseline_hash,
                limits=_limits_dict(limits),
            )
            _write_manifest(build / "manifest.json", manifest)
            _fsync_directory(build)
            os.replace(str(build), str(root))
            _fsync_directory(root.parent)
        except Exception:
            shutil.rmtree(build, ignore_errors=True)
            raise
        return cls.reopen(root)

    @classmethod
    def reopen(cls, session_root: str | Path) -> "DurableWorkspaceSession":
        root = _canonical_path(session_root)
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            raise WorkspaceSessionCorrupt("WORKSPACE_SESSION_CORRUPT")
        try:
            manifest = WorkspaceSessionManifest.model_validate(json.loads(manifest_path.read_text(encoding="utf-8")))
        except Exception as exc:
            raise WorkspaceSessionCorrupt("WORKSPACE_SESSION_CORRUPT") from exc
        if manifest.schema_version != SESSION_SCHEMA_VERSION or manifest.state not in _SESSION_STATES:
            raise WorkspaceSessionCorrupt("WORKSPACE_SESSION_CORRUPT")
        cls._validate_manifest_paths(root, manifest)
        limits = _limits_from_dict(manifest.limits)
        baseline = Path(manifest.baseline_dir)
        work = Path(manifest.work_dir)
        source = Path(manifest.source_root)
        if not baseline.is_dir() or not work.is_dir():
            raise WorkspaceSessionCorrupt("WORKSPACE_SESSION_CORRUPT")
        try:
            baseline_hash = tree_sha256(baseline, limits)
        except Exception as exc:
            raise WorkspaceBaselineCorrupt("WORKSPACE_BASELINE_CORRUPT") from exc
        if baseline_hash != manifest.baseline_tree_sha256:
            raise WorkspaceBaselineCorrupt("WORKSPACE_BASELINE_CORRUPT")
        try:
            source_hash = tree_sha256(source, limits)
        except Exception as exc:
            raise WorkspaceSourceDrift("WORKSPACE_SOURCE_DRIFT") from exc
        if source_hash != manifest.source_tree_sha256:
            raise WorkspaceSourceDrift("WORKSPACE_SOURCE_DRIFT")
        workspace = StagedWorkspace(source, work, limits, baseline_root=baseline)
        return cls(root, manifest, workspace)

    @staticmethod
    def _assert_disjoint(source: Path, root: Path) -> None:
        if _overlap(source, root):
            raise WorkspaceSessionError("WORKSPACE_SESSION_PATH_CONFLICT")

    @classmethod
    def _validate_manifest_paths(cls, root: Path, manifest: WorkspaceSessionManifest) -> None:
        try:
            source = _canonical_path(manifest.source_root)
            baseline = _canonical_path(manifest.baseline_dir)
            work = _canonical_path(manifest.work_dir)
        except Exception as exc:
            raise WorkspaceSessionCorrupt("WORKSPACE_SESSION_CORRUPT") from exc
        if not all(Path(value).is_absolute() for value in (manifest.source_root, manifest.baseline_dir, manifest.work_dir)):
            raise WorkspaceSessionCorrupt("WORKSPACE_SESSION_CORRUPT")
        for raw, resolved in ((manifest.source_root, source), (manifest.baseline_dir, baseline), (manifest.work_dir, work)):
            if os.path.normcase(str(Path(raw))) != os.path.normcase(str(resolved)):
                raise WorkspaceSessionCorrupt("WORKSPACE_SESSION_CORRUPT")
        if not _same_path(baseline, root / "baseline") or not _same_path(work, root / "work"):
            raise WorkspaceSessionCorrupt("WORKSPACE_SESSION_CORRUPT")
        if _same_path(baseline, work) or _overlap(source, root) or _overlap(source, baseline) or _overlap(source, work):
            raise WorkspaceSessionCorrupt("WORKSPACE_SESSION_CORRUPT")
        if not manifest.session_id:
            raise WorkspaceSessionCorrupt("WORKSPACE_SESSION_CORRUPT")

    def cleanup(self) -> None:
        """Explicitly remove only this validated session root; idempotently."""
        if not self.root.exists():
            return
        self._validate_manifest_paths(self.root, self.manifest)
        source = _canonical_path(self.manifest.source_root)
        if _overlap(source, self.root):
            raise WorkspaceSessionError("WORKSPACE_SESSION_PATH_CONFLICT")
        shutil.rmtree(self.root)


def _new_session_id() -> str:
    return hashlib.sha256(f"{datetime.now(timezone.utc).isoformat()}:{os.getpid()}".encode()).hexdigest()[:32]


def _write_manifest(path: Path, manifest: WorkspaceSessionManifest) -> None:
    payload = json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    _fsync_file(temporary)
    os.replace(str(temporary), str(path))
