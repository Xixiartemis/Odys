"""agentskills.io-style discovery with progressive disclosure."""

from __future__ import annotations

from pathlib import Path

from lhas.skills.models import SkillDocument, SkillMetadata

MAX_SKILL_CHARS = 40_000
MAX_REFERENCE_CHARS = 40_000


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    try:
        end = lines.index("---", 1)
    except ValueError:
        return {}, text
    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        if separator:
            metadata[key.strip()] = value.strip().strip('"\'')
    return metadata, "\n".join(lines[end + 1 :]).strip()


class SkillRegistry:
    def __init__(self, roots: list[Path]):
        self.roots = [root.resolve() for root in roots]
        self._skills: dict[str, tuple[SkillMetadata, Path]] = {}

    def discover(self) -> list[SkillMetadata]:
        found: dict[str, tuple[SkillMetadata, Path]] = {}
        for root in self.roots:
            if not root.is_dir():
                continue
            for skill_file in sorted(root.rglob("SKILL.md")):
                if not skill_file.is_file() or skill_file.is_symlink():
                    continue
                raw = skill_file.read_text(encoding="utf-8")[:MAX_SKILL_CHARS]
                frontmatter, _ = _frontmatter(raw)
                relative = skill_file.parent.relative_to(root).as_posix()
                name = frontmatter.get("name") or relative
                metadata = SkillMetadata(
                    name=name,
                    description=frontmatter.get("description", ""),
                    metadata={key: value for key, value in frontmatter.items() if key not in {"name", "description"}},
                )
                found.setdefault(name, (metadata, skill_file))
        self._skills = found
        return [found[name][0] for name in sorted(found)]

    def list(self) -> list[SkillMetadata]:
        return self.discover()

    def view(self, name: str, reference_path: str | None = None) -> SkillDocument:
        self.discover()
        try:
            metadata, skill_file = self._skills[name]
        except KeyError as exc:
            raise KeyError(f"unknown skill: {name}") from exc
        target = skill_file
        limit = MAX_SKILL_CHARS
        if reference_path:
            relative = Path(reference_path)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("SKILL_REFERENCE_PATH_ESCAPE")
            target = (skill_file.parent / "references" / relative).resolve()
            references_root = (skill_file.parent / "references").resolve()
            if not target.is_relative_to(references_root) or not target.is_file() or target.is_symlink():
                raise ValueError("SKILL_REFERENCE_NOT_FOUND")
            limit = MAX_REFERENCE_CHARS
        raw = target.read_text(encoding="utf-8")[:limit]
        _, body = _frontmatter(raw) if target == skill_file else ({}, raw)
        return SkillDocument(metadata=metadata, content=body, reference_path=reference_path)


class SkillLoader:
    """Explicit Level 1/2 loading facade."""

    def __init__(self, registry: SkillRegistry):
        self.registry = registry

    def load(self, name: str, reference_path: str | None = None) -> SkillDocument:
        return self.registry.view(name, reference_path)
