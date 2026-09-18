"""BaseFixture ABC for all benchmark task fixtures."""
from abc import ABC, abstractmethod
from pathlib import Path
import json
import hashlib


class BaseFixture(ABC):
    """Abstract base for executable benchmark fixtures.

    Each concrete fixture implements four lifecycle methods:
      setup()        – create initial workspace files
      inject_fault() – apply the task-specific fault
      observe()      – return observable state as a dict
      reset()        – remove all created files for the next repeat
    """

    task_id: str = ""
    family: str = ""
    fixture_id: str = ""
    fault_ids: list[str] = []

    @abstractmethod
    def setup(self, workspace_dir: Path) -> dict:
        """Create initial state (files, configs). Return metadata dict."""
        ...

    @abstractmethod
    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        """Apply the specified fault to the workspace."""
        ...

    @abstractmethod
    def observe(self, workspace_dir: Path) -> dict:
        """Return observable state: files_changed, tests_passed, etc."""
        ...

    @abstractmethod
    def reset(self, workspace_dir: Path) -> None:
        """Remove all created files, restore clean state."""
        ...

    # --- helpers -----------------------------------------------------------
    def _write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _read_json(self, path: Path) -> dict:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return {}

    def _write_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _read_text(self, path: Path) -> str:
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    def _file_hash(self, path: Path) -> str:
        if not path.exists():
            return ""
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _list_files(self, workspace_dir: Path) -> list[str]:
        """Return relative paths of all files in workspace."""
        if not workspace_dir.exists():
            return []
        return sorted(
            str(p.relative_to(workspace_dir))
            for p in workspace_dir.rglob("*")
            if p.is_file()
        )
