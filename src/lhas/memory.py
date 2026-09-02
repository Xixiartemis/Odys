"""Bounded persistent user memory, separate from sessions and knowledge."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from lhas.agent.models import AgentRole
from lhas.domain.models import new_id

MAX_MEMORY_CHARS = 64_000
MAX_MEMORY_ITEMS = 256
_ROW = re.compile(r"^- \[([0-9a-f]{32})\] (.*)$")


class MemoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    scope: str
    content: str


class MemoryProvider(Protocol):
    def list(self, scope: str | None = None) -> list[MemoryItem]: ...
    def search(self, query: str, scope: str | None = None) -> list[MemoryItem]: ...
    def add(self, content: str, *, scope: str, role: AgentRole, approved: bool = False) -> MemoryItem: ...
    def replace(self, item_id: str, content: str, *, role: AgentRole, approved: bool = False) -> MemoryItem: ...
    def remove(self, item_id: str, *, role: AgentRole, approved: bool = False) -> bool: ...


class BuiltinMemoryProvider:
    def __init__(self, root: Path, *, require_write_approval: bool = True):
        self.root = root.resolve()
        self.require_write_approval = require_write_approval

    def _path(self, scope: str) -> Path:
        if scope not in {"memory", "user"}:
            raise ValueError("memory scope must be 'memory' or 'user'")
        return self.root / ("MEMORY.md" if scope == "memory" else "USER.md")

    def _read_scope(self, scope: str) -> list[MemoryItem]:
        path = self._path(scope)
        if not path.exists():
            return []
        items: list[MemoryItem] = []
        for line in path.read_text(encoding="utf-8")[:MAX_MEMORY_CHARS].splitlines():
            match = _ROW.match(line)
            if match:
                items.append(MemoryItem(id=match.group(1), scope=scope, content=match.group(2)))
        return items[-MAX_MEMORY_ITEMS:]

    def list(self, scope: str | None = None) -> list[MemoryItem]:
        scopes = [scope] if scope else ["memory", "user"]
        return [item for current in scopes for item in self._read_scope(current)][-MAX_MEMORY_ITEMS:]

    def search(self, query: str, scope: str | None = None) -> list[MemoryItem]:
        terms = [term.casefold() for term in query.split() if term]
        if not terms:
            return []
        return [item for item in self.list(scope) if all(term in item.content.casefold() for term in terms)][:20]

    def _authorize(self, role: AgentRole, approved: bool) -> None:
        if role is not AgentRole.ROOT:
            raise PermissionError("MEMORY_WRITE_ROLE_DENIED")
        if self.require_write_approval and not approved:
            raise PermissionError("MEMORY_WRITE_APPROVAL_REQUIRED")

    def _write(self, scope: str, items: list[MemoryItem]) -> None:
        items = items[-MAX_MEMORY_ITEMS:]
        content = "# Odys Memory\n\n" + "\n".join(f"- [{item.id}] {item.content}" for item in items) + "\n"
        if len(content) > MAX_MEMORY_CHARS:
            raise ValueError("MEMORY_BOUND_EXCEEDED")
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._path(scope)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8", newline="\n")
        temporary.replace(target)

    def add(self, content: str, *, scope: str, role: AgentRole, approved: bool = False) -> MemoryItem:
        self._authorize(role, approved)
        normalized = " ".join(content.split())[:4_000]
        if not normalized:
            raise ValueError("memory content is empty")
        item = MemoryItem(id=new_id(), scope=scope, content=normalized)
        self._write(scope, self._read_scope(scope) + [item])
        return item

    def replace(self, item_id: str, content: str, *, role: AgentRole, approved: bool = False) -> MemoryItem:
        self._authorize(role, approved)
        for scope in ("memory", "user"):
            items = self._read_scope(scope)
            for index, item in enumerate(items):
                if item.id == item_id:
                    replacement = MemoryItem(id=item_id, scope=scope, content=" ".join(content.split())[:4_000])
                    items[index] = replacement
                    self._write(scope, items)
                    return replacement
        raise KeyError(f"memory item not found: {item_id}")

    def remove(self, item_id: str, *, role: AgentRole, approved: bool = False) -> bool:
        self._authorize(role, approved)
        for scope in ("memory", "user"):
            items = self._read_scope(scope)
            remaining = [item for item in items if item.id != item_id]
            if len(remaining) != len(items):
                self._write(scope, remaining)
                return True
        return False
