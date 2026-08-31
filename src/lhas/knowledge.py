"""Bounded lexical project knowledge provider (no vector database)."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class KnowledgeHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str
    score: int
    excerpt: str


class KnowledgeProvider(Protocol):
    def search(self, query: str) -> list[KnowledgeHit]: ...
    def open(self, ref: str) -> str: ...


class LocalKnowledgeProvider:
    def __init__(self, project_root: Path, knowledge_dirs: list[Path] | None = None):
        self.project_root = project_root.resolve()
        self.knowledge_dirs = [path.resolve() for path in (knowledge_dirs or [])]

    def _files(self) -> list[Path]:
        candidates: set[Path] = set()
        readme = self.project_root / "README.md"
        if readme.is_file():
            candidates.add(readme)
        roots = [self.project_root / "docs", *self.knowledge_dirs]
        for root in roots:
            if root.is_dir():
                for pattern in ("*.md", "*.txt"):
                    candidates.update(path for path in root.rglob(pattern) if path.is_file() and not path.is_symlink())
        return sorted(candidates)

    def _ref(self, path: Path) -> str:
        try:
            return path.relative_to(self.project_root).as_posix()
        except ValueError:
            for index, root in enumerate(self.knowledge_dirs):
                if path.is_relative_to(root):
                    return f"knowledge:{index}:{path.relative_to(root).as_posix()}"
        raise ValueError("KNOWLEDGE_PATH_ESCAPE")

    def search(self, query: str) -> list[KnowledgeHit]:
        terms = [term.casefold() for term in query.split() if term][:12]
        hits: list[KnowledgeHit] = []
        for path in self._files():
            content = path.read_text(encoding="utf-8", errors="replace")[:100_000]
            folded = content.casefold()
            score = sum(folded.count(term) for term in terms)
            if score:
                first = min((folded.find(term) for term in terms if term in folded), default=0)
                start = max(0, first - 120)
                hits.append(KnowledgeHit(ref=self._ref(path), score=score, excerpt=content[start : start + 500]))
        return sorted(hits, key=lambda hit: (-hit.score, hit.ref))[:20]

    def open(self, ref: str) -> str:
        if ref.startswith("knowledge:"):
            _, index, relative = ref.split(":", 2)
            root = self.knowledge_dirs[int(index)]
        else:
            root = self.project_root
            relative = ref
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("KNOWLEDGE_PATH_ESCAPE")
        target = (root / relative_path).resolve()
        if not target.is_relative_to(root) or not target.is_file() or target.is_symlink():
            raise ValueError("KNOWLEDGE_REF_NOT_FOUND")
        return target.read_text(encoding="utf-8", errors="replace")[:40_000]


class ProjectContextDiscovery:
    """Project instructions: AGENTS.md first, then higher-priority .odys.md."""

    def discover(self, project_root: Path) -> dict[str, str]:
        root = project_root.resolve()
        result: dict[str, str] = {}
        for name in ("AGENTS.md", ".odys.md"):
            path = root / name
            if path.is_file() and not path.is_symlink():
                result[name] = path.read_text(encoding="utf-8", errors="replace")[:20_000]
        return result
