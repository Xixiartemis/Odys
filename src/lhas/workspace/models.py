from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

@dataclass(frozen=True)
class WorkspaceEntry:
    path: str
    type: str
    size: int
    relative_path: str

@dataclass
class WorkspaceLimits:
    max_read_bytes: int = 256 * 1024
    hard_max_read_bytes: int = 4 * 1024 * 1024
    max_search_files: int = 5000
    max_file_bytes: int = 1024 * 1024
    max_output_bytes: int = 64 * 1024
    excluded_dirs: set[str] = field(default_factory=lambda: {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build"})
